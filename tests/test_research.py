"""Research tab datafeed: Nifty 500 search, bars for every timeframe with backward paging, saved layouts."""
from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from agent.clock import IST
from agent.data import minute
from agent.db import connect
from agent.research import data


def _seed(days=("2026-09-17", "2026-09-18", "2026-09-21")):
    with connect() as conn:
        conn.execute("INSERT INTO instruments (instrument_token, exchange, segment, symbol, name, tick_size, sector, "
                     "is_nse500, updated_at) VALUES (7001, 'NSE', 'NSE', 'ABC', 'ABC INDUSTRIES', 0.05, 'IT', 1, 'x'),"
                     "(7002, 'NSE', 'NSE', 'XYZ', 'XYZ LTD', 0.05, 'IT', 0, 'x'),"
                     "(256265, 'NSE', 'INDICES', 'NIFTY 50', 'NIFTY 50', 0.05, NULL, 0, 'x')")
    rows = []
    for d in days:
        t0 = datetime.combine(date.fromisoformat(d), minute.SESSION_START, IST)
        for i in range(375):
            px = 10000 + i                                   # paise
            rows.append((7001, int((t0 + timedelta(minutes=i)).timestamp()), px, px + 5, px - 5, px + 1, 10))
    with minute.connect() as conn:
        conn.executemany("INSERT INTO candles_1m VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def test_symbol_search_is_the_nifty_500_plus_the_index():
    _seed()
    assert [s["symbol"] for s in data.symbols("")] == ["ABC", "NIFTY 50"]
    assert [s["symbol"] for s in data.symbols("industries")] == ["ABC"]
    assert data.symbols("XYZ") == []                                      # not in the Nifty 500


def test_intraday_bars_page_backwards(fixed_clock):
    _seed()
    r = data.bars("ABC", "15m", limit=30)                                 # 25 per session
    assert len(r["bars"]) == 30 and r["more"]
    last = r["bars"][-1]
    assert datetime.fromtimestamp(last["timestamp"] / 1000, IST) == datetime(2026, 9, 21, 15, 15, tzinfo=IST)
    assert last["volume"] == 150 and last["open"] == 103.60 and last["close"] == 103.75
    older = data.bars("ABC", "15m", before_ms=r["bars"][0]["timestamp"], limit=100)
    assert older["bars"][-1]["timestamp"] < r["bars"][0]["timestamp"]
    assert len(older["bars"]) == 75 - 30 and not older["more"]           # everything stored is reached
    four = data.bars("ABC", "4h")["bars"]
    assert [datetime.fromtimestamp(b["timestamp"] / 1000, IST).strftime("%H:%M") for b in four[:2]] == ["09:15", "13:15"]


def test_daily_weekly_monthly(fixed_clock):
    _seed()
    with connect() as conn:
        d = date(2026, 6, 1)
        while d <= date(2026, 9, 21):
            if d.weekday() < 5:
                conn.execute("INSERT INTO candles_daily (symbol, date, open, high, low, close, volume, fetched_at) VALUES "
                             "('ABC', ?, 100, 110, 90, 105, 1000, 'x')", (d.isoformat(),))
            d += timedelta(days=1)
    day = data.bars("ABC", "1D", limit=10)["bars"]
    assert len(day) == 10
    week = data.bars("ABC", "1W", limit=4)["bars"]
    assert [datetime.fromtimestamp(b["timestamp"] / 1000, IST).date().weekday() for b in week] == [0, 0, 0, 0]
    assert week[-1]["volume"] == 1000                                      # Monday 21 Sep only
    assert week[-2]["volume"] == 5000
    month = data.bars("ABC", "1M", limit=12)
    assert [datetime.fromtimestamp(b["timestamp"] / 1000, IST).month for b in month["bars"]] == [6, 7, 8, 9]
    assert not month["more"]
    with pytest.raises(data.ResearchError):
        data.bars("ABC", "2m")


def test_layouts_and_api(fixed_clock):
    from fastapi.testclient import TestClient
    from agent.api.app import create_app
    _seed()
    with TestClient(create_app(start_scheduler=False)) as c:
        assert c.get("/api/research/layout/sym:ABC").json() == {"data": None}
        assert c.put("/api/research/layout/sym:ABC", json={"overlays": [{"name": "segment"}]}).status_code == 200
        assert c.get("/api/research/layout/sym:ABC").json()["data"] == {"overlays": [{"name": "segment"}]}
        assert c.put("/api/research/layout/bad", json={}).status_code == 400
        assert [s["symbol"] for s in c.get("/api/research/symbols?q=AB").json()] == ["ABC"]
        r = c.get("/api/research/bars?symbol=ABC&tf=1h&limit=5").json()
        assert len(r["bars"]) == 5


def test_replay_feed_steps_through_minutes_then_daily_before_them(fixed_clock):
    _seed()
    with connect() as conn:
        for d in ("2026-09-15", "2026-09-16", "2026-09-17"):
            conn.execute("INSERT INTO candles_daily (symbol, date, open, high, low, close, volume, fetched_at) VALUES "
                         "('ABC', ?, 100, 101, 99, 100.5, 5, 'x')", (d,))
    t = int(datetime(2026, 9, 18, 15, 28, tzinfo=IST).timestamp() * 1000)
    r = data.replay_feed("ABC", t, limit=5)
    assert r["base"] == "1m" and [b["timestamp"] for b in r["bars"][:2]] == [t, t + 60000]
    assert datetime.fromtimestamp(r["bars"][2]["timestamp"] / 1000, IST) == datetime(2026, 9, 21, 9, 15, tzinfo=IST)
    assert not r["end"]
    old = int(datetime(2026, 9, 15, tzinfo=IST).timestamp() * 1000)
    d = data.replay_feed("ABC", old)
    assert d["base"] == "1D" and len(d["bars"]) == 2 and not d["end"]          # 15th and 16th; minutes start on the 17th
    last = data.replay_feed("ABC", int(datetime(2026, 9, 21, 15, 29, tzinfo=IST).timestamp() * 1000))
    assert len(last["bars"]) == 1 and last["end"]


def test_replay_feed_hands_over_from_daily_to_minutes(fixed_clock):
    _seed()
    with connect() as conn:
        conn.execute("INSERT INTO candles_daily (symbol, date, open, high, low, close, volume, fetched_at) VALUES "
                     "('ABC', '2026-09-16', 100, 101, 99, 100.5, 5, 'x')")
    d = data.replay_feed("ABC", int(datetime(2026, 9, 16, tzinfo=IST).timestamp() * 1000))
    assert d["base"] == "1D" and len(d["bars"]) == 1
    nxt = data.replay_feed("ABC", d["bars"][-1]["timestamp"] + 86400000, limit=3)    # midnight of the first minute day
    assert nxt["base"] == "1m" and datetime.fromtimestamp(nxt["bars"][0]["timestamp"] / 1000, IST) == datetime(2026, 9, 17, 9, 15, tzinfo=IST)
