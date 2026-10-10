"""Backtest engine (the Backtest tab), decided 2026-09-25.

Runs the live engine (PaperBroker, order monitor, sizing, ranking, trailing, every exit rule) over a past date
range on its own database, with optional what-if strategy settings, and saves the results for comparison.

- Fills follow real 1-minute price paths from the 1-minute store (runtime/agent_1m.db). Inside one minute the
  order is open → low → high → close when the bar closes up, open → high → low → close when it closes down.
  The monitor is stepped only on the minutes that matter: the open, every minute whose range crosses an active
  GTT trigger or a working order's limit, and the minute after any fill (the exit goes on "at the next cycle").
  A stock-day with no 1-minute data falls back to the daily candle's four prices; the report counts them.
- What-ifs change the strategy numbers for that run only: signals are re-scanned with them. The live strategy
  file is never touched (changes to it go through the Learning screen).
- Fundamentals can't be replayed (no historical Screener data), so the watchlist and valuation gates are
  skipped. A drawdown halt is re-armed the same evening (nobody is there to do it) and counted.
- Each run is a separate process (python -m agent.cli backtest-run): it can never touch the live agent's clock,
  quotes or orders. Progress and results go to the live database's backtest_runs table.
"""
from __future__ import annotations

import copy
import json
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .clock import IST, now_ist, ts
from .settings import StrategyConfig, get_app_config, load_strategy_config

# What-if settings: key -> (path in the strategy YAML, label, min, max, step)
WHATIFS: dict[str, tuple[tuple[str, str], str, float, float, float]] = {
    "min_rr": (("levels", "min_rr"), "Minimum reward:risk", 1.0, 5.0, 0.1),
    "entry_trigger_buffer_pct": (("levels", "entry_trigger_buffer_pct"), "Entry trigger above close (%)", 0.0, 3.0, 0.05),
    "entry_limit_buffer_pct": (("levels", "entry_limit_buffer_pct"), "Limit above trigger (%)", 0.0, 3.0, 0.05),
    "stop_buffer_pct": (("levels", "stop_buffer_pct"), "Stop below signal low (%)", 0.0, 5.0, 0.05),
    "target_extension": (("levels", "target_extension"), "Target extension (× range)", 0.5, 5.0, 0.1),
    "trail_buffer_pct": (("exits", "trail_buffer_pct"), "Trail below prior low (%)", 0.0, 5.0, 0.05),
    "cancel_after_sessions": (("entry", "cancel_after_sessions"), "Cancel unfilled buy after (sessions)", 1, 10, 1),
}
BANDS = ["A", "B", "C", "D"]   # D = RR below 1.5, possible only when min_rr is set below 1.5
MIN_CAPITAL, MAX_CAPITAL, DEFAULT_CAPITAL = 1000.0, 1e8, 100000.0
ACCOUNT_ID = "Backtest"        # one standalone account per run; the paper accounts are never involved


class BacktestError(ValueError):
    pass


def live_strategy() -> StrategyConfig:
    """The champion: what-ifs are measured against the version the portfolios trade now."""
    from .strategy import versions
    return versions.champion()


def _get(params: dict[str, Any], path: tuple[str, str]) -> Any:
    return params[path[0]][path[1]]


def minute_db_path(live_db: Path) -> Path:
    return live_db.with_name(live_db.stem + "_1m.db")


def coverage(live_db: Path) -> dict[str, Any]:
    """Dates the 1-minute store covers (from the Nifty 50 series, which every download includes)."""
    path = minute_db_path(live_db)
    if not path.exists():
        return {"from": None, "to": None, "stocks": 0}
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30) as mconn, \
            sqlite3.connect(f"file:{live_db}?mode=ro", uri=True, timeout=30) as lconn:
        nifty = get_app_config()["kite"]["nifty50_symbol"]
        tok = lconn.execute("SELECT instrument_token FROM instruments WHERE symbol = ?", (nifty,)).fetchone()
        rng = mconn.execute("SELECT MIN(ts), MAX(ts) FROM candles_1m WHERE token = ?", (tok[0],)).fetchone() \
            if tok else (None, None)
    if not rng or rng[0] is None:
        return {"from": None, "to": None}
    f = datetime.fromtimestamp(rng[0], IST).date().isoformat()
    t = datetime.fromtimestamp(rng[1], IST).date().isoformat()
    return {"from": f, "to": t}


