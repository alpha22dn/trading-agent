"""Trade lifecycle (spec sections 6-8): entries, exit GTTs, trailing, expiry, equity.

States
  BUY_PENDING  buy GTT placed (or its limit order open for the day)
  FILLED       bought; exit GTT not attached yet (attached on the next monitor cycle)
  PROTECTED    exit GTT active: OCO (qty >= 4: stop on all, target on half) or single stop (qty 1-3)
  TRAILING     target reached; stop at or above entry and trailed at EOD
               (a position that closed below its stop is PROTECTED by an exit-at-next-open GTT: exit_at_open)
  CLOSED       fully sold
  CANCELLED    buy never filled (2 sessions, unfilled at close, rejected, kill switch)

Every transition is written to trade_events. The broker is always read back before acting.
The broker is called with no database transaction open (it writes its own rows).
"""
from __future__ import annotations

import json
import logging
import math
import threading
from functools import wraps
from datetime import date, datetime, timedelta
from typing import Any

from .. import audit
from ..broker import feed
from ..broker.base import BrokerReject, Leg, Quote
from ..broker.paper import get_broker
from ..clock import IST, now_ist, ts
from ..db import connect, get_state, set_state
from ..events import publish
from ..market_calendar import MarketCalendar
from ..risk import controls
from ..risk.sizing import AccountState, Candidate, select
from ..settings import StrategyConfig, get_app_config, load_strategy_config
from ..strategy.momentum_v1 import round_down

log = logging.getLogger("agent")

OPEN_STATES = ("BUY_PENDING", "FILLED", "PROTECTED", "TRAILING")
HOLDING_STATES = ("FILLED", "PROTECTED", "TRAILING")

# The monitor, the EOD run and dashboard actions (kill switch) never interleave on trades.
_lock = threading.RLock()


def _locked(fn):
    @wraps(fn)
    def wrapper(*a, **kw):
        with _lock:
            return fn(*a, **kw)
    return wrapper


# ---------- small DB helpers (each opens and closes its own connection) ----------

def _strategy() -> StrategyConfig:
    """The champion (what portfolios trade unless they name their own version). A backtest replaces this."""
    from ..strategy import versions
    return versions.champion()


def _strategy_named(version: str | None) -> StrategyConfig:
    """A version's parameters. The engine's current strategy wins when the name matches (a backtest runs its
    what-if settings under the live version's name)."""
    base = _strategy()
    if not version or version == base.version:
        return base
    from ..strategy import versions
    return versions.load(version)


def _trade(trade_id: int) -> dict[str, Any]:
    with connect() as conn:
        return dict(conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone())


def _open_trades(states: tuple[str, ...] = OPEN_STATES) -> list[dict[str, Any]]:
    q = ",".join("?" * len(states))
    with connect() as conn:
        return [dict(r) for r in conn.execute(f"SELECT * FROM trades WHERE state IN ({q}) ORDER BY id", states)]


def _set(trade_id: int, **fields: Any) -> None:
    fields["updated_at"] = ts()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE trades SET {cols} WHERE id = ?", (*fields.values(), trade_id))


def _event(trade_id: int, from_state: str | None, to_state: str | None, message: str,
           data: dict[str, Any] | None = None) -> None:
    with connect() as conn:
        conn.execute("INSERT INTO trade_events (trade_id, ts, from_state, to_state, message, data) "
                     "VALUES (?, ?, ?, ?, ?, ?)",
                     (trade_id, ts(), from_state, to_state, message, json.dumps(data, default=str) if data else None))
    publish("trades")


def _transition(t: dict[str, Any], to_state: str, message: str, data: dict[str, Any] | None = None,
                **fields: Any) -> None:
    _set(t["id"], state=to_state, **fields)
    _event(t["id"], t["state"], to_state, message, data)
    t.update(fields, state=to_state)


def _current_gtt(trade_id: int, purpose: str) -> dict[str, Any] | None:
    with connect() as conn:
        r = conn.execute("SELECT * FROM gtts WHERE trade_id = ? AND purpose = ? AND status = 'active' "
                         "ORDER BY id DESC LIMIT 1", (trade_id, purpose)).fetchone()
    return dict(r) if r else None


def _record_gtt(trade_id: int, broker_id: int, purpose: str, type_: str, triggers: list[float],
                legs: list[Leg]) -> None:
    now = ts()
    with connect() as conn:
        conn.execute("INSERT INTO gtts (trade_id, gtt_id, purpose, type, legs, status, trigger_history, created_at, "
                     "updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?, ?, ?)",
                     (trade_id, str(broker_id), purpose, type_,
                      json.dumps({"triggers": triggers, "orders": [l.__dict__ for l in legs]}),
                      json.dumps([{"at": now, "triggers": triggers}]), now, now))


def _close_gtt_row(row_id: int, status: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE gtts SET status = ?, updated_at = ? WHERE id = ?", (status, ts(), row_id))


def _tick(symbol: str) -> float:
    with connect() as conn:
        r = conn.execute("SELECT tick_size FROM instruments WHERE symbol = ? AND exchange = 'NSE'",
                         (symbol,)).fetchone()
    return float(r[0]) if r and r[0] else 0.05


def _alert_once(key: str, name: str, message: str, level: str = "warning") -> None:
    seen = get_state("alerts_sent", {})
    if key in seen:
        return
    seen[key] = ts()
    set_state("alerts_sent", seen)
    audit.alert(name, message, level)


def _linked_gtt_ids(account_id: str) -> set[int]:
    with connect() as conn:
        return {int(r[0]) for r in conn.execute(
            "SELECT g.gtt_id FROM gtts g JOIN trades t ON t.id = g.trade_id WHERE t.account_id = ? AND g.status = "
            "'active'", (account_id,))}


def _unlinked(account_id: str, symbol: str, side: str) -> list:
    broker = get_broker()
    linked = _linked_gtt_ids(account_id)
    return [g for g in broker.gtts(account_id, "active")
            if g.symbol == symbol and g.id not in linked and g.orders and g.orders[0].side == side]


def _cancel_unlinked(account_id: str, symbol: str, side: str) -> None:
    for g in _unlinked(account_id, symbol, side):
        try:
            get_broker().cancel_gtt(account_id, g.id)
        except BrokerReject:
            continue
        audit.record("system", "duplicate_prevented", f"{account_id} {symbol}: cancelled unlinked {side} GTT {g.id} "
                     "before placing a new one", "warning")


# ---------- entries (EOD) ----------

def account_state(account_id: str) -> AccountState:
    broker = get_broker()
    with connect() as conn:
        acc = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        rows = conn.execute(f"""SELECT id, symbol, sector, state, qty, bought_qty, entry_limit,
                                   EXISTS (SELECT 1 FROM gtts g WHERE g.trade_id = trades.id AND g.purpose = 'buy'
                                           AND g.status = 'active') AS buy_working
                                FROM trades WHERE account_id = ? AND state IN ({','.join('?' * len(OPEN_STATES))})""",
                            (account_id, *OPEN_STATES)).fetchall()
        # Stocks bought outside the system: left unmanaged, but they count toward the sector cap (and the
        # cash they used is already gone), and the agent never buys the same stock again (spec section 8).
        outside = conn.execute("""SELECT u.symbol, i.sector FROM unmanaged_holdings u LEFT JOIN instruments i
                                  ON i.symbol = u.symbol AND i.exchange = 'NSE' WHERE u.account_id = ? AND u.qty > 0""",
                               (account_id,)).fetchall()
    sectors: dict[str, int] = {}
    for r in list(rows) + list(outside):
        sectors[r["sector"] or "Unknown"] = sectors.get(r["sector"] or "Unknown", 0) + 1
    # Cash still committed to buys: whole pending buys, plus the unfilled rest of a partly filled buy.
    reserved = sum((r["qty"] - (r["bought_qty"] or 0)) * r["entry_limit"] for r in rows if r["buy_working"])
    return AccountState(account_id=account_id, capital=float(acc["capital"]), cash=broker.cash(account_id),
                        reserved=reserved, open_slots_used=len(rows),
                        symbols={r["symbol"] for r in rows} | {r["symbol"] for r in outside},
                        sector_counts=sectors, blocked_reason=controls.entries_blocked(account_id))


def _live_prices(symbols: list[str]) -> dict[str, float] | None:
    try:
        return {s: float(q.ltp) for s, q in feed.quote_source(symbols).items()}
    except Exception:  # noqa: BLE001 - no session / API down: the caller fails closed
        return None


@_locked
def refresh_entries(day: str, strategy: StrategyConfig | None = None) -> dict[str, Any]:
    """After every signal refresh (EOD run, login, watchlist upload, the Re-run button): cancel pending buys
    whose signal is no longer tradeable, then place buy GTTs for tradeable signals in rank order."""
    dropped = cancel_dropped_buys()
    out = place_entries(day, strategy)
    return out | {"dropped": dropped}


def _cancel_pending_buy(t: dict[str, Any], note: str, reason: str) -> bool:
    """Cancel a pending buy that has not triggered or bought anything. False when it is past that point."""
    broker = get_broker()
    if _bought(t):
        return False
    row = _current_gtt(t["id"], "buy")
    if row:
        g = broker.get_gtt(t["account_id"], int(row["gtt_id"]))
        if g and g.status != "active":
            return False  # already triggered: the order's own fate decides
        if g:
            try:
                broker.cancel_gtt(t["account_id"], g.id)
            except BrokerReject:
                return False
        _close_gtt_row(row["id"], "cancelled")
    _transition(t, "CANCELLED", note, exit_reason=reason, closed_at=ts())
    return True


@_locked
def cancel_buys(versions: list[str], accounts: list[str] | None, why: str, reason: str = "strategy_off",
                placed_before: str | None = None) -> int:
    """A strategy turned off, deleted or unticked for a portfolio (decided 2026-09-30): cancel its pending buys
    there. Open trades are not touched: they keep their stops and trail to exit under their own rules."""
    q = f"SELECT * FROM trades WHERE state = 'BUY_PENDING' AND strategy_version IN ({','.join('?' * len(versions))})"
    args: list[Any] = list(versions)
    if placed_before:
        q += " AND placed_on < ?"
        args.append(placed_before)
    if accounts is not None:
        if not accounts:
            return 0
        q += f" AND account_id IN ({','.join('?' * len(accounts))})"
        args += accounts
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(q, args)]
    n = sum(1 for t in rows if _cancel_pending_buy(t, f"Buy GTT cancelled: {why}", reason))
    if rows:
        publish("trades")
    return n


