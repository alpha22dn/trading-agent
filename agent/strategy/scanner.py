"""Signal scanning (spec sections 5 and 6).

- `scan_eod()`: the EOD run's scan of the last completed session. Applies all
  five eligibility gates and the market filter; stores every technical trigger
  in `signals` with its gate results, so the Signals screen can show why a
  trigger was or was not tradeable.
- `scan_history()`: the Phase 1 "done when" report. Replays the same technical
  rules, levels and RR over the whole candle cache and counts signals per month.
  Fundamentals are not available point-in-time (Screener pastes have no
  history), so historical rows leave the watchlist and valuation gates empty.

Gate 5 (no results soon) blocks a stock when
  (a) a results board meeting falls between the signal day and the 5th trading
      day after it (inclusive), or
  (b) the signal day is within 7 calendar days either side of the date one year
      after any past results meeting (same quarter last year).
"""
from __future__ import annotations

import json
import logging
import threading
from bisect import bisect_left
from collections import defaultdict
from datetime import date, timedelta
from typing import Any

import pandas as pd

from .. import audit
from ..clock import now_ist, ts
from ..data import events as events_data
from ..data import watchlist as watchlist_data
from ..db import connect, get_state, set_state
from ..events import publish
from ..market_calendar import MarketCalendar
from ..settings import StrategyConfig, get_app_config
from . import market_filter
from . import momentum_v1 as mv1

log = logging.getLogger("agent")
HISTORY_KEY = "history_scan"
GATES = ["nse500", "watchlist", "valuation", "liquidity", "results", "market"]


# ---------- gate 3: valuation ----------

def valuation_ok(pe: float | None, profit_growth: float, cap: float) -> tuple[bool, float]:
    """P/E > 0 and P/E < min(profit growth, cap). The cap stops a one-year jump off a weak
    base (e.g. 2,000% growth) from letting an expensive stock through.
    Returns (passes, growth used for the comparison)."""
    used = min(profit_growth, cap)
    return bool(pe is not None and pe > 0 and pe < used), used


def _growth_cap() -> float:
    return float(get_app_config()["universe"]["valuation_growth_cap_pct"])


# ---------- data loading ----------

def load_joined(conn, symbols: list[str] | None = None) -> pd.DataFrame:
    rows = conn.execute(
        """SELECT c.symbol, c.date, c.open, c.high, c.low, c.close, c.volume,
                  i.bb_upper, i.atr14, i.adx14, i.traded_value_20d
           FROM candles_daily c
           JOIN instruments ins ON ins.symbol = c.symbol AND ins.is_nse500 = 1
           LEFT JOIN indicators_daily i ON i.symbol = c.symbol AND i.date = c.date
           ORDER BY c.symbol, c.date""").fetchall()
    df = pd.DataFrame([tuple(r) for r in rows],
                      columns=["symbol", "date", "open", "high", "low", "close", "volume", "bb_upper", "atr14",
                               "adx14", "traded_value_20d"])
    if symbols is not None:
        df = df[df["symbol"].isin(symbols)]
    return df


def technical_frame(df: pd.DataFrame, params: dict[str, Any]) -> pd.DataFrame:
    """Adds prev-day values, 20-session low and the four condition flags."""
    sig, lv = params["signal"], params["levels"]
    g = df.groupby("symbol", sort=False)
    df = df.assign(prev_close=g["close"].shift(1), prev_atr=g["atr14"].shift(1),
                   low_min=g["low"].transform(lambda s: s.rolling(int(lv["swing_lookback"])).min()))
    df = df.assign(
        change_pct=(df["close"] / df["prev_close"] - 1) * 100,
        c_change=df["close"] >= df["prev_close"] * (1 + float(sig["min_change_pct"]) / 100),
        c_bb=df["close"] >= df["bb_upper"],
        c_atr=df["atr14"] > df["prev_atr"],
        c_adx=df["adx14"] > float(sig["adx_min"]),
    )
    # A comparison with NaN is False, so warm-up rows never trigger.
    df["trigger"] = df["c_change"] & df["c_bb"] & df["c_atr"] & df["c_adx"] & df["low_min"].notna()
    return df


def nifty_filter(conn, app) -> dict[str, bool]:
    mf = app["universe"]["market_filter"]
    n = int(mf["sma_period"])
    rows = conn.execute("SELECT date, close FROM candles_daily WHERE symbol = ? ORDER BY date",
                        (app["kite"]["nifty50_symbol"],)).fetchall()
    s = pd.Series([r[1] for r in rows], index=[r[0] for r in rows], dtype=float)
    sma = s.rolling(n).mean()
    return {d: bool(c >= m) for d, c, m in zip(s.index, s, sma) if pd.notna(m)}


