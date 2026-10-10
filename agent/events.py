"""Tiny pub/sub that pushes 'something changed' messages to dashboard WebSockets.

Jobs run in scheduler threads; `publish` is thread-safe and hands the message
to the asyncio loop that owns the WebSocket connections.
"""
from __future__ import annotations

import asyncio
import json
import threading
from typing import Any


class Broadcaster:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queues: set[asyncio.Queue[str]] = set()
        self._lock = threading.Lock()

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self) -> asyncio.Queue[str]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=100)
        with self._lock:
            self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[str]) -> None:
        with self._lock:
            self._queues.discard(q)

    def publish(self, topic: str, data: Any = None) -> None:
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        message = json.dumps({"topic": topic, "data": data}, default=str)
        loop.call_soon_threadsafe(self._fan_out, message)

    def _fan_out(self, message: str) -> None:
        with self._lock:
            queues = list(self._queues)
        for q in queues:
            if q.full():
                try:
                    q.get_nowait()  # drop the oldest; clients refetch anyway
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(message)


broadcaster = Broadcaster()


def publish(topic: str, data: Any = None) -> None:
    broadcaster.publish(topic, data)
