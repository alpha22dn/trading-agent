"""Tick-driven order monitor (decided 2026-10-01, paper mode).

The minute job stays the safety net. Between minutes, when a streamed price crosses a level that matters (an active
paper GTT trigger or a working order's limit), the monitor runs at once instead of waiting for the next minute; after
a cycle that changed something (a fill), it runs again a second later so the exit goes on within seconds.
At most one cycle per MIN_GAP seconds; only in market hours with a verified login.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

log = logging.getLogger("agent")
MIN_GAP = 1.0

_levels: dict[str, list[float]] = {}         # symbol -> prices where something can happen
_last_px: dict[str, float] = {}
_by_token: dict[int, str] = {}
_wake = threading.Event()
_thread: threading.Thread | None = None
_stop = threading.Event()
stats: dict[str, Any] = {"fast_cycles": 0, "last_fast_cycle": None}


def refresh_levels() -> None:
    """After every cycle: the prices at which the next simulated event can happen."""
    from ..db import connect
    lv: dict[str, list[float]] = {}
    with connect() as conn:
        for r in conn.execute("SELECT symbol, trigger_values FROM paper_gtts WHERE status = 'active'"):
            lv.setdefault(r[0], []).extend(float(x) for x in json.loads(r[1]))
        for r in conn.execute("SELECT symbol, price FROM paper_orders WHERE status = 'open'"):
            if r[1] is not None:
                lv.setdefault(r[0], []).append(float(r[1]))
        marks = ",".join("?" * len(lv))
        toks = {int(r[1]): r[0] for r in conn.execute(
            f"SELECT symbol, instrument_token FROM instruments WHERE exchange = 'NSE' AND symbol IN ({marks})",
            list(lv))} if lv else {}
    global _levels
    _levels = lv
    _by_token.update(toks)


def on_ticks(tokens: list[int]) -> None:
    """Called by the stream with the tokens that just ticked."""
    from ..broker import feed
    st = feed.stream()
    if st is None or not _levels:
        return
    hit = False
    for tok in tokens:
        sym = _by_token.get(tok)
        if sym is None or sym not in _levels:
            continue
        t = st.tick(tok)
        if t is None:
            continue
        prev = _last_px.get(sym, t.ltp)
        _last_px[sym] = t.ltp
        lo, hi = min(prev, t.ltp), max(prev, t.ltp)
        if any(lo <= x <= hi for x in _levels[sym]):
            hit = True
    if hit:
        request()


def request() -> None:
    _wake.set()


def _allowed() -> bool:
    from ..clock import now_ist
    from ..market_calendar import MarketCalendar
    from ..settings import get_app_config
    from .. import scheduler
    app = get_app_config()
    return (app["broker"]["mode"] == "paper" and MarketCalendar(app).is_market_open(now_ist())
            and scheduler.login_verified())


def _run() -> None:
    from ..db import get_state
    from ..scheduler import Skip
    from . import manager
    last = 0.0
    while not _stop.is_set():
        if not _wake.wait(timeout=1.0):
            continue
        _wake.clear()
        gap = MIN_GAP - (time.monotonic() - last)
        if gap > 0:
            time.sleep(gap)
        last = time.monotonic()
        try:
            if not _allowed():
                continue
            manager.on_cycle()
            stats["fast_cycles"] += 1
            from ..clock import ts
            stats["last_fast_cycle"] = ts()
            if int((get_state("monitor_last") or {}).get("events") or 0):
                request()                       # something happened (a fill): next cycle attaches the exit
        except Skip:
            pass
        except Exception:  # noqa: BLE001 - the minute job is the safety net
            log.exception("fast order-monitor cycle failed")


def start() -> None:
    global _thread
    if _thread and _thread.is_alive():
        return
    _stop.clear()
    try:
        refresh_levels()
    except Exception:  # noqa: BLE001
        pass
    _thread = threading.Thread(target=_run, name="fast-monitor", daemon=True)
    _thread.start()


def stop() -> None:
    _stop.set()
    _wake.set()
