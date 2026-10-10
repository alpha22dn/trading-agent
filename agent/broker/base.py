"""Broker interface (spec section 3: the key seam).

PaperBroker and KiteBroker implement the same methods and one config flag picks
which is used, so live trading runs exactly the code that passed paper testing.
Only GTT orders are placed: there is deliberately no method for market, limit or
AMO orders (spec section 2, rule 1).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class BrokerReject(Exception):
    """The broker refused a request (the exchange / Kite would reject it too)."""


@dataclass
class Leg:
    side: str          # BUY or SELL
    qty: int
    price: float       # limit price


@dataclass
class Gtt:
    id: int
    account_id: str
    symbol: str
    type: str                      # single | oco
    trigger_values: list[float]
    orders: list[Leg]
    status: str                    # active | triggered | cancelled | rejected | expired
    last_price: float
    triggered_leg: int | None = None
    order_id: int | None = None


@dataclass
class Order:
    """A broker order. Always read filled_qty: a partly filled order stays 'open' until it completes
    or the session ends (then 'cancelled' with the filled part kept)."""
    id: int
    account_id: str
    gtt_id: int | None
    symbol: str
    side: str
    qty: int
    price: float
    status: str                    # open (possibly partly filled) | complete | cancelled | rejected
    trade_date: str
    filled_qty: int = 0            # Kite filled_quantity; a cancelled order can still carry fills
    filled_price: float | None = None   # average price of the filled part
    filled_at: str | None = None
    charges: float | None = None
    reason: str | None = None


@dataclass
class Position:
    symbol: str
    qty: int
    avg_price: float


@dataclass
class Quote:
    ltp: float
    high: float
    low: float
    upper_circuit: float | None = None
    lower_circuit: float | None = None
    open: float | None = None      # today's open (Kite ohlc.open): tells a gap at the open from a crossing
    # Market depth (Kite quote 'depth'): [(price, quantity), ...]. None = unknown (fill in full).
    bids: list[tuple[float, int]] | None = None
    asks: list[tuple[float, int]] | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class Broker(ABC):
    @abstractmethod
    def place_gtt(self, account_id: str, symbol: str, type_: str, trigger_values: list[float],
                  last_price: float, orders: list[Leg]) -> int: ...

    @abstractmethod
    def modify_gtt(self, account_id: str, gtt_id: int, trigger_values: list[float],
                   last_price: float, orders: list[Leg]) -> None: ...

    @abstractmethod
    def cancel_gtt(self, account_id: str, gtt_id: int) -> None: ...

    @abstractmethod
    def get_gtt(self, account_id: str, gtt_id: int) -> Gtt | None: ...

    @abstractmethod
    def gtts(self, account_id: str, status: str | None = None) -> list[Gtt]: ...

    @abstractmethod
    def get_order(self, account_id: str, order_id: int) -> Order | None: ...

    @abstractmethod
    def orders(self, account_id: str, symbol: str | None = None) -> list[Order]:
        """All orders, oldest first (Kite: the day's order book). gtt_id is None for orders the user
        placed by hand."""

    @abstractmethod
    def positions(self, account_id: str) -> list[Position]: ...

    @abstractmethod
    def cash(self, account_id: str) -> float: ...
