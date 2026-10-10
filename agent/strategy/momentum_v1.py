"""Momentum Strategy v1 (spec section 6): signal conditions, trade levels, RR band.

Pure functions: numbers in, numbers out. Every parameter comes from
config/strategies/momentum_v1.yaml.

Prices are rounded to the stock's tick size so they are valid GTT prices,
always in the conservative direction: entry trigger and limit round up, stop
and target round down. RR is computed after rounding.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any


@dataclass
class Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    bb_upper: float | None
    atr14: float | None
    adx14: float | None


@dataclass
class Conditions:
    change_pct: float | None
    change_ok: bool
    bb_ok: bool
    atr_rising: bool
    adx_ok: bool

    @property
    def all_true(self) -> bool:
        return self.change_ok and self.bb_ok and self.atr_rising and self.adx_ok


@dataclass
class Levels:
    entry_trigger: float
    entry_limit: float
    stop: float
    swing_low: float
    target: float
    rr: float
    rr_band: str | None
    rr_ok: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _missing(*values: float | None) -> bool:
    return any(v is None or (isinstance(v, float) and math.isnan(v)) for v in values)


def check_conditions(today: Bar, yesterday: Bar, params: dict[str, Any]) -> Conditions:
    sig = params["signal"]
    if _missing(today.close, yesterday.close, today.bb_upper, today.atr14, yesterday.atr14, today.adx14):
        return Conditions(None, False, False, False, False)
    change_pct = (today.close / yesterday.close - 1) * 100
    return Conditions(
        change_pct=round(change_pct, 4),
        change_ok=today.close >= yesterday.close * (1 + float(sig["min_change_pct"]) / 100),
        bb_ok=today.close >= today.bb_upper,
        atr_rising=today.atr14 > yesterday.atr14,
        adx_ok=today.adx14 > float(sig["adx_min"]),
    )


def round_up(price: float, tick: float) -> float:
    return round(math.ceil(round(price / tick, 9)) * tick, 2)


def round_down(price: float, tick: float) -> float:
    return round(math.floor(round(price / tick, 9)) * tick, 2)


def rr_band(rr: float, params: dict[str, Any]) -> str | None:
    for b in params["ranking"]["rr_bands"]:  # ordered highest first
        if rr >= float(b["min"]):
            return b["band"]
    return None


def trade_levels(signal: Bar, lows_20: list[float], tick: float, params: dict[str, Any]) -> Levels:
    """lows_20: lows of the `swing_lookback` sessions ending on the signal day (inclusive)."""
    lv = params["levels"]
    tick = tick if tick and tick > 0 else 0.05
    entry_trigger = round_up(signal.close * (1 + float(lv["entry_trigger_buffer_pct"]) / 100), tick)
    entry_limit = round_up(entry_trigger * (1 + float(lv["entry_limit_buffer_pct"]) / 100), tick)
    stop = round_down(signal.low * (1 - float(lv["stop_buffer_pct"]) / 100), tick)
    swing_low = min(lows_20)
    target = round_down(swing_low + float(lv["target_extension"]) * (signal.high - swing_low), tick)
    risk = entry_limit - stop
    rr = (target - entry_limit) / risk if risk > 0 else float("-inf")
    rr = round(rr, 4) if math.isfinite(rr) else -999.0
    return Levels(entry_trigger=entry_trigger, entry_limit=entry_limit, stop=stop, swing_low=swing_low,
                  target=target, rr=rr, rr_band=rr_band(rr, params), rr_ok=rr >= float(lv["min_rr"]))
