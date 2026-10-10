"""NSE trading calendar: weekends + holidays from config/app.yaml."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .clock import IST, now_ist
from .settings import AppConfig


def _t(value: str) -> time:
    h, m = value.split(":")
    return time(int(h), int(m))


class MarketCalendar:
    def __init__(self, app: AppConfig):
        self.holidays = {date.fromisoformat(d): name for d, name in app.holidays.items()}
        self.valid_through = date.fromisoformat(str(app.get("holidays_valid_through", "1970-01-01")))
        m = app["market"]
        self.open_t = _t(m["open"])
        self.close_t = _t(m["close"])
        self.eod_t = _t(m["eod_after"])

    def holiday_name(self, d: date) -> str | None:
        return self.holidays.get(d)

    def is_trading_day(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.holidays

    def calendar_expired(self, d: date) -> bool:
        """True when `d` is beyond the last date the holiday list covers."""
        return d > self.valid_through

    def next_trading_day(self, d: date) -> date:
        d = d + timedelta(days=1)
        while not self.is_trading_day(d):
            d += timedelta(days=1)
        return d

    def previous_trading_day(self, d: date) -> date:
        d = d - timedelta(days=1)
        while not self.is_trading_day(d):
            d -= timedelta(days=1)
        return d

    def is_market_open(self, at: datetime | None = None) -> bool:
        at = (at or now_ist()).astimezone(IST)
        return self.is_trading_day(at.date()) and self.open_t <= at.time() < self.close_t

    def last_completed_session(self, at: datetime | None = None) -> date:
        """Most recent trading day whose daily candle is final (after eod_after)."""
        at = (at or now_ist()).astimezone(IST)
        today = at.date()
        if self.is_trading_day(today) and at.time() >= self.eod_t:
            return today
        return self.previous_trading_day(today)

    def trading_days_between(self, start: date, end: date) -> int:
        """Trading days in (start, end]."""
        n, d = 0, start
        while d < end:
            d += timedelta(days=1)
            if self.is_trading_day(d):
                n += 1
        return n