def sessions(conn, app) -> list[str]:
    rows = conn.execute("SELECT date FROM candles_daily WHERE symbol = ? ORDER BY date",
                        (app["kite"]["nifty50_symbol"],)).fetchall()
    return [r[0] for r in rows]


def nth_session_after(d: date, n: int, known: list[str], cal: MarketCalendar) -> date:
    """n-th trading day after d: real sessions from the candle cache, the calendar beyond it."""
    iso = d.isoformat()
    i = bisect_left(known, iso)
    if i < len(known) and known[i] == iso:
        i += 1
    if i + n - 1 < len(known):
        return date.fromisoformat(known[i + n - 1])
    cur = date.fromisoformat(known[-1]) if known and known[-1] > iso else d
    remaining = n - max(0, len(known) - i)
    for _ in range(remaining):
        cur = cal.next_trading_day(cur)
    return cur


def results_check(d: date, meetings: list[date], known: list[str], cal: MarketCalendar,
                  ahead_days: int, window_days: int) -> tuple[bool, str | None, date | None]:
    """(blocked, reason, next_meeting)."""
    horizon = nth_session_after(d, ahead_days, known, cal)
    upcoming = [m for m in meetings if m >= d]
    nxt = upcoming[0] if upcoming else None
    if nxt and nxt <= horizon:
        return True, f"Results meeting on {nxt.isoformat()}", nxt
    for m in meetings:
        try:
            anniv = m.replace(year=m.year + 1)
        except ValueError:  # 29 Feb
            anniv = m + timedelta(days=365)
        if abs((d - anniv).days) <= window_days:
            return True, f"Within {window_days} days of last year's results date ({m.isoformat()})", nxt
    return False, None, nxt


# ---------- EOD scan ----------

_scan_lock = threading.Lock()


def scan_eod(strategy: StrategyConfig, primary: bool = True) -> dict[str, Any]:
    """Scan the last completed session. Serialised: the EOD run, the button and the automatic re-scans
    (login, watchlist upload) never overlap. primary=False: a challenger version's scan (its signals are stored
    under its own version; the Signals screen's status stays the champion's)."""
    with _scan_lock:
        return _scan_eod(strategy, primary)


def _scan_eod(strategy: StrategyConfig, primary: bool = True) -> dict[str, Any]:
    app = get_app_config()
    cal = MarketCalendar(app)
    params = strategy.params
    d = cal.last_completed_session(now_ist())
    d_iso = d.isoformat()
    u = app["universe"]
    min_tv = float(u["min_traded_value_cr"]) * 1e7
    wl = watchlist_data.active()
    events_ok = events_data.fresh_for(d)
    with connect() as conn:
        df = load_joined(conn)
        mf_by_date = nifty_filter(conn, app)
        known = sessions(conn, app)
        meetings = events_data.results_dates(conn)
        ticks = {r[0]: r[1] for r in conn.execute("SELECT symbol, tick_size FROM instruments WHERE is_nse500 = 1")}
    if df.empty:
        raise RuntimeError("No candles/indicators to scan")
    tf = technical_frame(df, params)
    latest = tf.groupby("symbol").tail(1)
    stale = sorted(latest.loc[latest["date"] < d_iso, "symbol"])
    today_rows = tf[(tf["date"] == d_iso)]
    market_ok = mf_by_date.get(d_iso)

    results = []
    for _, row in today_rows[today_rows["trigger"]].iterrows():
        results.append(_evaluate(row, d, params, ticks.get(row.symbol, 0.05), tf, wl, market_ok, meetings, known, cal,
                                 u, min_tv, events_ok=events_ok, source="eod", strategy=strategy))
    _store(results, strategy.version, only_date=d_iso, source="eod")
    eligible = [r for r in results if r["eligible"]]
    summary = {"date": d_iso, "scanned": int(len(today_rows)), "triggers": len(results), "eligible": len(eligible),
               "stale_symbols": stale[:50], "stale_count": len(stale), "market_ok": market_ok,
               "events_ok": events_ok, "watchlist_size": len(wl), "at": ts()}
    if not primary:
        return summary
    set_state("last_scan", summary)
    audit.record("job", "signal_scan", f"{d_iso}: {len(results)} triggers, {len(eligible)} tradeable",
                 details={k: v for k, v in summary.items() if k != "stale_symbols"})
    if stale:
        audit.alert("stale_candles", f"{len(stale)} stock(s) have no candle for {d_iso} and were not scanned: "
                    f"{', '.join(stale[:8])}", level="warning")
    publish("signals")
    return summary