@_locked
@_locked
def cancel_symbol_buys(symbols: list[str], why: str, reason: str) -> int:
    """Cancel every untriggered pending buy in these stocks (e.g. they left the NSE500). Open trades untouched."""
    if not symbols:
        return 0
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            f"SELECT * FROM trades WHERE state = 'BUY_PENDING' AND symbol IN ({','.join('?' * len(symbols))})",
            list(symbols))]
    n = sum(1 for t in rows if _cancel_pending_buy(t, f"Buy GTT cancelled: {why}", reason))
    if n:
        publish("trades")
    return n


def cancel_dropped_buys() -> int:
    """A refresh made a pending buy's signal untradeable (e.g. the stock left the watchlist): cancel the buy GTT
    and free the slot (decided 2026-09-24). A buy that has triggered or bought anything is left alone."""
    with connect() as conn:
        rows = conn.execute("""SELECT t.*, s.block_reasons FROM trades t JOIN signals s ON s.id = t.signal_id
                               WHERE t.state = 'BUY_PENDING' AND s.eligible = 0""").fetchall()
    n = 0
    for r in rows:
        t = dict(r)
        why = "; ".join(json.loads(t.pop("block_reasons") or "[]")) or "no longer tradeable"
        if _cancel_pending_buy(t, f"Signal no longer tradeable after a refresh ({why}): buy GTT cancelled",
                               "signal_dropped"):
            n += 1
    return n


@_locked
def place_entries(day: str, strategy: StrategyConfig | None = None) -> dict[str, Any]:
    """Rank the day's tradeable signals per portfolio and place buy GTTs for those that fit.

    Each portfolio pools the signals of every ON strategy that ticks it (or its challenger version) into one
    ranking; a stock is bought once (decided 2026-09-30). `strategy` given (a backtest): every portfolio trades
    that one strategy.

    Placed later than the signal's own evening (a refresh the next day), the breakout may already have
    happened: a signal is placed only while the live price is still below its trigger by Kite's minimum gap;
    otherwise it is recorded as missed and does not take a slot (decided 2026-09-24). No live price → not
    placed (fail closed)."""
    with connect() as conn:
        accounts = [r[0] for r in conn.execute("SELECT id FROM accounts WHERE COALESCE(archived, 0) = 0 ORDER BY capital")]
    if strategy is not None:
        plan = {a: [strategy] for a in accounts}
    else:
        from ..strategy import strategies
        plan = {a: [_strategy_named(v) for v in strategies.versions_for_account(a)] for a in accounts}
    return _place_entries_for(day, plan)


@_locked
def place_intraday(day: str, signal_ids: dict[str, list[int]], strategy: StrategyConfig | None = None) -> dict[str, Any]:
    """Intraday pullback signals found by the 5-minute scan: place their buy GTTs now, in each portfolio that
    trades the version, ranked with the portfolio's free slots. The live price must still be below the trigger
    by Kite's minimum gap (as for a late refresh); otherwise the signal is recorded as missed."""
    with connect() as conn:
        accounts = [r[0] for r in conn.execute("SELECT id FROM accounts WHERE COALESCE(archived, 0) = 0 ORDER BY capital")]
    if strategy is not None:           # a backtest: every portfolio trades this one strategy
        plan = {a: [strategy] for a in accounts}
    else:
        from ..strategy import strategies
        plan = {a: [_strategy_named(v) for v in strategies.versions_for_account(a) if v in signal_ids] for a in accounts}
    ids = {i for v in signal_ids.values() for i in v}
    return _place_entries_for(day, plan, source="intraday", only_ids=ids, check_live=True)


