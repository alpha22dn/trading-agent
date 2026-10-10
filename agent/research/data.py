"""Datafeed for the Research chart: symbol search over the Nifty 500, bars for every timeframe with paging
backwards (the chart asks for older bars as you scroll left), and saved layouts.

Timeframes:
- 1m 5m 15m 1h 4h: from the stored 1-minute bars (agent_1m.db), anchored at 09:15 each session (4h = 09:15 and
  13:15). Today's session, not stored until the evening, is added live from Kite when logged in.
- 1D 1W 1M: from the daily candles; weeks start Monday, months on the 1st. Today's forming daily bar comes from a
  live quote while the market is open.
Timestamps are real epoch milliseconds (the chart shows them in IST).
"""
from __future__ import annotations

import json
import threading
import time
from datetime import date, datetime, timedelta
from typing import Any

from ..clock import IST, now_ist, ts
from ..db import connect
from ..data import minute

INTRADAY = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
DAILY = ("1D", "1W", "1M")
TIMEFRAMES = list(INTRADAY) + list(DAILY)
SESSION_MIN = 375                 # 09:15–15:30
MAX_LIMIT = 2000
LIVE_CACHE_SEC = 15
LAYOUT_MAX_BYTES = 2_000_000

_live_cache: dict[int, tuple[float, list[dict[str, Any]]]] = {}
_live_lock = threading.Lock()


class ResearchError(Exception):
    pass


# ---------- symbols ----------

def symbols(q: str = "", limit: int = 30) -> list[dict[str, Any]]:
    """Nifty 500 members (and the Nifty 50 index) matching the text by symbol or company name."""
    from ..settings import get_app_config
    q = (q or "").strip().upper()
    nifty = get_app_config()["kite"].get("nifty50_symbol", "NIFTY 50")
    with connect() as conn:
        rows = conn.execute(
            """SELECT symbol, name, sector, tick_size FROM instruments
               WHERE exchange = 'NSE' AND (is_nse500 = 1 OR symbol = ?)
                 AND (? = '' OR symbol LIKE ? OR UPPER(COALESCE(name, '')) LIKE ?)
               ORDER BY (symbol = ?) DESC, (symbol LIKE ?) DESC, symbol LIMIT ?""",
            (nifty, q, q + "%", "%" + q + "%", q, q + "%", int(limit))).fetchall()
    return [{"symbol": r["symbol"], "name": r["name"] or "", "sector": r["sector"] or ("Index" if r["symbol"] == nifty else ""),
             "tick": r["tick_size"] or 0.05} for r in rows]


def _token(symbol: str) -> int:
    with connect() as conn:
        r = conn.execute("SELECT instrument_token FROM instruments WHERE symbol = ? AND exchange = 'NSE'",
                         (symbol,)).fetchone()
    if r is None:
        raise ResearchError(f"Unknown symbol {symbol}")
    return int(r[0])


# ---------- bars ----------

def _ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def _bar(d: datetime, b: dict[str, Any]) -> dict[str, Any]:
    return {"timestamp": _ms(d), "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
            "volume": b.get("volume") or 0}


def _today_live_minutes(token: int, after: datetime | None) -> list[dict[str, Any]]:
    """Today's 1-minute bars from Kite (stored only after the close), cached briefly. [] without a login."""
    from ..broker import kite_session
    now = now_ist()
    open_ = datetime.combine(now.date(), minute.SESSION_START, IST)
    if now < open_:
        return []
    with _live_lock:
        hit = _live_cache.get(token)
        if hit and time.monotonic() - hit[0] < LIVE_CACHE_SEC:
            bars = hit[1]
        else:
            bars = None
    if bars is None:
        try:
            raw = kite_session.get_kite().historical_data(token, open_, now, "minute")
        except Exception:  # noqa: BLE001 - chart only: show what is stored
            return []
        bars = [b | {"date": b["date"].astimezone(IST) if b["date"].tzinfo else b["date"].replace(tzinfo=IST)}
                for b in raw]
        with _live_lock:
            _live_cache[token] = (time.monotonic(), bars)
    return [b for b in bars if after is None or b["date"] > after]


