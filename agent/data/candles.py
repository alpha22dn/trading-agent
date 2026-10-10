"""Daily candle cache: 2-year backfill and incremental refresh from Kite.

- Only completed sessions are stored (never today's partial candle before eod_after).
- Append-only: existing rows are never edited. The one exception is a detected
  split/bonus adjustment (Kite back-adjusts history): the overlap check sees old
  closes no longer match, raises an alert and re-downloads that symbol's history.
- Respects Kite's historical rate limit (config kite.historical_requests_per_sec).
- Fails closed: a rejected token aborts the whole run.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .. import audit
from ..broker.kite_session import SessionUnavailable, get_kite, invalidate, is_token_error
from ..clock import IST, now_ist, ts
from ..db import connect, get_state, set_state
from ..events import publish
from ..market_calendar import MarketCalendar
from ..settings import get_app_config

MAX_DAYS_PER_REQUEST = 1900  # Kite allows ~2000 days of day candles per call
STATE_KEY = "backfill"
# A change to the newest stored day's close up to this size is Kite replacing its provisional close with the
# official NSE close (seen: 0.3–2.4%). A split or bonus moves all history, typically by 33% or more.
REVISION_MAX_PCT = 5.0

_run_lock = threading.Lock()
_sleep: Callable[[float], None] = time.sleep


class AlreadyRunning(RuntimeError):
    pass


class RateLimiter:
    def __init__(self, per_sec: float):
        self.min_interval = 1.0 / per_sec
        self._last = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            gap = time.monotonic() - self._last
            if gap < self.min_interval:
                _sleep(self.min_interval - gap)
            self._last = time.monotonic()


_shared: RateLimiter | None = None


def shared_limiter(per_sec: float) -> RateLimiter:
    """One limiter for every historical-data caller (daily and 1-minute downloads can overlap, e.g. a long
    1-minute download while the EOD run refreshes daily candles), so together they stay under Kite's limit."""
    global _shared
    if _shared is None or abs(_shared.min_interval - 1.0 / per_sec) > 1e-9:
        _shared = RateLimiter(per_sec)
    return _shared


@dataclass
class Target:
    symbol: str
    token: int


@dataclass
class RunStats:
    total: int = 0
    done: int = 0
    updated: int = 0
    skipped: int = 0
    revised: int = 0
    adjusted: list[str] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)
    rows_added: int = 0


def _to_date(value: Any) -> date:
    if isinstance(value, datetime):
        return (value.astimezone(IST) if value.tzinfo else value).date()
    if isinstance(value, date):
        return value
    return datetime.fromisoformat(str(value)).date()


def targets() -> list[Target]:
    cfg = get_app_config()["kite"]
    with connect() as conn:
        rows = conn.execute(
            "SELECT symbol, instrument_token FROM instruments WHERE is_nse500 = 1 ORDER BY symbol").fetchall()
    out = [Target(cfg["nifty50_symbol"], int(cfg["nifty50_token"]))]
    out += [Target(r["symbol"], int(r["instrument_token"])) for r in rows]
    return out


def _fetch(kite, limiter: RateLimiter, token: int, start: date, end: date) -> list[dict[str, Any]]:
    """All day candles in [start, end], chunked, with retries on transient errors."""
    from kiteconnect import exceptions as kex

    out: list[dict[str, Any]] = []
    chunk_start = start
    while chunk_start <= end:
        chunk_end = min(end, chunk_start + timedelta(days=MAX_DAYS_PER_REQUEST))
        for attempt in range(4):
            limiter.wait()
            try:
                out += kite.historical_data(token, chunk_start, chunk_end, "day")
                break
            except (kex.NetworkException, kex.DataException, kex.GeneralException):
                if attempt == 3:
                    raise
                _sleep(2 ** attempt)
        chunk_start = chunk_end + timedelta(days=1)
    return out


def _insert(conn, symbol: str, token: int, candles: list[dict[str, Any]], upto: date) -> int:
    now = ts()
    added = 0
    for c in candles:
        d = _to_date(c["date"])
        if d > upto:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO candles_daily (symbol, date, open, high, low, close, volume, "
            "instrument_token, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (symbol, d.isoformat(), float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]),
             int(c.get("volume") or 0), token, now))
        added += cur.rowcount
    return added


def floor_date(upto: date, years: float) -> date:
    return upto - timedelta(days=int(365 * years) + 1)


