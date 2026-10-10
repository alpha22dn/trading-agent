"""Paper edit actions (spec section 8): on the manual-edit account (P60) the dashboard lets you change paper
GTTs and positions the way you would in Kite, so the reconciliation cases can be tested.

These act on the broker directly, like a person in the Kite app. The agent then detects the change in its
reconciliation step (run straight away here, and every monitor cycle) and adopts it per the spec's table.
"""
from __future__ import annotations

from typing import Any

from .. import audit
from ..broker.base import BrokerReject, Leg
from ..broker.paper import get_broker
from ..clock import now_ist
from ..db import connect, get_state, set_state
from ..settings import get_app_config, load_strategy_config
from ..strategy.momentum_v1 import round_down
from . import manager


class EditError(Exception):
    pass


def _account(account_id: str) -> dict[str, Any]:
    with connect() as conn:
        a = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if a is None:
        raise EditError(f"Unknown account {account_id}")
    if not a["manual_edits"]:
        raise EditError(f"{account_id} is a baseline account: manual edits are only allowed on the manual-edit account")
    if get_app_config()["broker"]["mode"] != "paper":
        raise EditError("Paper edits exist only in paper mode")
    return dict(a)


def last_price(symbol: str, side: str | None = None) -> float:
    """Price for a paper hand-order or a GTT edit: a live Kite quote while the market is open, else the latest
    monitor quote from today, else the last daily close. A market buy fills at the best offer (ask) and a market
    sell at the best bid, as on the exchange; without market depth, at the last traded price."""
    from ..broker import feed
    from ..market_calendar import MarketCalendar
    from ..settings import get_app_config
    if MarketCalendar(get_app_config()).is_market_open(now_ist()):
        try:
            q = feed.quote_source([symbol]).get(symbol)
        except Exception:  # noqa: BLE001 - no live quote: fall back below
            q = None
        if q:
            if side == "BUY" and q.asks:
                return float(min(p for p, _ in q.asks))
            if side == "SELL" and q.bids:
                return float(max(p for p, _ in q.bids))
            return float(q.ltp)
    lq = get_state("last_quotes") or {}
    if (lq.get("at") or "").startswith(now_ist().date().isoformat()) and symbol in (lq.get("ltp") or {}):
        return float(lq["ltp"][symbol])
    with connect() as conn:
        r = conn.execute("SELECT close FROM candles_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1",
                         (symbol,)).fetchone()
    if r is None:
        raise EditError(f"No price for {symbol}")
    return float(r[0])


def _trade(trade_id: int) -> dict[str, Any]:
    with connect() as conn:
        t = conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone()
    if t is None:
        raise EditError("No such trade")
    _account(t["account_id"])
    return dict(t)


def _gtt_row(trade_id: int, purpose: str) -> dict[str, Any]:
    with connect() as conn:
        r = conn.execute("SELECT * FROM gtts WHERE trade_id = ? AND purpose = ? AND status = 'active' ORDER BY id DESC "
                         "LIMIT 1", (trade_id, purpose)).fetchone()
    if r is None:
        raise EditError(f"No active {purpose} GTT for this trade")
    return dict(r)