def _evaluate(row, d: date, params, tick, tf, wl, market_ok, meetings, known, cal, u, min_tv, events_ok: bool | None,
              source: str, strategy: StrategyConfig) -> dict[str, Any]:
    sym = row.symbol
    bar = mv1.Bar(row.date, row.open, row.high, row.low, row.close, row.volume, row.bb_upper, row.atr14, row.adx14)
    # low_min = lowest low of the swing_lookback sessions ending on the signal day
    lv = mv1.trade_levels(bar, [float(row.low_min)], float(tick or 0.05), params)

    gates: dict[str, bool | None] = {g: None for g in GATES}
    reasons: list[str] = []
    gates["nse500"] = True
    if source == "eod":
        w = wl.get(sym)
        gates["watchlist"] = w is not None
        if not w:
            reasons.append("Not on the watchlist")
            gates["valuation"] = False
        else:
            pe = row.close / w["eps"] if w["eps"] else None
            gates["valuation"], used = valuation_ok(pe, w["profit_growth"], _growth_cap())
            if not gates["valuation"]:
                capped = f" (capped from {w['profit_growth']:.1f})" if used < w["profit_growth"] else ""
                reasons.append(f"Valuation: P/E {pe:.1f} vs profit growth {used:.1f}{capped}"
                               if pe is not None else "Valuation: EPS is zero")
    tv = row.traded_value_20d
    gates["liquidity"] = bool(pd.notna(tv) and tv >= min_tv)
    if not gates["liquidity"]:
        reasons.append(f"Liquidity: ₹{(tv or 0) / 1e7:.1f} cr/day < ₹{u['min_traded_value_cr']} cr")
    if events_ok is False:
        gates["results"] = False
        reasons.append(f"Results calendar not fetched since the {d.strftime('%d %b')} close")
    elif events_ok is None and not meetings:
        gates["results"] = None
    else:
        blocked, why, _ = results_check(d, meetings.get(sym, []), known, cal, int(u["results_blackout_days"]),
                                        int(u["last_year_window_days"]))
        gates["results"] = not blocked
        if blocked:
            reasons.append(why)
    if market_filter.enabled():
        gates["market"] = bool(market_ok) if market_ok is not None else False
        if not gates["market"]:
            reasons.append("Market filter: Nifty 50 below its 50-day SMA" if market_ok is False
                           else "Market filter: no Nifty data")
    else:
        del gates["market"]  # filter switched off: not a gate at all
    if not lv.rr_ok:
        reasons.insert(0, f"RR {lv.rr:.2f} < {params['levels']['min_rr']}")
    checked = [v for v in gates.values() if v is not None]
    eligible = lv.rr_ok and all(checked) and (source != "eod" or None not in gates.values())
    return {
        "date": row.date, "symbol": sym, "levels": lv, "gates": gates, "reasons": reasons, "eligible": bool(eligible),
        "indicators": {"close": row.close, "high": row.high, "low": row.low, "prev_close": row.prev_close,
                       "change_pct": round(float(row.change_pct), 2), "bb_upper": round(float(row.bb_upper), 2),
                       "atr14": round(float(row.atr14), 4), "atr14_prev": round(float(row.prev_atr), 4),
                       "adx14": round(float(row.adx14), 2),
                       "traded_value_cr": round(float(tv) / 1e7, 2) if pd.notna(tv) else None},
    }


