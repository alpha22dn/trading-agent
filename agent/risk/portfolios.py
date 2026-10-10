"""Paper portfolios (the Accounts screen), decided 2026-09-25.

- Add a portfolio: a short id, a name, a starting capital, and whether paper hand-edits are allowed on it.
- Edit: the name and the hand-edit switch any time. The capital only while the portfolio is flat (no open
  positions, no pending buys, nothing held outside the system): it then starts again at the new amount.
- Reset: cancel every pending buy and GTT, sell every holding at market (live price in market hours, else the
  latest price), then start again at the chosen capital. History is kept; the equity curve, return, drawdown and
  trade statistics restart from the reset (accounts.reset_at).

Once created, a portfolio lives in the database; config/app.yaml only seeds the first ones.
"""
from __future__ import annotations

import re
from typing import Any

from .. import audit
from ..broker.base import BrokerReject
from ..broker.paper import get_broker
from ..clock import now_ist, ts
from ..db import connect
from ..events import publish
from ..settings import get_app_config

MIN_CAPITAL, MAX_CAPITAL = 1000.0, 1e8
ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,11}$")
RESERVED_IDS = {"all", "backtest", "custom"}


class PortfolioError(ValueError):
    pass


def _capital(value: Any) -> float:
    try:
        x = round(float(value), 2)
    except (TypeError, ValueError) as exc:
        raise PortfolioError("Enter the capital in rupees") from exc
    if not MIN_CAPITAL <= x <= MAX_CAPITAL:
        raise PortfolioError("Capital must be between ₹1,000 and ₹10 crore")
    return x


def _row(account_id: str) -> dict[str, Any]:
    with connect() as conn:
        r = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if not r:
        raise PortfolioError(f"No portfolio {account_id}")
    return dict(r)


def holdings(account_id: str) -> dict[str, int]:
    """What a portfolio has in play: holding trades, pending buys, stocks held outside the system."""
    with connect() as conn:
        pos = conn.execute("SELECT COUNT(*) FROM trades WHERE account_id = ? AND state IN ('FILLED', 'PROTECTED', "
                           "'TRAILING')", (account_id,)).fetchone()[0]
        pend = conn.execute("SELECT COUNT(*) FROM trades WHERE account_id = ? AND state = 'BUY_PENDING'",
                            (account_id,)).fetchone()[0]
        out = conn.execute("SELECT COUNT(*) FROM unmanaged_holdings WHERE account_id = ? AND qty > 0",
                           (account_id,)).fetchone()[0]
    broker_pos = len(get_broker().positions(account_id))
    return {"positions": pos, "pending_buys": pend, "outside": out, "broker_positions": broker_pos,
            "flat": pos == 0 and pend == 0 and out == 0 and broker_pos == 0}


def _log(conn, account_id: str, kind: str, before: float | None, after: float, equity: float | None, note: str) -> None:
    conn.execute("INSERT INTO capital_changes (account_id, at, kind, capital_before, capital_after, equity_before, note) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?)", (account_id, ts(), kind, before, after, equity, note))


def _restart(account_id: str, capital: float, kind: str, note: str) -> None:
    """Start the portfolio's run again at `capital` (it must hold nothing)."""
    a = _row(account_id)
    today = now_ist().date().isoformat()
    with connect() as conn:
        _log(conn, account_id, kind, a["capital"], capital, a["cash"], note)
        conn.execute("UPDATE accounts SET capital = ?, cash = ?, peak_equity = ?, halted = 0, halt_reason = NULL, "
                     "halted_at = NULL, reset_at = ? WHERE id = ?", (capital, capital, capital, ts(), account_id))
        # the curve restarts at 0% today (a same-day point from before the restart is replaced)
        conn.execute("INSERT OR REPLACE INTO equity_daily (account_id, date, cash, holdings, equity, peak_equity) "
                     "VALUES (?, ?, ?, 0, ?, ?)", (account_id, today, capital, capital, capital))
    publish("controls")


