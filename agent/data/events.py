"""Results calendar from NSE's board-meeting announcements (spec section 5, gate 5).

- Daily fetch: meetings from a few days back to `lookahead_days` ahead.
- History fetch (once, and on demand): the last `history_years`, used for the
  "same quarter's result date last year" window and for historical scans.
- Only meetings whose purpose mentions results are treated as results meetings.
- A failed daily fetch is recorded; the gate then blocks every new entry that day.
"""
from __future__ import annotations

import logging
import time
from datetime import date, datetime, timedelta
from typing import Any, Callable

from .. import audit
from ..clock import now_ist, ts
from ..db import connect, get_state, set_state
from ..events import publish
from ..settings import get_app_config

log = logging.getLogger("agent")
STATUS_KEY = "results_calendar"
SOURCE = "nse_board_meetings"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/128.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/companies-listing/corporate-filings-board-meetings",
}


class NseClient:
    """Minimal NSE website client: primes cookies from the home page, then calls the JSON API."""

    def __init__(self, base: str = "https://www.nseindia.com", timeout: int = 20, pause_sec: float = 1.0):
        import requests
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.pause_sec = pause_sec
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._primed = False

    def _prime(self) -> None:
        self.session.get(self.base + "/", timeout=self.timeout)
        self._primed = True

    def board_meetings(self, start: date, end: date) -> list[dict[str, Any]]:
        params = {"index": "equities", "from_date": start.strftime("%d-%m-%Y"), "to_date": end.strftime("%d-%m-%Y")}
        for attempt in range(3):
            if not self._primed:
                self._prime()
            r = self.session.get(self.base + "/api/corporate-board-meetings", params=params, timeout=self.timeout)
            if r.status_code in (401, 403):
                self._primed = False
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            data = r.json()
            time.sleep(self.pause_sec)
            if isinstance(data, dict):  # some responses wrap the list
                data = data.get("data", [])
            return list(data)
        raise RuntimeError(f"NSE refused the request ({r.status_code}) after 3 attempts")


# Tests swap this for a fake.
client_factory: Callable[[], Any] = lambda: NseClient(**_client_kwargs())


def _client_kwargs() -> dict[str, Any]:
    cfg = get_app_config()["results_calendar"]
    return {"base": cfg["base_url"], "timeout": int(cfg["timeout_sec"]), "pause_sec": float(cfg["pause_sec"])}


def is_results_meeting(row: dict[str, Any]) -> bool:
    text = f"{row.get('bm_purpose', '')} {row.get('bm_desc', '')}".lower()
    return "result" in text


def _parse_date(value: str) -> date | None:
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d"):
        try:
            return datetime.strptime(value.strip(), fmt).date()
        except (ValueError, AttributeError):
            continue
    return None


def store(rows: list[dict[str, Any]]) -> int:
    now = ts()
    n = 0
    with connect() as conn:
        for r in rows:
            sym = (r.get("bm_symbol") or "").strip().upper()
            d = _parse_date(r.get("bm_date", ""))
            if not sym or not d or not is_results_meeting(r):
                continue
            purpose = (r.get("bm_purpose") or r.get("bm_desc") or "")[:300]
            cur = conn.execute(
                "INSERT INTO events (symbol, event_date, purpose, source, fetched_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol, event_date, source) DO UPDATE SET purpose = excluded.purpose, "
                "fetched_at = excluded.fetched_at", (sym, d.isoformat(), purpose, SOURCE, now))
            n += cur.rowcount
    return n


def fetch_range(start: date, end: date, chunk_days: int = 31) -> int:
    client = client_factory()
    total, s = 0, start
    while s <= end:
        e = min(end, s + timedelta(days=chunk_days - 1))
        total += store(client.board_meetings(s, e))
        s = e + timedelta(days=1)
    return total