def _store(results: list[dict[str, Any]], version: str, only_date: str | None, source: str) -> None:
    now = ts()
    with connect() as conn:
        if only_date:
            # Upsert keeps each signal's id, so trades and per-account candidates stay linked across
            # re-scans. Only rows that no longer trigger (and nothing points to) are removed.
            keep = [r["symbol"] for r in results]
            conn.execute(f"""DELETE FROM signals WHERE date = ? AND strategy_version = ? AND source = ?
                             AND symbol NOT IN ({','.join('?' * len(keep)) or "''"})
                             AND id NOT IN (SELECT signal_id FROM trades WHERE signal_id IS NOT NULL)
                             AND id NOT IN (SELECT signal_id FROM candidates)""",
                         (only_date, version, source, *keep))
        else:
            conn.execute("DELETE FROM signals WHERE strategy_version = ? AND source = ?", (version, source))
        conn.executemany(
            """INSERT INTO signals (date, symbol, strategy_version, entry_trigger, entry_limit, stop, target, swing_low,
                                    rr, rr_band, indicators, eligible, gates, block_reasons, source, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(date, symbol, strategy_version) DO UPDATE SET
                 entry_trigger = excluded.entry_trigger, entry_limit = excluded.entry_limit, stop = excluded.stop,
                 target = excluded.target, swing_low = excluded.swing_low, rr = excluded.rr,
                 rr_band = excluded.rr_band, indicators = excluded.indicators, eligible = excluded.eligible,
                 gates = excluded.gates, block_reasons = excluded.block_reasons, source = excluded.source,
                 created_at = excluded.created_at""",
            [(r["date"], r["symbol"], version, r["levels"].entry_trigger, r["levels"].entry_limit, r["levels"].stop,
              r["levels"].target, r["levels"].swing_low, r["levels"].rr, r["levels"].rr_band,
              json.dumps(r["indicators"]), 1 if r["eligible"] else 0, json.dumps(r["gates"]),
              json.dumps(r["reasons"]), source, now) for r in results])


# ---------- historical scan (Phase 1 done-check) ----------

def scan_history(strategy: StrategyConfig) -> dict[str, Any]:
    app = get_app_config()
    cal = MarketCalendar(app)
    params = strategy.params
    u = app["universe"]
    min_tv = float(u["min_traded_value_cr"]) * 1e7
    ev = events_data.status()
    with connect() as conn:
        df = load_joined(conn)
        mf_by_date = nifty_filter(conn, app)
        known = sessions(conn, app)
        meetings = events_data.results_dates(conn)
        ticks = {r[0]: r[1] for r in conn.execute("SELECT symbol, tick_size FROM instruments WHERE is_nse500 = 1")}
    tf = technical_frame(df, params)
    trig = tf[tf["trigger"]]
    hist = ev.get("history") or {}
    results_known_from = hist.get("from") if hist.get("ok") else None
    results = []
    for _, row in trig.iterrows():
        d = date.fromisoformat(row.date)
        have_events = bool(results_known_from and row.date >= results_known_from)
        r = _evaluate(row, d, params, ticks.get(row.symbol, 0.05), tf, {}, mf_by_date.get(row.date),
                      meetings if have_events else {}, known, cal, u, min_tv,
                      events_ok=True if have_events else None, source="scan", strategy=strategy)
        results.append(r)
    # EOD rows are kept; scan rows are rebuilt.
    with connect() as conn:
        eod_keys = {(r[0], r[1]) for r in conn.execute(
            "SELECT date, symbol FROM signals WHERE source = 'eod' AND strategy_version = ?", (strategy.version,))}
    _store([r for r in results if (r["date"], r["symbol"]) not in eod_keys], strategy.version, None, "scan")
    summary = summarize(strategy.version)
    summary.update({"at": ts(), "strategy": strategy.version, "first_date": tf["date"].min(),
                    "indicators_from": tf.loc[tf["adx14"].notna(), "date"].min() if tf["adx14"].notna().any() else None,
                    "last_date": tf["date"].max(), "results_history_from": results_known_from,
                    "symbols": int(tf["symbol"].nunique())})
    set_state(HISTORY_KEY, summary)
    audit.record("job", "history_scan", f"{summary['totals']['triggers']} triggers, "
                 f"{summary['totals']['tradeable']} tradeable before fundamentals")
    publish("signals")
    return summary


def summarize(version: str) -> dict[str, Any]:
    wl = watchlist_data.active()
    mf_on = market_filter.enabled()
    with connect() as conn:
        rows = conn.execute("SELECT date, symbol, rr, rr_band, gates, eligible FROM signals WHERE strategy_version = ? "
                            "ORDER BY date", (version,)).fetchall()
    months: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        m = r["date"][:7]
        g = json.loads(r["gates"] or "{}")
        rr_ok = r["rr_band"] is not None
        c = months[m]
        c["triggers"] += 1
        if not rr_ok:
            continue
        c["rr_ok"] += 1
        if not g.get("liquidity"):
            continue
        c["liquid"] += 1
        if mf_on and not g.get("market"):
            continue
        c["market_ok"] += 1
        if g.get("results") is False:
            continue
        c["tradeable"] += 1
        c[f"band_{r['rr_band']}"] += 1
        if r["symbol"] in wl:
            c["on_current_watchlist"] += 1
    keys = ["triggers", "rr_ok", "liquid", "market_ok", "tradeable", "band_A", "band_B", "band_C",
            "on_current_watchlist"]
    table = [{"month": m, **{k: int(months[m].get(k, 0)) for k in keys}} for m in sorted(months)]
    totals = {k: sum(t[k] for t in table) for k in keys}
    return {"months": table, "totals": totals, "market_filter_enabled": mf_on}