def whatifs_for(params: dict[str, Any]) -> dict[str, tuple[tuple[str, str], str, float, float, float]]:
    """The what-if settings a run may change: the momentum list, or the intraday pullback's own settings."""
    from .strategy import strategies
    kind = strategies.kind_of(params)
    return WHATIFS if kind == "momentum_breakout" else strategies.SETTINGS_BY_KIND[kind]


def _is_intraday(params: dict[str, Any]) -> bool:
    return params.get("kind") == "intraday_pullback"


def _base(version: str | None) -> StrategyConfig:
    from .strategy import strategies, versions
    if not version:
        return live_strategy()
    try:
        s = versions.load(version)
    except Exception as exc:  # noqa: BLE001
        raise BacktestError(f"Unknown strategy version {version}") from exc
    return s


def setup(live_db: Path) -> dict[str, Any]:
    """What the Backtest form needs: live values of every what-if, the default capital, the 1-minute coverage,
    and every strategy (a run starts from one of them)."""
    from .strategy import strategies
    s = live_strategy()
    app = get_app_config()
    strat = []
    for r in strategies.active_rows():
        p = _base(r["version"]).params
        wi = whatifs_for(p)
        strat.append({"id": r["id"], "name": r["name"], "version": r["version"], "enabled": bool(r["enabled"]),
                      "kind": r["kind"], "live": {k: _get(p, path) for k, (path, *_x) in wi.items()},
                      "whatifs": [{"key": k, "label": lab, "live": _get(p, path), "min": lo, "max": hi, "step": st}
                                  for k, (path, lab, lo, hi, st) in wi.items()]})
    return {
        "strategies": strat,
        "strategy": s.version,
        "whatifs": [{"key": k, "label": lab, "live": _get(s.params, path), "min": lo, "max": hi, "step": st}
                    for k, (path, lab, lo, hi, st) in WHATIFS.items()],
        "bands": [{"band": b["band"], "min": b["min"]} for b in s.params["ranking"]["rr_bands"]],
        "default_capital": DEFAULT_CAPITAL, "min_capital": MIN_CAPITAL, "max_capital": MAX_CAPITAL,
        "risk": {k: app["risk"][k] for k in ("risk_per_trade_pct", "max_position_pct", "max_open_positions",
                                             "max_per_sector", "drawdown_halt_pct")},
        "coverage": coverage(live_db),
    }


def validate(req: dict[str, Any], live_db: Path) -> dict[str, Any]:
    """Normalise a run request. Raises BacktestError with a message for the form."""
    cov = coverage(live_db)
    if not cov.get("from"):
        raise BacktestError("No 1-minute data yet: download it on the Overview screen first")
    try:
        start, end = date.fromisoformat(req["start"]), date.fromisoformat(req["end"])
    except (KeyError, ValueError) as exc:
        raise BacktestError("Pick a start and an end date") from exc
    if start > end:
        raise BacktestError("The start date is after the end date")
    if start.isoformat() < cov["from"] or end.isoformat() > cov["to"]:
        raise BacktestError(f"1-minute data covers {cov['from']} to {cov['to']}; pick dates inside that range")
    try:
        capital = float(req.get("capital"))
    except (TypeError, ValueError) as exc:
        raise BacktestError("Enter the starting capital") from exc
    if not MIN_CAPITAL <= capital <= MAX_CAPITAL:
        raise BacktestError("Starting capital must be between ₹1,000 and ₹10 crore")
    base = _base(req.get("strategy"))
    live = base.params
    overrides: dict[str, float] = {}
    allowed = whatifs_for(live)
    for k, v in (req.get("overrides") or {}).items():
        if k not in allowed or v in (None, ""):
            continue
        path, label, lo, hi, step = allowed[k]
        x = float(v)
        if not lo <= x <= hi:
            raise BacktestError(f"{label}: must be between {lo:g} and {hi:g}")
        if step == 1:
            x = int(round(x))
        if x != _get(live, path):
            overrides[k] = x
    min_rr = float(overrides.get("min_rr", live["levels"]["min_rr"]))
    bands = [b for b in (req.get("bands") or ["A", "B", "C", "D"]) if b in BANDS]
    if min_rr >= 1.5:
        bands = [b for b in bands if b != "D"]
    if not bands:
        raise BacktestError("Allow at least one RR band")
    name = (req.get("name") or "").strip()[:80] or f"{start:%d %b %Y} – {end:%d %b %Y}" + (
        " · " + ", ".join(f"{k}={v:g}" for k, v in overrides.items()) if overrides else "")
    return {"name": name, "start": start.isoformat(), "end": end.isoformat(), "capital": capital, "overrides": overrides, "bands": sorted(bands), "strategy": base.version,
            "base_params": base.params}


