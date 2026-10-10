"""Zerodha equity charges. Delivery (CNC) rates in config/app.yaml `charges`; shares bought and sold the same day
are charged as intraday (`charges_intraday`)."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from ..settings import get_app_config


@dataclass
class Charges:
    brokerage: float = 0.0
    stt: float = 0.0
    exchange: float = 0.0
    sebi: float = 0.0
    stamp: float = 0.0
    gst: float = 0.0
    dp: float = 0.0

    @property
    def total(self) -> float:
        return round(self.brokerage + self.stt + self.exchange + self.sebi + self.stamp + self.gst + self.dp, 2)

    def as_dict(self) -> dict[str, Any]:
        return {k: round(v, 2) for k, v in asdict(self).items()} | {"total": self.total}


def compute(side: str, qty: int, price: float, first_sell_today: bool = True,
            cfg: dict[str, Any] | None = None) -> Charges:
    """Charges for one fill. DP charge applies once per stock per day it is sold."""
    c = cfg or get_app_config()["charges"]
    turnover = qty * price
    brokerage = float(c["brokerage"])
    exchange = turnover * float(c["exchange_txn_pct"]) / 100
    sebi = turnover * float(c["sebi_per_crore"]) / 1e7
    return Charges(
        brokerage=brokerage,
        stt=turnover * float(c["stt_pct"]) / 100,
        exchange=exchange,
        sebi=sebi,
        stamp=turnover * float(c["stamp_buy_pct"]) / 100 if side == "BUY" else 0.0,
        gst=(brokerage + sebi + exchange) * float(c["gst_pct"]) / 100,
        dp=float(c["dp_per_scrip_sell_day"]) if side == "SELL" and first_sell_today else 0.0,
    )


def compute_intraday(side: str, qty: int, price: float, cfg: dict[str, Any] | None = None) -> Charges:
    """Intraday charges for one fill (no DP charge)."""
    c = cfg or get_app_config()["charges_intraday"]
    turnover = qty * price
    brokerage = min(float(c["brokerage_max"]), turnover * float(c["brokerage_pct"]) / 100) if qty else 0.0
    exchange = turnover * float(c["exchange_txn_pct"]) / 100
    sebi = turnover * float(c["sebi_per_crore"]) / 1e7
    return Charges(
        brokerage=brokerage,
        stt=turnover * float(c["stt_sell_pct"]) / 100 if side == "SELL" else 0.0,
        exchange=exchange,
        sebi=sebi,
        stamp=turnover * float(c["stamp_buy_pct"]) / 100 if side == "BUY" else 0.0,
        gst=(brokerage + sebi + exchange) * float(c["gst_pct"]) / 100,
    )


def same_day_sell(n_intraday: int, sell_price: float, buy_price: float, n_delivery: int,
                  first_delivery_sell: bool) -> Charges:
    """A sell of which `n_intraday` shares were bought today: those shares are charged as intraday on both sides
    (their buy was booked at delivery rates, so the difference is corrected here), the rest as delivery."""
    out = Charges()
    parts = []
    if n_delivery:
        parts.append(compute("SELL", n_delivery, sell_price, first_delivery_sell))
    if n_intraday:
        parts.append(compute_intraday("SELL", n_intraday, sell_price))
        parts.append(compute_intraday("BUY", n_intraday, buy_price))
        booked = compute("BUY", n_intraday, buy_price)
        parts.append(Charges(**{k: -v for k, v in asdict(booked).items()}))
    for p in parts:
        for k in asdict(out):
            setattr(out, k, getattr(out, k) + getattr(p, k))
    return out
