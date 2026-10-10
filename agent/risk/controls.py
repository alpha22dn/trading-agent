"""Kill switch and per-account halt state (spec sections 2 and 7).

Kill switch: one action cancels all pending buy GTTs and blocks new entries
until re-armed. The GTT cancellation hooks in once the PaperBroker exists
(Phase 2); the entry block is live from Phase 0 via `entries_blocked()`.
"""
from __future__ import annotations

from typing import Any, Callable

from .. import audit
from ..clock import ts
from ..db import connect, get_state, set_state
from ..events import publish

KILL_KEY = "kill_switch"

# Phase 2 registers a function here that cancels every pending buy GTT and
# returns how many it cancelled.
cancel_pending_buys: Callable[[], int] | None = None


def kill_switch_state() -> dict[str, Any]:
    return get_state(KILL_KEY, {"active": False})


def activate_kill_switch(reason: str = "Activated from dashboard") -> dict[str, Any]:
    cancelled = cancel_pending_buys() if cancel_pending_buys else 0
    state = {"active": True, "activated_at": ts(), "reason": reason, "cancelled_buy_gtts": cancelled}
    set_state(KILL_KEY, state)
    audit.user_action("kill_switch", f"Kill switch ON: {reason}", details=state)
    audit.alert("kill_switch", f"Kill switch is ON. {cancelled} pending buy GTT(s) cancelled; new entries blocked.",
                level="critical")
    publish("controls")
    return state


def rearm_kill_switch() -> dict[str, Any]:
    state = {"active": False, "rearmed_at": ts()}
    set_state(KILL_KEY, state)
    audit.user_action("kill_switch_rearm", "Kill switch re-armed")
    publish("controls")
    return state


def accounts() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT id, name, mode, capital, peak_equity, manual_edits, halted, halt_reason, "
                            "halted_at, strategy_version FROM accounts WHERE COALESCE(archived, 0) = 0 "
                            "ORDER BY capital").fetchall()
    return [dict(r) for r in rows]


def halt_account(account_id: str, reason: str) -> None:
    with connect() as conn:
        conn.execute("UPDATE accounts SET halted = 1, halt_reason = ?, halted_at = ? WHERE id = ?",
                     (reason, ts(), account_id))
    audit.alert("account_halt", f"{account_id} halted: {reason}", level="error")
    publish("controls")


def rearm_account(account_id: str) -> bool:
    """Re-arm after a drawdown halt. The peak restarts from the latest recorded equity, so the 6% limit is
    measured afresh from the point you re-armed (otherwise the next EOD run would halt the account again)."""
    with connect() as conn:
        last = conn.execute("SELECT equity FROM equity_daily WHERE account_id = ? ORDER BY date DESC LIMIT 1",
                            (account_id,)).fetchone()
        cur = conn.execute("UPDATE accounts SET halted = 0, halt_reason = NULL, halted_at = NULL, "
                           "peak_equity = COALESCE(?, peak_equity) WHERE id = ? AND halted = 1",
                           (last[0] if last else None, account_id))
        changed = cur.rowcount > 0
    if changed:
        audit.user_action("account_rearm", f"{account_id} re-armed")
        publish("controls")
    return changed


def entries_blocked(account_id: str | None = None) -> str | None:
    """Reason new entries are blocked, or None. Checked before any buy GTT (Phase 2)."""
    if kill_switch_state().get("active"):
        return "Kill switch is on"
    if account_id:
        with connect() as conn:
            row = conn.execute("SELECT halted, halt_reason FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            return f"Unknown account {account_id}"
        if row["halted"]:
            return f"Account halted: {row['halt_reason']}"
    return None