def edit_trade(trade_id: int, action: str, value: float | None = None, qty: int | None = None) -> dict[str, Any]:
    t = _trade(trade_id)
    broker = get_broker()
    acc, sym = t["account_id"], t["symbol"]
    try:
        if action in ("move_stop", "move_target"):
            row = _gtt_row(trade_id, "exit")
            g = broker.get_gtt(acc, int(row["gtt_id"]))
            if g is None or g.status != "active":
                raise EditError("The exit GTT is not active (it may have triggered)")
            if value is None or value <= 0:
                raise EditError("Enter a price")
            trig = list(g.trigger_values)
            legs = [Leg(o.side, o.qty, o.price) for o in g.orders]
            tick = _tick(sym)
            if action == "move_stop":
                pct = float(load_strategy_config(t["strategy_version"]).params["exits"]["stop_leg_limit_pct"]) / 100
                trig[0] = round(value, 2)
                legs[0] = Leg("SELL", legs[0].qty, round_down(value * (1 - pct), tick))
            else:
                if g.type != "oco":
                    raise EditError("This exit has no target leg (single stop)")
                trig[1] = round(value, 2)
                legs[1] = Leg("SELL", legs[1].qty, round(value, 2))
            broker.modify_gtt(acc, g.id, trig, last_price(sym), legs)
            what = f"{'stop' if action == 'move_stop' else 'target'} → {value}"
        elif action in ("cancel_exit", "cancel_buy"):
            row = _gtt_row(trade_id, "exit" if action == "cancel_exit" else "buy")
            broker.cancel_gtt(acc, int(row["gtt_id"]))
            what = f"{'exit' if action == 'cancel_exit' else 'buy'} GTT cancelled"
        elif action == "sell":
            n = int(qty or 0)
            if n < 1 or n > int(t["open_qty"] or 0):
                raise EditError(f"Sell between 1 and {t['open_qty']} shares")
            o = broker.manual_order(acc, sym, "SELL", n, last_price(sym, "SELL"))
            what = f"sold {n} by hand @ {o.filled_price}"
        else:
            raise EditError(f"Unknown action {action}")
    except BrokerReject as exc:
        raise EditError(str(exc)) from exc
    audit.user_action(f"paper_edit_{action}", f"{acc} {sym}: {what}")
    manager.reconcile_now()
    return manager._trade(trade_id)


def buy_outside(account_id: str, symbol: str, qty: int) -> dict[str, Any]:
    _account(account_id)
    symbol = symbol.strip().upper()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM instruments WHERE symbol = ? AND exchange = 'NSE'", (symbol,)).fetchone():
            raise EditError(f"{symbol} is not an NSE instrument")
    if qty < 1:
        raise EditError("Quantity must be at least 1")
    try:
        o = get_broker().manual_order(account_id, symbol, "BUY", qty, last_price(symbol, "BUY"))
    except BrokerReject as exc:
        raise EditError(str(exc)) from exc
    audit.user_action("paper_edit_buy_outside", f"{account_id} {symbol}: bought {qty} by hand @ {o.filled_price}")
    manager.reconcile_now()
    return {"order_id": o.id, "price": o.filled_price, "qty": o.filled_qty}


def _tick(symbol: str) -> float:
    return manager._tick(symbol)


def sell_outside(account_id: str, symbol: str, qty: int) -> dict[str, Any]:
    """Sell (part of) a holding bought outside the system, at a live price like a hand-placed market order."""
    _account(account_id)
    symbol = symbol.strip().upper()
    with connect() as conn:
        r = conn.execute("SELECT qty FROM unmanaged_holdings WHERE account_id = ? AND symbol = ?",
                         (account_id, symbol)).fetchone()
    held = int(r[0]) if r else 0
    if qty < 1 or qty > held:
        raise EditError(f"Sell between 1 and {held} shares of {symbol}")
    try:
        o = get_broker().manual_order(account_id, symbol, "SELL", qty, last_price(symbol, "SELL"))
    except BrokerReject as exc:
        raise EditError(str(exc)) from exc
    # This sale is known to be from the outside holding: book it there directly, so reconciliation doesn't
    # attribute it to an agent trade in the same stock.
    with connect() as conn:
        conn.execute("UPDATE unmanaged_holdings SET qty = MAX(0, qty - ?) WHERE account_id = ? AND symbol = ?",
                     (o.filled_qty, account_id, symbol))
    set_state("manual_orders_seen", sorted(set(get_state("manual_orders_seen", [])) | {o.id}))
    audit.user_action("paper_edit_sell_outside", f"{account_id} {symbol}: sold {qty} by hand @ {o.filled_price}")
    manager.reconcile_now()
    return {"order_id": o.id, "price": o.filled_price, "qty": o.filled_qty}
