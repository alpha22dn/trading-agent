"""Telegram notifications (spec section 10), chosen 2026-09-29: critical alerts and the daily EOD summary.

Set TELEGRAM_BOT_TOKEN (from @BotFather) and TELEGRAM_CHAT_ID in .env. Messages go out on a background worker, so
an alert raised inside order handling never waits on the network; a failure is logged, never raised. The token is
part of the request URL, so errors are scrubbed of it before they are logged. The same message is not repeated
within DEDUP_MIN minutes.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any

import requests

from .. import audit
from ..clock import now_ist
from ..db import connect, get_state, set_state
from ..settings import get_secrets

log = logging.getLogger("agent")
API = "https://api.telegram.org/bot{token}/sendMessage"
DEDUP_MIN = 30
ALERT_LEVELS = ("critical", "error")
_q: "queue.Queue[str]" = queue.Queue()
_worker: threading.Thread | None = None
_recent: dict[str, float] = {}


def configured() -> bool:
    s = get_secrets()
    return bool(s.telegram_bot_token and s.telegram_chat_id)


def _scrub(text: str) -> str:
    tok = get_secrets().telegram_bot_token
    return text.replace(tok, "***") if tok else text


def send_now(text: str) -> None:
    """Send synchronously (the test button). Raises RuntimeError with a readable, token-free message."""
    s = get_secrets()
    if not configured():
        raise RuntimeError("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env, then restart run.bat")
    try:
        r = requests.post(API.format(token=s.telegram_bot_token), timeout=15,
                          json={"chat_id": s.telegram_chat_id, "text": text[:4000], "disable_web_page_preview": True})
    except requests.RequestException as exc:
        raise RuntimeError(_scrub(f"Telegram not reachable: {type(exc).__name__}")) from None
    if r.status_code != 200:
        try:
            desc = r.json().get("description", "")
        except ValueError:
            desc = r.text[:200]
        raise RuntimeError(_scrub(f"Telegram refused the message ({r.status_code}): {desc}"))


def _run() -> None:
    while True:
        text = _q.get()
        try:
            send_now(text)
        except RuntimeError as exc:
            log.warning("telegram: %s", exc)
        except Exception:  # noqa: BLE001
            log.warning("telegram: unexpected error", exc_info=False)


def send(text: str) -> bool:
    """Queue a message; False when Telegram is not set up or the same text went out recently."""
    global _worker
    if not configured():
        return False
    now = time.monotonic()
    if now - _recent.get(text, -1e9) < DEDUP_MIN * 60:
        return False
    _recent[text] = now
    if _worker is None or not _worker.is_alive():
        _worker = threading.Thread(target=_run, name="telegram", daemon=True)
        _worker.start()
    _q.put(text)
    return True


_live_db: list = [None]   # set by the dashboard server (api/app.py) to the database it trades on


def bind_live(path) -> None:
    _live_db[0] = path


def _live() -> bool:
    """Only the live agent sends alerts. Backtests, replays and learning runs work on their own database copies
    (or in their own process, where nothing is bound): their alerts stay in those copies."""
    from ..db import current_db_path
    return _live_db[0] is not None and current_db_path() == _live_db[0]


def on_alert(name: str, message: str, level: str) -> None:
    if level in ALERT_LEVELS and _live():
        send(f"{'🔴' if level == 'critical' else '🟠'} Trading agent · {name.replace('_', ' ')}\n{message}")


audit.listeners.append(on_alert)


# ---------- the daily EOD summary ----------

def eod_text(day: str) -> str:
    from ..orders import views
    s = views.accounts_summary()
    lines = [f"📊 EOD {day}"]
    with connect() as conn:
        prev = {r[0]: r[1] for r in conn.execute(
            "SELECT account_id, equity FROM equity_daily e WHERE date = (SELECT MAX(date) FROM equity_daily "
            "WHERE account_id = e.account_id AND date < ?)", (day,))}
        placed = dict(conn.execute("SELECT account_id, COUNT(*) FROM trades WHERE placed_on = ? GROUP BY account_id",
                                   (day,)).fetchall())
        closed = [dict(r) for r in conn.execute("SELECT account_id, symbol, net_pnl, r_multiple, exit_reason FROM trades "
                                                "WHERE state = 'CLOSED' AND substr(closed_at, 1, 10) = ?", (day,))]
    for a in s["accounts"]:
        chg = a["equity"] - prev[a["id"]] if a["id"] in prev else None
        lines.append(f"{a['id']}: ₹{a['equity']:,.0f} ({a['return_pct']:+.2f}%"
                     + (f", today {'+' if chg >= 0 else '−'}₹{abs(chg):,.0f}" if chg is not None else "") + ")"
                     + f" · {a['open_positions']} open, {a['pending_buys']} buy GTT(s)"
                     + (f", {placed[a['id']]} new" if placed.get(a["id"]) else "")
                     + (" · HALTED" if a.get("halted") else ""))
    if closed:
        lines.append("Closed today:")
        lines += [f"  {c['account_id']} {c['symbol']}: {'+' if (c['net_pnl'] or 0) >= 0 else '−'}₹{abs(c['net_pnl'] or 0):,.0f}"
                  + (f" ({c['r_multiple']:+.2f}R)" if c["r_multiple"] is not None else "") + f", {c['exit_reason']}"
                  for c in closed]
    scan = get_state("last_scan") or {}
    if scan.get("date") == day:
        lines.append(f"Signals: {scan.get('triggers', 0)} triggers, {scan.get('eligible', 0)} tradeable")
    return "\n".join(lines)


def eod_summary(day: str) -> bool:
    """Once per session (a catch-up re-run does not send it twice)."""
    if not configured() or get_state("telegram_eod_sent") == day:
        return False
    ok = send(eod_text(day))
    if ok:
        set_state("telegram_eod_sent", day)
    return ok


def status() -> dict[str, Any]:
    return {"configured": configured(), "sends": ["critical and error alerts", "daily EOD summary"],
            "checked_at": now_ist().isoformat(timespec="seconds")}
