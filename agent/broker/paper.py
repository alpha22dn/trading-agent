"""PaperBroker (spec section 9): simulates Kite GTTs, orders, holdings and cash in SQLite.

Simulation rules
- Prices: live quotes (LTP, day high/low, circuit limits) fed in by the order monitor every minute.
- GTT trigger: fires when price crosses the trigger. A GTT armed before today's open is checked
  against the day's high/low (so a touch between two samples is not missed); one armed intraday
  is checked against the prices seen since it was armed. If both OCO legs are touched in the same
  window, the stop leg is assumed to have fired first (conservative).
- On trigger a limit order is placed for that leg, valid for the rest of the day (Kite behaviour).
  A just-triggered order fills at the trigger price (the price when it crossed), or at the open when
  the stock gapped through the trigger; a buy only if that is <= its limit, a sell-stop only if it is
  >= its limit (stop x 0.98). A target leg fills at its limit. An order left resting fills later at
  its limit price once the LTP is back on the right side of it.
- Upper circuit: buys do not fill. Lower circuit: sells do not fill.
- Partial fills: when the quote carries market depth, a cycle fills at most the quantity offered at or
  better than the limit (asks for a buy, bids for a sell). The rest stays open and keeps filling on later
  cycles, like a real resting order. Without depth the order fills in full.
- Unfilled orders (or the unfilled rest of a partly filled one) are cancelled at the end of the session.
- Kite validation: a trigger must be >= 0.25% away from LTP (9 paise for stocks <= Rs 50);
  OCO needs one trigger below and one above LTP. Buys are rejected at trigger time without funds.
- Charges: full Zerodha delivery charges on every fill (agent/orders/charges.py).
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from ..clock import IST, now_ist, ts
from ..db import connect
from ..orders.charges import compute as compute_charges, same_day_sell
from ..settings import get_app_config
from .base import Broker, BrokerReject, Gtt, Leg, Order, Position, Quote


def _legs(raw: str) -> list[Leg]:
    return [Leg(**x) for x in json.loads(raw)]


def _gtt(r) -> Gtt:
    return Gtt(id=r["id"], account_id=r["account_id"], symbol=r["symbol"], type=r["type"],
               trigger_values=json.loads(r["trigger_values"]), orders=_legs(r["orders"]), status=r["status"],
               last_price=r["last_price"], triggered_leg=r["triggered_leg"], order_id=r["order_id"])


def _order(r) -> Order:
    return Order(id=r["id"], account_id=r["account_id"], gtt_id=r["gtt_id"], symbol=r["symbol"], side=r["side"],
                 qty=r["qty"], price=r["price"], status=r["status"], trade_date=r["trade_date"],
                 filled_qty=r["filled_qty"] or 0, filled_price=r["filled_price"], filled_at=r["filled_at"], charges=r["charges"], reason=r["reason"])


class PaperBroker(Broker):
    # ---------- validation ----------

    def _min_gap(self, last_price: float) -> float:
        p = get_app_config()["paper"]
        if last_price > float(p["gtt_gap_price_threshold"]):
            return last_price * float(p["gtt_min_trigger_gap_pct"]) / 100
        return float(p["gtt_min_trigger_gap_abs"])

    def validate(self, type_: str, triggers: list[float], last_price: float, orders: list[Leg]) -> None:
        gap = self._min_gap(last_price)
        if type_ == "single":
            if len(triggers) != 1 or len(orders) != 1:
                raise BrokerReject("Single GTT needs one trigger and one order")
        elif type_ == "oco":
            if len(triggers) != 2 or len(orders) != 2:
                raise BrokerReject("OCO GTT needs two triggers and two orders")
            if not (triggers[0] < last_price < triggers[1]):
                raise BrokerReject("OCO: one trigger must be below and one above the last price")
        else:
            raise BrokerReject(f"Unknown GTT type {type_}")
        for t in triggers:
            if abs(t - last_price) < gap - 1e-9:
                raise BrokerReject(f"Trigger {t} is too close to last price {last_price} (min gap {gap:.2f})")
        for o in orders:
            if o.qty < 1 or o.price <= 0:
                raise BrokerReject("Order quantity and price must be positive")

    # ---------- GTT API ----------

    def place_gtt(self, account_id: str, symbol: str, type_: str, trigger_values: list[float],
                  last_price: float, orders: list[Leg]) -> int:
        self.validate(type_, trigger_values, last_price, orders)
        now = ts()
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO paper_gtts (account_id, symbol, type, trigger_values, orders, last_price, status, "
                "armed_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                (account_id, symbol, type_, json.dumps(trigger_values), json.dumps([o.__dict__ for o in orders]),
                 last_price, now, now, now))
            return int(cur.lastrowid)

    def modify_gtt(self, account_id: str, gtt_id: int, trigger_values: list[float], last_price: float,
                   orders: list[Leg]) -> None:
        g = self.get_gtt(account_id, gtt_id)
        if g is None or g.status != "active":
            raise BrokerReject(f"GTT {gtt_id} is not active")
        self.validate(g.type, trigger_values, last_price, orders)
        now = ts()
        with connect() as conn:
            conn.execute("UPDATE paper_gtts SET trigger_values = ?, orders = ?, last_price = ?, armed_at = ?, "
                         "seen_high = NULL, seen_low = NULL, updated_at = ? WHERE id = ?",
                         (json.dumps(trigger_values), json.dumps([o.__dict__ for o in orders]), last_price, now, now,
                          gtt_id))

    def cancel_gtt(self, account_id: str, gtt_id: int) -> None:
        with connect() as conn:
            cur = conn.execute("UPDATE paper_gtts SET status = 'cancelled', updated_at = ? "
                               "WHERE id = ? AND account_id = ? AND status = 'active'", (ts(), gtt_id, account_id))
            if cur.rowcount == 0:
                raise BrokerReject(f"GTT {gtt_id} is not active")

    def cancel_order(self, account_id: str, order_id: int, reason: str) -> None:
        with connect() as conn:
            conn.execute("UPDATE paper_orders SET status = 'cancelled', reason = ? "
                         "WHERE id = ? AND account_id = ? AND status = 'open'", (reason, order_id, account_id))

    def get_gtt(self, account_id: str, gtt_id: int) -> Gtt | None:
        with connect() as conn:
            r = conn.execute("SELECT * FROM paper_gtts WHERE id = ? AND account_id = ?", (gtt_id, account_id)).fetchone()
        return _gtt(r) if r else None

    def gtts(self, account_id: str, status: str | None = None) -> list[Gtt]:
        sql, args = "SELECT * FROM paper_gtts WHERE account_id = ?", [account_id]
        if status:
            sql += " AND status = ?"
            args.append(status)
        with connect() as conn:
            return [_gtt(r) for r in conn.execute(sql + " ORDER BY id", args)]

    def get_order(self, account_id: str, order_id: int) -> Order | None:
        with connect() as conn:
            r = conn.execute("SELECT * FROM paper_orders WHERE id = ? AND account_id = ?",
                             (order_id, account_id)).fetchone()
        return _order(r) if r else None

    def orders(self, account_id: str, symbol: str | None = None) -> list[Order]:
        sql, args = "SELECT * FROM paper_orders WHERE account_id = ?", [account_id]
        if symbol:
            sql += " AND symbol = ?"
            args.append(symbol)
        with connect() as conn:
            return [_order(r) for r in conn.execute(sql + " ORDER BY id", args)]

    def manual_order(self, account_id: str, symbol: str, side: str, qty: int, price: float,
                     now: datetime | None = None) -> Order:
        """Paper stand-in for an order the user places by hand in Kite (market order at `price`).
        Used only by the dashboard's paper-edit actions on manual-edit accounts; the agent itself never
        calls this (GTT only)."""
        now = (now or now_ist()).astimezone(IST)
        if qty < 1 or price <= 0:
            raise BrokerReject("Quantity and price must be positive")
        with connect() as conn:
            cur = conn.execute(
                "INSERT INTO paper_orders (account_id, gtt_id, symbol, side, qty, price, trigger, status, trade_date, "
                "placed_at) VALUES (?, NULL, ?, ?, ?, ?, NULL, 'open', ?, ?)",
                (account_id, symbol, side, qty, price, now.date().isoformat(), now.isoformat(timespec="seconds")))
            oid = int(cur.lastrowid)
            o = _order(conn.execute("SELECT * FROM paper_orders WHERE id = ?", (oid,)).fetchone())
            ev = self._try_fill(conn, o, None, Quote(ltp=price, high=price, low=price), now, just_triggered=True)
            if ev is None or ev["type"] == "rejected" or o.filled_qty < qty:
                conn.execute("UPDATE paper_orders SET status = 'rejected', reason = COALESCE(reason, 'Could not fill') "
                             "WHERE id = ? AND status != 'complete'", (oid,))
                reason = (ev or {}).get("reason", "could not fill")
                raise BrokerReject(f"Manual {side.lower()} rejected: {reason}")
            return _order(conn.execute("SELECT * FROM paper_orders WHERE id = ?", (oid,)).fetchone())

    def positions(self, account_id: str) -> list[Position]:
        with connect() as conn:
            rows = conn.execute("SELECT symbol, qty, avg_price FROM paper_positions WHERE account_id = ? AND qty > 0",
                                (account_id,)).fetchall()
        return [Position(r["symbol"], r["qty"], r["avg_price"]) for r in rows]

    def cash(self, account_id: str) -> float:
        with connect() as conn:
            return float(conn.execute("SELECT cash FROM accounts WHERE id = ?", (account_id,)).fetchone()[0])

    def watched_symbols(self) -> list[str]:
        with connect() as conn:
            rows = conn.execute("SELECT symbol FROM paper_gtts WHERE status = 'active' UNION "
                                "SELECT symbol FROM paper_orders WHERE status = 'open'").fetchall()
        return sorted(r[0] for r in rows)

    # ---------- simulation ----------

    def process(self, quotes: dict[str, Quote], now: datetime | None = None) -> list[dict[str, Any]]:
        """Advance the simulated exchange by one monitor cycle. Returns a list of what happened."""
        now = (now or now_ist()).astimezone(IST)
        open_dt = now.replace(hour=9, minute=15, second=0, microsecond=0).isoformat(timespec="seconds")
        events: list[dict[str, Any]] = []
        with connect() as conn:
            # 1. resting orders
            for r in conn.execute("SELECT * FROM paper_orders WHERE status = 'open'").fetchall():
                q = quotes.get(r["symbol"])
                if q:
                    ev = self._try_fill(conn, _order(r), r["trigger"], q, now, just_triggered=False)
                    if ev:
                        events.append(ev)
            # 2. active GTTs
            for r in conn.execute("SELECT * FROM paper_gtts WHERE status = 'active'").fetchall():
                q = quotes.get(r["symbol"])
                if not q:
                    continue
                pre_open = r["armed_at"] < open_dt
                if pre_open:  # armed before today's session: the whole day counts
                    hi, lo = max(q.high, q.ltp), min(q.low, q.ltp)
                else:
                    hi = max(q.ltp, r["seen_high"] if r["seen_high"] is not None else q.ltp)
                    lo = min(q.ltp, r["seen_low"] if r["seen_low"] is not None else q.ltp)
                conn.execute("UPDATE paper_gtts SET seen_high = ?, seen_low = ? WHERE id = ?", (hi, lo, r["id"]))
                g = _gtt(r)
                leg = self._hit_leg(g, hi, lo)
                if leg is None:
                    continue
                o = g.orders[leg]
                cur = conn.execute(
                    "INSERT INTO paper_orders (account_id, gtt_id, symbol, side, qty, price, trigger, status, "
                    "trade_date, placed_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)",
                    (g.account_id, g.id, g.symbol, o.side, o.qty, o.price, g.trigger_values[leg],
                     now.date().isoformat(), now.isoformat(timespec="seconds")))
                order_id = int(cur.lastrowid)
                conn.execute("UPDATE paper_gtts SET status = 'triggered', triggered_leg = ?, order_id = ?, "
                             "updated_at = ? WHERE id = ?", (leg, order_id, ts(), g.id))
                events.append({"type": "triggered", "gtt_id": g.id, "account_id": g.account_id, "symbol": g.symbol,
                               "leg": leg, "order_id": order_id})
                row = conn.execute("SELECT * FROM paper_orders WHERE id = ?", (order_id,)).fetchone()
                ref = self._trigger_price(g, leg, q, pre_open)
                ev = self._try_fill(conn, _order(row), g.trigger_values[leg], q, now, just_triggered=True, ref=ref)
                if ev:
                    events.append(ev)
        return events

    @staticmethod
    def _hit_leg(g: Gtt, hi: float, lo: float) -> int | None:
        if g.type == "single":
            t = g.trigger_values[0]
            if t > g.last_price:
                return 0 if hi >= t else None
            return 0 if lo <= t else None
        stop_hit = lo <= g.trigger_values[0]
        target_hit = hi >= g.trigger_values[1]
        if stop_hit:
            return 0          # both touched in one window: assume the stop fired first
        if target_hit:
            return 1
        return None

    @staticmethod
    def _trigger_price(g: Gtt, leg: int, q: Quote, pre_open: bool) -> float | None:
        """Price at the moment the trigger fired. Kite fires the GTT when the price crosses the trigger and the
        limit order goes to the exchange at once, so it fills around the trigger, not at whatever price the
        monitor happens to see a minute (or, in replay, hours) later. A gap at the open is the exception: then the
        first traded price is the open. Unknown open (no ohlc in the quote): None, the fill uses the LTP."""
        if q.open is None:
            return None
        trig = g.trigger_values[leg]
        upward = (leg == 1) if g.type == "oco" else trig > g.last_price
        if pre_open and ((upward and q.open >= trig) or (not upward and q.open <= trig)):
            return q.open
        return trig

    @staticmethod
    def _available(o: Order, q: Quote) -> int | None:
        """Quantity the book offers at or better than our limit; None when depth is unknown."""
        if o.side == "BUY":
            return sum(n for p, n in q.asks if p <= o.price + 1e-9) if q.asks else None
        return sum(n for p, n in q.bids if p >= o.price - 1e-9) if q.bids else None

    def _try_fill(self, conn, o: Order, trigger: float | None, q: Quote, now: datetime,
                  just_triggered: bool, ref: float | None = None) -> dict[str, Any] | None:
        limit = o.price
        use_ref = just_triggered and ref is not None
        if o.side == "BUY":
            if q.upper_circuit and q.ltp >= q.upper_circuit:
                return None
            if use_ref:
                if ref > limit + 1e-9:
                    return None              # opened above the limit: the order rests, no chasing
                price = ref
            else:
                if q.ltp > limit:
                    return None
                price = min(limit, max(q.ltp, trigger or q.ltp)) if just_triggered else limit
        else:
            if q.lower_circuit and q.ltp <= q.lower_circuit:
                return None
            is_target = trigger is not None and abs(limit - trigger) < 1e-9
            if is_target:
                if not just_triggered and q.ltp < limit:
                    return None
                price = limit
            elif use_ref:
                if ref < limit - 1e-9:
                    return None              # gapped below the stop leg's limit: the order rests
                price = ref
            else:
                if q.ltp < limit:
                    return None
                price = max(limit, min(q.ltp, trigger or q.ltp)) if just_triggered else limit
        price = round(price, 2)
        remaining = o.qty - o.filled_qty
        avail = self._available(o, q)
        n = remaining if avail is None else min(remaining, avail)
        if n <= 0:
            return None
        today = now.date().isoformat()
        if o.side == "SELL":
            held = conn.execute("SELECT qty FROM paper_positions WHERE account_id = ? AND symbol = ?",
                                (o.account_id, o.symbol)).fetchone()
            if not held or held["qty"] < n:
                conn.execute("UPDATE paper_orders SET status = 'rejected', reason = 'Not enough holdings' "
                             "WHERE id = ?", (o.id,))
                return {"type": "rejected", "order_id": o.id, "account_id": o.account_id, "symbol": o.symbol,
                        "reason": "Not enough holdings"}
            first_sell = conn.execute(
                "SELECT 1 FROM paper_orders WHERE account_id = ? AND symbol = ? AND side = 'SELL' AND filled_qty > 0 "
                "AND trade_date = ?", (o.account_id, o.symbol, today)).fetchone() is None
            # Shares bought today and sold today are intraday for Zerodha's charges (same-day sells match
            # same-day buys first).
            bought = conn.execute("SELECT COALESCE(SUM(filled_qty), 0), COALESCE(SUM(filled_qty * filled_price), 0) "
                                  "FROM paper_orders WHERE account_id = ? AND symbol = ? AND side = 'BUY' AND "
                                  "trade_date = ?", (o.account_id, o.symbol, today)).fetchone()
            sold = conn.execute("SELECT COALESCE(SUM(filled_qty), 0) FROM paper_orders WHERE account_id = ? AND "
                                "symbol = ? AND side = 'SELL' AND trade_date = ? AND id != ?",
                                (o.account_id, o.symbol, today, o.id)).fetchone()[0] + o.filled_qty
            n_intraday = max(0, min(n, int(bought[0]) - int(sold)))
            if n_intraday:
                ch = same_day_sell(n_intraday, price, bought[1] / bought[0], n - n_intraday, first_sell)
            else:
                ch = compute_charges("SELL", n, price, first_sell)
            conn.execute("UPDATE accounts SET cash = cash + ? WHERE id = ?", (n * price - ch.total, o.account_id))
            conn.execute("UPDATE paper_positions SET qty = qty - ? WHERE account_id = ? AND symbol = ?",
                         (n, o.account_id, o.symbol))
            conn.execute("DELETE FROM paper_positions WHERE account_id = ? AND symbol = ? AND qty <= 0",
                         (o.account_id, o.symbol))
        else:
            ch = compute_charges("BUY", n, price)
            cash = conn.execute("SELECT cash FROM accounts WHERE id = ?", (o.account_id,)).fetchone()[0]
            # Kite blocks funds for the whole order when it is placed: check once, before the first fill.
            if o.filled_qty == 0 and cash < remaining * price + compute_charges("BUY", remaining, price).total:
                conn.execute("UPDATE paper_orders SET status = 'rejected', reason = 'Insufficient funds' "
                             "WHERE id = ?", (o.id,))
                return {"type": "rejected", "order_id": o.id, "account_id": o.account_id, "symbol": o.symbol,
                        "reason": "Insufficient funds"}
            conn.execute("UPDATE accounts SET cash = cash - ? WHERE id = ?", (n * price + ch.total, o.account_id))
            pos = conn.execute("SELECT qty, avg_price FROM paper_positions WHERE account_id = ? AND symbol = ?",
                               (o.account_id, o.symbol)).fetchone()
            if pos:
                new_qty = pos["qty"] + n
                avg = (pos["qty"] * pos["avg_price"] + n * price) / new_qty
                conn.execute("UPDATE paper_positions SET qty = ?, avg_price = ? WHERE account_id = ? AND symbol = ?",
                             (new_qty, avg, o.account_id, o.symbol))
            else:
                conn.execute("INSERT INTO paper_positions (account_id, symbol, qty, avg_price) VALUES (?, ?, ?, ?)",
                             (o.account_id, o.symbol, n, price))
        filled = o.filled_qty + n
        avg_price = round(((o.filled_price or 0) * o.filled_qty + price * n) / filled, 4)
        status = "complete" if filled == o.qty else "open"
        conn.execute("UPDATE paper_orders SET status = ?, filled_qty = ?, filled_price = ?, filled_at = ?, "
                     "charges = COALESCE(charges, 0) + ? WHERE id = ?",
                     (status, filled, avg_price, now.isoformat(timespec="seconds"), ch.total, o.id))
        o.filled_qty, o.filled_price = filled, avg_price
        return {"type": "filled" if status == "complete" else "partial", "order_id": o.id,
                "account_id": o.account_id, "symbol": o.symbol, "side": o.side, "qty": n, "filled_qty": filled,
                "order_qty": o.qty, "price": price, "charges": ch.total}

    def end_session(self, day: str) -> int:
        """Kite cancels a triggered GTT's unfilled limit order at the end of the session."""
        with connect() as conn:
            cur = conn.execute("UPDATE paper_orders SET status = 'cancelled', reason = CASE WHEN filled_qty > 0 "
                               "THEN 'Partly filled; the rest was cancelled at session end' "
                               "ELSE 'Unfilled at session end' END WHERE status = 'open' AND trade_date <= ?", (day,))
            return cur.rowcount


_broker: PaperBroker | None = None


def get_broker() -> PaperBroker:
    """Broker selected by config broker.mode. Only paper exists in the MVP (KiteBroker is Phase 4+)."""
    global _broker
    mode = get_app_config()["broker"]["mode"]
    if mode != "paper":
        raise RuntimeError("Only the PaperBroker is available in the MVP")
    if _broker is None:
        _broker = PaperBroker()
    return _broker
