"""Market filter (spec section 5): no new buy GTTs in any account when Nifty 50
closes below its 50-day SMA. Open positions are still managed normally.

Switched off by config (universe.market_filter.enabled: false, decided 2026-09-23); the Nifty
status is still computed for information. When enabled it fails closed: missing or stale index
data blocks new entries.
"""
from __future__ import annotations

from typing import Any

from ..clock import now_ist
from ..db import connect
from ..market_calendar import MarketCalendar
from ..settings import get_app_config


def enabled(app: dict[str, Any] | None = None) -> bool:
    return bool(((app or get_app_config())["universe"]["market_filter"]).get("enabled", True))


def evaluate() -> dict[str, Any]:
    out = _evaluate()
    if not enabled():
        out = out | {"enabled": False, "allow_entries": True, "blocks": False,
                     "reason": "Market filter is off: Nifty shown for information only"}
    else:
        out = out | {"enabled": True, "blocks": not out["allow_entries"]}
    return out


def _evaluate() -> dict[str, Any]:
    app = get_app_config()
    mf = app["universe"]["market_filter"]
    period = int(mf["sma_period"])
    symbol = app["kite"]["nifty50_symbol"]
    expected = MarketCalendar(app).last_completed_session(now_ist()).isoformat()
    with connect() as conn:
        rows = conn.execute("SELECT date, close FROM candles_daily WHERE symbol = ? ORDER BY date DESC LIMIT ?",
                            (symbol, period)).fetchall()
    base = {"index": symbol, "sma_period": period, "expected_date": expected}
    if len(rows) < period:
        return base | {"allow_entries": False, "state": "no_data",
                       "reason": f"Need {period} daily closes of {symbol}; have {len(rows)}. Run the candle download."}
    last_date, close = rows[0]["date"], float(rows[0]["close"])
    sma = sum(float(r["close"]) for r in rows) / period
    if last_date < expected:
        return base | {"allow_entries": False, "state": "stale", "date": last_date, "close": close,
                       "sma": round(sma, 2),
                       "reason": f"Latest {symbol} close is from {last_date}; expected {expected}."}
    above = close >= sma
    return base | {
        "allow_entries": above, "state": "above" if above else "below", "date": last_date,
        "close": close, "sma": round(sma, 2),
        "reason": None if above else f"{symbol} closed below its {period}-day SMA: no new entries.",
    }