def _intraday(symbol: str, tf: str, before: datetime | None, limit: int) -> dict[str, Any]:
    token = _token(symbol)
    m = INTRADAY[tf]
    end = before or (now_ist() + timedelta(minutes=1))
    need_sessions = max(1, -(-limit * m // SESSION_MIN))
    days = int(need_sessions * 1.5) + 4
    with minute.connect() as conn:
        first = conn.execute("SELECT MIN(ts) FROM candles_1m WHERE token = ?", (token,)).fetchone()[0]
    floor = datetime.fromtimestamp(first, IST) if first else None
    bars: list[dict[str, Any]] = []
    start = end
    while True:
        start = datetime.combine((end - timedelta(days=days)).date(), datetime.min.time(), IST)
        raw = minute.load(token, start, end - timedelta(seconds=1)) if floor else []
        if before is None:
            raw = raw + _today_live_minutes(token, raw[-1]["date"] if raw else None)
        bars = minute.resample(raw, m)
        if len(bars) >= limit or floor is None or start <= floor:
            break
        days *= 2
    bars = [b for b in bars if b["date"] < end][-limit:]
    more = bool(floor and bars and floor < bars[0]["date"])
    return {"bars": [_bar(b["date"], b) for b in bars], "more": more,
            "first_stored": floor.isoformat() if floor else None}


def _today_daily(symbol: str) -> dict[str, Any] | None:
    from ..broker import feed
    from ..market_calendar import MarketCalendar
    from ..settings import get_app_config
    now = now_ist()
    if not MarketCalendar(get_app_config()).is_market_open(now):
        return None
    try:
        q = feed.quote_source([symbol]).get(symbol)
    except Exception:  # noqa: BLE001 - chart only
        return None
    if not q:
        return None
    o = q.open or q.ltp
    return {"date": now.date(), "open": o, "high": max(q.high, o), "low": min(q.low, o), "close": q.ltp, "volume": 0}


def _bucket(d: date, tf: str) -> date:
    if tf == "1W":
        return d - timedelta(days=d.weekday())
    if tf == "1M":
        return d.replace(day=1)
    return d


def _daily(symbol: str, tf: str, before: datetime | None, limit: int) -> dict[str, Any]:
    per = {"1D": 1, "1W": 5, "1M": 22}[tf]
    until = (before.astimezone(IST).date() if before else date(9999, 1, 1))
    if before is not None and tf != "1D":
        until = _bucket(until, tf)          # never a partial week/month at the paging edge
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT date, open, high, low, close, volume FROM candles_daily WHERE symbol = ? AND date < ? "
            "ORDER BY date DESC LIMIT ?", (symbol, until.isoformat(), limit * per + per)).fetchall()]
        first = conn.execute("SELECT MIN(date) FROM candles_daily WHERE symbol = ?", (symbol,)).fetchone()[0]
    rows.reverse()
    for r in rows:
        r["date"] = date.fromisoformat(r["date"])
    if before is None:
        live = _today_daily(symbol)
        if live and (not rows or live["date"] > rows[-1]["date"]):
            rows.append(live)
    out: list[dict[str, Any]] = []
    key = None
    for r in rows:
        k = _bucket(r["date"], tf)
        if k != key:
            out.append(dict(r) | {"date": k})
            key = k
        else:
            cur = out[-1]
            cur["high"] = max(cur["high"], r["high"])
            cur["low"] = min(cur["low"], r["low"])
            cur["close"] = r["close"]
            cur["volume"] = (cur["volume"] or 0) + (r["volume"] or 0)
    if tf != "1D" and len(rows) == limit * per + per and out:
        out = out[1:]                       # the oldest bucket may be cut off by the row limit
    out = out[-limit:]
    more = bool(first and out and date.fromisoformat(first) < out[0]["date"])
    return {"bars": [_bar(datetime.combine(b["date"], datetime.min.time(), IST), b) for b in out], "more": more,
            "first_stored": first}


