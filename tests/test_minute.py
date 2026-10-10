"""1-minute candle store: download, resume, resampling, charts built from it."""
from __future__ import annotations

from datetime import datetime

from agent import scheduler
from agent.clock import IST
from agent.data import intraday, minute
from agent.db import current_db_path

from .test_data import _setup_universe


def test_download_resume_and_separate_file(logged_in, fixed_clock):
    _setup_universe(logged_in)
    st = minute.run(days=6)                   # 10:00 on Tue 22 Sep: sessions 15–21 Sep, today not final
    assert st["error_count"] == 0 and st["done"] == 4          # 3 stocks + Nifty
    assert st["rows_added"] == 4 * 5 * 375                     # 15, 16, 17, 18, 21 Sep × 375 bars
    assert minute.db_path() != current_db_path() and minute.db_path().exists()
    again = minute.run(days=6)
    assert again["rows_added"] == 0                            # resumes from the last stored minute
    s = minute.status()
    assert s["symbols"] == 4 and s["latest"].startswith("2026-09-21T15:29")


def test_resample_anchored_at_0915():
    day = datetime(2026, 9, 21, 9, 15, tzinfo=IST)
    bars = []
    for i in range(375):
        t = day.replace(hour=9 + (15 + i) // 60, minute=(15 + i) % 60)
        bars.append({"date": t, "open": 100 + i, "high": 101 + i, "low": 99 + i, "close": 100.5 + i, "volume": 10})
    five = minute.resample(bars, 5)
    assert len(five) == 75 and five[1]["date"].strftime("%H:%M") == "09:20"
    assert (five[0]["open"], five[0]["high"], five[0]["low"], five[0]["close"]) == (100, 105, 99, 104.5)
    hour = minute.resample(bars, 60)
    assert [b["date"].strftime("%H:%M") for b in hour] == ["09:15", "10:15", "11:15", "12:15", "13:15", "14:15", "15:15"]
    assert hour[-1]["volume"] == 15 * 10                       # 15:15–15:29
    four = minute.resample(bars, 240)
    assert [b["date"].strftime("%H:%M") for b in four] == ["09:15", "13:15"] and four[0]["volume"] == 2400


def test_charts_use_stored_minutes(logged_in, fixed_clock):
    _setup_universe(logged_in)
    minute.run(days=6)
    logged_in.calls.clear()
    intraday._cache.clear()
    c = intraday.candles("TCS", "1h")
    hist = [x for x in logged_in.calls if x[0] == "historical"]
    assert len(hist) == 1 and hist[0][3] - hist[0][2] < (fixed_clock.now() - fixed_clock.now().replace(hour=0))  # today only
    assert len(c) >= 4 * 7 and c[1]["time"] - c[0]["time"] == 3600


def test_minute_job_is_scheduled():
    jobs = {j.name: j for j in scheduler.job_definitions()}
    assert jobs["minute_candles"].built and jobs["minute_candles"].trigger["hour"] == 22


def test_pause_after_current_stock_then_resume(logged_in, fixed_clock, monkeypatch):
    _setup_universe(logged_in)
    real = logged_in.historical_data
    seen = []

    def hist(token, start, end, interval, *a, **k):
        seen.append(token)
        if len(seen) == 1:
            assert minute.request_stop()                       # pressed while the first stock downloads
        return real(token, start, end, interval, *a, **k)
    monkeypatch.setattr(logged_in, "historical_data", hist)
    st = minute.run(days=6)
    assert st["done"] == 1 and st["rows_added"] == 5 * 375 and "paused by you" in st["aborted"]
    assert not minute.is_running() and not minute.request_stop()
    rest = minute.run(days=6)                                  # the next run picks up the other 3
    assert rest["done"] == 4 and rest["rows_added"] == 3 * 5 * 375 and rest["aborted"] is None


def test_eod_run_resumes_minute_download(logged_in, fixed_clock, monkeypatch):
    started = []
    monkeypatch.setattr(scheduler.threading, "Thread", lambda target, args, **k: type(
        "T", (), {"start": lambda self: started.append(args)})())
    scheduler._resume_minute_download()
    assert started == []                                       # never downloaded: the user starts it
    from agent.db import set_state
    set_state(minute.STATE_KEY, {"aborted": "paused by you"})
    scheduler._resume_minute_download()
    assert started == [("minute_candles",)]


def test_older_history_extends_back_until_kite_has_none(logged_in, monkeypatch):
    """After the new sessions, each stock is extended backwards in 60-day chunks until Kite returns nothing
    older; progress is kept per stock and nothing runs in the trading-day quiet hours."""
    from datetime import datetime, timedelta
    from agent.clock import IST
    from agent.data import minute
    from agent.data.candles import Target
    kite = logged_in
    floor = datetime(2026, 3, 2, 9, 15, tzinfo=IST)              # Kite "has" data from here
    calls = []

    def hist(token, start, end, interval):
        calls.append((start, end))
        out = []
        d = max(start, floor)
        while d <= end:
            if d.weekday() < 5:
                out.append({"date": datetime.combine(d.date(), minute.SESSION_START, IST), "open": 1, "high": 1, "low": 1,
                            "close": 1, "volume": 1})
            d = datetime.combine(d.date() + timedelta(days=1), minute.SESSION_START, IST)
        return out
    monkeypatch.setattr(kite, "historical_data", hist)
    monkeypatch.setattr(minute, "targets", lambda: [Target("ABC", 7001)])
    monkeypatch.setattr(minute, "_quiet_hours", lambda now: False)
    with minute.connect() as conn:
        conn.execute("INSERT INTO candles_1m VALUES (7001, ?, 100, 100, 100, 100, 1)",
                     (int(datetime(2026, 9, 1, 9, 15, tzinfo=IST).timestamp()),))
    st = minute.run(days=1)
    assert st["older_done"] == 1 and st["older_state"] == "complete"
    with minute.connect() as conn:
        first = conn.execute("SELECT MIN(ts) FROM candles_1m WHERE token = 7001").fetchone()[0]
        done = conn.execute("SELECT done FROM history_floor WHERE token = 7001").fetchone()[0]
    assert datetime.fromtimestamp(first, IST) == floor and done == 1
    n = len(calls)
    minute.run(days=1)                                            # complete: no older requests again
    assert all(c[0] >= datetime(2026, 9, 1, tzinfo=IST) for c in calls[n:])


def test_older_history_waits_for_the_close(logged_in, monkeypatch):
    from agent.data import minute
    from agent.data.candles import Target
    monkeypatch.setattr(minute, "targets", lambda: [Target("ABC", 7001)])
    monkeypatch.setattr(minute, "_quiet_hours", lambda now: True)
    monkeypatch.setattr(logged_in, "historical_data", lambda *a: [])
    with minute.connect() as conn:
        conn.execute("INSERT INTO candles_1m VALUES (7001, 1790000000, 100, 100, 100, 100, 1)")
    st = minute.run(days=1)
    assert st["older_state"] == "waiting" and st["older_done"] == 0
