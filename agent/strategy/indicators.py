"""Daily indicators for Momentum v1 (spec section 6), computed from the candle cache.

Definitions (standard, matching TradingView / TA-Lib):
- Bollinger upper = SMA(close, n) + k x population std-dev(close, n)
- ATR(n)  = Wilder's moving average (RMA, seeded with a simple mean) of true range
- ADX(n)  = RMA of DX, where DX comes from Wilder-smoothed +DM, -DM and TR
- traded_value_20d = mean(close x volume) over the last 20 sessions, in rupees

Wilder smoothing needs warm-up: values before `indicators.warmup_bars` sessions
(config/app.yaml) are left empty, so a new listing produces no signal until it
has enough history.
"""
from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import pandas as pd

from ..clock import ts
from ..db import connect
from ..events import publish
from ..settings import get_app_config

log = logging.getLogger("agent")


def rma(values: pd.Series, n: int) -> pd.Series:
    """Wilder's moving average: first value = mean of the first n valid inputs, then
    prev + (x - prev) / n. NaN until n valid inputs have been seen."""
    arr = values.to_numpy(dtype=float)
    out = np.full(arr.shape, np.nan)
    valid = np.flatnonzero(~np.isnan(arr))
    if len(valid) < n:
        return pd.Series(out, index=values.index)
    start = valid[0]
    seed_end = start + n  # assumes no gaps after the first valid value
    if np.isnan(arr[start:seed_end]).any():
        return pd.Series(out, index=values.index)
    prev = arr[start:seed_end].mean()
    out[seed_end - 1] = prev
    for i in range(seed_end, len(arr)):
        prev = prev + (arr[i] - prev) / n
        out[i] = prev
    return pd.Series(out, index=values.index)


def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    tr = pd.concat([df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
                   axis=1).max(axis=1)
    tr.iloc[0] = df["high"].iloc[0] - df["low"].iloc[0]
    return tr


def atr(df: pd.DataFrame, n: int) -> pd.Series:
    return rma(true_range(df), n)


def adx(df: pd.DataFrame, n: int) -> pd.Series:
    up = df["high"].diff()
    down = -df["low"].diff()
    plus_dm = pd.Series(np.where((up > down) & (up > 0), up, 0.0), index=df.index)
    minus_dm = pd.Series(np.where((down > up) & (down > 0), down, 0.0), index=df.index)
    # The first bar has no previous bar: start smoothing from bar 1.
    tr = true_range(df).iloc[1:]
    tr_s = rma(tr, n)
    plus_s = rma(plus_dm.iloc[1:], n)
    minus_s = rma(minus_dm.iloc[1:], n)
    plus_di = 100 * plus_s / tr_s
    minus_di = 100 * minus_s / tr_s
    denom = plus_di + minus_di
    dx = (100 * (plus_di - minus_di).abs() / denom).where(denom != 0, 0.0)
    out = rma(dx, n)
    return out.reindex(df.index)


def bollinger_upper(close: pd.Series, n: int, k: float) -> pd.Series:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return mid + k * sd


def compute(df: pd.DataFrame, params: dict[str, Any], warmup: int, tv_lookback: int) -> pd.DataFrame:
    """df: one symbol's candles sorted by date (columns open, high, low, close, volume)."""
    sig = params["signal"]
    out = pd.DataFrame(index=df.index)
    out["bb_upper"] = bollinger_upper(df["close"], int(sig["bb_period"]), float(sig["bb_std"]))
    out["atr14"] = atr(df, int(sig["atr_period"]))
    out["adx14"] = adx(df, int(sig["adx_period"]))
    out["traded_value_20d"] = (df["close"] * df["volume"]).rolling(tv_lookback).mean()
    if len(out) and warmup > 0:
        out.iloc[: min(warmup - 1, len(out))] = np.nan
    return out


def load_candles(conn, symbol: str) -> pd.DataFrame:
    rows = conn.execute("SELECT date, open, high, low, close, volume FROM candles_daily WHERE symbol = ? "
                        "ORDER BY date", (symbol,)).fetchall()
    df = pd.DataFrame([tuple(r) for r in rows], columns=["date", "open", "high", "low", "close", "volume"])
    return df.set_index("date") if len(df) else df


def _num(x: float) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else round(float(x), 6)


def compute_all(params: dict[str, Any], symbols: list[str] | None = None) -> dict[str, int]:
    """Recompute indicators for every NSE500 stock (full history: Wilder smoothing depends on it)."""
    app = get_app_config()
    warmup = int(app["indicators"]["warmup_bars"])
    tv_lookback = int(app["universe"]["traded_value_lookback"])
    with connect() as conn:
        if symbols is None:
            symbols = [r[0] for r in conn.execute("SELECT symbol FROM instruments WHERE is_nse500 = 1")]
        done = rows = 0
        now = ts()
        for sym in symbols:
            df = load_candles(conn, sym)
            if df.empty:
                continue
            ind = compute(df, params, warmup, tv_lookback)
            conn.execute("DELETE FROM indicators_daily WHERE symbol = ?", (sym,))
            conn.executemany(
                "INSERT INTO indicators_daily (symbol, date, bb_upper, atr14, adx14, traded_value_20d, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(sym, d, _num(r.bb_upper), _num(r.atr14), _num(r.adx14), _num(r.traded_value_20d), now)
                 for d, r in ind.iterrows()])
            done += 1
            rows += len(ind)
    publish("indicators")
    return {"symbols": done, "rows": rows}
