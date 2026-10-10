"""Intraday pullback, buy only (rules decided with the user 2026-09-30). Pure functions: bars in, levels out.

On 5-minute candles, for a candle that has just closed between 09:30 and 14:30 (its end time):
1. Up on the day: close >= previous session close x (1 + up_min_pct) and close above today's VWAP.
2. Pullback: the candle's low dips to the 20-EMA or VWAP (low <= level x (1 + touch_pct)) and the candle
   closes above that level.
3. Entry: buy GTT trigger a buffer above the candle's high, limit a buffer above the trigger.
4. Stop: a buffer below the candle's low (the pullback low). Target: the day's high before the pullback.
5. RR = (target - limit) / (limit - stop) must reach min_rr; RR bands rank as in Momentum v1.

Orders are GTT only (Zerodha has no GTT for intraday MIS, so the product is CNC): a buy not triggered by 15:15
is cancelled; a bought position keeps one OCO GTT for its whole quantity (stop and target) until one fills,
overnight if needed. One trade per stock per day.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any

from .momentum_v1 import round_down, round_up, rr_band

KIND = "intraday_pullback"
CANDLE_MIN = 5

TEMPLATE: dict[str, Any] = {
    "kind": KIND,
    "timeframe": "5minute",
    "side": "long",
    "product": "CNC",
    "signal": {
        "up_min_pct": 1.0,          # close >= previous close x 1.01
        "touch_pct": 0.1,           # the candle's low within 0.1% above the 20-EMA or VWAP counts as a touch
        "ema_period": 20,           # EMA of 5-minute closes (the previous session warms it up)
        "window_start": "09:30",    # candle END times allowed for a signal
        "window_end": "14:30",
    },
    "universe": {
        "watchlist": True,          # every stock on the watchlist
        "top_liquid": 50,           # plus the N NSE500 stocks with the highest traded value last session
    },
    "levels": {
        "entry_trigger_buffer_pct": 0.05,   # trigger = pullback candle high x 1.0005 (at least one tick above)
        "entry_limit_buffer_pct": 0.2,      # limit = trigger x 1.002
        "stop_buffer_pct": 0.1,             # stop = pullback candle low x 0.999
        "min_rr": 1.5,
    },
    "entry": {
        "cancel_at": "15:15",               # an untriggered buy GTT is cancelled at 15:15
        "cancel_after_sessions": 0,         # safety net at the EOD run: never carried to the next session
    },
    "exits": {
        "split_min_qty": 1000000000,        # never split: the whole position exits at the stop or the target
        "stop_leg_limit_pct": 2.0,          # stop leg limit = stop x 0.98 (as Momentum v1)
        "trail_buffer_pct": 0.0,            # no trailing: the stop and target stay until one fills
    },
    "ranking": {"rr_bands": [{"band": "A", "min": 2.5}, {"band": "B", "min": 2.0}, {"band": "C", "min": 1.5}]},
}


def template(version: str) -> dict[str, Any]:
    p = copy.deepcopy(TEMPLATE)
    p["version"] = version
    return p


@dataclass
class Setup:
    candle_at: datetime        # the pullback candle's start time
    close: float
    high: float
    low: float
    vwap: float
    ema: float
    touched: str               # "EMA" or "VWAP"
    day_high: float
    change_pct: float
    entry_trigger: float
    entry_limit: float
    stop: float
    target: float
    rr: float
    rr_band: str | None
    rr_ok: bool

    def indicators(self) -> dict[str, Any]:
        return {"candle_at": self.candle_at.isoformat(timespec="minutes"), "close": self.close, "high": self.high,
                "low": self.low, "vwap": round(self.vwap, 2), "ema20": round(self.ema, 2), "touched": self.touched,
                "day_high": self.day_high, "change_pct": round(self.change_pct, 2)}


def ema(values: list[float], n: int) -> list[float | None]:
    """Standard EMA (alpha 2/(n+1)), seeded with the simple average of the first n values."""
    out: list[float | None] = [None] * len(values)
    if len(values) < n:
        return out
    k = 2 / (n + 1)
    cur = sum(values[:n]) / n
    out[n - 1] = cur
    for i in range(n, len(values)):
        cur = values[i] * k + cur * (1 - k)
        out[i] = cur
    return out


def vwap(bars: list[dict[str, Any]]) -> list[float | None]:
    """Cumulative VWAP from the session's first candle (typical price x volume)."""
    out: list[float | None] = []
    pv = vol = 0.0
    for b in bars:
        v = float(b.get("volume") or 0)
        pv += (b["high"] + b["low"] + b["close"]) / 3 * v
        vol += v
        out.append(pv / vol if vol else None)
    return out


