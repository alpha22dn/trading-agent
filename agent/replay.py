"""Replay mode (spec section 9): run the same engine over a historical date range, day by day, deterministically.

A replay works on its own database (runtime/replays/...), never the live one. It copies the reference data
(instruments, candles, indicators, results calendar, the historical signal scan) from the live database, then for
every session:

  09:16  open            the monitor sees LTP = open (GTTs armed before the open see the open)
  11:00  first extreme   LTP = the low on an up day (open→low→high→close), the high on a down day
  13:30  second extreme  LTP = the other extreme; day high/low so far are known
  15:25  close           LTP = close, day high/low final
  Each point is seen twice, a minute apart, so an exit goes on at the next cycle after a fill.
  21:00  EOD             trade maintenance (session end, exits, expiry, trailing, equity), then buy GTTs

The order monitor, PaperBroker, sizing, ranking, trailing and all exit rules are the live code.

Limits: fundamentals can't be replayed (no historical Screener data), so the watchlist and valuation gates are
skipped; signals must pass RR, liquidity and the results blackout (and the market filter if it is on). Prices
inside a day follow the fixed open→low→high→close path above, so the order of intraday moves is assumed.
"""
from __future__ import annotations

import json
import statistics
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from . import clock, db
from .broker import feed
from .broker.base import Quote
from .settings import RUNTIME_DIR, get_app_config, load_strategy_config


def eod_time(day: str) -> datetime:
    """When the EOD run happens on `day`: 5 minutes after market.eod_after (21:00 since 2026-09-24)."""
    from .market_calendar import MarketCalendar
    return datetime.combine(date.fromisoformat(day), MarketCalendar(get_app_config()).eod_t) + timedelta(minutes=5)

# Each price point is seen twice, a minute apart, so an exit is attached "on the next cycle" after a fill,
# as the live monitor does, before the price moves on.
CHECKPOINTS = [("09:16", 0), ("09:17", 0), ("11:00", 1), ("11:01", 1), ("13:30", 2), ("13:31", 2),
               ("15:25", 3), ("15:26", 3)]


def day_path(o: float, h: float, l: float, c: float) -> list[tuple[float, float, float]]:
    """(ltp, high so far, low so far) at each checkpoint, for one daily candle."""
    first, second = (l, h) if c >= o else (h, l)
    pts = [o, first, second, c]
    out, hi, lo = [], o, o
    for p in pts:
        hi, lo = max(hi, p), min(lo, p)
        out.append((p, hi, lo))
    return out


class Replay:
    """Steps the live engine through sessions. `candles(day)` gives {symbol: (open, high, low, close)}."""

    def __init__(self, candles: Callable[[str], dict[str, tuple[float, float, float, float]]]):
        from .orders import manager
        self.manager = manager
        self.candles = candles
        self.clock = clock.FixedClock(datetime(2000, 1, 1))
        self._book: dict[str, Quote] = {}

    def __enter__(self) -> "Replay":
        self._prev_clock = clock.get_clock()
        self._prev_source = feed.quote_source
        clock.set_clock(self.clock)
        feed.quote_source = lambda syms: {s: self._book[s] for s in syms if s in self._book}
        return self

    def __exit__(self, *exc) -> None:
        clock.set_clock(self._prev_clock)
        feed.quote_source = self._prev_source

    def run_day(self, day: str, entries: bool = True, before_eod: Callable[[], None] | None = None) -> dict[str, Any]:
        from .scheduler import Skip
        bars = self.candles(day)
        paths = {s: day_path(*b[:4]) for s, b in bars.items()}
        circuits = {s: (b[4], b[5]) for s, b in bars.items() if len(b) >= 6}   # optional (upper, lower) limits
        for hhmm, i in CHECKPOINTS:
            self.clock.set(datetime.fromisoformat(f"{day}T{hhmm}"))
            self._book.clear()
            for s, p in paths.items():
                ltp, hi, lo = p[i]
                uc, lc = circuits.get(s, (None, None))
                self._book[s] = Quote(ltp=ltp, high=hi, low=lo, upper_circuit=uc, lower_circuit=lc, open=p[0][0])
            try:
                self.manager.on_cycle()
            except Skip:
                pass
        if before_eod:
            before_eod()
        self.clock.set(eod_time(day))
        out = {"maintenance": self.manager.eod_maintenance(day)}
        if entries:
            out["entries"] = self.manager.place_entries(day)
        return out


# ---------- historical replay on a copy of the live data ----------

COPY_TABLES = ["instruments", "candles_daily", "indicators_daily", "events"]


