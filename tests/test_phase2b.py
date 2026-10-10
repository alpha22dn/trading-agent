"""Phase 2b: reconciliation of manual changes (spec section 8) through the P60 paper-edit actions."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from agent import audit
from agent.broker.base import Leg, Quote
from agent.broker.paper import get_broker
from agent.db import connect
from agent.orders import manager, paper_edits

from .test_phase2 import add_candle, add_signal, clk, cycle, quotes, trade, trades  # noqa: F401 (fixtures)


def _protected(clk, quotes):
    """All three accounts hold ABC (P60: 26 shares) with an OCO exit: stop 482.55, target 560."""
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(503, 504, 499))
    return trade("P60")


def _overrides(trade_id):
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT change_type FROM overrides WHERE trade_id = ? ORDER BY id", (trade_id,))]


def test_user_moves_stop_and_target_are_adopted(clk, quotes):
    t = _protected(clk, quotes)
    paper_edits.edit_trade(t["id"], "move_stop", 490)
    paper_edits.edit_trade(t["id"], "move_target", 540)
    t = trade("P60")
    assert (t["stop"], t["target"]) == (490, 540)
    assert _overrides(t["id"]) == ["stop_moved", "target_moved"]
    g = get_broker().gtts("P60", "active")[0]
    assert g.trigger_values == [490, 540] and len(get_broker().gtts("P60", "active")) == 1
    # a later cycle sees no further difference (no repeated overrides)
    cycle(clk, datetime(2026, 9, 22, 10, 5), quotes, ABC=(505, 506, 499))
    assert _overrides(t["id"]) == ["stop_moved", "target_moved"]
    assert manager.check_invariants() == []


def test_user_lowered_stop_trailing_continues_from_it_only_tightening(clk, quotes):
    t = _protected(clk, quotes)
    paper_edits.edit_trade(t["id"], "move_stop", 470)
    assert trade("P60")["stop"] == 470
    # the target leg fills; at EOD the stop moves to entry (tightening from the user's 470)
    cycle(clk, datetime(2026, 9, 23, 11, 0), quotes, ABC=(561, 562, 540))
    clk.set(datetime(2026, 9, 23, 16, 0))
    add_candle("ABC", "2026-09-23", 545, 562, 540, 558)
    manager.eod_maintenance("2026-09-23")
    assert trade("P60")["stop"] == 503.0


def test_user_cancels_buy_gtt_no_reentry(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    t = trade("P60")
    paper_edits.edit_trade(t["id"], "cancel_buy")
    t = trade("P60")
    assert (t["state"], t["exit_reason"]) == ("CANCELLED", "manual_cancel")
    assert _overrides(t["id"]) == ["buy_cancelled"]
    manager.place_entries("2026-09-21")                          # same signal again: no new trade
    assert len(trades("P60")) == 1


def test_user_cancels_exit_gtt_waits_for_confirmation(clk, quotes):
    t = _protected(clk, quotes)
    paper_edits.edit_trade(t["id"], "cancel_exit")
    t = trade("P60")
    assert (t["state"], t["awaiting_user"]) == ("FILLED", "exit_cancelled")
    assert any(a["name"] == "exit_cancelled" and a["level"] == "critical" for a in audit.active_alerts())
    for m in (5, 6, 7):                                          # not re-placed on its own
        cycle(clk, datetime(2026, 9, 22, 10, m), quotes, ABC=(505, 506, 499))
    clk.set(datetime(2026, 9, 22, 16, 0))
    add_candle("ABC", "2026-09-22", 500, 506, 499, 505)
    manager.eod_maintenance("2026-09-22")
    assert trade("P60")["state"] == "FILLED" and get_broker().gtts("P60", "active") == []
    manager.confirm_new_exit(t["id"])
    cycle(clk, datetime(2026, 9, 23, 9, 20), quotes, ABC=(506, 507, 504))
    t = trade("P60")
    assert t["state"] == "PROTECTED" and len(get_broker().gtts("P60", "active")) == 1


def test_user_sells_by_hand_partly_then_fully(clk, quotes):
    t = _protected(clk, quotes)
    paper_edits.edit_trade(t["id"], "sell", qty=10)
    t = trade("P60")
    assert (t["state"], t["open_qty"]) == ("PROTECTED", 16)
    g = get_broker().gtts("P60", "active")[0]
    assert [l.qty for l in g.orders] == [16, 8]                  # exit resized to what is still held
    assert manager.check_invariants() == []
    paper_edits.edit_trade(t["id"], "sell", qty=16)
    t = trade("P60")
    assert (t["state"], t["exit_reason"], t["open_qty"]) == ("CLOSED", "manual_sell", 0)
    assert get_broker().gtts("P60", "active") == []              # leftover exit GTT cancelled
    assert [e["qty"] for e in json.loads(t["exits"])] == [10, 16]
    assert _overrides(t["id"]) == ["manual_sell", "manual_sell"]
    assert manager.check_invariants() == []


def test_user_buys_outside_the_system(clk, quotes):
    _protected(clk, quotes)
    add_signal(day="2026-09-22", symbol="XYZ", sector="Energy", close=200, trigger=201, limit=202.0, stop=190,
               target=230, rr=2.33, band="B")
    add_candle("XYZ", "2026-09-22", 199, 201, 198, 200)
    paper_edits.buy_outside("P60", "XYZ", 10)
    with connect() as conn:
        u = conn.execute("SELECT qty FROM unmanaged_holdings WHERE account_id = 'P60' AND symbol = 'XYZ'").fetchone()
    assert u[0] == 10
    st = manager.account_state("P60")
    assert "XYZ" in st.symbols and st.sector_counts.get("Energy") == 1
    assert manager.check_invariants() == []                      # 10 shares expected, no mismatch alert
    clk.set(datetime(2026, 9, 22, 16, 0))
    manager.place_entries("2026-09-22")
    assert [t["symbol"] for t in trades("P60")] == ["ABC"]       # never buys a stock held outside the system
    assert any(t["symbol"] == "XYZ" for t in trades("P100"))


def test_baseline_accounts_refuse_paper_edits(clk, quotes):
    _protected(clk, quotes)
    with pytest.raises(paper_edits.EditError, match="baseline"):
        paper_edits.edit_trade(trade("P100")["id"], "move_stop", 490)
    with pytest.raises(paper_edits.EditError, match="baseline"):
        paper_edits.buy_outside("P30", "ABC", 1)


def test_unrecorded_exit_gtt_is_adopted_not_duplicated(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    # crash between placing the exit GTT and recording it: the broker has it, the agent does not
    gid = get_broker().place_gtt("P100", "ABC", "oco", [482.55, 560.0], 503,
                                 [Leg("SELL", 44, 472.85), Leg("SELL", 22, 560.0)])
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(503, 504, 499))
    active = get_broker().gtts("P100", "active")
    assert [g.id for g in active] == [gid] and trade("P100")["state"] == "PROTECTED"
    assert manager.check_invariants() == []


def test_split_or_bonus_pauses_management(clk, quotes):
    _protected(clk, quotes)
    cycle(clk, datetime(2026, 9, 23, 11, 0), quotes, ABC=(561, 562, 540))       # target leg: now trailing
    manager.pause_for_adjustment(["ABC"])
    assert all(t["paused"] for t in trades() if t["state"] != "CLOSED")
    clk.set(datetime(2026, 9, 23, 16, 0))
    add_candle("ABC", "2026-09-23", 545, 562, 540, 558)
    manager.eod_maintenance("2026-09-23")
    assert trade("P100")["stop"] == 482.55                       # not trailed while paused
    assert any(a["name"] == "trade_paused" for a in audit.active_alerts())
    manager.resume_trade(trade("P100")["id"])
    clk.set(datetime(2026, 9, 24, 16, 0))
    add_candle("ABC", "2026-09-24", 558, 570, 550, 565)
    manager.eod_maintenance("2026-09-24")
    assert trade("P100")["stop"] == 547.25


def test_api_paper_edit_and_confirmations(clk, quotes):
    from fastapi.testclient import TestClient
    from agent.api.app import create_app
    t = _protected(clk, quotes)
    client = TestClient(create_app(start_scheduler=False))
    assert client.post(f"/api/trades/{t['id']}/edit", json={"action": "move_stop", "value": 490}).status_code == 400
    r = client.post(f"/api/trades/{t['id']}/edit", json={"action": "move_stop", "value": 490, "confirm": True})
    assert r.status_code == 200 and r.json()["stop"] == 490
    r = client.post(f"/api/trades/{trade('P100')['id']}/edit", json={"action": "sell", "qty": 1, "confirm": True})
    assert r.status_code == 400 and "baseline" in r.json()["detail"]
    client.post(f"/api/trades/{t['id']}/edit", json={"action": "cancel_exit", "confirm": True})
    r = client.post(f"/api/trades/{t['id']}/confirm-exit", json={"confirm": True})
    assert r.status_code == 200 and r.json()["awaiting_user"] is None
    d = client.get(f"/api/trades/{t['id']}").json()
    assert [o["change_type"] for o in d["overrides"]] == ["stop_moved", "exit_cancelled"] and d["manual_edits"]


def test_outside_holding_is_priced_live_and_counts_in_equity(clk, quotes):
    _protected(clk, quotes)
    add_candle("XYZ", "2026-09-21", 199, 201, 198, 200)
    from agent.orders import views
    with connect() as conn:
        conn.execute("INSERT INTO instruments (instrument_token, exchange, symbol, tick_size, sector, is_nse500, "
                     "updated_at) VALUES (7777, 'NSE', 'XYZ', 0.05, 'Energy', 1, 'x')")
    quotes["XYZ"] = Quote(210, 211, 205)
    clk.set(datetime(2026, 9, 22, 11, 0))
    r = paper_edits.buy_outside("P60", "XYZ", 10)
    assert r["price"] == 210                                     # live quote in market hours, not the last close
    cycle(clk, datetime(2026, 9, 22, 11, 5), quotes, XYZ=(215, 216, 205), ABC=(505, 506, 499))
    pos = views.positions("P60")
    u = pos["unmanaged"][0]
    assert (u["price"], u["unrealized"], u["price_source"]) == (215, 50, "live")
    from agent.orders.charges import compute
    with connect() as conn:  # the net figure uses the same buy charges the paper broker booked
        booked = conn.execute("SELECT charges FROM paper_orders WHERE symbol = 'XYZ' AND side = 'BUY'").fetchone()[0]
    assert u["buy_charges_open"] == pytest.approx(booked, abs=0.01)
    assert u["net_unrealized"] == pytest.approx(50 - booked - compute("SELL", 10, 215).total, abs=0.01)
    acc = {a["id"]: a for a in views.accounts_summary()["accounts"]}["P60"]
    assert acc["holdings"] == pytest.approx(26 * 505 + 10 * 215)


def test_sell_outside_holding(clk, quotes):
    _protected(clk, quotes)
    with connect() as conn:
        conn.execute("INSERT INTO instruments (instrument_token, exchange, symbol, tick_size, sector, is_nse500, "
                     "updated_at) VALUES (7777, 'NSE', 'XYZ', 0.05, 'Energy', 1, 'x')")
    quotes["XYZ"] = Quote(210, 211, 205)
    clk.set(datetime(2026, 9, 22, 11, 0))
    paper_edits.buy_outside("P60", "XYZ", 10)
    with pytest.raises(paper_edits.EditError):
        paper_edits.sell_outside("P60", "XYZ", 11)
    quotes["XYZ"] = Quote(220, 221, 205)
    r = paper_edits.sell_outside("P60", "XYZ", 4)
    assert (r["qty"], r["price"]) == (4, 220)
    paper_edits.sell_outside("P60", "XYZ", 6)
    with connect() as conn:
        assert conn.execute("SELECT qty FROM unmanaged_holdings WHERE symbol = 'XYZ'").fetchone()[0] == 0
    assert "XYZ" not in {p.symbol for p in get_broker().positions("P60")}
    assert trade("P60")["state"] == "PROTECTED" and trade("P60")["open_qty"] == 26   # agent trade untouched
    assert manager.check_invariants() == []


def test_hand_orders_fill_at_ask_and_bid(clk, quotes):
    _protected(clk, quotes)
    with connect() as conn:
        conn.execute("INSERT INTO instruments (instrument_token, exchange, symbol, tick_size, sector, is_nse500, "
                     "updated_at) VALUES (7777, 'NSE', 'XYZ', 0.05, 'Energy', 1, 'x')")
    clk.set(datetime(2026, 9, 22, 11, 0))
    quotes["XYZ"] = Quote(210, 211, 205, bids=[(209.9, 50), (209.8, 100)], asks=[(210.1, 40), (210.2, 80)])
    assert paper_edits.buy_outside("P60", "XYZ", 10)["price"] == 210.1
    assert paper_edits.sell_outside("P60", "XYZ", 10)["price"] == 209.9
