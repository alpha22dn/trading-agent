"""Intraday pullback runtime (strategy/intraday_pullback.py holds the rules).

- `scan()`: every 5 minutes in market hours (scheduler job intraday_scan). For each ON intraday-pullback strategy
  with a portfolio: fetch today's 5-minute candles from Kite for its universe (the watchlist plus the N most
  liquid NSE500 stocks of the last session), check every candle that closed since the last scan, store the first
  valid pullback of each stock as that day's signal, and place buy GTTs for the tradeable ones right away
  (orders/manager.place_intraday: one ranking with the portfolio's free slots, price checked against the trigger).
- Gates: results blackout (as Momentum v1) and liquidity for every stock; the watchlist's fundamentals and
  valuation checks do not apply (decided 2026-09-30). The market filter applies when it is switched on.
- `cutoff()`: 15:15 (job intraday_cutoff, caught up after a restart): cancel buy GTTs that have not triggered.
- The previous session's 5-minute candles (to warm up the 20-EMA) come from the 1-minute store when it has them,
  else from Kite; they are cached for the day.
"""
from __future__ import annotations

import logging
import threading
from datetime import date, datetime, time, timedelta
from typing import Any, Callable

from .. import audit
from ..clock import IST, now_ist
from ..db import connect
from ..market_calendar import MarketCalendar
from ..settings import StrategyConfig, get_app_config
from . import intraday_pullback as rules

log = logging.getLogger("agent")
_lock = threading.Lock()
_prev_cache: dict[tuple[str, int], list[dict[str, Any]]] = {}      # (day, token) -> previous session 5-min bars
_seen: dict[tuple[str, str, str], datetime] = {}                     # (day, version, symbol) -> last candle checked
SESSION_START = time(9, 15)


def strategies_on() -> list[StrategyConfig]:
    """ON intraday-pullback strategies that tick at least one portfolio."""
    from . import strategies, versions
    out = []
    for r in strategies.active_rows():
        if r["kind"] == rules.KIND and r["enabled"] and strategies.portfolios_of(r["id"]):
            out.append(versions.load(r["version"]))
    return out


def all_versions() -> list[str]:
    """Every version of every intraday-pullback strategy (deleted ones too: their buys still need the cutoff)."""
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT c.version FROM config_versions c JOIN strategies s ON "
                                           "s.id = c.strategy_id WHERE s.kind = ?", (rules.KIND,))]


def universe(params: dict[str, Any], day: date) -> list[dict[str, Any]]:
    from ..data import watchlist
    u = params.get("universe", {})
    syms: dict[str, str] = {}
    if u.get("watchlist", True):
        for s in watchlist.active():
            syms[s] = "watchlist"
    n = int(u.get("top_liquid", 0) or 0)
    with connect() as conn:
        last = conn.execute("SELECT MAX(date) FROM candles_daily WHERE date < ?", (day.isoformat(),)).fetchone()[0]
        if n and last:
            for (s,) in conn.execute("""SELECT c.symbol FROM candles_daily c JOIN instruments i ON i.symbol = c.symbol
                                        AND i.exchange = 'NSE' AND i.is_nse500 = 1 WHERE c.date = ?
                                        ORDER BY c.close * c.volume DESC LIMIT ?""", (last, n)):
                syms.setdefault(s, "most liquid")
        out = []
        for s, src in sorted(syms.items()):
            r = conn.execute("SELECT instrument_token, tick_size, sector FROM instruments WHERE symbol = ? AND "
                             "exchange = 'NSE'", (s,)).fetchone()
            c = conn.execute("SELECT close FROM candles_daily WHERE symbol = ? AND date < ? ORDER BY date DESC LIMIT 1",
                             (s, day.isoformat())).fetchone()
            tv = conn.execute("SELECT traded_value_20d FROM indicators_daily WHERE symbol = ? AND date < ? "
                              "ORDER BY date DESC LIMIT 1", (s, day.isoformat())).fetchone()
            if r and c:
                out.append({"symbol": s, "token": int(r[0]), "tick": float(r[1] or 0.05), "sector": r[2],
                            "prev_close": float(c[0]), "traded_value": float(tv[0]) if tv and tv[0] else None,
                            "source": src})
    return out