def _prepare(out_db: Path, source_db: Path, start: str, end: str, strategy=None, copy_signals: bool = True) -> list[str]:
    """copy_signals=False: the caller builds its own signals (a backtest with what-if settings re-scans)."""
    app = get_app_config()
    strategy = strategy or load_strategy_config(app["strategy"]["active_version"])
    if out_db.exists():
        out_db.unlink()
    for suffix in ("-wal", "-shm", "-journal"):   # a stale journal could be replayed into the new file
        p = Path(str(out_db) + suffix)
        if p.exists():
            p.unlink()
    db.set_db_path(out_db)
    db.init_db(app, strategy)
    mf_on = bool(app["universe"]["market_filter"].get("enabled", True))
    with db.connect() as conn:
        conn.execute("ATTACH DATABASE ? AS src", (str(source_db),))
        for t in COPY_TABLES:
            cols = [r[1] for r in conn.execute(f"PRAGMA src.table_info({t})")]
            mine = {r[1] for r in conn.execute(f"PRAGMA main.table_info({t})")}
            cols = [c for c in cols if c in mine]
            where = " WHERE date <= ?" if t in ("candles_daily", "indicators_daily") else ""
            conn.execute(f"INSERT INTO main.{t} ({','.join(cols)}) SELECT {','.join(cols)} FROM src.{t}{where}",
                         (end,) if where else ())
        # the results-history status tells the scanner which dates the blackout can be checked for
        conn.execute("INSERT OR REPLACE INTO main.app_state SELECT * FROM src.app_state WHERE key = 'results_calendar'")
        rows = conn.execute("SELECT * FROM src.signals WHERE strategy_version = ? AND date BETWEEN ? AND ?",
                            (strategy.version, start, end)).fetchall() if copy_signals else []
        for r in rows:
            g = json.loads(r["gates"] or "{}")
            ok = (r["rr_band"] is not None and g.get("liquidity") is True and g.get("results") is not False
                  and (not mf_on or g.get("market") is True))
            conn.execute(
                "INSERT INTO signals (date, symbol, strategy_version, entry_trigger, entry_limit, stop, target, "
                "swing_low, rr, rr_band, indicators, eligible, gates, block_reasons, source, created_at) VALUES "
                "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'eod', ?)",
                (r["date"], r["symbol"], r["strategy_version"], r["entry_trigger"], r["entry_limit"], r["stop"],
                 r["target"], r["swing_low"], r["rr"], r["rr_band"], r["indicators"], 1 if ok else 0, r["gates"],
                 r["block_reasons"], r["created_at"]))
        sessions = [r[0] for r in conn.execute(
            "SELECT DISTINCT date FROM candles_daily WHERE symbol = ? AND date BETWEEN ? AND ? ORDER BY date",
            (app["kite"]["nifty50_symbol"], start, end))]
    # Weekdays with no index candle were market holidays: tell the calendar, so "2 sessions" counts real sessions.
    have = set(sessions)
    hol = app.raw.setdefault("holidays", {})
    d = date.fromisoformat(start)
    while d <= date.fromisoformat(end):
        if d.weekday() < 5 and d.isoformat() not in have:
            hol.setdefault(d.isoformat(), "No session (replay)")
        d += timedelta(days=1)
    return sessions


def _journal(day: str, next_day: str | None) -> None:
    from .journal import trades as trade_journal, weekly
    trade_journal.sync(use_llm=False)
    d = date.fromisoformat(day)
    if next_day is None or weekly.week_of(date.fromisoformat(next_day))[0] != weekly.week_of(d)[0]:
        weekly.run(weekly.week_of(d)[0], use_llm=False)


def _candles_from_db(day: str) -> dict[str, tuple[float, float, float, float]]:
    with db.connect() as conn:
        return {r[0]: (r[1], r[2], r[3], r[4]) for r in conn.execute(
            "SELECT symbol, open, high, low, close FROM candles_daily WHERE date = ?", (day,))}


def _rearm_halted(day: str) -> list[str]:
    """Replay stands in for the user: a halted account is re-armed at once so the test covers the whole range.
    Each halt is recorded for the report."""
    from .risk import controls
    with db.connect() as conn:
        halted = [r[0] for r in conn.execute("SELECT id FROM accounts WHERE halted = 1")]
    for acc in halted:
        controls.rearm_account(acc)
    if halted:
        from .db import get_state, set_state
        log = get_state("replay_halts", [])
        log += [{"date": day, "account": a} for a in halted]
        set_state("replay_halts", log)
    return halted