def _hm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def evaluate(prev_bars: list[dict[str, Any]], today: list[dict[str, Any]], i: int, prev_close: float,
             tick: float, params: dict[str, Any], ema_now: float | None = None,
             vwap_now: float | None = None) -> tuple[Setup | None, str | None]:
    """Is today's candle i (completed) a pullback entry? Returns (setup, None) or (None, why not).
    ema_now / vwap_now: already computed for candle i (scan_day passes them; the live scan does not)."""
    sig, lv = params["signal"], params["levels"]
    c = today[i]
    end = c["date"] + timedelta(minutes=CANDLE_MIN)
    if not _hm(sig["window_start"]) <= end.time() <= _hm(sig["window_end"]):
        return None, "outside the entry window"
    if i == 0:
        return None, "no earlier candle today"
    if ema_now is None:
        closes = [b["close"] for b in prev_bars] + [b["close"] for b in today[: i + 1]]
        ema_now = ema(closes, int(sig["ema_period"]))[-1]
    if vwap_now is None:
        vwap_now = vwap(today[: i + 1])[-1]
    e, w = ema_now, vwap_now
    if e is None or w is None:
        return None, "not enough data for the EMA or VWAP"
    change = (c["close"] / prev_close - 1) * 100 if prev_close else 0.0
    if change < float(sig["up_min_pct"]):
        return None, f"up {change:.2f}% on the day (needs {sig['up_min_pct']}%)"
    if c["close"] <= w:
        return None, "close not above VWAP"
    touch = float(sig["touch_pct"]) / 100
    touched = [name for name, level in (("EMA", e), ("VWAP", w)) if c["low"] <= level * (1 + touch) and c["close"] > level]
    if not touched:
        return None, "no dip to the 20-EMA or VWAP"
    day_high = max(b["high"] for b in today[:i])
    trigger = max(round_up(c["high"] * (1 + float(lv["entry_trigger_buffer_pct"]) / 100), tick), round(c["high"] + tick, 2))
    limit = round_up(trigger * (1 + float(lv["entry_limit_buffer_pct"]) / 100), tick)
    stop = round_down(c["low"] * (1 - float(lv["stop_buffer_pct"]) / 100), tick)
    target = round_down(day_high, tick)
    if target <= limit:
        return None, f"the day's high {day_high:g} is not above the entry limit {limit:g}"
    rr = (target - limit) / (limit - stop) if limit > stop else 0.0
    band = rr_band(rr, params)
    s = Setup(candle_at=c["date"], close=c["close"], high=c["high"], low=c["low"], vwap=w, ema=e, touched=touched[0],
              day_high=day_high, change_pct=change, entry_trigger=trigger, entry_limit=limit, stop=stop, target=target,
              rr=round(rr, 3), rr_band=band, rr_ok=rr >= float(lv["min_rr"]) and band is not None)
    return s, None


def scan_day(prev_bars: list[dict[str, Any]], today: list[dict[str, Any]], prev_close: float, tick: float,
             params: dict[str, Any]) -> tuple[int, Setup] | None:
    """The day's first valid pullback (one trade per stock per day), candle by candle as the live scan sees them.
    Used by backtests: the EMA and VWAP are computed once for the whole day."""
    n = len(prev_bars)
    e = ema([b["close"] for b in prev_bars] + [b["close"] for b in today], int(params["signal"]["ema_period"]))
    w = vwap(today)
    for i in range(1, len(today)):
        setup, _ = evaluate(prev_bars, today, i, prev_close, tick, params, e[n + i], w[i])
        if setup is not None and setup.rr_ok:
            return i, setup
    return None
