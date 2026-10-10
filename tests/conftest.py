"""Shared fixtures: temp database, fixed IST clock, fake Kite client."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pytest

from agent import clock, db, settings
from agent.broker import kite_session
from agent.data import candles
from agent.market_calendar import MarketCalendar

NIFTY_TOKEN = 256265


class FakeKite:
    """Stands in for kiteconnect.KiteConnect. Prices are deterministic per token/date."""

    def __init__(self, api_key: str = "k", timeout: int | None = None):
        self.api_key = api_key
        self.access_token: str | None = None
        self.calls: list[tuple] = []
        self.price_shift: dict[int, float] = {}   # token -> multiplier (simulates split adjustment)
        self.fail_tokens: set[int] = set()        # tokens that raise InputException
        self.token_error = False                  # every call raises TokenException
        self.symbols = ["RELIANCE", "TCS", "INFY", "HDFCBANK", "BEL-BE"]

    # login
    def login_url(self) -> str:
        return f"https://kite.zerodha.com/connect/login?v=3&api_key={self.api_key}"

    def generate_session(self, request_token: str, api_secret: str) -> dict[str, Any]:
        self.calls.append(("generate_session", request_token))
        if request_token == "bad":
            from kiteconnect.exceptions import TokenException
            raise TokenException("Invalid request token")
        return {"access_token": "at-" + request_token, "user_id": "AB1234", "user_name": "Ani"}

    def profile(self) -> dict[str, Any]:
        self.calls.append(("profile",))
        if self.token_error:
            from kiteconnect.exceptions import TokenException
            raise TokenException("Incorrect `api_key` or `access_token`.")
        return {"user_id": "AB1234", "user_name": "Ani"}

    def set_access_token(self, token: str) -> None:
        self.access_token = token

    def invalidate_access_token(self, token: str) -> None:
        self.calls.append(("invalidate", token))

    # data
    def instruments(self, exchange: str) -> list[dict[str, Any]]:
        self._check()
        rows = [{"instrument_token": 1000 + i, "exchange_token": i, "tradingsymbol": s, "name": s,
                 "segment": "NSE", "instrument_type": "EQ", "exchange": "NSE", "tick_size": 0.05,
                 "lot_size": 1} for i, s in enumerate(self.symbols)]
        rows.append({"instrument_token": NIFTY_TOKEN, "exchange_token": 1, "tradingsymbol": "NIFTY 50",
                     "name": "NIFTY 50", "segment": "INDICES", "instrument_type": "EQ", "exchange": "NSE",
                     "tick_size": 0, "lot_size": 0})
        rows.append({"instrument_token": 9999, "exchange_token": 9, "tradingsymbol": "NIFTY24OCTFUT",
                     "segment": "NFO-FUT", "instrument_type": "FUT", "exchange": "NFO"})
        return rows

    def historical_data(self, token: int, from_date, to_date, interval: str, continuous=False, oi=False):
        self._check()
        self.calls.append(("historical", token, from_date, to_date))
        if token in self.fail_tokens:
            from kiteconnect.exceptions import InputException
            raise InputException("invalid token")
        if interval != "day":
            step = {"minute": 1, "5minute": 5, "15minute": 15, "60minute": 60}[interval]
            out, d = [], from_date.date() if hasattr(from_date, "date") else from_date
            last = to_date.date() if hasattr(to_date, "date") else to_date
            while d <= last:
                if d.weekday() < 5:
                    t = datetime(d.year, d.month, d.day, 9, 15, tzinfo=clock.IST)
                    while t < datetime(d.year, d.month, d.day, 15, 30, tzinfo=clock.IST):
                        base = 100 + (token % 97) + t.hour + t.minute / 100
                        out.append({"date": t, "open": base, "high": base + 1, "low": base - 1, "close": base + 0.5,
                                    "volume": 1000})
                        t += timedelta(minutes=step)
                d += timedelta(days=1)
            return out
        out, d = [], from_date
        mult = self.price_shift.get(token, 1.0)
        while d <= to_date:
            if d.weekday() < 5:
                base = (100 + (token % 97) + (d.toordinal() % 50)) * mult
                out.append({"date": datetime(d.year, d.month, d.day, tzinfo=clock.IST), "open": base,
                            "high": base * 1.02, "low": base * 0.98, "close": base * 1.01, "volume": 100000})
            d += timedelta(days=1)
        return out

    def _check(self) -> None:
        if self.token_error:
            from kiteconnect.exceptions import TokenException
            raise TokenException("Incorrect `api_key` or `access_token`.")


@pytest.fixture
def fake_kite(monkeypatch):
    kite = FakeKite()
    monkeypatch.setattr(kite_session, "kite_factory", lambda api_key, timeout: kite)
    return kite


@pytest.fixture
def fixed_clock():
    c = clock.FixedClock(datetime(2026, 9, 22, 10, 0))  # Tuesday, market open
    clock.set_clock(c)
    yield c
    clock.set_clock(clock.Clock())


@pytest.fixture(autouse=True)
def env(tmp_path, monkeypatch):
    """Every test gets its own database and Kite keys; the real .env is never used."""
    monkeypatch.setenv("KITE_API_KEY", "test-key")
    monkeypatch.setenv("KITE_API_SECRET", "test-secret")
    monkeypatch.setenv("HOST", "127.0.0.1")
    monkeypatch.setenv("PORT", "8000")
    settings.get_secrets.cache_clear()
    settings.get_app_config.cache_clear()
    monkeypatch.setattr(candles, "_sleep", lambda s: None)
    from agent.strategy import strategies
    (tmp_path / "strategy_files").mkdir()
    monkeypatch.setattr(strategies, "STRATEGY_DIR", tmp_path / "strategy_files")   # never into config/strategies
    db.set_db_path(tmp_path / "test.db")
    app = settings.get_app_config()
    db.init_db(app, settings.load_strategy_config(app["strategy"]["active_version"]))
    yield
    db.set_db_path(None)
    settings.get_secrets.cache_clear()


@pytest.fixture
def calendar() -> MarketCalendar:
    return MarketCalendar(settings.get_app_config())


@pytest.fixture
def logged_in(fake_kite, fixed_clock):
    kite_session.complete_login("req-1")
    return fake_kite


def nse500_csv(symbols: list[tuple[str, str, str]], pad_to: int = 450) -> str:
    """NSE-format CSV. Pads with dummy rows so the ~500-row sanity check passes."""
    lines = ["Company Name,Industry,Symbol,Series,ISIN Code"]
    for sym, series, industry in symbols:
        lines.append(f"{sym} Ltd,{industry},{sym},{series},INE000{sym[:4]}01")
    for i in range(pad_to - len(symbols)):
        lines.append(f"Dummy {i} Ltd,Services,DUMMY{i},EQ,INE999{i:04d}")
    return "\n".join(lines) + "\n"


def today() -> date:
    return clock.now_ist().date()


@pytest.fixture
def market_filter_on():
    """The Nifty market filter is off in config (decided 2026-09-23); tests of the filter itself switch it on."""
    settings.get_app_config()["universe"]["market_filter"]["enabled"] = True
    yield
