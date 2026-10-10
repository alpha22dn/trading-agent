"""Read-only views for the Accounts and Positions screens."""
from __future__ import annotations

import json
from typing import Any

from ..broker.paper import get_broker
from ..clock import now_ist
from ..db import connect, get_state
from ..risk import controls
from .charges import compute as compute_charges
from .manager import HOLDING_STATES, OPEN_STATES


def _net(gross: float, qty: int, price: float, buy_charges: float) -> dict[str, float]:
    """Net unrealised = gross − buy charges already paid on these shares − Zerodha charges to sell them now
    (STT, exchange, SEBI, GST and the DP charge, as the paper broker would book them)."""
    sell = compute_charges("SELL", int(qty), float(price)).total if qty > 0 else 0.0
    return {"net_unrealized": round(gross - buy_charges - sell, 2), "buy_charges_open": round(buy_charges, 2),
            "sell_charges_est": round(sell, 2)}


def _streamed(symbols: set[str]) -> dict[str, dict[str, Any]]:
    """Prices from Kite's stream (healthy connection, ticked on it), so screens move with every tick."""
    from ..broker import feed
    st = feed.stream()
    if st is None or not symbols or not st.healthy():
        return {}
    out: dict[str, dict[str, Any]] = {}
    at = now_ist().isoformat(timespec="seconds")
    for s, tok in feed._token_map(sorted(symbols)).items():
        t = st.tick(tok)
        if t is not None:
            out[s] = {"price": t.ltp, "as_of": at, "source": "stream"}
    return out


def _prices(symbols: set[str]) -> dict[str, dict[str, Any]]:
    """Latest price per symbol: the streamed price when Kite's stream has one, else today's live quote from the
    order monitor (once a minute), else the last daily close."""
    out: dict[str, dict[str, Any]] = _streamed(set(symbols))
    lq = get_state("last_quotes") or {}
    live_today = (lq.get("at") or "").startswith(now_ist().date().isoformat())
    for s in symbols:
        if s not in out and live_today and s in (lq.get("ltp") or {}):
            out[s] = {"price": lq["ltp"][s], "as_of": lq["at"], "source": "live"}
    missing = [s for s in symbols if s not in out]
    if missing:
        with connect() as conn:
            q = ",".join("?" * len(missing))
            for r in conn.execute(f"""SELECT c.symbol, c.close, c.date FROM candles_daily c
                                      JOIN (SELECT symbol, MAX(date) d FROM candles_daily WHERE symbol IN ({q})
                                            GROUP BY symbol) m ON m.symbol = c.symbol AND m.d = c.date""", missing):
                out[r["symbol"]] = {"price": r["close"], "as_of": r["date"], "source": "close"}
    return out


def _trades(where: str, args: tuple, limit: int | None = None) -> list[dict[str, Any]]:
    sql = f"SELECT * FROM trades WHERE {where} ORDER BY COALESCE(closed_at, created_at) DESC, account_id" + (f" LIMIT {int(limit)}" if limit else "")
    with connect() as conn:
        return [dict(r) for r in conn.execute(sql, args)]


def _decorate(t: dict[str, Any], prices: dict[str, dict[str, Any]]) -> dict[str, Any]:
    t = dict(t)
    t["exits"] = json.loads(t.get("exits") or "[]")
    p = prices.get(t["symbol"])
    t["price"] = p["price"] if p else None
    t["price_as_of"] = p["as_of"] if p else None
    t["price_source"] = p["source"] if p else None
    if t["state"] in HOLDING_STATES and t.get("entry_price") and p:
        realized = sum(e["qty"] * (e["price"] - t["entry_price"]) - e["charges"] for e in t["exits"])
        unreal = t["open_qty"] * (p["price"] - t["entry_price"])
        risk = (t["entry_price"] - (t["initial_stop"] or t["stop"])) * t["qty"]
        t["unrealized"] = round(unreal, 2)
        # buy charges belong to the shares still held pro rata; charges on sells already made are realised.
        # While a trade is open its `charges` holds the buy charges only (exit charges are added at close).
        bought = t.get("bought_qty") or t["qty"]
        buy_charges = t["charges"] or 0
        t.update(_net(unreal, t["open_qty"], p["price"], buy_charges * t["open_qty"] / bought if bought else 0))
        t["pnl_gross_so_far"] = round(realized + unreal - (t["charges"] or 0), 2)
        t["r_now"] = round((realized + unreal - (t["charges"] or 0)) / risk, 2) if risk > 0 else None
        t["value"] = round(t["open_qty"] * p["price"], 2)
        t["locked_in"] = round(t["open_qty"] * (t["stop"] - t["entry_price"]), 2)
    return t