def fetch_daily() -> dict[str, Any]:
    """Refresh upcoming results meetings. Records success/failure for the blackout gate."""
    cfg = get_app_config()["results_calendar"]
    today = now_ist().date()
    try:
        n = fetch_range(today - timedelta(days=int(cfg["lookback_days"])), today + timedelta(days=int(cfg["lookahead_days"])))
        now = ts()
        status = {"date": today.isoformat(), "ok": True, "stored": n, "at": now, "error": None, "last_ok_at": now}
        audit.record("api", "results_calendar", f"{n} results meetings stored")
    except Exception as exc:  # noqa: BLE001 - any failure blocks entries (fail closed)
        # A failed refresh keeps the time of the last good fetch: that data is still valid for the
        # session it was fetched after (see fresh_for).
        status = {"date": today.isoformat(), "ok": False, "stored": 0, "at": ts(), "error": str(exc)[:300],
                  "last_ok_at": _last_ok_at((get_state(STATUS_KEY, {}) or {}).get("daily") or {})}
        audit.alert("results_calendar", f"Could not fetch NSE board meetings: {exc}. New entries stay blocked "
                    "unless the calendar was fetched after the last session's close.", level="error")
    prev = get_state(STATUS_KEY, {})
    set_state(STATUS_KEY, prev | {"daily": status})
    publish("events")
    return status


def fetch_history() -> dict[str, Any]:
    cfg = get_app_config()["results_calendar"]
    today = now_ist().date()
    start = today - timedelta(days=int(365 * float(cfg["history_years"])))
    try:
        n = fetch_range(start, today)
        hist = {"ok": True, "from": start.isoformat(), "to": today.isoformat(), "stored": n, "at": ts(), "error": None}
        audit.record("api", "results_calendar_history", f"{n} past results meetings stored since {start}")
    except Exception as exc:  # noqa: BLE001
        hist = {"ok": False, "from": start.isoformat(), "to": today.isoformat(), "stored": 0, "at": ts(),
                "error": str(exc)[:300]}
        audit.alert("results_calendar", f"Could not load results history from NSE: {exc}", level="warning")
    prev = get_state(STATUS_KEY, {})
    set_state(STATUS_KEY, prev | {"history": hist})
    publish("events")
    return hist


def _last_ok_at(daily: dict[str, Any]) -> str | None:
    return daily.get("last_ok_at") or (daily.get("at") if daily.get("ok") else None)


def fresh_for(session: date) -> bool:
    """The results gate for signals from `session` needs a calendar fetched after that session's close
    (that evening's EOD run, or any later fetch, e.g. at the next morning's login)."""
    from ..clock import IST
    from ..market_calendar import MarketCalendar
    at = _last_ok_at((get_state(STATUS_KEY, {}) or {}).get("daily") or {})
    if not at:
        return False
    close = datetime.combine(session, MarketCalendar(get_app_config()).close_t, IST)
    return datetime.fromisoformat(at) >= close


def status() -> dict[str, Any]:
    from ..market_calendar import MarketCalendar
    st = get_state(STATUS_KEY, {})
    with connect() as conn:
        row = conn.execute("SELECT COUNT(*), MIN(event_date), MAX(event_date) FROM events WHERE source = ?",
                           (SOURCE,)).fetchone()
    daily = st.get("daily") or {}
    return {
        "daily": daily or None,
        "history": st.get("history"),
        "fetched_today": daily.get("date") == now_ist().date().isoformat() and bool(daily.get("ok")),
        "last_ok_at": _last_ok_at(daily),
        "session": (session := MarketCalendar(get_app_config()).last_completed_session(now_ist())).isoformat(),
        "fresh": fresh_for(session),   # good enough for the gate on the latest session's signals
        "count": row[0], "first": row[1], "last": row[2],
    }


def results_dates(conn, symbols: list[str] | None = None) -> dict[str, list[date]]:
    sql = "SELECT symbol, event_date FROM events WHERE source = ?"
    args: list[Any] = [SOURCE]
    rows = conn.execute(sql, args).fetchall()
    out: dict[str, list[date]] = {}
    wanted = set(symbols) if symbols else None
    for r in rows:
        if wanted is None or r[0] in wanted:
            out.setdefault(r[0], []).append(date.fromisoformat(r[1]))
    for v in out.values():
        v.sort()
    return out