def bars(symbol: str, tf: str, before_ms: int | None = None, limit: int = 500) -> dict[str, Any]:
    """Up to `limit` bars ending before `before_ms` (exclusive), oldest first; `more` = older bars exist."""
    if tf not in TIMEFRAMES:
        raise ResearchError(f"Unknown timeframe {tf}")
    limit = max(1, min(int(limit), MAX_LIMIT))
    before = datetime.fromtimestamp(before_ms / 1000, IST) if before_ms else None
    out = _intraday(symbol, tf, before, limit) if tf in INTRADAY else _daily(symbol, tf, before, limit)
    return {"symbol": symbol, "tf": tf} | out


# ---------- replay feed ----------

REPLAY_MAX = 20000


def replay_feed(symbol: str, after_ms: int, limit: int = 5000) -> dict[str, Any]:
    """The next bars at or after `after_ms`, oldest first, for the replay to reveal one by one.
    base '1m' while 1-minute bars exist; before the 1-minute store begins, base '1D' (daily bars up to that point),
    so older history can still be replayed day by day on the daily, weekly and monthly charts."""
    limit = max(1, min(int(limit), REPLAY_MAX))
    token = _token(symbol)
    after = datetime.fromtimestamp(after_ms / 1000, IST)
    with minute.connect() as conn:
        first = conn.execute("SELECT MIN(ts) FROM candles_1m WHERE token = ?", (token,)).fetchone()[0]
        first_day = (int(datetime.combine(datetime.fromtimestamp(first, IST).date(), datetime.min.time(), IST).timestamp())
                     if first is not None else None)
        if first_day is not None and after_ms // 1000 >= first_day:
            rows = conn.execute("SELECT ts, open, high, low, close, volume FROM candles_1m WHERE token = ? AND ts >= ? "
                                "ORDER BY ts LIMIT ?", (token, after_ms // 1000, limit)).fetchall()
            bars = [{"timestamp": r[0] * 1000, "open": r[1] / 100, "high": r[2] / 100, "low": r[3] / 100,
                     "close": r[4] / 100, "volume": r[5]} for r in rows]
            return {"symbol": symbol, "base": "1m", "bars": bars, "end": len(bars) < limit,
                    "minutes_from": datetime.fromtimestamp(first, IST).isoformat()}
    floor_day = datetime.fromtimestamp(first, IST).date().isoformat() if first is not None else "9999-12-31"
    with connect() as conn:
        rows = conn.execute("SELECT date, open, high, low, close, volume FROM candles_daily WHERE symbol = ? AND date >= ? "
                            "AND date < ? ORDER BY date LIMIT ?",
                            (symbol, after.date().isoformat(), floor_day, limit)).fetchall()
    bars = [_bar(datetime.combine(date.fromisoformat(r[0]), datetime.min.time(), IST),
                 {"open": r[1], "high": r[2], "low": r[3], "close": r[4], "volume": r[5]}) for r in rows]
    # the daily part ends where the minutes begin: the replay then continues on 1-minute bars
    return {"symbol": symbol, "base": "1D", "bars": bars, "end": first is None and len(bars) < limit,
            "minutes_from": datetime.fromtimestamp(first, IST).isoformat() if first is not None else None}


# ---------- layouts ----------

def get_layout(key: str) -> dict[str, Any] | None:
    with connect() as conn:
        r = conn.execute("SELECT data, updated_at FROM research_layouts WHERE key = ?", (key,)).fetchone()
    return {"data": json.loads(r[0]), "updated_at": r[1]} if r else None


def put_layout(key: str, data: Any) -> dict[str, Any]:
    if not (key == "global" or key.startswith("sym:")) or len(key) > 64:
        raise ResearchError("Bad layout key")
    text = json.dumps(data)
    if len(text) > LAYOUT_MAX_BYTES:
        raise ResearchError("Layout too large")
    now = ts()
    with connect() as conn:
        conn.execute("INSERT INTO research_layouts (key, data, updated_at) VALUES (?, ?, ?) ON CONFLICT(key) DO "
                     "UPDATE SET data = excluded.data, updated_at = excluded.updated_at", (key, text, now))
    return {"ok": True, "updated_at": now}
