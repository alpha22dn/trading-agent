"""1-minute candle store: every NSE500 stock and the Nifty 50, from which any timeframe is built.

Kept in its own SQLite file next to the main database (runtime/agent_1m.db) because it is large
(about 190,000 bars a trading day, roughly 1.5–2 GB a year): the main database stays small and the daily
backup does not copy it. To keep it compact, rows are keyed by instrument token and prices are stored in
paise (integers).

- First run: downloads `data.minute_history_days` of history (Kite serves 60 days of 1-minute bars per call).
- Afterwards: a job after the close adds the new sessions. Only completed sessions are stored.
- Resumable: each stock continues from its last stored minute, so a stopped download picks up where it left off.
- Older history (decided 2026-10-01: "as far back as Kite provides"): after the new sessions, each run extends every
  stock backwards in 60-day chunks until Kite returns nothing older (Kite's minute data starts around early 2015)
  or `data.minute_history_from` is reached. Progress per stock is kept in `history_floor`, so it resumes after a
  pause or restart. It never runs between 08:45 and 16:00 on a trading day, so it cannot slow the intraday scan,
  which shares the historical-data rate limit; it carries on after the next close.
- Chart display and research only; trading decisions use daily candles.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .. import audit
from ..broker.kite_session import SessionUnavailable, get_kite, invalidate, is_token_error
from ..clock import IST, now_ist, ts
from ..db import current_db_path, get_state, set_state
from ..events import publish
from ..market_calendar import MarketCalendar
from ..settings import get_app_config
from .candles import shared_limiter, targets

STATE_KEY = "minute_backfill"
DAYS_PER_REQUEST = 60          # Kite's limit for 1-minute history per call
SESSION_START = dtime(9, 15)
SESSION_END = dtime(15, 30)

SCHEMA = """
CREATE TABLE IF NOT EXISTS candles_1m (
    token  INTEGER NOT NULL,
    ts     INTEGER NOT NULL,   -- bar start, Unix seconds
    open   INTEGER NOT NULL,   -- prices in paise
    high   INTEGER NOT NULL,
    low    INTEGER NOT NULL,
    close  INTEGER NOT NULL,
    volume INTEGER NOT NULL,
    PRIMARY KEY (token, ts)
) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS history_floor (
    token      INTEGER PRIMARY KEY,
    done       INTEGER NOT NULL DEFAULT 0,   -- 1 = nothing older at Kite (or the configured start reached)
    earliest   INTEGER,                      -- earliest stored bar, epoch seconds
    updated_at TEXT
);
"""

_run_lock = threading.Lock()
_stop = threading.Event()   # "pause after this stock": checked between stocks, never mid-stock


def db_path() -> Path:
    main = current_db_path()
    return main.with_name(main.stem + "_1m.db")


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.executescript(SCHEMA)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _paise(x: float) -> int:
    return int(round(float(x) * 100))


def _epoch(d: datetime) -> int:
    return int((d if d.tzinfo else d.replace(tzinfo=IST)).timestamp())


# ---------- download ----------

class AlreadyRunning(RuntimeError):
    pass


def is_running() -> bool:
    return _run_lock.locked()


def request_stop() -> bool:
    """Finish the stock being downloaded, then stop. The next run (the EOD run, the 22:30 job or the
    button) resumes with the remaining stocks."""
    if not is_running():
        return False
    _stop.set()
    set_state(STATE_KEY, (get_state(STATE_KEY) or {}) | {"stopping": True})
    publish("minute")
    return True


def run(days: int | None = None) -> dict[str, Any]:
    if not _run_lock.acquire(blocking=False):
        raise AlreadyRunning("A 1-minute download is already running")
    try:
        return _run(days)
    finally:
        _run_lock.release()


def _run(days: int | None) -> dict[str, Any]:
    _stop.clear()
    app = get_app_config()
    cal = MarketCalendar(app)
    kite = get_kite()  # SessionUnavailable before anything is touched
    upto = cal.last_completed_session(now_ist())
    end_dt = datetime.combine(upto, SESSION_END, IST)
    days = int(days or app["data"].get("minute_history_days", 365))
    floor_dt = datetime.combine(upto - timedelta(days=days), SESSION_START, IST)
    limiter = shared_limiter(float(app["kite"]["historical_requests_per_sec"]))
    tlist = targets()
    st: dict[str, Any] = {"running": True, "total": len(tlist), "done": 0, "rows_added": 0, "current": None,
                          "errors": [], "upto": upto.isoformat(), "from": floor_dt.date().isoformat(),
                          "started_at": ts(), "finished_at": None, "aborted": None}

    def save() -> None:
        set_state(STATE_KEY, st | {"errors": st["errors"][-50:], "error_count": len(st["errors"])})
        publish("minute")

    save()
    audit.record("job", "minute_candles", f"Started: {len(tlist)} symbols, {floor_dt.date()} → {upto}")
    with connect() as conn:
        last = {int(r[0]): int(r[1]) for r in conn.execute("SELECT token, MAX(ts) FROM candles_1m GROUP BY token")}
    for t in tlist:
        if _stop.is_set():
            _stop.clear()
            left = st["total"] - st["done"]
            st.update(running=False, current=None, finished_at=ts(),
                      aborted=f"paused by you after {st['done']} of {st['total']} stocks; {left} left for the next run")
            save()
            audit.record("job", "minute_candles", f"Paused by you: {st['done']} symbols done, {st['rows_added']:,} bars "
                         f"added, {left} left (resumes at the next run)")
            return get_state(STATE_KEY)
        st["current"] = t.symbol
        if st["done"] % 10 == 0:
            save()
        start = max(floor_dt, datetime.fromtimestamp(last[t.token] + 60, IST)) if t.token in last else floor_dt
        try:
            while start <= end_dt:
                chunk_end = min(end_dt, start + timedelta(days=DAYS_PER_REQUEST))
                limiter.wait()
                bars = kite.historical_data(t.token, start, chunk_end, "minute")
                rows = [(t.token, _epoch(b["date"]), _paise(b["open"]), _paise(b["high"]), _paise(b["low"]),
                         _paise(b["close"]), int(b.get("volume") or 0)) for b in bars]
                if rows:
                    with connect() as conn:
                        before = conn.total_changes
                        conn.executemany("INSERT OR IGNORE INTO candles_1m VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
                        st["rows_added"] += conn.total_changes - before
                start = chunk_end + timedelta(minutes=1)
        except Exception as exc:  # noqa: BLE001 - per-symbol isolation; a rejected token aborts
            if is_token_error(exc):
                invalidate("token rejected during the 1-minute download")
                st.update(running=False, aborted="Kite session rejected", finished_at=ts())
                save()
                raise SessionUnavailable("Kite session rejected") from exc
            st["errors"].append({"symbol": t.symbol, "error": str(exc)[:200]})
        st["done"] += 1
    older = _extend_back(kite, tlist, limiter, st, save)
    if older == "paused_by_you":
        return get_state(STATE_KEY)
    st.update(running=False, current=None, finished_at=ts())
    save()
    msg = f"{st['done']} symbols, {st['rows_added']:,} bars added, {len(st['errors'])} failed"
    audit.record("job", "minute_candles", msg, "warning" if st["errors"] else "info")
    if st["errors"]:
        audit.alert("minute_candles", f"1-minute download: {len(st['errors'])} symbol(s) failed; the next run "
                    "retries them.", level="warning")
    return get_state(STATE_KEY)


def _quiet_hours(now: datetime) -> bool:
    """08:45–16:00 on trading days: leave the historical-data limit to the trading jobs."""
    from ..market_calendar import MarketCalendar
    return MarketCalendar(get_app_config()).is_trading_day(now.date()) and dtime(8, 45) <= now.time() < dtime(16, 0)


def _extend_back(kite, tlist, limiter, st: dict[str, Any], save) -> str:
    """Download older 1-minute history, stock by stock, until Kite has none older. Returns 'done', 'paused'
    (quiet hours, continues after the close) or 'paused_by_you'."""
    app = get_app_config()
    start_from = datetime.combine(datetime.fromisoformat(str(app["data"].get("minute_history_from", "2015-01-01"))).date(),
                                  SESSION_START, IST)
    with connect() as conn:
        done = {int(r[0]) for r in conn.execute("SELECT token FROM history_floor WHERE done = 1")}
    todo = [t for t in tlist if t.token not in done]
    st.update(older_total=len(tlist), older_done=len(tlist) - len(todo), older_state="running" if todo else "complete")
    save()
    for t in todo:
        if _stop.is_set():
            _stop.clear()
            left = len([x for x in todo if x.token not in done])
            st.update(running=False, current=None, finished_at=ts(), older_state="paused",
                      aborted=f"paused by you during the older history; {left} stocks left for the next run")
            save()
            return "paused_by_you"
        if _quiet_hours(now_ist()):
            st.update(older_state="waiting", older_note="continues after today's close")
            save()
            return "paused"
        st["current"] = f"{t.symbol} (older history)"
        with connect() as conn:
            earliest = conn.execute("SELECT MIN(ts) FROM candles_1m WHERE token = ?", (t.token,)).fetchone()[0]
        if earliest is None:
            continue                                    # nothing stored yet: the forward pass handles it first
        finished = False
        try:
            while not _stop.is_set() and not _quiet_hours(now_ist()):
                end = datetime.fromtimestamp(earliest, IST) - timedelta(minutes=1)
                if end < start_from:
                    finished = True
                    break
                begin = max(start_from, end - timedelta(days=DAYS_PER_REQUEST))
                limiter.wait()
                bars = kite.historical_data(t.token, begin, end, "minute")
                rows = [(t.token, _epoch(b["date"]), _paise(b["open"]), _paise(b["high"]), _paise(b["low"]),
                         _paise(b["close"]), int(b.get("volume") or 0)) for b in bars]
                rows = [r for r in rows if r[1] < earliest]
                with connect() as conn:
                    if rows:
                        before = conn.total_changes
                        conn.executemany("INSERT OR IGNORE INTO candles_1m VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
                        st["rows_added"] += conn.total_changes - before
                        earliest = min(r[1] for r in rows)
                    else:
                        finished = True                 # Kite has nothing older for this stock
                    conn.execute("INSERT INTO history_floor (token, done, earliest, updated_at) VALUES (?, ?, ?, ?) "
                                 "ON CONFLICT(token) DO UPDATE SET done = excluded.done, earliest = excluded.earliest, "
                                 "updated_at = excluded.updated_at", (t.token, int(finished), earliest, ts()))
                if finished:
                    break
        except Exception as exc:  # noqa: BLE001 - per-symbol isolation; a rejected token aborts
            if is_token_error(exc):
                invalidate("token rejected during the 1-minute download")
                st.update(running=False, aborted="Kite session rejected", finished_at=ts())
                save()
                raise SessionUnavailable("Kite session rejected") from exc
            st["errors"].append({"symbol": t.symbol, "error": f"older history: {str(exc)[:180]}"})
        if finished:
            done.add(t.token)
            st["older_done"] = len(done & {x.token for x in tlist})
            if st["older_done"] % 10 == 0:
                save()
    st["older_state"] = "complete" if all(x.token in done for x in tlist) else st.get("older_state", "running")
    if st["older_state"] == "running" and _quiet_hours(now_ist()):
        st["older_state"] = "waiting"
    return "done"


def status() -> dict[str, Any]:
    out = get_state(STATE_KEY) or {}
    path = db_path()
    out = dict(out) | {"file_mb": round(path.stat().st_size / 1e6, 1) if path.exists() else 0,
                       "running": is_running()}
    if path.exists():
        with connect() as conn:
            r = conn.execute("SELECT MIN(ts), MAX(ts) FROM candles_1m").fetchone()
            n = conn.execute("SELECT COUNT(DISTINCT token) FROM candles_1m").fetchone()[0]
        out.update(earliest=datetime.fromtimestamp(r[0], IST).isoformat() if r[0] else None,
                   latest=datetime.fromtimestamp(r[1], IST).isoformat() if r[1] else None, symbols=n)
    return out


# ---------- reading and resampling ----------

def load(token: int, start: datetime, end: datetime | None = None) -> list[dict[str, Any]]:
    end = end or now_ist()
    with connect() as conn:
        rows = conn.execute("SELECT ts, open, high, low, close, volume FROM candles_1m WHERE token = ? AND ts BETWEEN "
                            "? AND ? ORDER BY ts", (token, _epoch(start), _epoch(end))).fetchall()
    return [{"date": datetime.fromtimestamp(r[0], IST), "open": r[1] / 100, "high": r[2] / 100, "low": r[3] / 100,
             "close": r[4] / 100, "volume": r[5]} for r in rows]


def resample(bars: list[dict[str, Any]], minutes: int) -> list[dict[str, Any]]:
    """Merge 1-minute bars into N-minute bars anchored at 09:15 each session (Kite's convention:
    5m 09:15, 09:20…; 1h 09:15, 10:15…; 4h 09:15 and 13:15)."""
    if minutes <= 1:
        return bars
    out: list[dict[str, Any]] = []
    key = None
    for b in bars:
        d = b["date"].astimezone(IST)
        start = datetime.combine(d.date(), SESSION_START, IST)
        k = start + timedelta(minutes=((d - start).total_seconds() // 60) // minutes * minutes)
        if k != key:
            out.append({"date": k, "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
                        "volume": b.get("volume", 0)})
            key = k
        else:
            cur = out[-1]
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] = cur.get("volume", 0) + b.get("volume", 0)
    return out