def _place_entries_for(day: str, plan: dict[str, list[StrategyConfig]], source: str = "eod",
                       only_ids: set[int] | None = None, check_live: bool | None = None) -> dict[str, Any]:
    app = get_app_config()
    broker = get_broker()
    by_version = {s.version: s for ss in plan.values() for s in ss}
    cands_of: dict[str, list[Candidate]] = {}
    closes = {}
    with connect() as conn:
        for version in by_version:
            sigs = [s for s in conn.execute(
                """SELECT s.*, i.sector FROM signals s LEFT JOIN instruments i ON i.symbol = s.symbol AND i.exchange='NSE'
                   WHERE s.date = ? AND s.strategy_version = ? AND s.source = ? AND s.eligible = 1""",
                (day, version, source)).fetchall() if only_ids is None or s["id"] in only_ids]
            cands_of[version] = []
            for s in sigs:
                ind = json.loads(s["indicators"] or "{}")
                closes[s["symbol"]] = ind.get("close")
                cands_of[version].append(Candidate(
                    signal_id=s["id"], symbol=s["symbol"], sector=s["sector"], entry_trigger=s["entry_trigger"],
                    entry_limit=s["entry_limit"], stop=s["stop"], target=s["target"], rr=s["rr"], rr_band=s["rr_band"],
                    traded_value=(ind.get("traded_value_cr") or 0) * 1e7, version=version))
    all_cands = [c for cs in cands_of.values() for c in cs]
    summary: dict[str, Any] = {"date": day, "signals": len(all_cands), "placed": {}, "skipped": {}, "missed": {}}
    # Later than the signal's evening: check each trigger against the live price first.
    late = check_live if check_live is not None else day < now_ist().date().isoformat()
    missed: dict[int, str] = {}
    if late and all_cands:
        prices = _live_prices(sorted({c.symbol for c in all_cands}))
        for c in all_cands:
            ltp = (prices or {}).get(c.symbol)
            if ltp is None:
                missed[c.signal_id] = "No live price to check the trigger (log in to Kite); not placed"
            elif ltp >= c.entry_trigger:
                missed[c.signal_id] = f"Missed: price {ltp:g} already past the trigger {c.entry_trigger:g}"
            elif c.entry_trigger < ltp + broker._min_gap(ltp):
                missed[c.signal_id] = f"Price {ltp:g} within Kite's minimum gap of the trigger {c.entry_trigger:g}"
            else:
                closes[c.symbol] = ltp
    for acc, strategies_ in plan.items():
        cands = [c for s in strategies_ for c in cands_of.get(s.version, [])]   # strategy order breaks ties
        if not strategies_:
            continue
        with connect() as conn:  # a signal cancelled only because a refresh dropped it may be placed again
            done = {r[0] for r in conn.execute("SELECT signal_id FROM trades WHERE account_id = ? AND signal_id IS NOT "
                                               "NULL AND COALESCE(exit_reason, '') != 'signal_dropped'", (acc,))}
        state = account_state(acc)
        for c in cands:
            if c.signal_id in missed and c.signal_id not in done:
                with connect() as conn:
                    conn.execute(
                        "INSERT INTO candidates (signal_id, account_id, qty, rank, taken, skip_reason, created_at) "
                        "VALUES (?, ?, 0, NULL, 0, ?, ?) ON CONFLICT(signal_id, account_id) DO UPDATE SET "
                        "rank = NULL, taken = 0, skip_reason = excluded.skip_reason",
                        (c.signal_id, acc, missed[c.signal_id], ts()))
        summary["missed"][acc] = sum(1 for c in cands if c.signal_id in missed and c.signal_id not in done)
        picks = select([c for c in cands if c.signal_id not in done and c.signal_id not in missed], state, app["risk"])
        taken_by = {c.symbol: c.version for c in picks if c.taken}
        for c in picks:   # the same stock signalled by two strategies: bought once
            if not c.taken and taken_by.get(c.symbol) and taken_by[c.symbol] != c.version:
                c.skip_reason = f"Same stock taken from strategy version {taken_by[c.symbol]}: bought once"
        placed = 0
        for c in picks:
            if c.taken:
                _cancel_unlinked(acc, c.symbol, "BUY")  # a leftover from a crash must not become a duplicate
                try:
                    legs = [Leg("BUY", c.qty, c.entry_limit)]
                    gid = broker.place_gtt(acc, c.symbol, "single", [c.entry_trigger], float(closes[c.symbol]), legs)
                except BrokerReject as exc:
                    c.taken, c.skip_reason = False, f"Broker rejected: {exc}"
                else:
                    now = ts()
                    with connect() as conn:
                        cur = conn.execute(
                            """INSERT INTO trades (account_id, signal_id, symbol, strategy_version, state, qty, open_qty,
                                   sizing_case, entry_trigger, entry_limit, stop, initial_stop, target, placed_on,
                                   sector, rr, rr_band, charges, exits, created_at, updated_at)
                               VALUES (?, ?, ?, ?, 'BUY_PENDING', ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, '[]', ?, ?)""",
                            (acc, c.signal_id, c.symbol, c.version, c.qty,
                             _sizing_case(c.qty, by_version[c.version]),
                             c.entry_trigger, c.entry_limit, c.stop, c.stop, c.target, day, c.sector, c.rr, c.rr_band,
                             now, now))
                        trade_id = int(cur.lastrowid)
                    _record_gtt(trade_id, gid, "buy", "single", [c.entry_trigger], legs)
                    _event(trade_id, None, "BUY_PENDING",
                           f"Buy GTT placed: {c.qty} @ trigger {c.entry_trigger}, limit {c.entry_limit} (rank {c.rank})",
                           {"band": c.rr_band, "rr": c.rr, "stop": c.stop, "target": c.target})
                    placed += 1
            with connect() as conn:
                conn.execute(
                    "INSERT INTO candidates (signal_id, account_id, qty, rank, taken, skip_reason, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(signal_id, account_id) DO UPDATE SET qty = excluded.qty, "
                    "rank = excluded.rank, taken = excluded.taken, skip_reason = excluded.skip_reason",
                    (c.signal_id, acc, c.qty, c.rank, 1 if c.taken else 0, c.skip_reason, ts()))
        summary["placed"][acc] = placed
        summary["skipped"][acc] = sum(1 for c in picks if not c.taken)
    if all_cands:  # refreshes are frequent: log only when there was something to place
        n_missed = len(missed)
        audit.record("job", "place_entries", f"{day}: placed " + ", ".join(f"{a} {n}" for a, n in summary["placed"].items())
                     + (f"; {n_missed} signal(s) missed/unchecked vs the live price" if n_missed else ""), details=summary)
    publish("trades")
    return summary


# ---------- exits ----------

def _for_trade(t: dict[str, Any], strategy: StrategyConfig) -> StrategyConfig:
    """Each trade keeps the rules of the version that opened it (a champion change does not alter open trades)."""
    v = t.get("strategy_version")
    return strategy if not v or v == strategy.version else _strategy_named(v)


def _sizing_case(qty: int, strategy: StrategyConfig) -> str:
    """split: half at the target, then trail (Momentum v1). trail_only: too few shares to split. full: the whole
    position exits at the stop or the target, no trailing (intraday pullback)."""
    if strategy.params.get("kind") == "intraday_pullback":
        return "full"
    return "split" if qty >= int(strategy.params["exits"]["split_min_qty"]) else "trail_only"


