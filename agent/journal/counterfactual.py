"""What a plan would have done, on daily candles (used by the weekly review and the learning evidence).

A rough model, stated in every report that uses it: a buy fills if a day's high reaches the trigger within the
allowed sessions (at the trigger, or at the open if it gapped over; not above the limit); after that, the first
day whose low reaches the stop or whose high reaches the target decides (both on one day: the stop, as the paper
broker assumes). No trailing, no half exit. Result in R (multiples of trigger − stop), before charges.
"""
from __future__ import annotations

from typing import Any

from ..db import connect


def _bars(symbol: str, after: str, limit: int = 400) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT date, open, high, low, close FROM candles_daily WHERE symbol = ? "
                                              "AND date > ? ORDER BY date LIMIT ?", (symbol, after, limit))]


def entry_plan(symbol: str, signal_date: str, trigger: float, limit: float, stop: float, target: float,
               sessions: int = 2) -> dict[str, Any]:
    """A buy GTT from the signal's evening: filled? then stop or target first?"""
    bars = _bars(symbol, signal_date)
    for i, b in enumerate(bars[:sessions]):
        if b["high"] >= trigger:
            price = max(trigger, b["open"]) if b["open"] >= trigger else trigger
            if price > limit:
                return {"outcome": "not_filled", "why": f"opened at {b['open']:g}, above the limit {limit:g}"}
            out = exit_plan(bars[i:], price, stop, target)   # the fill day counts too (stop first: conservative)
            return {"filled_on": b["date"], "entry": round(price, 2), **out}
    return {"outcome": "not_filled", "why": f"never reached the trigger within {sessions} session(s)"}


def exit_plan(bars: list[dict[str, Any]], entry: float, stop: float, target: float) -> dict[str, Any]:
    risk = entry - stop
    for b in bars:
        lo, hi = b["low"], b["high"]
        if lo <= stop:
            return {"outcome": "stop", "exit_on": b["date"], "exit": stop, "r": round(-1.0, 2) if risk > 0 else None}
        if hi >= target:
            return {"outcome": "target", "exit_on": b["date"], "exit": target,
                    "r": round((target - entry) / risk, 2) if risk > 0 else None}
    if not bars:
        return {"outcome": "no_data", "r": None}
    last = bars[-1]
    return {"outcome": "open", "as_of": last["date"], "price": last["close"],
            "r": round((last["close"] - entry) / risk, 2) if risk > 0 else None}


def hold_from(symbol: str, from_date: str, entry: float, stop: float, target: float) -> dict[str, Any]:
    """A position already held from `from_date` (exclusive) with this stop and target."""
    return exit_plan(_bars(symbol, from_date), entry, stop, target)
