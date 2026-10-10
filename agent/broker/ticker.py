"""Kite streaming quotes (WebSocket), decided 2026-10-01.

One connection to wss://ws.kite.trade in its own thread (asyncio + the `websockets` library; Kite's own KiteTicker
runs on Twisted, whose reactor cannot be restarted in one process, which a daily re-login needs).

- Runs only in the market window (09:00–15:35 on trading days) while the Kite session is valid; outside it, the
  connection is closed. A new login (new token) reconnects. Reconnects back off 1 s → 30 s.
- Mode "full": last price, the day's open/high/low/close, 5-level depth, exchange time. Kite's stream carries no
  circuit limits; broker/feed.py adds them from the REST quote (refreshed every 30 minutes).
- `want(tokens)` subscribes; tokens stay subscribed for the day (the set is cleared when the window closes).
- `base()` (optional): tokens subscribed on every connection (decided 2026-10-01: every NSE500 stock + Nifty 50).
  Kite allows 3,000 instruments per connection.
- Health: Kite sends a 1-byte heartbeat every second, so no message for STALE_SEC means a stalled connection;
  callers then fall back to REST quotes.
- `on_ticks(tokens)` is called (in the stream's thread) with the tokens that ticked, for the fast order monitor.
"""
from __future__ import annotations

import asyncio
import json
import logging
import struct
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, time as dtime
from typing import Any, Callable

log = logging.getLogger("agent")

ROOT_URI = "wss://ws.kite.trade"
STALE_SEC = 5.0
WINDOW = (dtime(9, 0), dtime(15, 35))


@dataclass
class Tick:
    ltp: float
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    bids: list[tuple[float, int]] | None = None
    asks: list[tuple[float, int]] | None = None
    exchange_time: datetime | None = None
    volume: int | None = None              # traded today (cumulative), quote/full packets
    exchange_ts: int | None = None         # exchange timestamp, epoch seconds (full packets)
    received: float = field(default_factory=time.monotonic)


# ---------- Kite's binary tick format (kite.trade/docs/connect/v3/websocket) ----------

def _u(b: bytes, s: int, e: int, fmt: str = "I") -> int:
    return struct.unpack(">" + fmt, b[s:e])[0]


def parse(payload: bytes) -> dict[int, Tick]:
    """Equity/index packets: LTP (8 bytes), index quote/full (28/32), quote (44), full (184). Prices in paise."""
    out: dict[int, Tick] = {}
    if len(payload) < 2:
        return out                          # heartbeat
    n = _u(payload, 0, 2, "H")
    j = 2
    for _ in range(n):
        size = _u(payload, j, j + 2, "H")
        p = payload[j + 2:j + 2 + size]
        j += 2 + size
        tok = _u(p, 0, 4)
        div = 100.0
        if size == 8:
            out[tok] = Tick(ltp=_u(p, 4, 8) / div)
        elif size in (28, 32):
            out[tok] = Tick(ltp=_u(p, 4, 8) / div, high=_u(p, 8, 12) / div, low=_u(p, 12, 16) / div,
                            open=_u(p, 16, 20) / div, close=_u(p, 20, 24) / div)
        elif size in (44, 184):
            t = Tick(ltp=_u(p, 4, 8) / div, open=_u(p, 28, 32) / div, high=_u(p, 32, 36) / div,
                     low=_u(p, 36, 40) / div, close=_u(p, 40, 44) / div, volume=_u(p, 16, 20))
            if size == 184:
                t.exchange_ts = _u(p, 60, 64) or None
                try:
                    t.exchange_time = datetime.fromtimestamp(_u(p, 60, 64))
                except (OverflowError, OSError, ValueError):
                    t.exchange_time = None
                levels = [(_u(p, k + 4, k + 8) / div, _u(p, k, k + 4)) for k in range(64, 184, 12)]
                t.bids = [x for x in levels[:5] if x[1]] or None
                t.asks = [x for x in levels[5:] if x[1]] or None
            out[tok] = t
    return out


# ---------- the stream ----------