def accounts_summary() -> dict[str, Any]:
    broker = get_broker()
    accts = controls.accounts()
    open_trades = _trades(f"state IN ({','.join('?' * len(OPEN_STATES))})", OPEN_STATES)
    broker_pos = {a["id"]: broker.positions(a["id"]) for a in accts}
    prices = _prices({t["symbol"] for t in open_trades if t["state"] in HOLDING_STATES}
                     | {p.symbol for ps in broker_pos.values() for p in ps})
    with connect() as conn:
        # statistics and the equity curve cover the current run: since the last reset or capital change
        closed = {r["account_id"]: dict(r) for r in conn.execute(
            """SELECT t.account_id, COUNT(*) n, SUM(t.net_pnl) pnl, SUM(t.net_pnl > 0) wins, SUM(t.charges) charges,
                      AVG(t.r_multiple) avg_r FROM trades t JOIN accounts a ON a.id = t.account_id
               WHERE t.state = 'CLOSED' AND t.created_at >= COALESCE(a.reset_at, '') GROUP BY t.account_id""")}
        curve: dict[str, list[dict[str, Any]]] = {}
        for r in conn.execute("""SELECT e.account_id, e.date, e.equity, e.cash, e.holdings, e.peak_equity
                                 FROM equity_daily e JOIN accounts a ON a.id = e.account_id
                                 WHERE e.date >= substr(COALESCE(a.reset_at, ''), 1, 10) ORDER BY e.date"""):
            curve.setdefault(r["account_id"], []).append(dict(r))
        runs = {r[0]: r[1] for r in conn.execute("SELECT account_id, COUNT(*) FROM capital_changes WHERE kind != "
                                                 "'created' GROUP BY account_id")}
    with connect() as conn:
        acc_reset = {r[0]: r[1] for r in conn.execute("SELECT id, reset_at FROM accounts")}
    out = []
    for a in accts:
        mine = [t for t in open_trades if t["account_id"] == a["id"]]
        cash = broker.cash(a["id"])
        reserved = sum(t["qty"] * t["entry_limit"] for t in mine if t["state"] == "BUY_PENDING")
        holdings = 0.0
        for pos in broker_pos[a["id"]]:
            p = prices.get(pos.symbol)
            holdings += pos.qty * (p["price"] if p else pos.avg_price)
        equity = cash + holdings
        peak = max(float(a["peak_equity"]), equity)
        c = closed.get(a["id"]) or {}
        out.append(a | {
            "cash": round(cash, 2), "reserved": round(reserved, 2), "free_cash": round(cash - reserved, 2),
            "holdings": round(holdings, 2), "equity": round(equity, 2), "peak": round(peak, 2),
            "drawdown_pct": round((peak - equity) / peak * 100, 2) if peak else 0,
            "return_pct": round((equity / a["capital"] - 1) * 100, 2),
            "open_positions": sum(1 for t in mine if t["state"] in HOLDING_STATES),
            "pending_buys": sum(1 for t in mine if t["state"] == "BUY_PENDING"),
            "closed_trades": c.get("n") or 0, "wins": c.get("wins") or 0,
            "realized_pnl": round(c.get("pnl") or 0, 2), "charges_paid": round(c.get("charges") or 0, 2),
            "avg_r": round(c["avg_r"], 2) if c.get("avg_r") is not None else None,
            "equity_curve": curve.get(a["id"], []),
            "reset_at": acc_reset.get(a["id"]), "restarts": runs.get(a["id"], 0),
            "flat": not mine and not any(p for p in broker_pos[a["id"]]),
        })
    return {"accounts": out, "kill_switch": controls.kill_switch_state(), "monitor": get_state("monitor_last")}