def refresh_symbol(kite, limiter: RateLimiter, t: Target, upto: date, years: float,
                   overlap: int, tolerance_pct: float, extend_back: bool = False) -> tuple[str, int]:
    """Bring one symbol up to `upto`. Returns (outcome, rows_added); outcome in skipped/updated/adjusted.
    extend_back: also fetch older history when the configured window grew (backfill_years raised)."""
    floor = floor_date(upto, years)
    extended = 0
    if extend_back:
        with connect() as conn:
            first = conn.execute("SELECT MIN(date) FROM candles_daily WHERE symbol = ?", (t.symbol,)).fetchone()[0]
        if first and date.fromisoformat(first) > floor + timedelta(days=7):
            older = _fetch(kite, limiter, t.token, floor, date.fromisoformat(first) - timedelta(days=1))
            with connect() as conn:
                extended = _insert(conn, t.symbol, t.token, older, upto)
    with connect() as conn:
        last_rows = conn.execute(
            "SELECT date, close FROM candles_daily WHERE symbol = ? ORDER BY date DESC LIMIT ?",
            (t.symbol, overlap)).fetchall()
    if last_rows and date.fromisoformat(last_rows[0]["date"]) >= upto:
        return ("updated", extended) if extended else ("skipped", 0)

    start = date.fromisoformat(last_rows[-1]["date"]) if last_rows else floor
    candles = _fetch(kite, limiter, t.token, start, upto)

    revised = False
    if last_rows:
        stored = {r["date"]: float(r["close"]) for r in last_rows}
        latest = last_rows[0]["date"]
        for c in candles:
            key = _to_date(c["date"]).isoformat()
            old = stored.get(key)
            if not old:
                continue
            change = abs(float(c["close"]) - old) / old * 100
            if key == latest and change <= REVISION_MAX_PCT:
                # The newest stored day was saved with Kite's provisional close; it now has the official NSE
                # close. A correction, not a split: update that day in place, no alert (decided 2026-09-24).
                with connect() as conn:  # always take Kite's latest version of that day (OHLC and volume)
                    conn.execute("UPDATE candles_daily SET open = ?, high = ?, low = ?, close = ?, volume = ?, "
                                 "fetched_at = ? WHERE symbol = ? AND date = ?",
                                 (float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"]),
                                  int(c.get("volume") or 0), ts(), t.symbol, key))
                revised = revised or change > 0
                continue
            if change > tolerance_pct:
                full = _fetch(kite, limiter, t.token, floor, upto)
                with connect() as conn:
                    conn.execute("DELETE FROM candles_daily WHERE symbol = ?", (t.symbol,))
                    added = _insert(conn, t.symbol, t.token, full, upto)
                audit.alert("price_adjustment",
                            f"{t.symbol}: past prices changed on {key} ({old} → {c['close']}). Likely a split "
                            "or bonus; history re-downloaded. Check any open position in it.",
                            details={"symbol": t.symbol, "date": key, "old": old, "new": c["close"]})
                return "adjusted", added

    with connect() as conn:
        added = _insert(conn, t.symbol, t.token, candles, upto)
    return ("revised" if revised else "updated"), added + extended


def is_running() -> bool:
    return _run_lock.locked()


def mark_interrupted() -> None:
    """On startup: a 'running' marker left by a crash is closed off (the next run resumes)."""
    st = get_state(STATE_KEY)
    if st and st.get("running") and not is_running():
        st.update(running=False, finished_at=ts(), aborted="Interrupted by a restart; run again to resume")
        set_state(STATE_KEY, st)


def run_backfill(symbols: list[str] | None = None) -> dict[str, Any]:
    """Backfill / refresh every NSE500 stock plus Nifty 50. One run at a time."""
    if not _run_lock.acquire(blocking=False):
        raise AlreadyRunning("A candle download is already running")
    try:
        return _run(symbols)
    finally:
        _run_lock.release()