class Stream:
    def __init__(self, credentials: Callable[[], tuple[str, str] | None], window_open: Callable[[], bool],
                 root: str = ROOT_URI, base: Callable[[], set[int] | list[int]] | None = None):
        """credentials() -> (api_key, access_token) or None when there is no valid session."""
        self.credentials, self.window_open, self.root, self.base = credentials, window_open, root, base
        self.on_ticks: Callable[[list[int]], None] | None = None
        self.listeners: list[Callable[[dict[int, Tick]], None]] = []   # get the parsed ticks (1-second recorder)
        self._ticks: dict[int, Tick] = {}
        self._fresh: set[int] = set()             # ticked on the current connection
        self._wanted: set[int] = set()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ws = None
        self.connected = False
        self.last_message = 0.0
        self.connects = 0
        self.last_error: str | None = None
        self.ticks_received = 0

    # ---- control (any thread) ----

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="kite-stream", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        ws, loop = self._ws, self._loop
        if ws is not None and loop is not None:
            try:
                asyncio.run_coroutine_threadsafe(ws.close(), loop)
            except RuntimeError:
                pass
        if self._thread:
            self._thread.join(timeout=5)

    def want(self, tokens: set[int] | list[int]) -> None:
        new = set(int(t) for t in tokens) - self._wanted
        if not new:
            return
        self._wanted |= new
        ws, loop = self._ws, self._loop
        if ws is not None and loop is not None and self.connected:
            asyncio.run_coroutine_threadsafe(self._subscribe(ws, sorted(new)), loop)

    def healthy(self) -> bool:
        return self.connected and time.monotonic() - self.last_message < STALE_SEC

    def tick(self, token: int) -> Tick | None:
        """The latest tick of a token that has ticked on the current, healthy connection."""
        if not self.healthy():
            return None
        with self._lock:
            return self._ticks.get(token) if token in self._fresh else None

    def changed_since(self, since: float) -> dict[int, Tick]:
        """Ticks received after `since` (time.monotonic) on the current, healthy connection."""
        if not self.healthy():
            return {}
        with self._lock:
            return {t: k for t, k in self._ticks.items() if t in self._fresh and k.received > since}

    def status(self) -> dict[str, Any]:
        return {"connected": self.connected, "healthy": self.healthy(), "subscribed": len(self._wanted),
                "streaming": len(self._fresh), "connects": self.connects, "ticks": self.ticks_received,
                "last_message_age": round(time.monotonic() - self.last_message, 1) if self.last_message else None,
                "last_error": self.last_error}

    # ---- the stream's own thread ----

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        try:
            self._loop.run_until_complete(self._main())
        finally:
            self._loop.close()
            self._loop = None

    async def _subscribe(self, ws, tokens: list[int]) -> None:
        for k in range(0, len(tokens), 500):
            chunk = tokens[k:k + 500]
            await ws.send(json.dumps({"a": "subscribe", "v": chunk}))
            await ws.send(json.dumps({"a": "mode", "v": ["full", chunk]}))

    async def _main(self) -> None:
        import websockets
        delay = 1.0
        while not self._stop.is_set():
            creds = self.credentials()
            if not creds or not self.window_open():
                if not self.window_open():
                    self._wanted.clear()
                await asyncio.sleep(2)
                continue
            api_key, token = creds
            url = f"{self.root}?api_key={api_key}&access_token={token}"
            try:
                async with websockets.connect(url, open_timeout=10, ping_interval=None, max_size=None,
                                              additional_headers={"X-Kite-Version": "3"}) as ws:
                    self._ws = ws
                    self.connected = True
                    self.connects += 1
                    self.last_message = time.monotonic()
                    with self._lock:
                        self._fresh.clear()
                    if self.base is not None:
                        try:
                            self._wanted |= {int(t) for t in self.base()}
                        except Exception:  # noqa: BLE001 - the universe is a convenience; held stocks still subscribe
                            log.exception("could not read the stocks to subscribe")
                    if self._wanted:
                        await self._subscribe(ws, sorted(self._wanted))
                    log.info("Kite price stream connected (%d stocks subscribed)", len(self._wanted))
                    delay = 1.0
                    self.last_error = None
                    checked = time.monotonic()
                    while not self._stop.is_set():
                        if time.monotonic() - checked >= 1.0:   # at most once a second (they read the database)
                            checked = time.monotonic()
                            if self.credentials() != creds or not self.window_open():
                                break                 # new login or the window closed: reconnect / idle
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=1.0)
                        except asyncio.TimeoutError:
                            if time.monotonic() - self.last_message > 3 * STALE_SEC:
                                raise ConnectionError("no data or heartbeat from Kite for 15 s")
                            continue
                        self.last_message = time.monotonic()
                        if isinstance(msg, (bytes, bytearray)):
                            ticks = parse(bytes(msg))
                            if ticks:
                                with self._lock:
                                    self._ticks.update(ticks)
                                    self._fresh.update(ticks)
                                self.ticks_received += len(ticks)
                                if self.on_ticks:
                                    try:
                                        self.on_ticks(list(ticks))
                                    except Exception:  # noqa: BLE001 - a listener never breaks the stream
                                        log.exception("tick listener failed")
                                for fn in self.listeners:
                                    try:
                                        fn(ticks)
                                    except Exception:  # noqa: BLE001
                                        log.exception("tick listener failed")
                        else:
                            try:
                                m = json.loads(msg)
                            except ValueError:
                                continue
                            if m.get("type") == "error":
                                self.last_error = str(m.get("data"))[:200]
                                log.warning("Kite stream error: %s", self.last_error)
            except Exception as exc:  # noqa: BLE001 - reconnect with backoff
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}".replace(token, "***")
                log.warning("Kite stream disconnected: %s", self.last_error)
            finally:
                self.connected = False
                self._ws = None
            if not self._stop.is_set():
                await asyncio.sleep(delay)
                delay = min(30.0, delay * 2)
