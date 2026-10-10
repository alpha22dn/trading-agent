"""audit_log: every job run, API call, alert and user action."""
from __future__ import annotations

import json
import logging
from typing import Any

from .clock import ts
from .db import connect
from .events import publish

log = logging.getLogger("agent")

LEVELS = ("info", "warning", "error", "critical")
listeners: list = []   # called with (name, message, level) for every alert: Telegram registers here


def record(kind: str, name: str, message: str = "", level: str = "info",
           details: dict[str, Any] | None = None) -> int:
    if level not in LEVELS:
        raise ValueError(f"bad level {level}")
    with connect() as conn:
        cur = conn.execute(
            "INSERT INTO audit_log (ts, kind, name, level, message, details) VALUES (?, ?, ?, ?, ?, ?)",
            (ts(), kind, name, level, message, json.dumps(details, default=str) if details else None),
        )
        row_id = int(cur.lastrowid)
    getattr(log, "warning" if level == "warning" else ("error" if level in ("error", "critical") else "info"))(
        "[%s] %s: %s", kind, name, message)
    if kind == "alert":
        publish("alerts")
    return row_id


def alert(name: str, message: str, level: str = "warning", details: dict[str, Any] | None = None) -> int:
    """Raise an alert. Shown on the Overview until acknowledged; critical and error alerts also go to Telegram."""
    row = record("alert", name, message, level, details)
    for fn in listeners:
        try:
            fn(name, message, level)
        except Exception:  # noqa: BLE001 - a notification must never break the caller
            log.exception("alert listener failed")
    return row


def user_action(name: str, message: str = "", details: dict[str, Any] | None = None) -> int:
    return record("user", name, message, "info", details)


def active_alerts(limit: int = 50) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT id, ts, name, level, message, details FROM audit_log "
            "WHERE kind = 'alert' AND acknowledged = 0 ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) | {"details": json.loads(r["details"]) if r["details"] else None} for r in rows]


def acknowledge(alert_id: int) -> bool:
    with connect() as conn:
        cur = conn.execute("UPDATE audit_log SET acknowledged = 1 WHERE id = ? AND kind = 'alert'", (alert_id,))
        changed = cur.rowcount > 0
    if changed:
        publish("alerts")
    return changed


def acknowledge_all(name: str) -> int:
    """Dismiss every active alert of one kind (e.g. a batch of price-change alerts from one run)."""
    with connect() as conn:
        n = conn.execute("UPDATE audit_log SET acknowledged = 1 WHERE kind = 'alert' AND name = ? AND "
                         "COALESCE(acknowledged, 0) = 0", (name,)).rowcount
    if n:
        publish("alerts")
    return n


def recent(limit: int = 100, kind: str | None = None) -> list[dict[str, Any]]:
    sql = "SELECT id, ts, kind, name, level, message FROM audit_log"
    args: tuple[Any, ...] = ()
    if kind:
        sql += " WHERE kind = ?"
        args = (kind,)
    sql += " ORDER BY id DESC LIMIT ?"
    with connect() as conn:
        rows = conn.execute(sql, args + (limit,)).fetchall()
    return [dict(r) for r in rows]