def _run(symbols: list[str] | None) -> dict[str, Any]:
    app = get_app_config()
    cal = MarketCalendar(app)
    kite = get_kite()  # raises SessionUnavailable before we touch anything
    upto = cal.last_completed_session(now_ist())
    tlist = targets()
    if symbols:
        wanted = set(symbols)
        tlist = [t for t in tlist if t.symbol in wanted]
    limiter = shared_limiter(float(app["kite"]["historical_requests_per_sec"]))
    data_cfg = app["data"]
    stats = RunStats(total=len(tlist))
    started = ts()
    years = float(data_cfg["backfill_years"])
    floor = floor_date(upto, years).isoformat()
    done_floor = get_state("history_floor")
    extend_back = bool(done_floor) and floor < done_floor
    if done_floor is None:
        with connect() as conn:  # existing cache from before this marker existed
            extend_back = conn.execute("SELECT COUNT(*) FROM candles_daily").fetchone()[0] > 0

    def save(running: bool, current: str | None = None, aborted: str | None = None) -> None:
        set_state(STATE_KEY, {
            "running": running, "total": stats.total, "done": stats.done, "updated": stats.updated,
            "skipped": stats.skipped, "adjusted": stats.adjusted, "errors": stats.errors[-50:],
            "error_count": len(stats.errors), "rows_added": stats.rows_added, "current": current,
            "upto": upto.isoformat(), "started_at": started, "finished_at": None if running else ts(),
            "aborted": aborted,
        })
        publish("backfill")

    save(True)
    audit.record("job", "candle_backfill", f"Started for {stats.total} symbols up to {upto}")
    for t in tlist:
        save(True, t.symbol)
        try:
            outcome, added = refresh_symbol(kite, limiter, t, upto, years, int(data_cfg["overlap_days"]),
                                            float(data_cfg["adjustment_tolerance_pct"]), extend_back)
            stats.rows_added += added
            if outcome == "skipped":
                stats.skipped += 1
            else:
                stats.updated += 1
                if outcome == "adjusted":
                    stats.adjusted.append(t.symbol)
                elif outcome == "revised":
                    stats.revised += 1
        except Exception as exc:  # noqa: BLE001 - per-symbol isolation; token errors abort
            if is_token_error(exc):
                invalidate("token rejected during candle download")
                save(False, aborted="Kite session rejected")
                audit.record("job", "candle_backfill", "Aborted: Kite session rejected", "error")
                raise SessionUnavailable("Kite session rejected") from exc
            stats.errors.append({"symbol": t.symbol, "error": str(exc)[:200]})
        stats.done += 1

    save(False)
    if not symbols and not stats.errors:
        set_state("history_floor", floor)
    level = "warning" if stats.errors else "info"
    msg = (f"{stats.updated} updated, {stats.skipped} already current, {len(stats.errors)} failed, "
           f"{stats.rows_added} candles added"
           + (f", {stats.revised} previous closes corrected to the official NSE close" if stats.revised else ""))
    audit.record("job", "candle_backfill", msg, level, details={"errors": stats.errors[:20]})
    if stats.errors:
        audit.alert("candle_backfill", f"Candle download: {len(stats.errors)} symbol(s) failed. "
                    "Re-run the download; failed symbols are retried.", details={"errors": stats.errors[:20]})
    return get_state(STATE_KEY)


def coverage() -> dict[str, Any]:
    """How complete the cache is: the Phase 0 'done when' check."""
    app = get_app_config()
    cal = MarketCalendar(app)
    upto = cal.last_completed_session(now_ist())
    nifty = app["kite"]["nifty50_symbol"]
    with connect() as conn:
        rows = conn.execute(
            """SELECT i.symbol, MIN(c.date) AS first, MAX(c.date) AS last, COUNT(c.date) AS n
               FROM instruments i LEFT JOIN candles_daily c ON c.symbol = i.symbol
               WHERE i.is_nse500 = 1 GROUP BY i.symbol""").fetchall()
        nrow = conn.execute("SELECT MIN(date) AS first, MAX(date) AS last, COUNT(*) AS n FROM candles_daily "
                            "WHERE symbol = ?", (nifty,)).fetchone()
        total_rows = conn.execute("SELECT COUNT(*) FROM candles_daily").fetchone()[0]
    missing = [r["symbol"] for r in rows if r["n"] == 0]
    stale = [r["symbol"] for r in rows if r["n"] and r["last"] < upto.isoformat()]
    current = len(rows) - len(missing) - len(stale)
    return {
        "upto": upto.isoformat(),
        "nse500_symbols": len(rows),
        "current": current,
        "stale": stale,
        "missing": missing,
        "complete": bool(rows) and current == len(rows) and (nrow["last"] or "") >= upto.isoformat(),
        "earliest": min((r["first"] for r in rows if r["first"]), default=None),
        "nifty": dict(nrow),
        "total_rows": int(total_rows),
    }
