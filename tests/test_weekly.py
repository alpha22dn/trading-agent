"""Weekly review: metrics per portfolio and combined, skipped signals and overrides with counterfactuals."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from agent import settings
from agent.db import connect
from agent.journal import counterfactual, weekly
from agent.orders import manager, paper_edits

from .test_phase2 import add_candle, add_signal, clk, cycle, quotes, trade  # noqa: F401 - fixtures


@pytest.fixture(autouse=True)
def template_llm(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "template")
    settings.get_secrets.cache_clear()


def test_counterfactual_entry_and_exit():
    add_candle("XYZ", "2026-09-21", 100, 101, 99, 100)
    add_candle("XYZ", "2026-09-22", 100.2, 101.5, 100, 101)     # reaches the 100.5 trigger
    add_candle("XYZ", "2026-09-23", 101, 112, 100.5, 111)       # target 110
    r = counterfactual.entry_plan("XYZ", "2026-09-21", 100.5, 101.0, 96.0, 110.0)
    assert (r["filled_on"], r["entry"], r["outcome"], r["exit_on"]) == ("2026-09-22", 100.5, "target", "2026-09-23")
    assert r["r"] == pytest.approx((110 - 100.5) / (100.5 - 96), abs=0.01)
    assert counterfactual.entry_plan("XYZ", "2026-09-23", 120, 121, 110, 140)["outcome"] == "not_filled"


def test_weekly_review(clk, quotes):  # noqa: F811
    add_signal()                                                        # ABC: bought, then stopped out
    manager.place_entries("2026-09-21")
    sid = add_signal(symbol="XYZ", sector="Energy", close=100, trigger=100.5, limit=101.0, stop=96.0, target=110.0, rr=2.2)
    with connect() as conn:                                             # outranked in P30
        conn.execute("INSERT INTO candidates (signal_id, account_id, qty, rank, taken, skip_reason, created_at) "
                     "VALUES (?, 'P30', 10, 2, 0, 'No free slot (5 positions incl. pending buys): outranked', 'x')", (sid,))
    add_candle("XYZ", "2026-09-22", 100.2, 101.5, 100, 101)
    add_candle("XYZ", "2026-09-23", 101, 112, 100.5, 111)
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(510, 512, 499))
    clk.set(datetime(2026, 9, 22, 11, 0))
    paper_edits.edit_trade(trade("P60")["id"], "move_stop", 495.0)       # hand-moved stop on P60
    cycle(clk, datetime(2026, 9, 22, 11, 1), quotes, ABC=(510, 512, 499))
    add_candle("ABC", "2026-09-22", 503, 512, 499, 505)
    clk.set(datetime(2026, 9, 22, 21, 0))
    manager.eod_maintenance("2026-09-22")
    cycle(clk, datetime(2026, 9, 23, 11, 0), quotes, ABC=(486, 512, 486))  # P60's raised stop (495) hits first
    cycle(clk, datetime(2026, 9, 23, 11, 30), quotes, ABC=(480, 512, 479)) # then the original stops (482.55)
    add_candle("ABC", "2026-09-23", 505, 506, 479, 481)
    clk.set(datetime(2026, 9, 23, 21, 0))
    manager.eod_maintenance("2026-09-23")
    assert {trade(a)["state"] for a in ("P30", "P60", "P100")} == {"CLOSED"}

    out = weekly.run(date(2026, 9, 21))
    [rev] = weekly.reviews()
    m = rev["metrics"]
    assert out["week_start"] == rev["week_start"] == "2026-09-21" and m["week_end"] == "2026-09-27"
    assert m["combined"]["closed"] == 3 and m["combined"]["wins"] == 0 and m["combined"]["expectancy_r"] < -0.9
    assert m["accounts"]["P100"]["week"]["closed"] == 1 and m["accounts"]["P100"]["by_band"]["B"]["closed"] == 1
    assert m["accounts"]["P100"]["week_return_pct"] is not None
    [sk] = m["skipped"]
    assert sk["symbol"] == "XYZ" and "outranked" in sk["reasons"]["P30"] and sk["would_have"]["outcome"] == "target"
    [ov] = [o for o in m["overrides"] if o["change"] == "stop_moved"]
    assert ov["account_id"] == "P60" and ov["actual"]["state"] == "CLOSED"
    assert ov["system_plan"]["outcome"] == "stop"                          # the original stop was hit too
    assert "3 trade(s) closed, 0 won" in rev["narrative"] and "would have reached the target" in rev["narrative"]
    weekly.run(date(2026, 9, 21))                                          # re-running replaces, not duplicates
    assert len(weekly.reviews()) == 1
