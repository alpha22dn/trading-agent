"""Paper trading inside a replay: charges per round trip, the trade log, and the agent overlay for the chart."""
from __future__ import annotations

from datetime import datetime

import pytest

from agent.clock import IST
from agent.orders.charges import compute, compute_intraday
from agent.research import trading


def ms(d, h, m):
    return int(datetime(2026, 9, d, h, m, tzinfo=IST).timestamp() * 1000)


def test_same_day_is_intraday_overnight_is_delivery():
    a = trading.round_trip("long", 10, 100.0, 110.0, ms(22, 10, 0), ms(22, 14, 0))
    assert a["intraday"] and a["gross"] == 100.0
    assert a["charges"] == round(compute_intraday("BUY", 10, 100).total + compute_intraday("SELL", 10, 110).total, 2)
    b = trading.round_trip("long", 10, 100.0, 110.0, ms(22, 10, 0), ms(23, 10, 0))
    assert not b["intraday"]
    assert b["charges"] == round(compute("BUY", 10, 100).total + compute("SELL", 10, 110).total, 2)
    s = trading.round_trip("short", 10, 110.0, 100.0, ms(22, 10, 0), ms(22, 11, 0))
    assert s["gross"] == 100.0 and s["intraday"] and s["net"] == round(100 - s["charges"], 2)
    with pytest.raises(trading.TradeError):
        trading.round_trip("long", 0, 1, 1, 0, 0)


def test_trade_log_api(fixed_clock):
    from fastapi.testclient import TestClient
    from agent.api.app import create_app
    t = {"session": "S1", "symbol": "ABC", "side": "long", "qty": 5, "entry_ts": ms(22, 10, 0), "entry_price": 100,
         "exit_ts": ms(22, 10, 30), "exit_price": 104, "exit_reason": "target"}
    with TestClient(create_app(start_scheduler=False)) as c:
        r = c.post("/api/research/trades", json=t).json()
        assert r["gross"] == 20.0 and r["intraday"] and r["net"] < 20
        c.post("/api/research/trades", json=t | {"side": "short", "exit_price": 103, "exit_reason": "stop"})
        lst = c.get("/api/research/trades?session=S1").json()
        assert lst["summary"]["trades"] == 2 and lst["sessions"][0]["session"] == "S1"
        assert c.post("/api/research/trades", json=t | {"exit_ts": ms(22, 9, 0)}).status_code == 400
        q = c.get(f"/api/research/charges?side=long&qty=5&entry=100&exit=100&entry_ts={ms(22, 10, 0)}&exit_ts={ms(22, 10, 0)}").json()
        assert q["charges"] > 0
        assert c.delete("/api/research/trades?session=S1").json()["deleted"] == 2


def test_agent_overlay_lists_signals_and_trades(fixed_clock):
    from .test_phase2 import add_signal
    from agent.db import connect
    sid = add_signal()
    with connect() as conn:
        conn.execute("INSERT INTO trades (account_id, signal_id, symbol, strategy_version, state, qty, entry_price, entry_at, "
                     "exits, stop, initial_stop, target, created_at, updated_at) VALUES ('P60', ?, 'ABC', 'momentum_v1', "
                     "'PROTECTED', 3, 502.5, '2026-09-22T10:01:00+05:30', '[{\"at\": \"2026-09-22T14:00:00+05:30\", "
                     "\"price\": 560, \"qty\": 1, \"reason\": \"target\", \"charges\": 1}]', 490, 482.55, 560, 'x', 'x')", (sid,))
    o = trading.agent_overlay("ABC")
    assert o["signals"][0]["entry_trigger"] == 502.5
    t = o["trades"][0]
    assert t["account_id"] == "P60" and t["exits"] == [{"at": "2026-09-22T14:00:00+05:30", "price": 560, "qty": 1, "reason": "target"}]
