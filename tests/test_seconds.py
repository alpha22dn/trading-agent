"""1-second bars recorded from the live stream: encoding, aggregation, daily files, the disk guard, reading back."""
from __future__ import annotations

from collections import namedtuple
from datetime import datetime

from agent import audit
from agent.broker import ticker
from agent.clock import IST
from agent.data import seconds

T = namedtuple("T", "ltp volume exchange_ts")


def _ts(h, m, s=0):
    return int(datetime(2026, 9, 22, h, m, s, tzinfo=IST).timestamp())


def test_encoding_round_trip():
    rows = [(0, 118550, 118600, 118500, 118500, 120), (1, 118500, 118500, 118400, 118450, 0),
            (17, 1, 9_000_000, 1, 8_999_999, 10_000_000), (59, 118000, 118000, 118000, 118000, 5)]
    assert seconds.decode(seconds.encode(rows)) == rows
    assert seconds.decode(seconds.encode([])) == []


def test_recorder_builds_seconds_and_writes_finished_minutes():
    now = {"t": _ts(10, 0, 0)}
    rec = seconds.Recorder(clock=lambda: now["t"])
    tick = lambda ltp, vol, sec: rec.on_ticks({7001: T(ltp, vol, sec)})            # noqa: E731
    tick(100.0, 5000, _ts(10, 0, 0))            # first tick of the day: volume baseline, 0 in this second
    tick(100.5, 5010, _ts(10, 0, 0))
    tick(99.8, 5030, _ts(10, 0, 0))
    tick(100.1, 5031, _ts(10, 0, 3))
    tick(100.2, 5050, _ts(10, 1, 0))            # next minute
    now["t"] = _ts(9, 10, 0)
    rec.on_ticks({7002: T(50.0, None, _ts(9, 10, 0))})                             # pre-open: not kept
    now["t"] = _ts(10, 0, 30)
    assert rec.flush() == 0                      # minute 10:00 not over yet
    now["t"] = _ts(10, 1, 5)
    assert rec.flush() == 1                      # 10:00 written
    got = seconds.read(7001, _ts(10, 0) * 1000, _ts(10, 2) * 1000)
    assert got == {_ts(10, 0) * 1000: [[0, 100.0, 100.5, 99.8, 99.8, 30], [3, 100.1, 100.1, 100.1, 100.1, 1]]}
    rec.flush(final=True)
    got = seconds.read(7001, _ts(10, 0) * 1000, _ts(10, 2) * 1000)
    assert got[_ts(10, 1) * 1000] == [[0, 100.2, 100.2, 100.2, 100.2, 19]]
    assert seconds.read(7002, _ts(9, 0) * 1000, _ts(10, 0) * 1000) == {}
    assert seconds.days() == ["2026-09-22"] and seconds.summary()["days"] == 1


def test_exchange_time_is_used_only_when_sane():
    now = {"t": _ts(11, 0, 10)}
    rec = seconds.Recorder(clock=lambda: now["t"])
    rec.on_ticks({1: T(10.0, None, _ts(11, 0, 8))})                                # exchange time, 2 s behind
    rec.on_ticks({2: T(20.0, None, 1)})                                            # nonsense: the clock is used
    rec.flush(final=True)
    assert list(seconds.read(1, _ts(11, 0) * 1000, _ts(11, 1) * 1000).values())[0][0][0] == 8
    assert list(seconds.read(2, _ts(11, 0) * 1000, _ts(11, 1) * 1000).values())[0][0][0] == 10


def test_low_disk_pauses_and_resumes(monkeypatch, fixed_clock):
    free = {"b": 5 * 1024 ** 3}
    monkeypatch.setattr(seconds.shutil, "disk_usage", lambda p: type("U", (), {"free": free["b"]})())
    rec = seconds.Recorder(clock=lambda: _ts(10, 0))
    rec.check_disk()
    assert rec.paused_low_disk and any(a["name"] == "seconds_recording" for a in audit.active_alerts())
    rec.on_ticks({1: T(10.0, None, _ts(10, 0))})
    rec.flush(final=True)
    assert seconds.days() == []                                                    # nothing recorded while paused
    free["b"] = int(10.5 * 1024 ** 3)
    rec.check_disk()
    assert rec.paused_low_disk                                                     # needs 1 GB more than the minimum
    free["b"] = 12 * 1024 ** 3
    rec.check_disk()
    assert not rec.paused_low_disk


def test_full_packets_carry_volume_and_exchange_time():
    from .test_stream import frame, full_packet
    t = ticker.parse(frame(full_packet(7001, 101.5, 100, 102, 99.5, 100.2)))[7001]
    assert t.volume == 5000 and t.exchange_ts == 1727760000


def test_research_seconds_api(fixed_clock):
    from fastapi.testclient import TestClient
    from agent.api.app import create_app
    from agent.db import connect
    with connect() as conn:
        conn.execute("INSERT INTO instruments (instrument_token, exchange, segment, symbol, tick_size, is_nse500, "
                     "updated_at) VALUES (7001, 'NSE', 'NSE', 'ABC', 0.05, 1, 'x')")
    rec = seconds.Recorder(clock=lambda: _ts(10, 0, 5))
    rec.on_ticks({7001: T(100.0, 10, _ts(10, 0, 4))})
    rec.flush(final=True)
    with TestClient(create_app(start_scheduler=False)) as c:
        assert c.get("/api/research/seconds/days").json()["days"] == ["2026-09-22"]
        r = c.get(f"/api/research/seconds?symbol=ABC&start={_ts(10, 0) * 1000}&end={_ts(10, 30) * 1000}").json()
        assert r["minutes"] == {str(_ts(10, 0) * 1000): [[4, 100.0, 100.0, 100.0, 100.0, 0]]}