def create(account_id: str, name: str, capital: Any, manual_edits: bool = False,
           strategy_ids: list[int] | None = None) -> dict[str, Any]:
    """strategy_ids: the strategies it trades (Strategies screen checkboxes); None = every ON strategy."""
    account_id = (account_id or "").strip()
    if not ID_RE.match(account_id) or account_id.lower() in RESERVED_IDS:
        raise PortfolioError("Id: 1–12 letters, digits, - or _, starting with a letter (e.g. P200)")
    name = (name or "").strip()[:60] or account_id
    cap = _capital(capital)
    with connect() as conn:
        if conn.execute("SELECT 1 FROM accounts WHERE lower(id) = lower(?)", (account_id,)).fetchone():
            raise PortfolioError(f"A portfolio called {account_id} already exists")
        conn.execute("INSERT INTO accounts (id, name, mode, capital, peak_equity, manual_edits, created_at, cash, reset_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (account_id, name, get_app_config()["broker"]["mode"], cap, cap,
                                                            1 if manual_edits else 0, ts(), cap, ts()))
        _log(conn, account_id, "created", None, cap, None, "")
        known = {r[0]: r[1] for r in conn.execute("SELECT id, enabled FROM strategies WHERE deleted_at IS NULL")}
        ids = [i for i in known if known[i]] if strategy_ids is None else [int(i) for i in strategy_ids if int(i) in known]
        conn.executemany("INSERT INTO strategy_portfolios (strategy_id, account_id) VALUES (?, ?)",
                         [(i, account_id) for i in ids])
        names = [r[0] for r in conn.execute(f"SELECT name FROM strategies WHERE id IN ({','.join('?' * len(ids))})", ids)] if ids else []
    audit.user_action("portfolio_created", f"{account_id} ({name}) created with ₹{cap:,.0f}"
                      + (", hand edits allowed" if manual_edits else "")
                      + (f"; trades {', '.join(names)}" if names else "; no strategy ticked yet"))
    publish("strategies")
    publish("controls")
    return _row(account_id)


def edit(account_id: str, name: str | None = None, capital: Any = None, manual_edits: bool | None = None) -> dict[str, Any]:
    a = _row(account_id)
    changes = []
    if capital not in (None, "") and _capital(capital) != float(a["capital"]):
        cap = _capital(capital)
        h = holdings(account_id)
        if not h["flat"]:
            raise PortfolioError(f"{account_id} has {h['positions']} open position(s), {h['pending_buys']} pending buy(s) "
                                 f"and {h['outside']} holding(s) outside the system. Capital can only be changed when "
                                 "it holds nothing; use Reset to close everything and start again.")
        _restart(account_id, cap, "capital_changed", f"capital ₹{a['capital']:,.0f} → ₹{cap:,.0f}")
        changes.append(f"capital ₹{a['capital']:,.0f} → ₹{cap:,.0f} (restarted)")
    with connect() as conn:
        if name is not None and name.strip() and name.strip()[:60] != a["name"]:
            conn.execute("UPDATE accounts SET name = ? WHERE id = ?", (name.strip()[:60], account_id))
            changes.append(f"name → {name.strip()[:60]}")
        if manual_edits is not None and bool(manual_edits) != bool(a["manual_edits"]):
            conn.execute("UPDATE accounts SET manual_edits = ? WHERE id = ?", (1 if manual_edits else 0, account_id))
            changes.append("hand edits " + ("allowed" if manual_edits else "off"))
    if changes:
        audit.user_action("portfolio_edited", f"{account_id}: " + "; ".join(changes))
        publish("controls")
    return _row(account_id) | {"changes": changes}


def reset(account_id: str, capital: Any = None) -> dict[str, Any]:
    """Close everything at market, then start again at `capital` (default: the current capital)."""
    from ..orders import manager
    a = _row(account_id)
    cap = _capital(capital) if capital not in (None, "") else float(a["capital"])
    try:
        closed = manager.close_account(account_id)
    except BrokerReject as exc:
        audit.alert("portfolio_reset", f"{account_id} reset stopped: {exc}. Nothing was restarted; try again later.",
                    level="error")
        raise PortfolioError(f"Reset stopped: {exc}. Positions already sold stay sold; try again later.") from exc
    after = _row(account_id)
    _restart(account_id, cap, "reset", f"closed {closed['closed']} trade(s), cancelled {closed['cancelled_buys']} "
                                       f"buy(s), sold {closed['outside_sold']} outside holding(s); cash was "
                                       f"₹{after['cash']:,.2f}")
    audit.user_action("portfolio_reset", f"{account_id} reset to ₹{cap:,.0f}: closed {closed['closed']} trade(s), "
                      f"cancelled {closed['cancelled_buys']} pending buy(s), sold {closed['outside_sold']} outside "
                      f"holding(s); cash before restart ₹{after['cash']:,.2f}")
    return {"account": _row(account_id), "closed": closed, "cash_before_restart": round(after["cash"], 2)}


def history(account_id: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM capital_changes WHERE account_id = ? ORDER BY id DESC",
                                              (account_id,))]
