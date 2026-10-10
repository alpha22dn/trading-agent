"""Backtest engine: what-if strategy, real 1-minute fill paths, saved runs, the API."""
from __future__ import annotations

from datetime import datetime, time as dtime

import pytest

from agent import backtest
from agent.clock import IST
from agent.data import minute
from agent.db import connect, current_db_path
from agent.strategy import scanner
from agent.settings import load_strategy_config

from .conftest import NIFTY_TOKEN
from .test_phase1 import _insert_candles, market  # noqa: F401 - the fixture

STRAT = load_strategy_config("momentum_v1")


def _token(symbol):
    with connect() as conn:
        return conn.execute("SELECT instrument_token FROM instruments WHERE symbol = ?", (symbol,)).fetchone()[0]


def _bars(token, day, prices):
    """prices: {"HH:MM": (o, h, l, c)}; minutes not listed repeat the previous close (flat bars)."""
    d = datetime.fromisoformat(day).date()
    rows, last = [], None
    for i in range(375):
        t = datetime.combine(d, dtime(9, 15), IST).replace(hour=9 + (15 + i) // 60, minute=(15 + i) % 60)
        o, h, lo, c = prices.get(t.strftime("%H:%M")) or (last, last, last, last)
        last = c
        rows.append((token, int(t.timestamp()), round(o * 100), round(h * 100), round(lo * 100), round(c * 100), 100))
    with minute.connect() as conn:
        conn.executemany("INSERT OR REPLACE INTO candles_1m VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


@pytest.fixture
def world(market, fixed_clock, tmp_path, monkeypatch):  # noqa: F811
    """TCS breaks out on 22 Sep. On 23 Sep it crosses the entry trigger at 10:31; on 24 Sep it falls through the
    stop at 11:05. Daily candles and 1-minute bars agree."""
    scanner.scan_history(STRAT)
    with connect() as conn:
        sig = dict(conn.execute("SELECT * FROM signals WHERE symbol = 'TCS' AND date = '2026-09-22'").fetchone())
    trig, stop = sig["entry_trigger"], sig["stop"]
    days = ["2026-09-23", "2026-09-24"]
    with connect() as conn:
        rows = [("TCS", days[0], trig * 0.99, trig * 1.002, trig * 0.985, trig * 1.001),
                ("TCS", days[1], trig * 1.001, trig * 1.003, stop * 0.99, stop * 0.995)]
        for sym in ("INFY", "NIFTY 50"):
            last = conn.execute("SELECT close FROM candles_daily WHERE symbol = ? ORDER BY date DESC LIMIT 1",
                                (sym,)).fetchone()[0]
            rows += [(sym, d, last, last, last, last) for d in days]
        conn.executemany("INSERT INTO candles_daily (symbol, date, open, high, low, close, volume, fetched_at) "
                         "VALUES (?, ?, ?, ?, ?, ?, 1e6, 'x')", rows)
    for d in ["2026-09-22"] + days:
        _bars(NIFTY_TOKEN, d, {"09:15": (20000, 20000, 20000, 20000)})
    t = _token("TCS")
    _bars(t, "2026-09-23", {"09:15": (trig * 0.99,) * 4,
                            "09:40": (trig * 0.99, trig * 0.99, trig * 0.985, trig * 0.99),
                            "10:31": (trig * 0.995, trig * 1.002, trig * 0.994, trig * 1.001)})
    _bars(t, "2026-09-24", {"09:15": (trig * 1.001,) * 4,
                            "11:05": (trig * 1.001, trig * 1.003, stop * 0.99, stop * 0.995)})
    fixed_clock.set(datetime(2026, 9, 25, 10, 0))
    return {"trigger": trig, "stop": stop, "signal": sig}


def _run(cfg):
    cfg = backtest.validate(cfg, current_db_path())
    rid = backtest.create_run(current_db_path(), cfg)
    backtest.run(rid, current_db_path())
    return backtest.get_run(current_db_path(), rid)


def test_minute_fills_follow_the_real_path(world):
    run = _run({"start": "2026-09-22", "end": "2026-09-24", "capital": 100000, "accounts": ["P30"]})  # accounts ignored
    assert run["status"] == "done" and run["progress"] == 1
    rep = run["report"]
    assert list(rep["accounts"]) == ["Backtest"] and rep["sessions"] == 3
    [t] = [x for x in rep["trades"] if x["symbol"] == "TCS"]
    assert t["signal_date"] == "2026-09-22" and t["state"] == "CLOSED" and t["exit_reason"] == "stop"
    assert t["entry_price"] == pytest.approx(world["trigger"])                   # filled at the crossing
    assert t["entry_at"].startswith("2026-09-23T10:31")                          # the minute it crossed
    assert t["exits"][0]["at"].startswith("2026-09-24T11:05") and t["exits"][0]["price"] == pytest.approx(world["stop"])
    assert t["r_multiple"] < -1                                                  # a full stop, plus charges
    assert rep["minute_stock_days"] >= 2 and rep["fallback_stock_days"] == 0
    assert rep["accounts"]["Backtest"]["closed"] == 1 and rep["equity"]["Backtest"]


def test_what_if_settings_change_the_trade_not_the_live_strategy(world):
    # the breakout's RR is just 1.50 (band C): a 2% stop buffer pushes it below 1.5, so it trades only with a
    # lower minimum RR, in band D
    strict = _run({"start": "2026-09-22", "end": "2026-09-24", "capital": 250000,
                   "overrides": {"stop_buffer_pct": 2.0}})
    assert strict["report"]["trades"] == []
    run = _run({"start": "2026-09-22", "end": "2026-09-24", "capital": 250000,
                "overrides": {"stop_buffer_pct": 2.0, "min_rr": 1.2, "cancel_after_sessions": 3}})
    assert run["config"]["overrides"] == {"stop_buffer_pct": 2.0, "min_rr": 1.2, "cancel_after_sessions": 3}
    [t] = [x for x in run["report"]["trades"] if x["symbol"] == "TCS"]
    sig = world["signal"]
    assert t["account_id"] == "Backtest" and t["initial_stop"] < sig["stop"]         # 2% below the low, not 0.5%
    assert t["rr_band"] == "D" and 1.2 <= t["rr"] < 1.5
    assert load_strategy_config("momentum_v1").params["levels"]["stop_buffer_pct"] == 0.5
    assert run["report"]["accounts"]["Backtest"]["capital"] == 250000


def test_band_filter_can_exclude_every_signal(world):
    band = world["signal"]["rr_band"]
    run = _run({"start": "2026-09-22", "end": "2026-09-24", "capital": 30000,
                "bands": [b for b in "ABC" if b != band]})
    assert run["report"]["trades"] == [] and run["report"]["signals"]["tradeable"] == 0


def test_validation(world):
    db = current_db_path()
    with pytest.raises(backtest.BacktestError, match="covers 2026-09-22 to 2026-09-24"):
        backtest.validate({"start": "2026-09-01", "end": "2026-09-24", "capital": 30000}, db)
    with pytest.raises(backtest.BacktestError, match="starting capital"):
        backtest.validate({"start": "2026-09-22", "end": "2026-09-24"}, db)
    with pytest.raises(backtest.BacktestError, match="between ₹1,000"):
        backtest.validate({"start": "2026-09-22", "end": "2026-09-24", "capital": 500}, db)
    with pytest.raises(backtest.BacktestError, match="Minimum reward:risk"):
        backtest.validate({"start": "2026-09-22", "end": "2026-09-24", "capital": 30000,
                           "overrides": {"min_rr": 9}}, db)
    cfg = backtest.validate({"start": "2026-09-22", "end": "2026-09-24", "capital": 30000,
                             "overrides": {"min_rr": 1.5, "target_extension": 2.5}}, db)
    assert cfg["overrides"] == {"target_extension": 2.5}           # a value equal to live is not a what-if
    assert "D" not in cfg["bands"]                                 # band D only exists below RR 1.5
    low = backtest.validate({"start": "2026-09-22", "end": "2026-09-24", "capital": 30000,
                             "overrides": {"min_rr": 1.2}}, db)
    assert "D" in low["bands"] and any(b["band"] == "D" for b in backtest.build_strategy(low).params["ranking"]["rr_bands"])


def test_api_runs_list_and_delete(world, monkeypatch):
    from fastapi.testclient import TestClient
    import agent.api.app as app_module
    launched = []
    monkeypatch.setattr(app_module, "launch_backtest", lambda rid, db: launched.append(rid) or backtest.run(rid, db))
    client = TestClient(app_module.create_app(start_scheduler=False))
    s = client.get("/api/backtests/setup").json()
    assert s["coverage"] == {"from": "2026-09-22", "to": "2026-09-24"}
    assert {w["key"] for w in s["whatifs"]} >= {"min_rr", "stop_buffer_pct", "trail_buffer_pct"}
    body = {"start": "2026-09-22", "end": "2026-09-24", "capital": 60000, "name": "baseline"}
    assert client.post("/api/backtests", json=body).status_code == 400          # needs confirm
    r = client.post("/api/backtests", json=body | {"confirm": True}).json()
    assert launched == [r["id"]]
    runs = client.get("/api/backtests").json()["runs"]
    assert runs[0]["name"] == "baseline" and runs[0]["status"] == "done" and runs[0]["summary"]["Backtest"]["capital"] == 60000
    assert client.get(f"/api/backtests/{r['id']}").json()["report"]["trades"]
    assert client.post(f"/api/backtests/{r['id']}/delete", json={"confirm": True}).json() == {"deleted": r["id"]}
    assert client.get("/api/backtests").json()["runs"] == []


def test_backtest_never_touches_the_paper_portfolio(world):
    with connect() as conn:
        before = [tuple(r) for r in conn.execute("SELECT id, capital, cash, peak_equity, halted FROM accounts ORDER BY id")]
        n_trades = conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0]
    run = _run({"start": "2026-09-22", "end": "2026-09-24", "capital": 500000})
    assert run["report"]["trades"]                                  # it did trade, in its own database
    with connect() as conn:
        assert [tuple(r) for r in conn.execute("SELECT id, capital, cash, peak_equity, halted FROM accounts ORDER BY id")] == before
        assert conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == n_trades
        assert conn.execute("SELECT COUNT(*) FROM paper_gtts").fetchone()[0] == 0