def _exit_legs(t: dict[str, Any], strategy: StrategyConfig) -> tuple[str, list[float], list[Leg]]:
    strategy = _for_trade(t, strategy)
    ex = strategy.params["exits"]
    tick = _tick(t["symbol"])
    stop_limit = round_down(t["stop"] * (1 - float(ex["stop_leg_limit_pct"]) / 100), tick)
    qty = int(t["open_qty"])
    if t["sizing_case"] == "full" and t["state"] != "TRAILING" and not t["target_hit_on"]:
        return "oco", [t["stop"], t["target"]], [Leg("SELL", qty, stop_limit), Leg("SELL", qty, t["target"])]
    if t["state"] != "TRAILING" and not t["target_hit_on"] and qty >= int(ex["split_min_qty"]) \
            and t["sizing_case"] == "split":
        return "oco", [t["stop"], t["target"]], [Leg("SELL", qty, stop_limit), Leg("SELL", qty // 2, t["target"])]
    return "single", [t["stop"]], [Leg("SELL", qty, stop_limit)]


def _tick_up(x: float, tick: float) -> float:
    return round(math.ceil(round(x / tick, 6)) * tick, 2)


def place_exit_at_open(t: dict[str, Any], last_price: float, strategy: StrategyConfig) -> bool:
    """Gap below the stop (decided 2026-09-23): sell the whole position at the next open.

    GTT-only: an OCO bracket as tight as Kite allows around the close (trigger >= 0.25% either side),
    both legs selling the full quantity with the stop-leg limit (trigger x 0.98). Any move at the open
    fires one leg, and a sell limit below the market fills at the market price."""
    broker = get_broker()
    tick = _tick(t["symbol"])
    gap = broker._min_gap(last_price)
    lo = round_down(last_price - gap - tick, tick)
    hi = _tick_up(last_price + gap + tick, tick)
    pct = float(_for_trade(t, strategy).params["exits"]["stop_leg_limit_pct"]) / 100
    qty = int(t["open_qty"])
    legs = [Leg("SELL", qty, round_down(lo * (1 - pct), tick)), Leg("SELL", qty, round_down(hi * (1 - pct), tick))]
    try:
        gid = broker.place_gtt(t["account_id"], t["symbol"], "oco", [lo, hi], last_price, legs)
    except BrokerReject as exc:
        _alert_once(f"gap-exit-{t['id']}-{now_ist().date()}", "exit_attach",
                    f"{t['account_id']} {t['symbol']}: could not place the exit-at-open GTT ({exc}). "
                    "The position is unprotected; sell it manually.", "critical")
        return False
    _record_gtt(t["id"], gid, "exit", "oco", [lo, hi], legs)
    day = now_ist().date().isoformat()
    _transition(t, "PROTECTED", f"Close {last_price} is below the stop {t['stop']}: exit GTT placed to sell all "
                f"{qty} at the next open (triggers {lo} / {hi})", {"stop": t["stop"]}, exit_at_open=day)
    audit.alert("gap_exit", f"{t['account_id']} {t['symbol']}: gapped below the stop {t['stop']} (close "
                f"{last_price}). Selling all {qty} at the next open.", "error")
    return True


def attach_exit(t: dict[str, Any], last_price: float, strategy: StrategyConfig, late_after_min: int,
                at_close: bool = False) -> bool:
    """Place the exit GTT for a FILLED position. Returns True if placed."""
    broker = get_broker()
    if t.get("paused") or t.get("awaiting_user"):
        return False
    existing = _unlinked(t["account_id"], t["symbol"], "SELL")
    if existing:
        # An exit GTT already sits at the broker for this stock but is not recorded (a crash between placing
        # and recording, or the user placed one): adopt it instead of placing a duplicate.
        return _adopt_exit(t, existing[-1])
    type_, triggers, legs = _exit_legs(t, strategy)
    if triggers[0] >= last_price:
        # A sell GTT with its trigger above the price would fire on a rise, not protect on a fall.
        if at_close:
            return place_exit_at_open(t, last_price, strategy)
        _alert_once(f"below-stop-{t['id']}-{now_ist().date()}", "exit_attach",
                    f"{t['account_id']} {t['symbol']}: price {last_price} is below the stop {triggers[0]}, so no "
                    "stop GTT can be placed. If it is still below at the close, the agent sells at the next open.",
                    "critical")
        return False
    try:
        gid = broker.place_gtt(t["account_id"], t["symbol"], type_, triggers, last_price, legs)
    except BrokerReject as exc:
        _alert_once(f"attach-{t['id']}", "exit_attach",
                    f"{t['account_id']} {t['symbol']}: could not attach the exit GTT ({exc}). Retrying every cycle; "
                    "the position is unprotected until it succeeds.", "critical")
        return False
    _record_gtt(t["id"], gid, "exit", type_, triggers, legs)
    to = "TRAILING" if t["target_hit_on"] else "PROTECTED"
    desc = (f"OCO: stop {triggers[0]} on {legs[0].qty}, target {triggers[1]} on {legs[1].qty}" if type_ == "oco"
            else f"stop {triggers[0]} on {legs[0].qty}")
    _transition(t, to, f"Exit GTT attached ({desc})", exit_at_open=None)
    with connect() as conn:
        first_exit = conn.execute("SELECT COUNT(*) FROM gtts WHERE trade_id = ? AND purpose = 'exit'",
                                  (t["id"],)).fetchone()[0] == 1
    if t.get("entry_at") and first_exit:
        waited = (now_ist() - datetime.fromisoformat(t["entry_at"])).total_seconds() / 60
        if waited > late_after_min:
            audit.alert("exit_attach_late", f"{t['account_id']} {t['symbol']}: exit attached {waited:.0f} min after "
                        f"the fill (limit {late_after_min} min)", "warning")
    return True


def _override(t: dict[str, Any], change: str, before: Any, after: Any, message: str) -> None:
    """Log a change the user made at the broker that the agent adopted (spec section 8: every adopted change is
    logged, so the journal can compare user decisions with what the system would have done)."""
    with connect() as conn:
        conn.execute("INSERT INTO overrides (trade_id, change_type, before, after, detected_at) VALUES (?, ?, ?, ?, ?)",
                     (t["id"], change, json.dumps(before, default=str), json.dumps(after, default=str), ts()))
    _event(t["id"], t["state"], t["state"], f"Manual change adopted: {message}", {"change": change})
    audit.record("user", f"override_{change}", f"{t['account_id']} {t['symbol']}: {message}")


def _adopt_exit(t: dict[str, Any], g) -> bool:
    legs = g.orders
    before = {"stop": t.get("stop"), "target": t.get("target")}
    _record_gtt(t["id"], g.id, "exit", g.type, list(g.trigger_values), legs)
    fields: dict[str, Any] = {}
    if abs(g.trigger_values[0] - float(t["stop"])) > 1e-9:
        fields["stop"] = g.trigger_values[0]
    if g.type == "oco" and len(g.trigger_values) > 1 and abs(g.trigger_values[1] - float(t["target"])) > 1e-9:
        fields["target"] = g.trigger_values[1]
    to = "TRAILING" if t["target_hit_on"] else "PROTECTED"
    _transition(t, to, f"Existing exit GTT {g.id} at the broker adopted (no duplicate placed)", **fields)
    if fields:
        _override(t, "exit_adopted", before, fields,
                  f"exit levels taken from the GTT at the broker: {fields}")
    return True


def _upsert_exit(t: dict[str, Any], o, reason: str) -> list[dict]:
    """One exit record per sell order, updated as the order fills (it can fill in parts)."""
    exits = [e for e in json.loads(t.get("exits") or "[]") if e.get("order_id") != o.id]
    if o.filled_qty:
        exits.append({"order_id": o.id, "qty": o.filled_qty, "price": o.filled_price, "charges": o.charges or 0,
                      "reason": reason, "at": o.filled_at})
    return exits


def _bought(t: dict[str, Any]) -> int:
    return int(t.get("bought_qty") or 0)


def _sold(exits: list[dict]) -> int:
    return sum(int(e["qty"]) for e in exits)


def _close_trade(t: dict[str, Any], exits: list[dict], reason: str) -> None:
    qty = _bought(t) or t["qty"]
    sold = sum(e["qty"] * e["price"] for e in exits)
    bought = qty * t["entry_price"]
    charges = float(t["charges"] or 0) + sum(e["charges"] for e in exits)
    net = sold - bought - charges
    risk = (t["entry_price"] - t["initial_stop"]) * qty
    r_mult = net / risk if risk > 0 else None
    _transition(t, "CLOSED", f"Closed ({reason}): net P&L ₹{net:,.2f}" + (f", {r_mult:.2f}R" if r_mult is not None else ""),
                {"net_pnl": net, "r": r_mult},
                open_qty=0, exits=json.dumps(exits), charges=round(charges, 2), net_pnl=round(net, 2),
                r_multiple=round(r_mult, 3) if r_mult is not None else None, exit_reason=reason, closed_at=ts())


def _resize_exit(t: dict[str, Any], last_price: float, strategy: StrategyConfig) -> None:
    """More shares were bought after the exit GTT went on: make it cover the whole holding."""
    row = _current_gtt(t["id"], "exit")
    if row is None:
        return
    broker = get_broker()
    g = broker.get_gtt(t["account_id"], int(row["gtt_id"]))
    if g is None or g.status != "active" or t.get("exit_at_open") or t.get("paused"):
        return
    type_, triggers, legs = _exit_legs(t, strategy)
    try:
        if type_ == g.type:
            broker.modify_gtt(t["account_id"], g.id, triggers, last_price, legs)
            with connect() as conn:
                conn.execute("UPDATE gtts SET legs = ?, updated_at = ? WHERE id = ?",
                             (json.dumps({"triggers": triggers, "orders": [l.__dict__ for l in legs]}), ts(),
                              row["id"]))
        else:
            gid = broker.place_gtt(t["account_id"], t["symbol"], type_, triggers, last_price, legs)
            broker.cancel_gtt(t["account_id"], g.id)
            _close_gtt_row(row["id"], "replaced")
            _record_gtt(t["id"], gid, "exit", type_, triggers, legs)
    except BrokerReject as exc:
        _alert_once(f"resize-{t['id']}-{t['open_qty']}", "exit_attach",
                    f"{t['account_id']} {t['symbol']}: the exit GTT could not be resized to {t['open_qty']} shares "
                    f"({exc}); only the earlier quantity is covered.", "critical")
        return
    _event(t["id"], t["state"], t["state"], f"Exit GTT resized to cover {t['open_qty']} shares")


# ---------- reconciliation (spec section 8: the broker wins) ----------

def _row_legs(row: dict[str, Any]) -> tuple[list[float], list[tuple[str, int, float]]]:
    d = json.loads(row["legs"])
    return [float(x) for x in d["triggers"]], [(o["side"], int(o["qty"]), float(o["price"])) for o in d["orders"]]


def _same(a: list[float], b: list[float]) -> bool:
    return len(a) == len(b) and all(abs(x - y) < 1e-6 for x, y in zip(a, b))


def _store_row_legs(row_id: int, triggers: list[float], legs: list[Leg], history: bool) -> None:
    with connect() as conn:
        cur = conn.execute("SELECT trigger_history FROM gtts WHERE id = ?", (row_id,)).fetchone()
        hist = json.loads(cur[0] or "[]")
        if history:
            hist.append({"at": ts(), "triggers": triggers, "by": "user"})
        conn.execute("UPDATE gtts SET legs = ?, trigger_history = ?, updated_at = ? WHERE id = ?",
                     (json.dumps({"triggers": triggers, "orders": [l.__dict__ for l in legs]}), json.dumps(hist),
                      ts(), row_id))


def _reconcile_trade(t: dict[str, Any]) -> None:
    """Compare what the agent placed with what the broker has now and adopt the user's changes."""
    broker = get_broker()
    acc = t["account_id"]
    brow = _current_gtt(t["id"], "buy")
    if brow is not None:
        g = broker.get_gtt(acc, int(brow["gtt_id"]))
        if g is None or g.status == "cancelled":
            _close_gtt_row(brow["id"], "cancelled_by_user")
            if t["state"] == "BUY_PENDING" and not _bought(t):
                _override(t, "buy_cancelled", {"state": "BUY_PENDING"}, {"state": "CANCELLED"},
                          "buy GTT cancelled by the user; no re-entry on this signal")
                _transition(t, "CANCELLED", "Buy GTT cancelled by the user", exit_reason="manual_cancel",
                            closed_at=ts())
            return
        if g.status == "active":
            trig, legs = _row_legs(brow)
            now_legs = [(o.side, o.qty, o.price) for o in g.orders]
            if not _same(trig, g.trigger_values) or now_legs != legs:
                before = {"trigger": t["entry_trigger"], "limit": t["entry_limit"], "qty": t["qty"]}
                after = {"trigger": g.trigger_values[0], "limit": g.orders[0].price, "qty": g.orders[0].qty}
                _store_row_legs(brow["id"], list(g.trigger_values), g.orders, history=True)
                _set(t["id"], entry_trigger=after["trigger"], entry_limit=after["limit"], qty=after["qty"])
                t.update(entry_trigger=after["trigger"], entry_limit=after["limit"], qty=after["qty"])
                _override(t, "buy_modified", before, after, f"buy GTT changed to {after}")
    xrow = _current_gtt(t["id"], "exit")
    if xrow is None or t["state"] not in ("PROTECTED", "TRAILING"):
        return
    g = broker.get_gtt(acc, int(xrow["gtt_id"]))
    if g is None or g.status == "cancelled":
        _close_gtt_row(xrow["id"], "cancelled_by_user")
        _override(t, "exit_cancelled", {"stop": t["stop"]}, None,
                  "exit GTT cancelled by the user; waiting for confirmation before placing a new one")
        _transition(t, "FILLED", "Exit GTT cancelled by the user: position unprotected until you confirm a new "
                    "exit", awaiting_user="exit_cancelled", exit_at_open=None)
        audit.alert("exit_cancelled", f"{acc} {t['symbol']}: you cancelled the exit GTT. {t['open_qty']} shares have "
                    "no stop. Confirm on the Positions screen to place a new one.", "critical")
        return
    if g.status != "active":
        return
    trig, legs = _row_legs(xrow)
    now_legs = [(o.side, o.qty, o.price) for o in g.orders]
    if _same(trig, g.trigger_values) and now_legs == legs:
        return
    _store_row_legs(xrow["id"], list(g.trigger_values), g.orders, history=True)
    fields: dict[str, Any] = {}
    if t.get("exit_at_open"):
        fields["exit_at_open"] = None   # the user reworked the exit-at-open GTT: their levels stand
    if abs(g.trigger_values[0] - trig[0]) > 1e-6:
        fields["stop"] = g.trigger_values[0]
        _override(t, "stop_moved", t["stop"], g.trigger_values[0],
                  f"stop moved {t['stop']} → {g.trigger_values[0]}; trailing continues from it, only tightening")
    if g.type == "oco" and len(trig) > 1 and abs(g.trigger_values[1] - trig[1]) > 1e-6:
        fields["target"] = g.trigger_values[1]
        _override(t, "target_moved", t["target"], g.trigger_values[1],
                  f"target moved {t['target']} → {g.trigger_values[1]}")
    if [l[1] for l in legs] != [o.qty for o in g.orders]:
        _override(t, "exit_qty_changed", [l[1] for l in legs], [o.qty for o in g.orders],
                  f"exit leg quantities changed to {[o.qty for o in g.orders]}")
    if fields:
        _set(t["id"], **fields)
        t.update(fields)


def _manual_seen() -> set[int]:
    return set(get_state("manual_orders_seen", []))


def _reconcile_manual_orders(account_id: str, quotes: dict[str, Quote], strategy: StrategyConfig) -> None:
    """Orders the user placed by hand (no GTT behind them): a sell closes or shrinks the trade and cancels leftover
    exit GTTs; a buy outside the system is recorded as an unmanaged holding."""
    broker = get_broker()
    seen = _manual_seen()
    fresh = [o for o in broker.orders(account_id) if o.gtt_id is None and o.filled_qty > 0 and o.id not in seen]
    if not fresh:
        return
    for o in fresh:
        if o.side == "SELL":
            left = o.filled_qty
            for t in _open_trades(HOLDING_STATES):
                if left <= 0:
                    break
                if t["account_id"] != account_id or t["symbol"] != o.symbol or int(t["open_qty"] or 0) <= 0:
                    continue
                n = min(left, int(t["open_qty"]))
                left -= n
                exits = [e for e in json.loads(t.get("exits") or "[]") if e.get("order_id") != o.id]
                exits.append({"order_id": o.id, "qty": n, "price": o.filled_price,
                              "charges": round((o.charges or 0) * n / o.filled_qty, 2), "reason": "manual_sell",
                              "at": o.filled_at})
                remaining = _bought(t) - _sold(exits)
                _set(t["id"], exits=json.dumps(exits), open_qty=remaining)
                t.update(exits=json.dumps(exits), open_qty=remaining)
                _override(t, "manual_sell", {"open_qty": remaining + n}, {"sold": n, "price": o.filled_price},
                          f"sold {n} by hand @ {o.filled_price}")
                if remaining <= 0:
                    for purpose in ("exit", "buy"):
                        row = _current_gtt(t["id"], purpose)
                        if row:
                            g = broker.get_gtt(account_id, int(row["gtt_id"]))
                            if g and g.status == "active":
                                broker.cancel_gtt(account_id, g.id)
                            elif g and g.order_id:
                                broker.cancel_order(account_id, g.order_id, "Position sold by hand")
                            _close_gtt_row(row["id"], "cancelled")
                    _close_trade(t, exits, "manual_sell")
                else:
                    q = quotes.get(o.symbol)
                    _resize_exit(t, q.ltp if q else o.filled_price, strategy)
            if left > 0:
                with connect() as conn:
                    conn.execute("UPDATE unmanaged_holdings SET qty = MAX(0, qty - ?) WHERE account_id = ? AND "
                                 "symbol = ?", (left, account_id, o.symbol))
        else:
            with connect() as conn:
                row = conn.execute("SELECT qty, avg_price FROM unmanaged_holdings WHERE account_id = ? AND symbol = ?",
                                   (account_id, o.symbol)).fetchone()
                qty = (row["qty"] if row else 0) + o.filled_qty
                avg = (((row["qty"] * (row["avg_price"] or 0)) if row else 0) + o.filled_qty * o.filled_price) / qty
                conn.execute("INSERT INTO unmanaged_holdings (account_id, symbol, qty, avg_price, detected_at) VALUES "
                             "(?, ?, ?, ?, ?) ON CONFLICT(account_id, symbol) DO UPDATE SET qty = excluded.qty, "
                             "avg_price = excluded.avg_price", (account_id, o.symbol, qty, round(avg, 4), ts()))
            audit.alert("unmanaged_holding", f"{account_id} {o.symbol}: {o.filled_qty} bought outside the system. Left "
                        "unmanaged (no stop from the agent); it counts toward cash and the sector cap.", "warning")
        seen.add(o.id)
    set_state("manual_orders_seen", sorted(seen))


@_locked
def close_account(account_id: str, reason_text: str = "Portfolio reset") -> dict[str, Any]:
    """Portfolio reset (decided 2026-09-25): cancel every buy GTT and working order, sell every holding at market
    (live price in market hours, else the latest price) and close the account's trades with reason 'reset'. Stocks
    held outside the system are sold too. Raises BrokerReject if a sale can't go through (e.g. lower circuit);
    what was done so far stays done and the caller does not restart the account."""
    from . import paper_edits
    broker = get_broker()
    seen = _manual_seen()
    out = {"cancelled_buys": 0, "closed": 0, "outside_sold": 0, "proceeds": 0.0}

    def cancel(row) -> None:
        g = broker.get_gtt(account_id, int(row["gtt_id"]))
        if g and g.status == "active":
            broker.cancel_gtt(account_id, g.id)
        elif g and g.order_id:
            broker.cancel_order(account_id, g.order_id, reason_text)
        _close_gtt_row(row["id"], "cancelled")

    def sell(symbol: str, qty: int):
        o = broker.manual_order(account_id, symbol, "SELL", qty, paper_edits.last_price(symbol, "SELL"))
        seen.add(o.id)
        set_state("manual_orders_seen", sorted(seen))
        out["proceeds"] += o.filled_qty * o.filled_price - (o.charges or 0)
        return o

    def fresh(trade_id: int) -> dict[str, Any]:
        with connect() as conn:
            return dict(conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone())

    for t in [x for x in _open_trades() if x["account_id"] == account_id]:
        _sync_buy(t, {}, now_ist().isoformat(timespec="seconds"), _strategy())   # pick up any last partial fill
        for purpose in ("buy", "exit"):
            row = _current_gtt(t["id"], purpose)
            if row:
                cancel(row)
        t = fresh(t["id"])
        if t["state"] not in OPEN_STATES:
            continue
        held = _bought(t) - _sold(json.loads(t.get("exits") or "[]"))
        if held > 0:
            o = sell(t["symbol"], held)
            exits = json.loads(t.get("exits") or "[]") + [{"order_id": o.id, "qty": held, "price": o.filled_price,
                                                             "charges": o.charges or 0, "reason": "reset",
                                                             "at": o.filled_at}]
            _close_trade(t, exits, "reset")
            out["closed"] += 1
        elif t["state"] in OPEN_STATES:
            _transition(t, "CANCELLED", f"{reason_text}: buy cancelled", exit_reason="reset", closed_at=ts())
            out["cancelled_buys"] += 1
    # The broker is the truth: whatever it still holds now is held outside the system (bought by hand). Sell it.
    for p in broker.positions(account_id):
        sell(p.symbol, int(p.qty))
        out["outside_sold"] += 1
    with connect() as conn:
        conn.execute("UPDATE unmanaged_holdings SET qty = 0 WHERE account_id = ?", (account_id,))
    for g in broker.gtts(account_id, "active"):          # anything left (an orphan) goes too
        broker.cancel_gtt(account_id, g.id)
    for o in broker.orders(account_id):
        if o.status == "open":
            broker.cancel_order(account_id, o.id, reason_text)
    left = broker.positions(account_id)
    if left:
        raise BrokerReject(f"Still holding {', '.join(p.symbol for p in left)} after the reset sells")
    publish("trades")
    return out


@_locked
def reconcile_now() -> None:
    """Run only the reconciliation part of a cycle (used right after a paper edit so the screen updates at once)."""
    strategy = _strategy()
    quotes = {}
    with connect() as conn:
        accounts = [r[0] for r in conn.execute("SELECT id FROM accounts")]
    for acc in accounts:
        _reconcile_manual_orders(acc, quotes, strategy)
    for t in _open_trades():
        _reconcile_trade(t)
    publish("trades")


# ---------- user confirmations ----------

@_locked
def confirm_new_exit(trade_id: int) -> dict[str, Any]:
    """After the user cancelled an exit GTT: they confirm, the agent places a new one on the next cycle."""
    t = _trade(trade_id)
    if t["state"] != "FILLED" or not t.get("awaiting_user"):
        raise ValueError("This trade is not waiting for a confirmation")
    _set(trade_id, awaiting_user=None, filled_at_cycle="")
    _event(trade_id, t["state"], t["state"], "You confirmed: a new exit GTT is placed on the next cycle")
    audit.user_action("confirm_new_exit", f"{t['account_id']} {t['symbol']}: new exit GTT confirmed")
    return _trade(trade_id)


def pause_for_adjustment(symbols: list[str]) -> list[int]:
    """Split or bonus seen in the candle history: pause management of open trades in those stocks and alert."""
    paused = []
    for t in _open_trades():
        if t["symbol"] in symbols and not t.get("paused"):
            reason = "Price history adjusted (split or bonus?)"
            _set(t["id"], paused=reason)
            _event(t["id"], t["state"], t["state"], f"Management paused: {reason}. Check the levels, then resume.")
            audit.alert("trade_paused", f"{t['account_id']} {t['symbol']}: management paused ({reason}). Stops are "
                        "not trailed or re-placed until you resume it on the Positions screen.", "critical")
            paused.append(t["id"])
    return paused


@_locked
def resume_trade(trade_id: int) -> dict[str, Any]:
    t = _trade(trade_id)
    if not t.get("paused"):
        raise ValueError("This trade is not paused")
    _set(trade_id, paused=None)
    _event(trade_id, t["state"], t["state"], "Management resumed by you")
    audit.user_action("resume_trade", f"{t['account_id']} {t['symbol']}: management resumed")
    return _trade(trade_id)


# ---------- monitor cycle ----------

def _sync_buy(t: dict[str, Any], quotes: dict[str, Quote], cycle: str, strategy: StrategyConfig) -> None:
    """Follow the buy until its order is finished. A partly filled order is never treated as done: the
    shares bought so far are protected, and the rest keeps being monitored until it fills or the
    session ends."""
    broker = get_broker()
    row = _current_gtt(t["id"], "buy")
    if row is None:
        return
    acc = t["account_id"]
    g = broker.get_gtt(acc, int(row["gtt_id"]))
    if g is None:
        return
    if g.status == "cancelled":
        _close_gtt_row(row["id"], "cancelled")
        if t["state"] == "BUY_PENDING":
            _transition(t, "CANCELLED", "Buy GTT cancelled", exit_reason="buy_cancelled", closed_at=ts())
        return
    if g.status != "triggered":
        return
    o = broker.get_order(acc, g.order_id)
    had = _bought(t)
    if o.filled_qty > had:
        exits = json.loads(t.get("exits") or "[]")
        fields = dict(bought_qty=o.filled_qty, entry_price=o.filled_price, charges=o.charges,
                      open_qty=o.filled_qty - _sold(exits), entry_at=t.get("entry_at") or o.filled_at)
        part = "" if o.filled_qty == o.qty else f" (partly: {o.filled_qty} of {o.qty}; the rest keeps working)"
        if t["state"] == "BUY_PENDING":
            _transition(t, "FILLED", f"Bought {o.filled_qty} @ {o.filled_price} (charges ₹{o.charges:.2f}){part}",
                        {"order_id": o.id}, filled_at_cycle=cycle, **fields)
        else:
            _set(t["id"], **fields)
            t.update(fields)
            _event(t["id"], t["state"], t["state"], f"Bought {o.filled_qty - had} more: {o.filled_qty} of {o.qty}, "
                   f"average {o.filled_price}", {"order_id": o.id})
            q = quotes.get(t["symbol"])
            if q is not None:
                _resize_exit(t, q.ltp, strategy)
    if o.status == "complete":
        _close_gtt_row(row["id"], "triggered")
        if had and had < o.qty:
            _event(t["id"], t["state"], t["state"], f"Buy complete: {o.qty} of {o.qty}")
    elif o.status in ("cancelled", "rejected"):
        _close_gtt_row(row["id"], "triggered")
        if _bought(t) == 0:
            _transition(t, "CANCELLED", f"Buy order {o.status}: {o.reason}", exit_reason=f"buy_{o.status}",
                        closed_at=ts())
        else:
            _event(t["id"], t["state"], t["state"], f"Buy ended partly filled: {_bought(t)} of {o.qty} bought; the rest "
                   f"was {o.status} ({o.reason}). Managing {_bought(t)} shares.", {"order_id": o.id})
            audit.alert("partial_fill", f"{acc} {t['symbol']}: buy filled {_bought(t)} of {o.qty}; the rest was "
                        f"{o.status} at the session end.", "warning")


def _sync_exit(t: dict[str, Any], now: datetime, quotes: dict[str, Quote], attach_now: bool,
               strategy: StrategyConfig, late: int) -> None:
    broker = get_broker()
    acc, sym = t["account_id"], t["symbol"]
    row = _current_gtt(t["id"], "exit")
    if row is None:
        return
    g = broker.get_gtt(acc, int(row["gtt_id"]))
    if g is None or g.status == "cancelled":
        _alert_once(f"exit-cancelled-{row['id']}", "exit_cancelled",
                    f"{acc} {sym}: the exit GTT was cancelled outside the agent. Not re-placing it without "
                    "your confirmation.", "critical")
        return
    if g.status != "triggered":
        return
    o = broker.get_order(acc, g.order_id)
    is_target = g.type == "oco" and g.triggered_leg == 1 and not t.get("exit_at_open")
    reason = "gap_exit" if t.get("exit_at_open") else "target" if is_target else "stop"
    exits = _upsert_exit(t, o, reason)
    new_open = _bought(t) - _sold(exits)
    if new_open != int(t["open_qty"]):
        prev = int(t["open_qty"])
        _set(t["id"], exits=json.dumps(exits), open_qty=new_open)
        t.update(exits=json.dumps(exits), open_qty=new_open)
        if o.status == "open":
            _event(t["id"], t["state"], t["state"], f"{reason.replace('_', ' ').capitalize()} order partly filled: "
                   f"{o.filled_qty} of {o.qty} sold (avg {o.filled_price}); the rest keeps working",
                   {"order_id": o.id, "sold_now": prev - new_open})
    if o.status == "open":
        if o.filled_qty == 0:
            _alert_once(f"stop-open-{o.id}", "exit_unfilled",
                        f"{acc} {sym}: exit triggered at {g.trigger_values[g.triggered_leg]} but the limit "
                        f"{o.price} has not filled (gap or circuit). The order stays live today; if it expires "
                        "and the price is still below the stop at the close, the agent sells at the next open.",
                        "error")
        else:
            _alert_once(f"exit-partial-{o.id}", "partial_fill",
                        f"{acc} {sym}: exit order partly filled ({o.filled_qty} of {o.qty}). Monitoring the rest.",
                        "warning")
        return
    _close_gtt_row(row["id"], "triggered")
    remaining = int(t["open_qty"])
    if remaining <= 0:
        _close_trade(t, exits, reason)
        return
    if is_target and o.filled_qty > 0 and not t["target_hit_on"]:
        _set(t["id"], target_hit_on=now.date().isoformat())
        t["target_hit_on"] = now.date().isoformat()
    if o.status == "complete" and is_target:
        _event(t["id"], t["state"], t["state"], f"Target hit: sold {o.qty} @ {o.filled_price}; {remaining} left",
               {"order_id": o.id})
        q = quotes.get(sym)
        t["state"] = "TRAILING"
        if not attach_exit(t, q.ltp if q else o.filled_price, strategy, late, at_close=attach_now):
            _transition(t, "FILLED", "New stop not placed yet; retrying", filled_at_cycle="")
        return
    # Order ended (cancelled at the session end, rejected) or completed while shares remain: those shares
    # still need an exit, so they go back to FILLED and get a new one (or the exit-at-open rule at the close).
    what = f"{o.filled_qty} of {o.qty} sold, " if o.filled_qty else ""
    _transition(t, "FILLED", f"Exit order {o.status} ({what}{o.reason or 'done'}); {remaining} shares still held, "
                "placing a new exit", filled_at_cycle="", exit_at_open=None)
    if o.status != "complete":
        audit.alert("exit_unfilled", f"{acc} {sym}: exit order {o.status} with {remaining} shares still held; a new "
                    "exit is being placed.", "error")


def sync(now: datetime, quotes: dict[str, Quote], attach_now: bool = False) -> None:
    """Read the broker back and move every open trade along its state machine."""
    strategy = _strategy()
    late = int(get_app_config()["market"]["exit_attach_alert_min"])
    cycle = now.isoformat(timespec="seconds")
    with connect() as conn:
        accounts = [r[0] for r in conn.execute("SELECT id FROM accounts")]
    for acc in accounts:
        _reconcile_manual_orders(acc, quotes, strategy)
    for t in _open_trades():
        acc, sym = t["account_id"], t["symbol"]
        _reconcile_trade(t)
        if t["state"] in ("CLOSED", "CANCELLED"):
            continue
        _sync_buy(t, quotes, cycle, strategy)
        if t["state"] == "FILLED":
            if t.get("awaiting_user") or t.get("paused"):
                continue
            q = quotes.get(sym)
            if (attach_now or (t["filled_at_cycle"] or "") < cycle) and q is not None:
                attach_exit(t, q.ltp, strategy, late, at_close=attach_now)
            elif t.get("entry_at"):
                waited = (now - datetime.fromisoformat(t["entry_at"])).total_seconds() / 60
                if waited > late:
                    _alert_once(f"unprotected-{t['id']}", "exit_attach",
                                f"{acc} {sym}: no stop {waited:.0f} min after the fill.", "critical")
        elif t["state"] in ("PROTECTED", "TRAILING"):
            _sync_exit(t, now, quotes, attach_now, strategy, late)


@_locked
def on_cycle(now: datetime | None = None) -> str:
    """Order monitor (every minute in market hours)."""
    from ..scheduler import Skip
    now = (now or now_ist()).astimezone(IST)
    cal = MarketCalendar(get_app_config())
    if not cal.is_market_open(now):
        raise Skip("Market closed")
    broker = get_broker()
    # A triggered GTT's limit order is a day order: Kite cancels it at the session's end. If the agent was off
    # then (the EOD run missed), expire it here so it cannot keep filling in a later session.
    broker.end_session(cal.previous_trading_day(now.date()).isoformat())
    with connect() as conn:  # stocks held outside the system are priced too (their P&L and the account's equity)
        outside = {r[0] for r in conn.execute("SELECT symbol FROM unmanaged_holdings WHERE qty > 0")}
    # every open trade's stock (not only live GTTs/orders): a buy whose order just expired must still be synced
    symbols = sorted(set(broker.watched_symbols()) | {t["symbol"] for t in _open_trades()} | outside)
    if not symbols:
        return "Nothing to monitor"
    try:
        quotes = feed.quote_source(symbols)
    except Exception as exc:  # noqa: BLE001 - fail closed: no prices, no simulated fills this cycle
        _alert_once(f"quotes-{now.date()}", "order_monitor",
                    f"No live prices ({exc}). Paper GTTs are not being checked until quotes return.", "error")
        raise Skip(f"No quotes: {exc}") from exc
    set_state("last_quotes", {"at": now.isoformat(timespec="seconds"),
                              "ltp": {s: q.ltp for s, q in quotes.items()}})
    events = broker.process(quotes, now)
    sync(now, quotes)
    problems = check_invariants()
    set_state("monitor_last", {"at": now.isoformat(timespec="seconds"), "symbols": len(symbols),
                               "events": len(events), "invariant_problems": problems})
    try:
        from . import fastloop
        fastloop.refresh_levels()          # the prices the tick-driven monitor watches for
    except Exception:  # noqa: BLE001 - only speed depends on it
        log.exception("could not refresh the fast monitor's levels")
    if events:
        publish("trades")
    return f"{len(symbols)} symbols, {len(events)} broker events" + (f", {len(problems)} invariant alerts"
                                                                      if problems else "")


def check_invariants() -> list[str]:
    """Spec section 8 invariants: one active stop per protected position, no duplicate or orphan GTTs."""
    broker = get_broker()
    problems: list[str] = []
    known: set[tuple[str, int]] = set()
    trades = {t["id"]: t for t in _open_trades()}
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM gtts WHERE status = 'active'")]
    per_trade: dict[tuple[int, str], int] = {}
    for r in rows:
        t = trades.get(r["trade_id"])
        if t:
            known.add((t["account_id"], int(r["gtt_id"])))
            per_trade[(r["trade_id"], r["purpose"])] = per_trade.get((r["trade_id"], r["purpose"]), 0) + 1
    for (trade_id, purpose), n in per_trade.items():
        if n > 1:
            t = trades[trade_id]
            problems.append(f"duplicate-{trade_id}-{purpose}")
            _alert_once(f"dup-{trade_id}-{purpose}", "invariant",
                        f"{t['account_id']} {t['symbol']}: {n} active {purpose} GTTs for one trade.", "critical")
    for t in trades.values():
        if t["state"] in ("PROTECTED", "TRAILING") and not per_trade.get((t["id"], "exit")):
            problems.append(f"no-stop-{t['id']}")
            _alert_once(f"nostop-{t['id']}-{t['updated_at']}", "invariant",
                        f"{t['account_id']} {t['symbol']}: marked protected but no active exit GTT is recorded.",
                        "critical")
    with connect() as conn:
        accounts = {r[0]: bool(r[1]) for r in conn.execute("SELECT id, manual_edits FROM accounts")}
        outside = {(r[0], r[1]): int(r[2]) for r in conn.execute(
            "SELECT account_id, symbol, qty FROM unmanaged_holdings WHERE qty > 0")}
    held: dict[tuple[str, str], int] = dict(outside)
    for t in trades.values():
        if t["state"] in HOLDING_STATES:
            held[(t["account_id"], t["symbol"])] = held.get((t["account_id"], t["symbol"]), 0) + int(t["open_qty"] or 0)
    for acc, manual in accounts.items():
        # Quantities: what the broker holds must equal the open trades plus holdings bought outside the system.
        broker_qty = {p.symbol: p.qty for p in broker.positions(acc)}
        for sym in set(broker_qty) | {s for (a, s) in held if a == acc}:
            if broker_qty.get(sym, 0) != held.get((acc, sym), 0):
                problems.append(f"qty-{acc}-{sym}")
                _alert_once(f"qty-{acc}-{sym}-{broker_qty.get(sym, 0)}-{held.get((acc, sym), 0)}", "invariant",
                            f"{acc} {sym}: broker holds {broker_qty.get(sym, 0)} shares but the agent expects "
                            f"{held.get((acc, sym), 0)}.", "critical")
        for g in broker.gtts(acc, "active"):
            if (acc, g.id) in known:
                continue
            is_sell = bool(g.orders) and g.orders[0].side == "SELL"
            if manual and not (is_sell and broker_qty.get(g.symbol, 0) == 0):
                # On the manual-edit account a GTT the user placed is theirs: flag it, don't touch it.
                _alert_once(f"userg-{acc}-{g.id}", "invariant",
                            f"{acc} {g.symbol}: GTT {g.id} was not placed by the agent; leaving it alone.", "warning")
                continue
            problems.append(f"orphan-{acc}-{g.id}")
            try:
                broker.cancel_gtt(acc, g.id)
                msg = "cancelled"
            except BrokerReject as exc:
                msg = f"could not cancel ({exc})"
            _alert_once(f"orphan-{acc}-{g.id}", "invariant",
                        f"{acc} {g.symbol}: GTT {g.id} is not linked to any open trade; {msg}.", "error")
    return problems


# ---------- EOD maintenance ----------

def _last_candle(symbol: str, day: str) -> dict[str, Any] | None:
    with connect() as conn:
        r = conn.execute("SELECT * FROM candles_daily WHERE symbol = ? AND date = ?", (symbol, day)).fetchone()
    return dict(r) if r else None


def expire_buys(day: str, strategy: StrategyConfig) -> int:
    """Cancel buy GTTs not triggered within `cancel_after_sessions` sessions."""
    cal = MarketCalendar(get_app_config())
    broker = get_broker()
    n = 0
    for t in _open_trades(("BUY_PENDING",)):
        n_sessions = int(_for_trade(t, strategy).params["entry"]["cancel_after_sessions"])
        if cal.trading_days_between(date.fromisoformat(t["placed_on"]), date.fromisoformat(day)) < n_sessions:
            continue
        row = _current_gtt(t["id"], "buy")
        if row:
            try:
                broker.cancel_gtt(t["account_id"], int(row["gtt_id"]))
            except BrokerReject:
                continue  # already triggered: the order's own fate decides
            _close_gtt_row(row["id"], "cancelled")
        _transition(t, "CANCELLED", f"Not filled within {n_sessions} sessions", exit_reason="expired", closed_at=ts())
        n += 1
    return n


def trail_stops(day: str, strategy: StrategyConfig) -> list[str]:
    """EOD trailing (spec section 6). Stops only move up; updates Kite would reject are skipped and logged."""
    broker = get_broker()
    log: list[str] = []
    for t in _open_trades(("PROTECTED", "TRAILING")):
        ex = _for_trade(t, strategy).params["exits"]
        buf = float(ex["trail_buffer_pct"]) / 100
        if t.get("exit_at_open") or t.get("paused"):
            continue  # being sold at the next open, or paused (split/bonus): nothing to trail
        c = _last_candle(t["symbol"], day)
        if not c:
            continue
        row = _current_gtt(t["id"], "exit")
        if row is None:
            continue
        new_stop = t["stop"]
        became_trailing = False
        if t["state"] == "PROTECTED":
            if t["sizing_case"] == "trail_only" and c["close"] >= t["target"]:
                became_trailing = True
                new_stop = max(t["stop"], t["entry_price"])
            else:
                continue  # OCO before target: the stop does not trail
        elif t["target_hit_on"] == day:
            new_stop = max(t["stop"], t["entry_price"])
        else:
            new_stop = max(t["stop"], t["entry_price"], c["low"] * (1 - buf))
        new_stop = round_down(new_stop, _tick(t["symbol"]))
        if became_trailing:
            _transition(t, "TRAILING", f"Close {c['close']} reached target {t['target']}: trailing", target_hit_on=day)
        if new_stop <= t["stop"]:
            continue
        if new_stop >= c["close"]:
            msg = f"{t['account_id']} {t['symbol']}: trail to {new_stop} skipped (not below the close {c['close']})"
            audit.record("system", "trail_skipped", msg, "warning")
            log.append(msg)
            continue
        legs = [Leg("SELL", int(t["open_qty"]), round_down(new_stop * (1 - float(ex["stop_leg_limit_pct"]) / 100),
                                                            _tick(t["symbol"])))]
        try:
            broker.modify_gtt(t["account_id"], int(row["gtt_id"]), [new_stop], float(c["close"]), legs)
        except BrokerReject as exc:
            msg = f"{t['account_id']} {t['symbol']}: trail to {new_stop} skipped ({exc})"
            audit.record("system", "trail_skipped", msg, "warning")
            log.append(msg)
            continue
        with connect() as conn:
            hist = json.loads(conn.execute("SELECT trigger_history FROM gtts WHERE id = ?", (row["id"],)).fetchone()[0]
                              or "[]")
            hist.append({"at": ts(), "triggers": [new_stop]})
            conn.execute("UPDATE gtts SET trigger_history = ?, legs = ?, updated_at = ? WHERE id = ?",
                         (json.dumps(hist), json.dumps({"triggers": [new_stop], "orders": [l.__dict__ for l in legs]}),
                          ts(), row["id"]))
        old = t["stop"]
        _set(t["id"], stop=new_stop, last_trail_on=day)
        _event(t["id"], t["state"], t["state"], f"Stop trailed {old} → {new_stop}", {"low": c["low"]})
        log.append(f"{t['account_id']} {t['symbol']}: {old} → {new_stop}")
    return log


def equity_snapshot(day: str) -> dict[str, Any]:
    """Mark to the day's close, update peak equity, halt accounts past the drawdown limit."""
    broker = get_broker()
    halt_pct = float(get_app_config()["risk"]["drawdown_halt_pct"])
    out = {}
    with connect() as conn:
        accounts = [dict(r) for r in conn.execute("SELECT * FROM accounts WHERE COALESCE(archived, 0) = 0")]
    for a in accounts:
        cash = broker.cash(a["id"])
        holdings = 0.0
        for p in broker.positions(a["id"]):
            c = _last_candle(p.symbol, day)
            holdings += p.qty * (c["close"] if c else p.avg_price)
        equity = cash + holdings
        peak = max(float(a["peak_equity"]), equity)
        with connect() as conn:
            conn.execute("INSERT INTO equity_daily (account_id, date, cash, holdings, equity, peak_equity) "
                         "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(account_id, date) DO UPDATE SET cash = excluded.cash, "
                         "holdings = excluded.holdings, equity = excluded.equity, peak_equity = excluded.peak_equity",
                         (a["id"], day, round(cash, 2), round(holdings, 2), round(equity, 2), round(peak, 2)))
            conn.execute("UPDATE accounts SET peak_equity = ? WHERE id = ?", (round(peak, 2), a["id"]))
        dd = (peak - equity) / peak * 100 if peak else 0
        if dd >= halt_pct and not a["halted"]:
            controls.halt_account(a["id"], f"Drawdown {dd:.1f}% from peak ₹{peak:,.0f} (limit {halt_pct}%)")
        out[a["id"]] = {"equity": round(equity, 2), "drawdown_pct": round(dd, 2)}
    return out


@_locked
def eod_maintenance(day: str) -> dict[str, Any]:
    """Runs in the EOD job after candles are in and before today's scan."""
    strategy = _strategy()
    broker = get_broker()
    expired_orders = broker.end_session(day)
    now = now_ist()
    closes = {}
    for t in _open_trades():
        c = _last_candle(t["symbol"], day)
        if c:
            closes[t["symbol"]] = Quote(ltp=c["close"], high=c["high"], low=c["low"])
    sync(now, closes, attach_now=True)  # unfilled buys -> CANCELLED; unprotected positions get a stop now
    sync(now, closes, attach_now=True)  # second pass: exits cancelled at session end fall back to FILLED above
    expired = expire_buys(day, strategy)
    trails = trail_stops(day, strategy)
    equity = equity_snapshot(day)
    return {"orders_expired": expired_orders, "buys_expired": expired, "trails": trails, "equity": equity}


@_locked
def cancel_all_pending_buys() -> int:
    """Kill switch: cancel every pending buy GTT and every working buy order (including the unfilled rest of a
    partly filled buy) in all accounts. Shares already bought stay and keep their stop."""
    broker = get_broker()
    n = 0
    for t in _open_trades():
        row = _current_gtt(t["id"], "buy")
        if row is None:
            continue
        g = broker.get_gtt(t["account_id"], int(row["gtt_id"]))
        if g and g.status == "active":
            broker.cancel_gtt(t["account_id"], g.id)
        elif g and g.order_id:
            broker.cancel_order(t["account_id"], g.order_id, "Kill switch")
        _close_gtt_row(row["id"], "cancelled")
        if t["state"] == "BUY_PENDING" and not _bought(t):
            _transition(t, "CANCELLED", "Cancelled by kill switch", exit_reason="kill_switch", closed_at=ts())
        else:
            _event(t["id"], t["state"], t["state"], f"Kill switch: the rest of the buy was cancelled; keeping "
                   f"{_bought(t)} shares")
        n += 1
    return n


controls.cancel_pending_buys = cancel_all_pending_buys