def signals_for(version: str, day: str | None = None, month: str | None = None) -> list[dict[str, Any]]:
    sql = """SELECT s.*, i.sector FROM signals s LEFT JOIN instruments i ON i.symbol = s.symbol
             WHERE s.strategy_version = ?"""
    args: list[Any] = [version]
    if day:
        sql += " AND s.date = ?"
        args.append(day)
    if month:
        sql += " AND substr(s.date, 1, 7) = ?"
        args.append(month)
    sql += " ORDER BY s.date DESC, s.eligible DESC, s.rr DESC"
    with connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        for k in ("indicators", "gates", "block_reasons"):
            d[k] = json.loads(d[k]) if d.get(k) else None
        out.append(d)
    return out


def latest_signal_date(version: str) -> str | None:
    with connect() as conn:
        row = conn.execute("SELECT MAX(date) FROM signals WHERE strategy_version = ? AND source IN ('eod', 'intraday')",
                           (version,)).fetchone()
    return row[0]


def history_state() -> dict[str, Any] | None:
    return get_state(HISTORY_KEY)


# ---------- gate table for the Watchlist screen ----------

def gate_table(only_watchlist: bool = False) -> dict[str, Any]:
    """Every NSE500 stock (or only watchlist stocks) with pass/fail on each eligibility gate,
    as of the latest cached session."""
    app = get_app_config()
    cal = MarketCalendar(app)
    u = app["universe"]
    min_tv = float(u["min_traded_value_cr"]) * 1e7
    wl = watchlist_data.active()
    ev = events_data.status()
    cap = _growth_cap()
    with connect() as conn:
        rows = conn.execute(
            """SELECT ins.symbol, ins.sector, c.date, c.close, i.traded_value_20d
               FROM instruments ins
               LEFT JOIN candles_daily c ON c.symbol = ins.symbol
                    AND c.date = (SELECT MAX(date) FROM candles_daily WHERE symbol = ins.symbol)
               LEFT JOIN indicators_daily i ON i.symbol = ins.symbol AND i.date = c.date
               WHERE ins.is_nse500 = 1 ORDER BY ins.symbol""").fetchall()
        known = sessions(conn, app)
        meetings = events_data.results_dates(conn)
    as_of = cal.last_completed_session(now_ist())
    events_ok = ev["fetched_today"] or bool(ev["count"])
    out = []
    for r in rows:
        sym = r["symbol"]
        w = wl.get(sym)
        if only_watchlist and not w:
            continue
        pe = (r["close"] / w["eps"]) if (w and w["eps"] and r["close"]) else None
        tv = r["traded_value_20d"]
        if events_ok:
            blocked, why, nxt = results_check(as_of, meetings.get(sym, []), known, cal,
                                              int(u["results_blackout_days"]), int(u["last_year_window_days"]))
        else:
            blocked, why, nxt = True, "Results calendar not loaded", None
        gates = {
            "nse500": True,
            "watchlist": w is not None,
            "valuation": bool(w) and valuation_ok(pe, w["profit_growth"], cap)[0],
            "liquidity": bool(tv is not None and tv >= min_tv),
            "results": not blocked,
        }
        out.append({"symbol": sym, "sector": r["sector"], "date": r["date"], "close": r["close"],
                    "eps": w["eps"] if w else None, "profit_growth": w["profit_growth"] if w else None,
                    "pe": round(pe, 2) if pe is not None else None,
                    "growth_used": min(w["profit_growth"], cap) if w else None,
                    "traded_value_cr": round(tv / 1e7, 2) if tv is not None else None,
                    "next_results": nxt.isoformat() if nxt else None, "results_reason": why,
                    "gates": gates, "eligible": all(gates.values())})
    pasted = max((w["pasted_on"] for w in wl.values()), default=None)
    return {"as_of": as_of.isoformat(), "watchlist_size": len(wl), "growth_cap": cap, "pasted_on": pasted, "rows": out,
            "eligible_count": sum(1 for o in out if o["eligible"]), "results_calendar": ev}