def build_strategy(cfg: dict[str, Any]) -> StrategyConfig:
    """The run's base strategy with its what-ifs applied (same version name, so all engine code applies)."""
    s = StrategyConfig(version=cfg["strategy"], params=cfg["base_params"]) if cfg.get("base_params") \
        else _base(cfg.get("strategy"))
    params = copy.deepcopy(s.params)
    allowed = whatifs_for(params)
    for k, v in (cfg.get("overrides") or {}).items():
        path = allowed[k][0]
        params[path[0]][path[1]] = v
    min_rr = float(params["levels"]["min_rr"])
    lowest = min(float(b["min"]) for b in params["ranking"]["rr_bands"])
    if min_rr < lowest:  # a band for signals between the new minimum and the old lowest band (ranked last)
        params["ranking"]["rr_bands"].append({"band": "D", "min": min_rr})
    return StrategyConfig(version=s.version, params=params)


# ---------- run bookkeeping (in the live database) ----------

def _live(live_db: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(live_db), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def create_run(live_db: Path, cfg: dict[str, Any]) -> int:
    with _live(live_db) as conn:
        cur = conn.execute("INSERT INTO backtest_runs (name, config, status, progress, message, created_at, updated_at) "
                           "VALUES (?, ?, 'queued', 0, 'Starting…', ?, ?)", (cfg["name"], json.dumps(cfg), ts(), ts()))
        return int(cur.lastrowid)


def update_run(live_db: Path, run_id: int, **fields: Any) -> None:
    fields["updated_at"] = ts()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _live(live_db) as conn:
        conn.execute(f"UPDATE backtest_runs SET {cols} WHERE id = ?", (*fields.values(), run_id))


def get_run(live_db: Path, run_id: int) -> dict[str, Any] | None:
    with _live(live_db) as conn:
        r = conn.execute("SELECT * FROM backtest_runs WHERE id = ?", (run_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["config"] = json.loads(d["config"])
    d["report"] = json.loads(d["report"]) if d["report"] else None
    return d


def list_runs(live_db: Path) -> list[dict[str, Any]]:
    """Newest first, with a short per-account summary (no trade lists)."""
    out = []
    with _live(live_db) as conn:
        rows = conn.execute("SELECT * FROM backtest_runs ORDER BY id DESC").fetchall()
    for r in rows:
        d = dict(r)
        d["config"] = json.loads(d["config"])
        if d["config"].get("purpose"):          # runs made by the learning loop are shown on the Learning screen
            continue
        rep = json.loads(d.pop("report")) if d["report"] else None
        d["summary"] = {a: {k: v for k, v in x.items() if k in ("capital", "return_pct", "max_drawdown_pct", "closed",
                                                                 "win_pct", "avg_r", "net_pnl")}
                        for a, x in (rep or {}).get("accounts", {}).items()}
        d["seconds"] = (rep or {}).get("seconds")
        out.append(d)
    return out


def mark_interrupted(live_db: Path) -> None:
    """At server start: a run that was going when the server stopped did not finish."""
    with _live(live_db) as conn:
        conn.execute("UPDATE backtest_runs SET status = 'interrupted', message = 'Stopped when the agent restarted', "
                     "updated_at = ? WHERE status IN ('queued', 'running')", (ts(),))


# ---------- the engine (runs in its own process) ----------

def _events_last_cycle() -> int:
    from .db import get_state
    return int((get_state("monitor_last") or {}).get("events") or 0)


def _levels() -> dict[str, list[float]]:
    """Prices at which something can happen next: active GTT triggers and working orders' limits."""
    from .db import connect
    out: dict[str, list[float]] = {}
    with connect() as conn:
        for r in conn.execute("SELECT symbol, trigger_values FROM paper_gtts WHERE status = 'active'"):
            out.setdefault(r[0], []).extend(float(x) for x in json.loads(r[1]))
        for r in conn.execute("SELECT symbol, price FROM paper_orders WHERE status = 'open'"):
            out.setdefault(r[0], []).append(float(r[1]))
    return out


def _minute_path(o: float, h: float, lo: float, c: float) -> list[float]:
    return [o, lo, h, c] if c >= o else [o, h, lo, c]


class MinuteDay:
    """One session's intraday replay on real 1-minute bars (see the module docstring)."""

    def __init__(self, rp, mconn: sqlite3.Connection, tokens: dict[str, int]):
        self.rp, self.mconn, self.tokens = rp, mconn, tokens
        self.fallback_days = 0
        self.minute_days = 0

    def _bars(self, day: str, symbols: list[str]) -> dict[str, list[tuple[datetime, float, float, float, float]]]:
        from .db import connect
        from .replay import day_path
        start = datetime.combine(date.fromisoformat(day), datetime.min.time(), IST).replace(hour=9, minute=15)
        end = start.replace(hour=15, minute=30)
        out: dict[str, list] = {}
        for s in symbols:
            tok = self.tokens.get(s)
            rows = self.mconn.execute("SELECT ts, open, high, low, close FROM candles_1m WHERE token = ? AND ts >= ? "
                                      "AND ts < ? ORDER BY ts", (tok, int(start.timestamp()), int(end.timestamp()))
                                      ).fetchall() if tok else []
            if rows:
                self.minute_days += 1
                out[s] = [(datetime.fromtimestamp(r[0], IST).replace(tzinfo=None), r[1] / 100, r[2] / 100, r[3] / 100,
                           r[4] / 100) for r in rows]
                continue
            with connect() as conn:  # no 1-minute data for this stock-day: the daily candle's four prices
                c = conn.execute("SELECT open, high, low, close FROM candles_daily WHERE symbol = ? AND date = ?",
                                 (s, day)).fetchone()
            if c:
                self.fallback_days += 1
                pts = [p[0] for p in day_path(*c)]
                times = ["09:15", "11:00", "13:30", "15:25"]
                out[s] = [(datetime.fromisoformat(f"{day}T{t}"), p, p, p, p) for t, p in zip(times, pts)]
        return out

    def run(self, day: str, extra_symbols: tuple | list = (), events: dict[datetime, Any] | None = None) -> None:
        """events: {wall-clock minute (naive): callable} run at that minute before its prices are replayed, with
        the book holding the last prices (intraday signals placing their buys, the 15:15 cutoff)."""
        from .broker.base import Quote
        from .db import connect
        from .scheduler import Skip
        from .broker.paper import get_broker
        broker = get_broker()
        with connect() as conn:
            held = {r[0] for r in conn.execute("SELECT DISTINCT symbol FROM trades WHERE state IN "
                                               "('BUY_PENDING', 'FILLED', 'PROTECTED', 'TRAILING')")}
        symbols = sorted(set(broker.watched_symbols()) | held | set(extra_symbols))
        if not symbols:
            return
        bars = self._bars(day, symbols)
        minutes = sorted({b[0] for bs in bars.values() for b in bs})
        if not minutes:
            return
        due: dict[int, list[Any]] = {}              # minute index -> events due at (or just before) it
        for t, fn in sorted((events or {}).items()):
            j = next((k for k, m in enumerate(minutes) if m >= t), None)
            if j is not None:
                due.setdefault(j, []).append(fn)
        by_min = {s: {b[0]: b for b in bs} for s, bs in bars.items()}
        day_open = {s: bs[0][1] for s, bs in bars.items()}
        hi = {s: bs[0][1] for s, bs in bars.items()}
        lo = dict(hi)
        last = dict(hi)

        def advance(m: datetime) -> None:   # a skipped minute: the day's range and last price move on
            for s, bm in by_min.items():
                b = bm.get(m)
                if b:
                    hi[s], lo[s], last[s] = max(hi[s], b[2]), min(lo[s], b[3]), b[4]

        def cycle(m: datetime) -> int:      # a minute that matters: four prices, one monitor cycle each
            events = 0
            for k in range(4):
                self.rp.clock.set(m + timedelta(seconds=k * 15))
                self.rp._book.clear()
                for s, bm in by_min.items():
                    b = bm.get(m)
                    p = _minute_path(*b[1:5])[k] if b else last[s]
                    hi[s], lo[s], last[s] = max(hi[s], p), min(lo[s], p), p
                    self.rp._book[s] = Quote(ltp=p, high=hi[s], low=lo[s], open=day_open[s])
                try:
                    self.rp.manager.on_cycle()
                    events += _events_last_cycle()
                except Skip:
                    pass
            return events

        def fire(j: int) -> None:           # events at minute j: the book shows the prices just before it
            if j not in due:
                return
            self.rp.clock.set(minutes[j])
            self.rp._book.clear()
            for s in by_min:
                self.rp._book[s] = Quote(ltp=last[s], high=hi[s], low=lo[s], open=day_open[s])
            for fn in due.pop(j):
                fn()

        def unprotected() -> bool:
            with connect() as conn:
                return conn.execute("SELECT 1 FROM trades WHERE state = 'FILLED' AND COALESCE(awaiting_user, 0) = 0 "
                                    "AND COALESCE(paused, '') = ''").fetchone() is not None

        i = 0
        fire(0)
        events = cycle(minutes[0])
        while True:
            busy = events > 0 or unprotected()   # after a fill, the next minute (the exit goes on)
            nxt = None
            next_event = min((j for j in due if j > i), default=None)
            if busy and i + 1 < len(minutes):
                nxt = i + 1
            else:
                levels = _levels()
                prev = dict(last)
                for j in range(i + 1, (next_event if next_event is not None else len(minutes))):
                    m = minutes[j]
                    hit = False
                    for s, ls in levels.items():
                        b = by_min.get(s, {}).get(m)
                        if not b:
                            continue
                        lo_j, hi_j = min(prev[s], b[3]), max(prev[s], b[2])
                        if any(lo_j <= x <= hi_j for x in ls):
                            hit = True
                            break
                        prev[s] = b[4]
                    if hit:
                        nxt = j
                        break
                if nxt is None and next_event is not None:
                    nxt = next_event
            if nxt is None:
                break
            for j in range(i + 1, nxt):
                advance(minutes[j])
            i = nxt
            fire(i)
            events = cycle(minutes[i])
        for j in range(i + 1, len(minutes)):   # the rest of the day: nothing crosses a level
            advance(minutes[j])


def _make_signals(strategy: StrategyConfig, cfg: dict[str, Any]) -> dict[str, int]:
    """Re-scan history with this run's strategy, then mark the run's signals tradeable the way the replay does:
    RR at or above the minimum, an allowed band, liquid, clear of results (and the market filter if on)."""
    from .db import connect
    from .strategy import scanner
    app = get_app_config()
    mf_on = bool(app["universe"]["market_filter"].get("enabled", True))
    scanner.scan_history(strategy)
    min_rr = float(strategy.params["levels"]["min_rr"])
    bands = set(cfg["bands"])
    n = tradeable = 0
    with connect() as conn:
        rows = conn.execute("SELECT id, rr, rr_band, gates FROM signals WHERE date BETWEEN ? AND ?",
                            (cfg["start"], cfg["end"])).fetchall()
        for r in rows:
            g = json.loads(r["gates"] or "{}")
            ok = (r["rr"] is not None and r["rr"] >= min_rr - 1e-9 and r["rr_band"] in bands
                  and g.get("liquidity") is True and g.get("results") is not False
                  and (not mf_on or g.get("market") is True))
            conn.execute("UPDATE signals SET source = 'eod', eligible = ? WHERE id = ?", (1 if ok else 0, r["id"]))
            n += 1
            tradeable += ok
        conn.execute("DELETE FROM signals WHERE date NOT BETWEEN ? AND ?", (cfg["start"], cfg["end"]))
    return {"triggers": n, "tradeable": tradeable}


def _day_bars(mconn: sqlite3.Connection, token: int, day: str) -> list[dict[str, Any]]:
    """One session's 5-minute bars from the 1-minute store."""
    from .data import minute
    start = datetime.combine(date.fromisoformat(day), datetime.min.time(), IST).replace(hour=9, minute=15)
    end = start.replace(hour=15, minute=30)
    rows = mconn.execute("SELECT ts, open, high, low, close, volume FROM candles_1m WHERE token = ? AND ts >= ? AND "
                         "ts < ? ORDER BY ts", (token, int(start.timestamp()), int(end.timestamp()))).fetchall()
    bars = [{"date": datetime.fromtimestamp(r[0], IST), "open": r[1] / 100, "high": r[2] / 100, "low": r[3] / 100,
             "close": r[4] / 100, "volume": r[5]} for r in rows]
    return minute.resample(bars, 5)


def _make_intraday_signals(strategy: StrategyConfig, cfg: dict[str, Any], sessions: list[str],
                           mconn: sqlite3.Connection, watchlist: list[str],
                           progress: Any = None) -> tuple[dict[str, int], dict[str, list[tuple]]]:
    """The intraday pullback, day by day as the live 5-minute scan would have seen it: its universe (the watchlist
    you have now, plus that day's N most liquid NSE500 stocks by the previous session's traded value), the first
    valid pullback of each stock (one trade per stock per day), gated by liquidity, the results blackout, the
    allowed RR bands and the market filter if it is on. Returns the counts and {day: [(minute, symbol, signal id,
    tradeable)]}: the minute the candle closed, when the live scan would place the buy."""
    from .db import connect
    from .market_calendar import MarketCalendar
    from .strategy import intraday_pullback as rules, scanner
    from .data import events as events_data
    app = get_app_config()
    u = app["universe"]
    cal = MarketCalendar(app)
    p = strategy.params
    n_top = int(p.get("universe", {}).get("top_liquid", 0) or 0)
    use_wl = bool(p.get("universe", {}).get("watchlist", True))
    min_tv = float(u["min_traded_value_cr"]) * 1e7
    bands = set(cfg["bands"])
    mf_on = bool(u["market_filter"].get("enabled", True))
    plan: dict[str, list[tuple]] = {}
    n = tradeable = 0
    cache: dict[tuple[int, str], list[dict[str, Any]]] = {}
    with connect() as conn:
        known = scanner.sessions(conn, app)
        meetings = events_data.results_dates(conn)
        mf = scanner.nifty_filter(conn, app) if mf_on else {}
        inst = {r[0]: (int(r[1]), float(r[2] or 0.05), bool(r[3])) for r in conn.execute(
            "SELECT symbol, instrument_token, tick_size, is_nse500 FROM instruments WHERE exchange = 'NSE'")}
        for k, day in enumerate(sessions):
            prev_day = cal.previous_trading_day(date.fromisoformat(day)).isoformat()
            syms = set(s for s in watchlist if s in inst) if use_wl else set()
            if n_top:
                syms |= {r[0] for r in conn.execute(
                    "SELECT c.symbol FROM candles_daily c JOIN instruments i ON i.symbol = c.symbol AND i.exchange = 'NSE' "
                    "AND i.is_nse500 = 1 WHERE c.date = ? ORDER BY c.close * c.volume DESC LIMIT ?", (prev_day, n_top))}
            for sym in sorted(syms):
                tok, tick, _ = inst[sym]
                today = cache.pop((tok, day), None) or _day_bars(mconn, tok, day)
                if not today:
                    continue
                prev = cache.get((tok, prev_day)) or _day_bars(mconn, tok, prev_day)
                cache[(tok, day)] = today            # tomorrow's warm-up
                pc = conn.execute("SELECT close FROM candles_daily WHERE symbol = ? AND date = ?", (sym, prev_day)).fetchone()
                if not pc:
                    continue
                found = rules.scan_day(prev, today, float(pc[0]), tick, p)
                if not found:
                    continue
                i, setup = found
                tv = conn.execute("SELECT traded_value_20d FROM indicators_daily WHERE symbol = ? AND date = ?",
                                  (sym, prev_day)).fetchone()
                gates: dict[str, Any] = {"liquidity": bool(tv and tv[0] and tv[0] >= min_tv)}
                blocked, why, _ = scanner.results_check(date.fromisoformat(day), meetings.get(sym, []), known, cal,
                                                        int(u["results_blackout_days"]), int(u["last_year_window_days"]))
                gates["results"] = not blocked
                if mf_on:
                    gates["market"] = bool(mf.get(prev_day))
                ok = all(gates.values()) and setup.rr_band in bands
                ind = setup.indicators() | {"universe": "watchlist" if sym in watchlist else "most liquid"}
                cur = conn.execute(
                    """INSERT OR IGNORE INTO signals (date, symbol, strategy_version, entry_trigger, entry_limit, stop,
                           target, swing_low, rr, rr_band, indicators, eligible, gates, block_reasons, source, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, 'intraday', ?)""",
                    (day, sym, strategy.version, setup.entry_trigger, setup.entry_limit, setup.stop, setup.target,
                     setup.rr, setup.rr_band, json.dumps(ind), 1 if ok else 0, json.dumps(gates),
                     json.dumps([why] if blocked else []), ts()))
                end = (setup.candle_at + timedelta(minutes=rules.CANDLE_MIN)).replace(tzinfo=None)
                plan.setdefault(day, []).append((end, sym, int(cur.lastrowid), ok))
                n += 1
                tradeable += ok
            for key in [x for x in cache if x[1] < day]:
                del cache[key]
            if progress and (k % 10 == 0):
                progress(k, len(sessions))
    return {"triggers": n, "tradeable": tradeable}, plan


def _set_accounts(cfg: dict[str, Any]) -> None:
    from .db import connect
    cap = float(cfg["capital"])
    with connect() as conn:  # the run's own database: the copied paper accounts are replaced by one of its own
        conn.execute("DELETE FROM strategy_portfolios")
        conn.execute("DELETE FROM accounts")
        conn.execute("INSERT INTO accounts (id, name, mode, capital, peak_equity, manual_edits, created_at, cash) "
                     "VALUES (?, 'Backtest', 'paper', ?, ?, 0, ?, ?)", (ACCOUNT_ID, cap, cap, ts(), cap))


def _skip_kind(reason: str | None) -> str:
    r = (reason or "").lower()
    if "minimum gap" in r:
        return "Price within Kite's minimum trigger gap (0.25%) when placed"
    if "already past the trigger" in r:
        return "Price already past the trigger when placed"
    if "no free slot" in r:
        return "No free slot (5 positions incl. pending buys)"
    if "already held" in r or "bought once" in r:
        return "Stock already held or pending"
    if "sector cap" in r:
        return "Sector cap"
    if "free cash" in r:
        return "Not enough free cash"
    if "no live price" in r:
        return "No price to check the trigger"
    return (reason or "other")[:60]


def _details() -> dict[str, Any]:
    """Equity curves, the trade list and why tradeable signals were not placed, for the report."""
    from collections import Counter
    from .db import connect
    out: dict[str, Any] = {"equity": {}, "trades": []}
    with connect() as conn:
        out["not_placed"] = dict(Counter(_skip_kind(r[0]) for r in conn.execute(
            "SELECT skip_reason FROM candidates WHERE taken = 0")).most_common())
        for r in conn.execute("SELECT account_id, date, equity FROM equity_daily ORDER BY date"):
            out["equity"].setdefault(r[0], []).append({"date": r[1], "equity": round(r[2], 2)})
        for r in conn.execute("""SELECT t.id, t.account_id, t.symbol, t.state, t.qty, t.sizing_case, t.rr_band, t.rr,
                                        t.entry_trigger, t.entry_limit, t.initial_stop, t.stop, t.target, t.entry_price,
                                        t.entry_at, t.exit_reason, t.exits, t.closed_at, t.net_pnl, t.r_multiple,
                                        t.charges, s.date AS signal_date
                                 FROM trades t LEFT JOIN signals s ON s.id = t.signal_id ORDER BY t.id"""):
            d = dict(r)
            d["exits"] = json.loads(d["exits"] or "[]")
            out["trades"].append(d)
    return out


def _intraday_events(day: str, plan: dict[str, list[tuple]], strategy: StrategyConfig) -> dict[datetime, Any]:
    """The live intraday jobs inside one replayed session: each tradeable pullback places its buy at the minute its
    candle closed, and at 15:15 unfilled buys are cancelled."""
    from .orders import manager
    from .strategy import intraday_pullback as rules
    by_min: dict[datetime, list[int]] = {}
    for end, _sym, sid, ok in plan.get(day, []):
        if ok:
            by_min.setdefault(end, []).append(sid)
    out: dict[datetime, Any] = {m: (lambda ids=ids: manager.place_intraday(day, {strategy.version: ids}, strategy))
                                for m, ids in by_min.items()}
    cut = rules.TEMPLATE["entry"]["cancel_at"]
    out[datetime.fromisoformat(f"{day}T{cut}")] = lambda: manager.cancel_buys(
        [strategy.version], None, f"intraday buy not triggered by {cut}", reason="intraday_cutoff")
    return out


def run(run_id: int, live_db: Path) -> dict[str, Any]:
    """Entry point of the backtest process."""
    from . import db, replay
    from .orders import manager
    t0 = time.monotonic()
    run_row = get_run(live_db, run_id)
    if run_row is None:
        raise BacktestError(f"No backtest run {run_id}")
    cfg = run_row["config"]
    update_run(live_db, run_id, status="running", started_at=ts(), message="Copying data and re-scanning signals…",
               progress=0.01)
    out_dir = Path(live_db).parent / "backtests"   # beside the live database (runtime\\backtests on the laptop)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_db = out_dir / f"bt_{run_id}.db"
    strategy = build_strategy(cfg)
    intraday = _is_intraday(strategy.params)
    live_strategy_fn = manager._strategy
    manager._strategy = lambda: strategy          # this process only: the engine trades the what-if strategy
    try:
        watchlist: list[str] = []
        if intraday:                              # the watchlist you have now (it has no history)
            from .data import watchlist as wl_data
            watchlist = sorted(wl_data.active())
        sessions = replay._prepare(out_db, Path(live_db), cfg["start"], cfg["end"], strategy=strategy,
                                   copy_signals=False)
        if not sessions:
            raise BacktestError("No trading sessions in that range")
        mconn = sqlite3.connect(f"file:{minute_db_path(Path(live_db))}?mode=ro", uri=True, timeout=30)
        plan: dict[str, list[tuple]] = {}
        if intraday:
            sig, plan = _make_intraday_signals(
                strategy, cfg, sessions, mconn, watchlist,
                lambda k, total: update_run(live_db, run_id, progress=round(0.01 + 0.2 * k / total, 4),
                                            message=f"Finding pullbacks on 5-minute candles: day {k + 1} of {total}…"))
        else:
            sig = _make_signals(strategy, cfg)
        _set_accounts(cfg)
        with db.connect() as conn:
            tokens = {r[0]: r[1] for r in conn.execute("SELECT symbol, instrument_token FROM instruments")}
        start_p = 0.21 if intraday else 0.05
        update_run(live_db, run_id, message=f"{sig['tradeable']} tradeable signals; replaying {len(sessions)} sessions…",
                   progress=start_p)
        with replay.Replay(lambda day: {}) as rp:
            md = MinuteDay(rp, mconn, tokens)
            for n, day in enumerate(sessions, 1):
                if intraday:
                    md.run(day, extra_symbols=[x[1] for x in plan.get(day, [])], events=_intraday_events(day, plan, strategy))
                else:
                    md.run(day)
                rp.clock.set(replay.eod_time(day))
                manager.eod_maintenance(day)
                if not intraday:
                    manager.place_entries(day, strategy)
                replay._rearm_halted(day)
                if n == 1 or n % 5 == 0 or n == len(sessions):
                    status = get_run(live_db, run_id)["status"]
                    if status == "cancelling":
                        update_run(live_db, run_id, status="cancelled", finished_at=ts(),
                                   message=f"Cancelled at {day} ({n} of {len(sessions)} sessions)")
                        return {}
                    update_run(live_db, run_id, progress=round(start_p + (0.98 - start_p) * n / len(sessions), 4),
                               message=f"{day} · session {n} of {len(sessions)}")
        mconn.close()
        rep = replay.report()
        rep.update(_details())
        rep.update({"start": cfg["start"], "end": cfg["end"], "sessions": len(sessions), "signals": sig,
                    "minute_stock_days": md.minute_days, "fallback_stock_days": md.fallback_days,
                    "seconds": round(time.monotonic() - t0, 1), "config": cfg})
        update_run(live_db, run_id, status="done", progress=1, finished_at=ts(), report=json.dumps(rep, default=str),
                   message=f"Done in {rep['seconds']:.0f} s")
        return rep
    except Exception as exc:
        update_run(live_db, run_id, status="error", finished_at=ts(), message=f"{type(exc).__name__}: {exc}"[:500])
        raise
    finally:
        manager._strategy = live_strategy_fn
        db.set_db_path(Path(live_db))
        for suffix in ("", "-wal", "-shm", "-journal"):   # the run's own database is scratch: the report holds the results
            p = Path(str(out_db) + suffix)
            try:
                if p.exists():
                    p.unlink()
            except OSError:
                pass
