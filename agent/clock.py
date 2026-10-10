"""Single source of 'now' so tests and replay mode can control time."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")


class Clock:
    def now(self) -> datetime:
        return datetime.now(IST)


class FixedClock(Clock):
    """Test/replay clock. Accepts naive datetimes as IST."""

    def __init__(self, at: datetime):
        self.set(at)

    def set(self, at: datetime) -> None:
        self._at = at if at.tzinfo else at.replace(tzinfo=IST)

    def now(self) -> datetime:
        return self._at


_clock: Clock = Clock()


def get_clock() -> Clock:
    return _clock


def set_clock(clock: Clock) -> None:
    global _clock
    _clock = clock


def now_ist() -> datetime:
    return _clock.now().astimezone(IST)


def ts() -> str:
    """Timestamp string for database rows (IST, second precision)."""
    return now_ist().isoformat(timespec="seconds")
