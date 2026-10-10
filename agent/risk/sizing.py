"""Position sizing, ranking and selection per account (spec section 7).

Sizing:   qty = floor(capital x 1% / (entry_limit - stop)), capped at floor(capital x 25% / entry_limit)
Ranking:  RR band A, then B, then C; within a band lower share price first, then higher
          20-day average traded value.
Selection (per account, in rank order): skip if qty 0, account halted / kill switch, already held
          or pending in this account, no free slot, sector cap reached, or not enough free cash
          after reserving every pending buy GTT. Every candidate not taken gets its reason.

Pending buy GTTs count toward open slots, the sector cap and reserved cash, so the limits hold
even if every pending buy fills.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

BAND_ORDER = {"A": 0, "B": 1, "C": 2}


@dataclass
class AccountState:
    account_id: str
    capital: float
    cash: float
    reserved: float
    open_slots_used: int
    symbols: set[str] = field(default_factory=set)
    sector_counts: dict[str, int] = field(default_factory=dict)
    blocked_reason: str | None = None

    @property
    def free_cash(self) -> float:
        return self.cash - self.reserved


@dataclass
class Candidate:
    signal_id: int
    symbol: str
    sector: str | None
    entry_trigger: float
    entry_limit: float
    stop: float
    target: float
    rr: float
    rr_band: str
    traded_value: float
    qty: int = 0
    rank: int | None = None
    taken: bool = False
    skip_reason: str | None = None
    version: str | None = None      # the strategy version that signalled it (several may share a portfolio)


def size(capital: float, entry_limit: float, stop: float, risk_pct: float, max_position_pct: float) -> int:
    risk_per_share = entry_limit - stop
    if risk_per_share <= 0 or entry_limit <= 0:
        return 0
    qty = math.floor(capital * risk_pct / 100 / risk_per_share)
    cap = math.floor(capital * max_position_pct / 100 / entry_limit)
    return max(0, min(qty, cap))


def rank_key(c: Candidate) -> tuple:
    return (BAND_ORDER.get(c.rr_band, 9), c.entry_limit, -(c.traded_value or 0))


def select(candidates: list[Candidate], acct: AccountState, risk: dict[str, Any]) -> list[Candidate]:
    """Returns fresh Candidate copies for this account with qty, rank, taken and skip_reason set."""
    max_positions = int(risk["max_open_positions"])
    max_per_sector = int(risk["max_per_sector"])
    out: list[Candidate] = []
    for c in candidates:
        d = Candidate(**{k: getattr(c, k) for k in c.__dataclass_fields__})
        d.qty = size(acct.capital, d.entry_limit, d.stop, float(risk["risk_per_trade_pct"]),
                     float(risk["max_position_pct"]))
        out.append(d)
    ranked = sorted(out, key=rank_key)
    slots = max_positions - acct.open_slots_used
    free_cash = acct.free_cash
    sectors = dict(acct.sector_counts)
    for i, c in enumerate(ranked, 1):
        c.rank = i
        cost = c.qty * c.entry_limit
        sector = c.sector or "Unknown"
        if acct.blocked_reason:
            c.skip_reason = acct.blocked_reason
        elif c.qty < 1:
            c.skip_reason = "Risk too wide for budget (qty 0)"
        elif c.symbol in acct.symbols:
            c.skip_reason = "Already held or pending in this account"
        elif slots <= 0:
            c.skip_reason = f"No free slot ({max_positions} positions incl. pending buys): outranked"
        elif sectors.get(sector, 0) >= max_per_sector:
            c.skip_reason = f"Sector cap: {max_per_sector} already in {sector}"
        elif cost > free_cash:
            c.skip_reason = f"Not enough free cash after reservations (need ₹{cost:,.0f}, free ₹{free_cash:,.0f})"
        else:
            c.taken = True
            slots -= 1
            free_cash -= cost
            sectors[sector] = sectors.get(sector, 0) + 1
            acct.symbols.add(c.symbol)
    return ranked
