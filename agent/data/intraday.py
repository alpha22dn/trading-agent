"""Intraday candles for the charts (1m, 5m, 15m, 1h, 4h).

Built from the stored 1-minute bars (agent/data/minute.py), resampled to the timeframe and anchored at 09:15
each session (4h = 09:15–13:15 and 13:15–15:30). Today's bars are not stored until after the close, so during
the day they are added live from Kite. When nothing is stored yet for a stock, the chart falls back to asking
Kite for that timeframe directly. A short in-memory cache avoids re-fetching when you switch back and forth.
Chart display only: the strategy uses daily candles.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any

from ..broker import kite_session
from ..clock import IST, now_ist
from ..db import connect
from . import minute

# timeframe -> (Kite interval, days of history to show). Kite allows up to 60 days of 1-minute data,
# 100 days of 5/15-minute and 400 days of 60-minute data per request.
TIMEFRAMES: dict[str, tuple[str, int]] = {
    "1m": ("minute", 5),
    "5m": ("5minute", 30),
    "15m": ("15minute", 60),
    "1h": ("60minute", 180),
    "4h": ("60minute", 365),
}
MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}
CACHE_SECONDS = 20
IST_OFFSET = 19800  # charts show exchange time: epoch seconds shifted to IST wall-clock

_cache: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}
_lock = threading.Lock()


class IntradayError(Exception):
    pass


def _token(symbol: str) -> int:
    with connect() as conn:
        r = conn.execute("SELECT instrument_token FROM instruments WHERE symbol = ? AND exchange = 'NSE'",
                         (symbol,)).fetchone()
    if r is None:
        raise IntradayError(f"Unknown symbol {symbol}")
    return int(r[0])


def _ts(d: datetime) -> int:
    if d.tzinfo is None:
        d = d.replace(tzinfo=IST)
    return int(d.timestamp()) + IST_OFFSET


def to_4h(bars: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge 1-hour bars into two per session: 09:15–13:15 and 13:15–15:30 (IST)."""
    out: list[dict[str, Any]] = []
    key = None
    for b in bars:
        d = b["date"].astimezone(IST) if b["date"].tzinfo else b["date"].replace(tzinfo=IST)
        mins = d.hour * 60 + d.minute - (9 * 60 + 15)
        k = (d.date(), 0 if mins < 240 else 1)
        if k != key:
            start = datetime.combine(d.date(), datetime.min.time(), IST) + timedelta(minutes=9 * 60 + 15 + 240 * k[1])
            out.append({"date": start, "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
                        "volume": b.get("volume", 0)})
            key = k
        else:
            cur = out[-1]
            cur["high"] = max(cur["high"], b["high"])
            cur["low"] = min(cur["low"], b["low"])
            cur["close"] = b["close"]
            cur["volume"] = cur.get("volume", 0) + b.get("volume", 0)
    return out


def _live(token: int, start: datetime, interval: str) -> list[dict[str, Any]]:
    try:
        kite = kite_session.get_kite()
    except kite_session.SessionUnavailable as exc:
        raise IntradayError("Log in to Kite to see intraday charts") from exc
    try:
        return kite.historical_data(token, start, now_ist(), interval)
    except Exception as exc:  # noqa: BLE001 - shown on the chart; never affects trading
        if kite_session.is_token_error(exc):
            kite_session.invalidate("token rejected while fetching a chart")
        raise IntradayError(f"Kite did not return candles: {exc}") from exc


def candles(symbol: str, tf: str) -> list[dict[str, Any]]:
    """[{time (IST epoch seconds), open, high, low, close, volume}] for the chart."""
    if tf not in TIMEFRAMES:
        raise IntradayError(f"Unknown timeframe {tf}")
    with _lock:
        hit = _cache.get((symbol, tf))
        if hit and time.monotonic() - hit[0] < CACHE_SECONDS:
            return hit[1]
    token = _token(symbol)
    interval, days = TIMEFRAMES[tf]
    start = now_ist() - timedelta(days=days)
    stored = minute.load(token, start)
    if stored:
        bars = stored
        last = stored[-1]["date"]
        today_open = datetime.combine(now_ist().date(), minute.SESSION_START, IST)
        if last < today_open <= now_ist():
            try:  # today's session so far, live (stored only after the close)
                bars = bars + [b for b in _live(token, today_open, "minute") if _as_ist(b["date"]) > last]
            except IntradayError:
                pass  # show what is stored; the live part needs a Kite login
        bars = minute.resample([b | {"date": _as_ist(b["date"])} for b in bars], MINUTES[tf])
    else:
        bars = _live(token, start, interval)
        if tf == "4h":
            bars = to_4h(bars)
    out = [{"time": _ts(b["date"]), "open": b["open"], "high": b["high"], "low": b["low"], "close": b["close"],
            "volume": b.get("volume", 0)} for b in bars]
    with _lock:
        _cache[(symbol, tf)] = (time.monotonic(), out)
    return out


def _as_ist(d: datetime) -> datetime:
    return d.astimezone(IST) if d.tzinfo else d.replace(tzinfo=IST)