def positions(account_id: str | None = None, closed_limit: int = 50) -> dict[str, Any]:
    acc_sql, acc_args = ("account_id = ? AND ", (account_id,)) if account_id else ("", ())
    open_rows = _trades(acc_sql + f"state IN ({','.join('?' * len(OPEN_STATES))})", (*acc_args, *OPEN_STATES))
    closed_rows = _trades(acc_sql + "state IN ('CLOSED', 'CANCELLED')", acc_args, closed_limit)
    prices = _prices({t["symbol"] for t in open_rows})
    with connect() as conn:
        gtt_rows = {}
        if open_rows:
            ids = [t["id"] for t in open_rows]
            for r in conn.execute(f"SELECT * FROM gtts WHERE status = 'active' AND trade_id IN "
                                  f"({','.join('?' * len(ids))}) ORDER BY id", ids):
                d = dict(r)
                d["legs"] = json.loads(d["legs"])
                d["trigger_history"] = json.loads(d["trigger_history"] or "[]")
                gtt_rows.setdefault(r["trade_id"], []).append(d)
    broker = get_broker()
    out_open = []
    for t in open_rows:
        d = _decorate(t, prices)
        d["gtts"] = gtt_rows.get(t["id"], [])
        for g in d["gtts"]:
            bg = broker.get_gtt(t["account_id"], int(g["gtt_id"]))
            g["broker_status"] = bg.status if bg else "missing"
            if bg and bg.order_id:
                o = broker.get_order(t["account_id"], bg.order_id)
                g["order"] = o.__dict__ if o else None
        out_open.append(d)
    with connect() as conn:
        u_sql = "SELECT * FROM unmanaged_holdings WHERE qty > 0" + (" AND account_id = ?" if account_id else "")
        unmanaged = [dict(r) for r in conn.execute(u_sql, (account_id,) if account_id else ())]
        manual_accounts = [r[0] for r in conn.execute("SELECT id FROM accounts WHERE manual_edits = 1 AND "
                                                      "COALESCE(archived, 0) = 0")]
    uprices = _prices({u["symbol"] for u in unmanaged})
    for u in unmanaged:
        p = uprices.get(u["symbol"])
        u["price"] = p["price"] if p else None
        u["unrealized"] = round(u["qty"] * (p["price"] - u["avg_price"]), 2) if p and u["avg_price"] else None
        if u["unrealized"] is not None:  # the hand buy was charged like any fill: same rates on its value
            u.update(_net(u["unrealized"], u["qty"], p["price"], compute_charges("BUY", u["qty"], u["avg_price"]).total))
        u["price_source"] = p["source"] if p else None
        u["price_as_of"] = p["as_of"] if p else None
    with connect() as conn:
        resets = {r[0]: r[1] for r in conn.execute("SELECT id, reset_at FROM accounts")}
        all_ids = [r[0] for r in conn.execute("SELECT id FROM accounts WHERE COALESCE(archived, 0) = 0 "
                                              "ORDER BY capital, id")]
    closed_out = []
    for t in closed_rows:
        d = _decorate(t, {})
        d["before_reset"] = bool(resets.get(t["account_id"]) and t["created_at"] < resets[t["account_id"]])
        closed_out.append(d)
    return {"open": out_open, "closed": closed_out, "unmanaged": unmanaged, "accounts": all_ids,
            "manual_accounts": manual_accounts,
            "last_quotes_at": (get_state("last_quotes") or {}).get("at"), "monitor": get_state("monitor_last")}


def trade_detail(trade_id: int) -> dict[str, Any] | None:
    with connect() as conn:
        t = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
        if not t:
            return None
        events = [dict(r) | {"data": json.loads(r["data"]) if r["data"] else None} for r in conn.execute(
            "SELECT * FROM trade_events WHERE trade_id = ? ORDER BY id", (trade_id,))]
        gtts = [dict(r) | {"legs": json.loads(r["legs"]), "trigger_history": json.loads(r["trigger_history"] or "[]")}
                for r in conn.execute("SELECT * FROM gtts WHERE trade_id = ? ORDER BY id", (trade_id,))]
        sig = conn.execute("SELECT date, indicators, gates FROM signals WHERE id = ?", (t["signal_id"],)).fetchone()
        overrides = [dict(r) | {"before": json.loads(r["before"]) if r["before"] else None,
                                "after": json.loads(r["after"]) if r["after"] else None}
                     for r in conn.execute("SELECT * FROM overrides WHERE trade_id = ? ORDER BY id", (trade_id,))]
        manual = bool(conn.execute("SELECT manual_edits FROM accounts WHERE id = ?", (t["account_id"],)).fetchone()[0])
    d = _decorate(dict(t), _prices({t["symbol"]}))
    d["events"] = events
    d["overrides"] = overrides
    d["manual_edits"] = manual
    d["gtt_history"] = gtts
    d["signal"] = {"date": sig["date"], "indicators": json.loads(sig["indicators"] or "{}")} if sig else None
    return d


def candidates_for(signal_ids: list[int]) -> dict[int, list[dict[str, Any]]]:
    if not signal_ids:
        return {}
    out: dict[int, list[dict[str, Any]]] = {}
    with connect() as conn:
        q = ",".join("?" * len(signal_ids))
        for r in conn.execute(f"""SELECT c.signal_id, c.account_id, c.qty, c.rank, c.taken, c.skip_reason,
                                         t.state AS trade_state, t.id AS trade_id
                                  FROM candidates c LEFT JOIN trades t ON t.id = (
                                       SELECT MAX(id) FROM trades WHERE signal_id = c.signal_id
                                       AND account_id = c.account_id)
                                  WHERE c.signal_id IN ({q}) ORDER BY c.account_id""", signal_ids):
            out.setdefault(r["signal_id"], []).append(dict(r))
    return out