def _to_bars(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for b in raw:
        d = b["date"]
        d = d.astimezone(IST) if d.tzinfo else d.replace(tzinfo=IST)
        out.append({"date": d, "open": float(b["open"]), "high": float(b["high"]), "low": float(b["low"]),
                    "close": float(b["close"]), "volume": float(b.get("volume") or 0)})
    return out


def kite_fetch(token: int, start: datetime, end: datetime) -> list[dict[str, Any]]:
    from ..broker import kite_session
    from ..data.candles import shared_limiter
    shared_limiter(float(get_app_config()["kite"]["historical_requests_per_sec"])).wait()
    return _to_bars(kite_session.get_kite().historical_data(token, start, end, "5minute"))


def _prev_bars(token: int, day: date, fetch: Callable) -> list[dict[str, Any]]:
    key = (day.isoformat(), token)
    if key not in _prev_cache:
        prev = MarketCalendar(get_app_config()).previous_trading_day(day)
        start = datetime.combine(prev, SESSION_START, IST)
        end = datetime.combine(prev, time(15, 30), IST)
        bars: list[dict[str, Any]] = []
        try:
            from ..data import minute
            if minute.db_path().exists():
                bars = minute.resample(minute.load(token, start, end), rules.CANDLE_MIN)
        except Exception:  # noqa: BLE001 - Kite below
            bars = []
        if not bars:
            bars = fetch(token, start, end)
        if len(_prev_cache) > 5000:
            _prev_cache.clear()
        _prev_cache[key] = bars
    return _prev_cache[key]


def _gates(stock: dict[str, Any], day: date, ctx: dict[str, Any]) -> tuple[dict[str, bool | None], list[str]]:
    from .scanner import results_check
    app = get_app_config()
    u = app["universe"]
    gates: dict[str, bool | None] = {"nse500": None, "watchlist": stock["source"] == "watchlist" or None}
    reasons: list[str] = []
    min_tv = float(u["min_traded_value_cr"]) * 1e7
    gates["liquidity"] = stock["traded_value"] is not None and stock["traded_value"] >= min_tv
    if not gates["liquidity"]:
        reasons.append(f"Liquidity: ₹{(stock['traded_value'] or 0) / 1e7:.1f} cr/day < ₹{u['min_traded_value_cr']} cr")
    if not ctx["events_ok"]:
        gates["results"] = False
        reasons.append("Results calendar not fetched since the last session's close")
    else:
        blocked, why, _ = results_check(day, ctx["meetings"].get(stock["symbol"], []), ctx["known"], ctx["cal"],
                                        int(u["results_blackout_days"]), int(u["last_year_window_days"]))
        gates["results"] = not blocked
        if blocked:
            reasons.append(why)
    if ctx["market"] is not None:
        gates["market"] = ctx["market"]
        if not ctx["market"]:
            reasons.append("Market filter: entries blocked")
    return gates, reasons


def scan(now: datetime | None = None, fetch: Callable | None = None) -> dict[str, Any]:
    """One 5-minute pass. Returns what it found and placed."""
    from ..data import events as events_data
    from ..orders import manager
    from . import market_filter, scanner
    now = (now or now_ist()).astimezone(IST)
    app = get_app_config()
    cal = MarketCalendar(app)
    day = now.date()
    strats = strategies_on()
    out: dict[str, Any] = {"signals": [], "placed": {}, "checked": 0, "strategies": [s.version for s in strats]}
    if not strats or not cal.is_market_open(now):
        return out
    fetch = fetch or kite_fetch
    with _lock:
        with connect() as conn:
            ctx = {"cal": cal, "known": scanner.sessions(conn, app), "meetings": events_data.results_dates(conn),
                   "events_ok": events_data.fresh_for(cal.previous_trading_day(day)),
                   "market": (market_filter.evaluate().get("allow_entries") if market_filter.enabled() else None)}
            done = {(r[0], r[1]) for r in conn.execute("SELECT strategy_version, symbol FROM signals WHERE date = ?",
                                                       (day.isoformat(),))}
        bars_of: dict[str, list[dict[str, Any]]] = {}
        stocks: dict[str, dict[str, Any]] = {}
        for s in strats:
            for st in universe(s.params, day):
                stocks.setdefault(st["symbol"], st)
        start = datetime.combine(day, SESSION_START, IST)
        new_ids: dict[str, list[int]] = {}
        for s in strats:
            members = {st["symbol"] for st in universe(s.params, day)}
            for sym in sorted(members):
                if (s.version, sym) in done:
                    continue           # one trade per stock per day
                st = stocks[sym]
                if sym not in bars_of:
                    try:
                        raw = fetch(st["token"], start, now)
                    except Exception as exc:  # noqa: BLE001 - one stock never stops the scan
                        from ..broker import kite_session
                        if kite_session.is_token_error(exc):
                            kite_session.invalidate("Kite rejected the session token (intraday scan)")
                            raise
                        log.warning("intraday bars %s: %s", sym, exc)
                        bars_of[sym] = []
                        continue
                    bars_of[sym] = [b for b in raw if b["date"] + timedelta(minutes=rules.CANDLE_MIN) <= now
                                    and b["date"].date() == day]
                today = bars_of[sym]
                if not today:
                    continue
                last = _seen.get((day.isoformat(), s.version, sym))
                # first look this session (start-up, restart): only candles that closed in the last 10 minutes,
                # never a setup from hours ago
                fresh_from = now - timedelta(minutes=2 * rules.CANDLE_MIN + rules.CANDLE_MIN)
                prev = None
                for i, b in enumerate(today):
                    if last is not None and b["date"] <= last:
                        continue
                    if last is None and b["date"] < fresh_from:
                        continue
                    if prev is None:
                        prev = _prev_bars(st["token"], day, fetch)
                    out["checked"] += 1
                    setup, _why = rules.evaluate(prev, today, i, st["prev_close"], st["tick"], s.params)
                    if setup is None or not setup.rr_ok:
                        continue
                    gates, reasons = _gates(st, day, ctx)
                    eligible = all(v for v in gates.values() if v is not None)
                    sid = _store(day, sym, s, setup, gates, reasons, eligible, st)
                    out["signals"].append({"symbol": sym, "version": s.version, "eligible": eligible,
                                           "at": setup.candle_at.strftime("%H:%M"), "rr": setup.rr})
                    if eligible and sid:
                        new_ids.setdefault(s.version, []).append(sid)
                    break
                _seen[(day.isoformat(), s.version, sym)] = today[-1]["date"]
        if new_ids:
            out["placed"] = manager.place_intraday(day.isoformat(), new_ids)
        if out["signals"]:
            audit.record("job", "intraday_scan", f"{now:%H:%M}: {len(out['signals'])} pullback signal(s) "
                         f"({sum(1 for x in out['signals'] if x['eligible'])} tradeable): "
                         + ", ".join(f"{x['symbol']} {x['at']}" for x in out["signals"]), details=out)
    return out


def _store(day: date, sym: str, s: StrategyConfig, setup: rules.Setup, gates: dict, reasons: list[str],
           eligible: bool, st: dict[str, Any]) -> int | None:
    import json

    from ..clock import ts
    ind = setup.indicators() | {"traded_value_cr": round(st["traded_value"] / 1e7, 2) if st["traded_value"] else None,
                                "universe": st["source"]}
    with connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO signals (date, symbol, strategy_version, entry_trigger, entry_limit, stop, target,
                   swing_low, rr, rr_band, indicators, eligible, gates, block_reasons, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'intraday', ?)""",
            (day.isoformat(), sym, s.version, setup.entry_trigger, setup.entry_limit, setup.stop, setup.target, setup.rr,
             setup.rr_band, json.dumps(ind), 1 if eligible else 0, json.dumps(gates), json.dumps(reasons), ts()))
        return int(cur.lastrowid) if cur.rowcount else None


def _cut_time() -> time:
    return rules._hm(rules.TEMPLATE["entry"]["cancel_at"])


def cutoff(now: datetime | None = None) -> int:
    """Cancel intraday buy GTTs that have not triggered: all of them from 15:15 (or on a non-trading day), and
    before 15:15 only those left from an earlier session (never carried into the next one)."""
    from ..orders import manager
    vs = all_versions()
    if not vs:
        return 0
    now = (now or now_ist()).astimezone(IST)
    cal = MarketCalendar(get_app_config())
    if cal.is_trading_day(now.date()) and now.time() < _cut_time():
        return manager.cancel_buys(vs, None, "intraday buy left from an earlier session", reason="intraday_cutoff",
                                   placed_before=now.date().isoformat())
    return manager.cancel_buys(vs, None, f"intraday buy not triggered by {rules.TEMPLATE['entry']['cancel_at']}",
                               reason="intraday_cutoff")


def pending_count(placed_before: str | None = None) -> int:
    vs = all_versions()
    if not vs:
        return 0
    q = f"SELECT COUNT(*) FROM trades WHERE state = 'BUY_PENDING' AND strategy_version IN ({','.join('?' * len(vs))})"
    args: list[Any] = list(vs)
    if placed_before:
        q += " AND placed_on < ?"
        args.append(placed_before)
    with connect() as conn:
        return conn.execute(q, args).fetchone()[0]


def reset_day_state() -> None:
    _seen.clear()