def run(start: str, end: str, source_db: Path | None = None, out_db: Path | None = None,
        progress: Callable[[str, int, int], None] | None = None, rearm_after_halt: bool = True,
        journal: bool = True) -> dict[str, Any]:
    """rearm_after_halt: a live account stays halted until you re-arm it; in a replay nobody is there, so by
    default the account is re-armed the same evening (the halt is still counted in the report).
    journal: journal each closed trade at the session's EOD and write a weekly review after each week's last
    session, as the live agent does (template text; no LLM calls in a replay)."""
    source_db = source_db or db.current_db_path()
    out_dir = RUNTIME_DIR / "replays"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_db = out_db or out_dir / f"replay_{start}_{end}.db"
    if Path(out_db).resolve() == Path(source_db).resolve():
        raise ValueError("A replay never writes to the live database")
    sessions = _prepare(Path(out_db), Path(source_db), start, end)
    with Replay(_candles_from_db) as rp:
        for i, day in enumerate(sessions, 1):
            rp.run_day(day)
            if rearm_after_halt:
                _rearm_halted(day)
            if journal:
                _journal(day, sessions[i] if i < len(sessions) else None)
            if progress:
                progress(day, i, len(sessions))
    rep = report()
    rep.update({"start": start, "end": end, "sessions": len(sessions), "db": str(out_db)})
    if journal:
        with db.connect() as conn:
            rep["journal"] = {"trades": conn.execute("SELECT COUNT(*) FROM journal WHERE kind = 'trade'").fetchone()[0],
                              "weeks": conn.execute("SELECT COUNT(*) FROM journal WHERE kind = 'week'").fetchone()[0],
                              "closed_trades": conn.execute("SELECT COUNT(*) FROM trades WHERE state = 'CLOSED' AND "
                                                            "entry_price IS NOT NULL").fetchone()[0]}
    Path(str(out_db).replace(".db", ".json")).write_text(json.dumps(rep, indent=2, default=str))
    return rep


def report() -> dict[str, Any]:
    """Per-account results of the database currently in use (a replay DB after `run`)."""
    from .db import get_state
    halts = get_state("replay_halts", [])
    out: dict[str, Any] = {"accounts": {}}
    with db.connect() as conn:
        accts = [dict(r) for r in conn.execute("SELECT id, capital FROM accounts ORDER BY capital")]
        for a in accts:
            tr = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE account_id = ? ORDER BY id", (a["id"],))]
            closed = [t for t in tr if t["state"] == "CLOSED"]
            eq = [dict(r) for r in conn.execute("SELECT date, equity, peak_equity FROM equity_daily WHERE account_id = ? "
                                                "ORDER BY date", (a["id"],))]
            rs = [t["r_multiple"] for t in closed if t["r_multiple"] is not None]
            max_dd = max([(e["peak_equity"] - e["equity"]) / e["peak_equity"] * 100 for e in eq] or [0])
            final = eq[-1]["equity"] if eq else a["capital"]

            def group(key):
                g: dict[str, list[float]] = {}
                for t in closed:
                    if t["r_multiple"] is not None:
                        g.setdefault(str(t[key]), []).append(t["r_multiple"])
                return {k: {"trades": len(v), "win_pct": round(100 * sum(x > 0 for x in v) / len(v), 1),
                            "avg_r": round(statistics.mean(v), 3)} for k, v in sorted(g.items())}

            out["accounts"][a["id"]] = {
                "capital": a["capital"], "final_equity": round(final, 2),
                "return_pct": round((final / a["capital"] - 1) * 100, 2), "max_drawdown_pct": round(max_dd, 2),
                "trades_placed": len(tr), "closed": len(closed),
                "cancelled": sum(1 for t in tr if t["state"] == "CANCELLED"),
                "open": sum(1 for t in tr if t["state"] not in ("CLOSED", "CANCELLED")),
                "win_pct": round(100 * sum(r > 0 for r in rs) / len(rs), 1) if rs else None,
                "avg_r": round(statistics.mean(rs), 3) if rs else None,
                "net_pnl": round(sum(t["net_pnl"] or 0 for t in closed), 2),
                "charges": round(sum(t["charges"] or 0 for t in closed), 2),
                "by_band": group("rr_band"), "by_sizing": group("sizing_case"), "by_exit": group("exit_reason"),
                "halted": bool(conn.execute("SELECT halted FROM accounts WHERE id = ?", (a["id"],)).fetchone()[0]),
                "halts": [h["date"] for h in halts if h["account"] == a["id"]],
            }
    return out
