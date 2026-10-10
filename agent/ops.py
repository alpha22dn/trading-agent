"""Keeping an eye on the agent itself (decided 2026-10-01, for running around the clock on a server).

- Start notice: a Telegram message every time the agent starts. If the previous run did not stop cleanly
  (crash, power cut, forced reboot), it says so and when the agent was last seen alive.
- Heartbeat: every 5 minutes the agent records that it is alive and, when HEALTHCHECK_URL is set in .env, pings that
  URL (e.g. a free Healthchecks.io check). If the pings stop, that service alerts you: a stopped server cannot send
  its own alert. The ping carries nothing but the request itself.
"""
from __future__ import annotations

import logging
import socket
import threading
from datetime import datetime
from typing import Any

from .clock import IST, now_ist, ts
from .db import get_state, set_state

log = logging.getLogger("agent")
STATE_KEY = "agent_process"
BEAT_SEC = 300
_stop = threading.Event()
_thread: threading.Thread | None = None


def _fmt(iso: str | None) -> str:
    if not iso:
        return "unknown"
    return datetime.fromisoformat(iso).astimezone(IST).strftime("%d %b %H:%M")


def started() -> str:
    """Record this start; return the start notice."""
    prev = get_state(STATE_KEY) or {}
    host = socket.gethostname()
    if not prev:
        text = f"🟢 Trading agent started on {host}."
    elif prev.get("clean_stop"):
        text = f"🟢 Trading agent started on {host} (stopped normally at {_fmt(prev.get('stopped_at'))})."
    else:
        text = (f"🟠 Trading agent restarted on {host} after an unexpected stop. Last seen alive "
                f"{_fmt(prev.get('last_alive'))}; missed jobs catch up once Kite confirms the login.")
    set_state(STATE_KEY, {"started_at": ts(), "last_alive": ts(), "clean_stop": False, "host": host})
    return text


def stopped() -> None:
    st = get_state(STATE_KEY) or {}
    set_state(STATE_KEY, st | {"clean_stop": True, "stopped_at": ts()})


def beat() -> dict[str, Any]:
    from .settings import get_secrets
    st = get_state(STATE_KEY) or {}
    set_state(STATE_KEY, st | {"last_alive": ts()})
    url = get_secrets().healthcheck_url
    if not url:
        return {"pinged": False}
    import requests
    try:
        requests.get(url, timeout=10)
        return {"pinged": True}
    except requests.RequestException as exc:
        log.warning("uptime ping failed: %s", type(exc).__name__)       # the URL is not logged
        return {"pinged": False, "error": type(exc).__name__}


def _run() -> None:
    while not _stop.wait(BEAT_SEC):
        try:
            beat()
        except Exception:  # noqa: BLE001 - monitoring never stops the agent
            log.exception("heartbeat failed")


def start() -> str:
    """At server start: the notice (sent to Telegram), the first ping, then a ping every 5 minutes."""
    global _thread
    text = started()
    from .notify import telegram
    telegram.send(text)
    threading.Thread(target=beat, name="heartbeat-first", daemon=True).start()
    _stop.clear()
    if not (_thread and _thread.is_alive()):
        _thread = threading.Thread(target=_run, name="heartbeat", daemon=True)
        _thread.start()
    return text


def stop() -> None:
    _stop.set()
    stopped()


def status() -> dict[str, Any]:
    from .settings import get_secrets
    return (get_state(STATE_KEY) or {}) | {"uptime_check": bool(get_secrets().healthcheck_url), "now": now_ist().isoformat()}
