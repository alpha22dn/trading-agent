"""Phase 2a: charges, sizing and ranking, PaperBroker, trade lifecycle, trailing, kill switch, halts."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from agent import audit, clock, settings
from agent.broker import feed
from agent.broker.base import BrokerReject, Leg, Quote
from agent.broker.paper import get_broker
from agent.db import connect, get_state
from agent.orders import charges, manager, views
from agent.risk import controls
from agent.risk.sizing import AccountState, Candidate, select, size

STRAT = settings.load_strategy_config("momentum_v1")
RISK = {"risk_per_trade_pct": 1.0, "max_position_pct": 25.0, "max_open_positions": 5, "max_per_sector": 2}


# ---------- helpers ----------

@pytest.fixture
def clk():
    c = clock.FixedClock(datetime(2026, 9, 21, 16, 0))  # Monday after the close
    clock.set_clock(c)
    yield c
    clock.set_clock(clock.Clock())


@pytest.fixture
def quotes(monkeypatch):
    book: dict[str, Quote] = {}
    monkeypatch.setattr(feed, "quote_source", lambda symbols: {s: book[s] for s in symbols if s in book})
    return book


_token = [5000]


def add_signal(day="2026-09-21", symbol="ABC", sector="Finance", close=500.0, trigger=502.5, limit=505.05,
               stop=482.55, target=560.0, rr=2.44, band="B", tv_cr=50.0) -> int:
    now = clock.ts()
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM instruments WHERE symbol = ?", (symbol,)).fetchone():
            _token[0] += 1
            conn.execute("INSERT INTO instruments (instrument_token, exchange, symbol, tick_size, sector, is_nse500, "
                         "updated_at) VALUES (?, 'NSE', ?, 0.05, ?, 1, ?)", (_token[0], symbol, sector, now))
        cur = conn.execute(
            "INSERT INTO signals (date, symbol, strategy_version, entry_trigger, entry_limit, stop, target, rr, "
            "rr_band, indicators, eligible, source, created_at) VALUES (?, ?, 'momentum_v1', ?, ?, ?, ?, ?, ?, ?, 1, "
            "'eod', ?)", (day, symbol, trigger, limit, stop, target, rr, band,
                          json.dumps({"close": close, "traded_value_cr": tv_cr}), now))
        return int(cur.lastrowid)


def add_candle(symbol, day, o, h, l, c):
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO candles_daily (symbol, date, open, high, low, close, volume, fetched_at) "
                     "VALUES (?, ?, ?, ?, ?, ?, 100000, ?)", (symbol, day, o, h, l, c, clock.ts()))


def trades(account=None):
    with connect() as conn:
        sql = "SELECT * FROM trades" + (" WHERE account_id = ?" if account else "") + " ORDER BY id"
        return [dict(r) for r in conn.execute(sql, (account,) if account else ())]


def trade(account, symbol="ABC"):
    return next(t for t in trades(account) if t["symbol"] == symbol)


def cycle(clk, when, book, **q):
    clk.set(when)
    for sym, (ltp, hi, lo) in q.items():
        book[sym] = Quote(ltp=ltp, high=hi, low=lo)
    return manager.on_cycle()


def cash(acc):
    return get_broker().cash(acc)


# ---------- charges ----------

def test_delivery_charges():
    b = charges.compute("BUY", 100, 500)
    assert (round(b.stt, 2), round(b.stamp, 2), b.dp) == (50.0, 7.5, 0.0)
    assert b.total == pytest.approx(50 + 1.535 + 0.05 + 7.5 + (1.535 + 0.05) * 0.18, abs=0.01)
    s = charges.compute("SELL", 100, 500)
    assert s.stamp == 0 and s.dp == 15.34
    assert s.total == pytest.approx(50 + 1.535 + 0.05 + (1.535 + 0.05) * 0.18 + 15.34, abs=0.01)
    assert charges.compute("SELL", 100, 500, first_sell_today=False).dp == 0


# ---------- sizing and selection ----------

def test_size_risk_and_position_caps():
    assert size(100000, 505.05, 482.55, 1, 25) == 44            # 1,000 / 22.5
    assert size(100000, 100, 99, 1, 25) == 250                  # capped: 25,000 / 100
    assert size(30000, 505.05, 200, 1, 25) == 0                 # risk too wide


def _cand(i, sym, band="B", price=100.0, stop=95.0, sector="S1", tv=1e8):
    return Candidate(signal_id=i, symbol=sym, sector=sector, entry_trigger=price * 0.995, entry_limit=price,
                     stop=stop, target=price * 1.2, rr=2.2, rr_band=band, traded_value=tv)


def test_ranking_band_then_price_then_liquidity():
    acct = AccountState("P100", 100000, 100000, 0, 0)
    cands = [_cand(1, "C1", "C", 50, 48), _cand(2, "B1", "B", 200, 190, "S2"), _cand(3, "A2", "A", 300, 290, "S3"),
             _cand(4, "A1", "A", 100, 95, "S4", tv=1e8), _cand(5, "A1b", "A", 100, 95, "S5", tv=5e8)]
    out = select(cands, acct, RISK)
    assert [c.symbol for c in out] == ["A1b", "A1", "A2", "B1", "C1"]


def test_selection_limits_count_pending_buys():
    acct = AccountState("P100", 100000, 100000, reserved=90000, open_slots_used=4, symbols={"HELD"},
                        sector_counts={"S1": 2})
    out = {c.symbol: c for c in select([_cand(1, "HELD", sector="S9"), _cand(2, "X", sector="S1"),
                                        _cand(3, "Y", "A", sector="S2"), _cand(4, "Z", "C", sector="S3", price=10, stop=9)],
                                       acct, RISK)}
    assert "Already held" in out["HELD"].skip_reason
    assert "Sector cap" in out["X"].skip_reason
    assert "Not enough free cash" in out["Y"].skip_reason        # 200 x 100 > 10,000 free
    assert out["Z"].taken                                        # 1,000 x 10 = the free 10,000: takes the last slot
    acct2 = AccountState("P100", 100000, 100000, 0, 5)
    assert "No free slot" in select([_cand(1, "Q")], acct2, RISK)[0].skip_reason
    blocked = AccountState("P100", 100000, 100000, 0, 0, blocked_reason="Kill switch is on")
    assert select([_cand(1, "Q")], blocked, RISK)[0].skip_reason == "Kill switch is on"


# ---------- PaperBroker validation ----------

def test_gtt_validation_rules(clk):
    b = get_broker()
    with pytest.raises(BrokerReject, match="too close"):
        b.place_gtt("P30", "ABC", "single", [500.5], 500.0, [Leg("BUY", 1, 501)])     # 0.1% < 0.25%
    b.place_gtt("P30", "ABC", "single", [501.25], 500.0, [Leg("BUY", 1, 502)])        # exactly 0.25%
    with pytest.raises(BrokerReject, match="too close"):
        b.place_gtt("P30", "PENNY", "single", [40.05], 40.0, [Leg("BUY", 1, 40.1)])    # < 9 paise
    with pytest.raises(BrokerReject, match="below and one above"):
        b.place_gtt("P30", "ABC", "oco", [490, 495], 500.0, [Leg("SELL", 4, 480), Leg("SELL", 2, 495)])


# ---------- full lifecycle ----------

def test_full_trade_lifecycle_split(clk, quotes):
    add_signal()
    s = manager.place_entries("2026-09-21")
    assert s["placed"] == {"P30": 1, "P60": 1, "P100": 1}
    t = trade("P100")
    assert (t["state"], t["qty"], t["sizing_case"]) == ("BUY_PENDING", 44, "split")
    assert trade("P30")["qty"] == 13 and trade("P60")["qty"] == 26
    assert manager.account_state("P100").reserved == pytest.approx(44 * 505.05)

    # Day 2, 10:00: the buy GTT (armed before the open) triggers on the day high and fills near the trigger
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    t = trade("P100")
    assert (t["state"], t["entry_price"], t["open_qty"]) == ("FILLED", 503.0, 44)
    assert cash("P100") == pytest.approx(100000 - 44 * 503 - t["charges"])

    # 10:01: exit attached on the next cycle: OCO, stop leg all 44 at 472.85, target leg 22 at 560
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(503, 504, 499))
    t = trade("P100")
    assert t["state"] == "PROTECTED"
    g = get_broker().gtts("P100", "active")[0]
    assert g.type == "oco" and g.trigger_values == [482.55, 560.0]
    assert [(l.qty, l.price) for l in g.orders] == [(44, 472.85), (22, 560.0)]

    # Day 3: target touched; half sold at 560, new stop on the rest at the existing stop -> TRAILING
    cycle(clk, datetime(2026, 9, 23, 11, 0), quotes, ABC=(561, 562, 540))
    t = trade("P100")
    assert (t["state"], t["open_qty"], t["stop"]) == ("TRAILING", 22, 482.55)
    g = get_broker().gtts("P100", "active")[0]
    assert g.type == "single" and g.trigger_values == [482.55] and g.orders[0].qty == 22

    # EOD day 3: stop moves to entry (target day)
    clk.set(datetime(2026, 9, 23, 16, 0))
    add_candle("ABC", "2026-09-23", 545, 562, 540, 558)
    manager.eod_maintenance("2026-09-23")
    assert trade("P100")["stop"] == 503.0
    # EOD day 4: stop = max(stop, low x 0.995)
    clk.set(datetime(2026, 9, 24, 16, 0))
    add_candle("ABC", "2026-09-24", 558, 570, 550, 565)
    manager.eod_maintenance("2026-09-24")
    assert trade("P100")["stop"] == 547.25
    # a lower low never moves the stop down
    add_candle("ABC", "2026-09-24", 558, 570, 540, 565)
    manager.eod_maintenance("2026-09-24")
    assert trade("P100")["stop"] == 547.25

    # Day 5: the trailed stop is hit; the rest sells near the trigger
    cycle(clk, datetime(2026, 9, 25, 10, 0), quotes, ABC=(546, 566, 545))
    t = trade("P100")
    assert (t["state"], t["exit_reason"], t["open_qty"]) == ("CLOSED", "stop", 0)
    exits = json.loads(t["exits"])
    assert [(e["qty"], e["price"], e["reason"]) for e in exits] == [(22, 560.0, "target"), (22, 546.0, "stop")]
    gross = 22 * (560 - 503) + 22 * (546 - 503)
    assert t["net_pnl"] == pytest.approx(gross - t["charges"], abs=0.01)
    assert t["r_multiple"] == pytest.approx(t["net_pnl"] / (44 * (503 - 482.55)), abs=0.001)
    assert cash("P100") == pytest.approx(100000 + t["net_pnl"], abs=0.01)
    assert get_broker().positions("P100") == []
    with connect() as conn:
        states = [r[0] for r in conn.execute("SELECT to_state FROM trade_events WHERE trade_id = ? ORDER BY id",
                                             (t["id"],))]
    assert states[0] == "BUY_PENDING" and states[-1] == "CLOSED" and "PROTECTED" in states
    assert manager.check_invariants() == []


def test_trail_only_small_qty(clk, quotes):
    add_signal(stop=300.0, target=900.0, rr=1.9, band="C")          # 205 risk/share: P30 1, P60 2, P100 4
    manager.place_entries("2026-09-21")
    assert trade("P30")["qty"] == 1 and trade("P30")["sizing_case"] == "trail_only"
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(503, 504, 499))
    g = get_broker().gtts("P30", "active")[0]
    assert g.type == "single" and g.trigger_values == [300.0]
    assert get_broker().gtts("P100", "active")[0].type == "oco"
    # close >= target: trail-only position starts trailing, stop to entry
    clk.set(datetime(2026, 9, 22, 16, 0))
    add_candle("ABC", "2026-09-22", 500, 910, 499, 905)
    manager.eod_maintenance("2026-09-22")
    t = trade("P30")
    assert (t["state"], t["stop"], t["target_hit_on"]) == ("TRAILING", 503.0, "2026-09-22")
    assert trade("P100")["state"] == "PROTECTED"                 # OCO trails only after its target leg fills


def test_buy_expires_after_two_sessions(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    clk.set(datetime(2026, 9, 22, 16, 0))
    manager.eod_maintenance("2026-09-22")
    assert trade("P100")["state"] == "BUY_PENDING"
    clk.set(datetime(2026, 9, 23, 16, 0))
    manager.eod_maintenance("2026-09-23")
    t = trade("P100")
    assert (t["state"], t["exit_reason"]) == ("CANCELLED", "expired")
    assert get_broker().gtts("P100", "active") == []


def test_gap_above_limit_does_not_fill_and_expires_at_close(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 9, 16), quotes, ABC=(512, 514, 510))
    assert trade("P100")["state"] == "BUY_PENDING"               # triggered, limit order resting unfilled
    cycle(clk, datetime(2026, 9, 22, 11, 0), quotes, ABC=(505, 514, 504))
    t = trade("P100")
    assert (t["state"], t["entry_price"]) == ("FILLED", 505.05)  # resting order fills at its limit
    # P30 would fill the same way; now an order that never fills is cancelled at the session end
    add_signal(day="2026-09-22", symbol="XYZ", sector="Energy", close=200, trigger=201, limit=202.0, stop=190,
               target=230, rr=2.33, band="B")
    clk.set(datetime(2026, 9, 22, 16, 0))
    manager.place_entries("2026-09-22")
    cycle(clk, datetime(2026, 9, 23, 9, 20), quotes, XYZ=(210, 211, 205), ABC=(506, 507, 505))
    clk.set(datetime(2026, 9, 23, 16, 0))
    manager.eod_maintenance("2026-09-23")
    x = trade("P100", "XYZ")
    assert (x["state"], x["exit_reason"]) == ("CANCELLED", "buy_cancelled")


def test_upper_circuit_blocks_buy(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    clk.set(datetime(2026, 9, 22, 10, 0))
    quotes["ABC"] = Quote(ltp=504, high=504, low=500, upper_circuit=504)
    manager.on_cycle()
    assert trade("P100")["state"] == "BUY_PENDING"


def test_gap_down_through_stop_sells_at_next_open(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(503, 504, 499))
    # next day opens below the stop leg's limit: the stop triggers but its limit does not fill
    cycle(clk, datetime(2026, 9, 23, 9, 16), quotes, ABC=(460, 470, 455))
    assert trade("P100")["state"] == "PROTECTED"
    assert any(a["name"] == "exit_unfilled" for a in audit.active_alerts())
    # still below the stop at the close: the stop order expires and an exit-at-next-open GTT replaces it
    clk.set(datetime(2026, 9, 23, 16, 0))
    add_candle("ABC", "2026-09-23", 468, 470, 455, 465)
    manager.eod_maintenance("2026-09-23")
    t = trade("P100")
    assert (t["state"], t["exit_at_open"]) == ("PROTECTED", "2026-09-23")
    g = get_broker().gtts("P100", "active")[0]
    assert g.type == "oco" and g.trigger_values[0] < 465 < g.trigger_values[1]
    assert [l.qty for l in g.orders] == [44, 44]                  # both legs sell everything
    assert g.trigger_values == [463.75, 466.25]                   # 0.25% + one tick either side of the close
    assert t["stop"] == 482.55                                    # no trailing while exiting
    assert any(a["name"] == "gap_exit" for a in audit.active_alerts())
    # next open: the bracket fires and the whole position sells at the open price
    cycle(clk, datetime(2026, 9, 24, 9, 16), quotes, ABC=(458, 458, 458))
    t = trade("P100")
    assert (t["state"], t["exit_reason"], t["open_qty"]) == ("CLOSED", "gap_exit", 0)
    assert json.loads(t["exits"])[0]["price"] == 458.0
    assert get_broker().positions("P100") == []
    assert manager.check_invariants() == []


def test_gap_exit_also_works_after_opening_higher(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 23, 9, 16), quotes, ABC=(460, 470, 455))
    clk.set(datetime(2026, 9, 23, 16, 0))
    add_candle("ABC", "2026-09-23", 468, 470, 455, 465)
    manager.eod_maintenance("2026-09-23")
    cycle(clk, datetime(2026, 9, 24, 9, 16), quotes, ABC=(475, 475, 475))   # opens above: the upper leg fires
    t = trade("P100")
    assert (t["state"], t["exit_reason"]) == ("CLOSED", "gap_exit")


def test_price_below_stop_intraday_waits_for_the_close(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(480, 504, 478))   # crashed through the stop before attach
    t = trade("P100")
    assert t["state"] == "FILLED" and not t["exit_at_open"]
    cycle(clk, datetime(2026, 9, 22, 11, 0), quotes, ABC=(490, 504, 478))   # recovered above the stop: attach it
    assert trade("P100")["state"] == "PROTECTED" and not trade("P100")["exit_at_open"]


def test_stop_first_when_both_legs_touch(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 23, 15, 0), quotes, ABC=(500, 565, 480))
    t = trade("P100")
    assert (t["state"], t["exit_reason"]) == ("CLOSED", "stop")
    assert json.loads(t["exits"])[0]["price"] == 482.55         # just triggered: min(ltp, trigger)
    assert t["r_multiple"] < -1                                   # a full loss plus charges


def test_insufficient_funds_rejects_buy(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    with connect() as conn:
        conn.execute("UPDATE accounts SET cash = 1000 WHERE id = 'P100'")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    t = trade("P100")
    assert (t["state"], t["exit_reason"]) == ("CANCELLED", "buy_rejected")


def test_kill_switch_cancels_pending_buys(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    state = controls.activate_kill_switch("test")
    assert state["cancelled_buy_gtts"] == 3
    assert all(t["state"] == "CANCELLED" and t["exit_reason"] == "kill_switch" for t in trades())
    assert all(get_broker().gtts(a, "active") == [] for a in ("P30", "P60", "P100"))
    add_signal(day="2026-09-22", symbol="XYZ", sector="Energy")
    s = manager.place_entries("2026-09-22")
    assert s["placed"] == {"P30": 0, "P60": 0, "P100": 0}
    with connect() as conn:
        reasons = {r[0] for r in conn.execute("SELECT skip_reason FROM candidates WHERE taken = 0")}
    assert "Kill switch is on" in reasons


def test_rerun_of_entries_is_idempotent(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    manager.place_entries("2026-09-21")
    assert len(trades("P100")) == 1
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM candidates WHERE taken = 1").fetchone()[0] == 3
    assert len(get_broker().gtts("P100", "active")) == 1


def test_drawdown_halt_and_equity_snapshot(clk, quotes):
    with connect() as conn:
        conn.execute("UPDATE accounts SET cash = 93000 WHERE id = 'P100'")   # 7% below the 1,00,000 peak
    out = manager.equity_snapshot("2026-09-21")
    assert out["P100"]["drawdown_pct"] == 7.0 and out["P30"]["drawdown_pct"] == 0
    assert controls.entries_blocked("P100") is not None
    with connect() as conn:
        assert conn.execute("SELECT halted FROM accounts WHERE id = 'P100'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM equity_daily").fetchone()[0] == 3


def test_orphan_gtt_is_cancelled(clk, quotes):
    gid = get_broker().place_gtt("P60", "ORPH", "single", [100], 110, [Leg("SELL", 5, 98)])
    clk.set(datetime(2026, 9, 22, 10, 0))
    quotes["ORPH"] = Quote(ltp=110, high=111, low=109)
    manager.on_cycle()
    assert get_broker().get_gtt("P60", gid).status == "cancelled"
    assert any(a["name"] == "invariant" for a in audit.active_alerts())


def test_monitor_skips_when_market_closed(clk, quotes):
    from agent.scheduler import Skip
    clk.set(datetime(2026, 9, 22, 8, 0))
    with pytest.raises(Skip):
        manager.on_cycle()


def test_quote_failure_fails_closed(clk, monkeypatch):
    from agent.scheduler import Skip
    add_signal()
    manager.place_entries("2026-09-21")

    def boom(symbols):
        raise RuntimeError("network down")
    monkeypatch.setattr(feed, "quote_source", boom)
    clk.set(datetime(2026, 9, 22, 10, 0))
    with pytest.raises(Skip):
        manager.on_cycle()
    assert trade("P100")["state"] == "BUY_PENDING"


def test_views_and_api(clk, quotes):
    from fastapi.testclient import TestClient
    from agent.api.app import create_app
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(510, 511, 499))
    client = TestClient(create_app(start_scheduler=False))
    acc = {a["id"]: a for a in client.get("/api/accounts").json()["accounts"]}
    p100 = acc["P100"]
    assert p100["open_positions"] == 1 and p100["holdings"] == pytest.approx(44 * 510)
    assert p100["equity"] == pytest.approx(p100["cash"] + p100["holdings"])
    pos = client.get("/api/positions?account=P100").json()
    o = pos["open"][0]
    assert o["state"] == "PROTECTED" and o["price"] == 510 and o["unrealized"] == pytest.approx(44 * 7)
    # net = gross − the buy charges booked on the fill − Zerodha charges to sell all 44 at 510 now
    from agent.orders.charges import compute
    sell = compute("SELL", 44, 510).total
    assert o["sell_charges_est"] == pytest.approx(sell) and sell > 15.34            # includes the DP charge
    assert o["charges"] > 0 and o["buy_charges_open"] == pytest.approx(o["charges"])   # nothing sold yet
    assert o["net_unrealized"] == pytest.approx(44 * 7 - o["charges"] - sell, abs=0.01)
    assert o["gtts"][0]["broker_status"] == "active"
    d = client.get(f"/api/trades/{o['id']}").json()
    assert [e["to_state"] for e in d["events"]] == ["BUY_PENDING", "FILLED", "PROTECTED"]
    sig = client.get("/api/signals?date=2026-09-21").json()["signals"][0]
    assert {a["account_id"]: a["taken"] for a in sig["accounts"]} == {"P30": 1, "P60": 1, "P100": 1}


# ---------- partial fills: never treated as complete ----------

def q(ltp, hi, lo, bids=None, asks=None):
    return Quote(ltp=ltp, high=hi, low=lo, bids=bids, asks=asks)


def _held(acc, sym="ABC"):
    return {p.symbol: p.qty for p in get_broker().positions(acc)}.get(sym, 0)


def test_partial_buy_is_protected_and_monitored_until_full(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    # only 20 shares offered at or below the limit: 20 of 44 bought, the rest keeps working
    clk.set(datetime(2026, 9, 22, 10, 0))
    quotes["ABC"] = q(503, 504, 499, asks=[(503, 20), (506, 500)])
    manager.on_cycle()
    t = trade("P100")
    assert (t["state"], t["bought_qty"], t["open_qty"], t["entry_price"]) == ("FILLED", 20, 20, 503.0)
    assert manager.account_state("P100").reserved == pytest.approx(24 * 505.05)   # the rest is still committed
    # next cycle: the 20 held shares get their exit (OCO on 20, target leg 10) while the buy keeps working
    clk.set(datetime(2026, 9, 22, 10, 1))
    quotes["ABC"] = q(504, 504, 499, asks=[(506, 500)])                           # nothing at the limit now
    manager.on_cycle()
    t = trade("P100")
    assert t["state"] == "PROTECTED" and t["bought_qty"] == 20
    g = get_broker().gtts("P100", "active")[0]
    assert [l.qty for l in g.orders] == [20, 10]
    # more is offered: the rest fills at the limit and the exit GTT grows to cover all 44
    clk.set(datetime(2026, 9, 22, 10, 5))
    quotes["ABC"] = q(505, 505, 499, asks=[(505, 100)])
    manager.on_cycle()
    t = trade("P100")
    assert (t["bought_qty"], t["open_qty"]) == (44, 44)
    assert t["entry_price"] == pytest.approx((20 * 503 + 24 * 505.05) / 44, abs=0.001)
    g = get_broker().gtts("P100", "active")[0]
    assert g.type == "oco" and [l.qty for l in g.orders] == [44, 22]
    assert _held("P100") == 44 and manager.check_invariants() == []
    assert manager.account_state("P100").reserved == 0
    with connect() as conn:
        msgs = [r[0] for r in conn.execute("SELECT message FROM trade_events WHERE trade_id = ? ORDER BY id", (t["id"],))]
    assert any("partly: 20 of 44" in m for m in msgs) and any("resized to cover 44" in m for m in msgs)


def test_partial_buy_cut_at_session_end_keeps_what_was_bought(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    cycle_q = lambda h, m, quote: (clk.set(datetime(2026, 9, 22, h, m)), quotes.__setitem__("ABC", quote),
                                   manager.on_cycle())
    cycle_q(10, 0, q(503, 504, 499, asks=[(503, 2)]))
    cycle_q(10, 1, q(504, 504, 499, asks=[(506, 500)]))
    t = trade("P100")
    assert t["state"] == "PROTECTED" and get_broker().gtts("P100", "active")[0].type == "single"   # 2 shares: stop only
    clk.set(datetime(2026, 9, 22, 16, 0))
    add_candle("ABC", "2026-09-22", 500, 506, 499, 504)
    manager.eod_maintenance("2026-09-22")
    t = trade("P100")
    assert (t["state"], t["qty"], t["bought_qty"], t["open_qty"]) == ("PROTECTED", 44, 2, 2)
    assert manager.account_state("P100").reserved == 0
    assert any(a["name"] == "partial_fill" for a in audit.active_alerts())
    assert manager.check_invariants() == []


def test_partial_stop_keeps_selling_until_flat(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(503, 504, 499))
    # stop triggers; only 10 shares bid at or above the stop leg's limit (472.85)
    clk.set(datetime(2026, 9, 23, 11, 0))
    quotes["ABC"] = q(480, 500, 479, bids=[(480, 10), (470, 500)])
    manager.on_cycle()
    t = trade("P100")
    assert (t["state"], t["open_qty"]) == ("PROTECTED", 34)
    assert _held("P100") == 34 and manager.check_invariants() == []
    assert any(a["name"] == "partial_fill" for a in audit.active_alerts())
    # the rest fills later at the limit: trade closes, one exit record for the order
    clk.set(datetime(2026, 9, 23, 11, 5))
    quotes["ABC"] = q(478, 500, 477, bids=[(478, 100)])
    manager.on_cycle()
    t = trade("P100")
    assert (t["state"], t["exit_reason"], t["open_qty"]) == ("CLOSED", "stop", 0)
    exits = json.loads(t["exits"])
    assert len(exits) == 1 and exits[0]["qty"] == 44
    assert exits[0]["price"] == pytest.approx((10 * 480 + 34 * 472.85) / 44, abs=0.001)
    assert exits[0]["charges"] == pytest.approx(charges.compute("SELL", 10, 480).total
                                                + charges.compute("SELL", 34, 472.85, first_sell_today=False).total,
                                                abs=0.02)                  # DP charged once for the day
    assert _held("P100") == 0


def test_partial_stop_cut_at_close_sells_rest_next_open(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(503, 504, 499))
    clk.set(datetime(2026, 9, 23, 11, 0))
    quotes["ABC"] = q(480, 500, 479, bids=[(480, 10), (470, 500)])
    manager.on_cycle()
    clk.set(datetime(2026, 9, 23, 16, 0))
    add_candle("ABC", "2026-09-23", 495, 500, 465, 468)
    manager.eod_maintenance("2026-09-23")
    t = trade("P100")
    assert (t["state"], t["open_qty"], t["exit_at_open"]) == ("PROTECTED", 34, "2026-09-23")
    assert [l.qty for l in get_broker().gtts("P100", "active")[0].orders] == [34, 34]
    cycle(clk, datetime(2026, 9, 24, 9, 16), quotes, ABC=(460, 460, 460))
    t = trade("P100")
    assert (t["state"], t["exit_reason"]) == ("CLOSED", "gap_exit")
    assert [e["qty"] for e in json.loads(t["exits"])] == [10, 34]
    assert _held("P100") == 0 and manager.check_invariants() == []


def test_kill_switch_cancels_rest_of_partly_filled_buy(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    clk.set(datetime(2026, 9, 22, 10, 0))
    quotes["ABC"] = q(503, 504, 499, asks=[(503, 20)])
    manager.on_cycle()
    controls.activate_kill_switch("test")
    clk.set(datetime(2026, 9, 22, 10, 1))
    quotes["ABC"] = q(504, 504, 499, asks=[(504, 500)])
    manager.on_cycle()
    t = trade("P100")
    assert (t["state"], t["bought_qty"], t["open_qty"]) == ("PROTECTED", 20, 20)   # kept and protected, no more bought
    assert _held("P100") == 20 and manager.check_invariants() == []


def test_quantity_mismatch_raises_invariant(clk, quotes):
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    with connect() as conn:
        conn.execute("UPDATE paper_positions SET qty = 40 WHERE account_id = 'P100'")
    assert "qty-P100-ABC" in manager.check_invariants()


# ---------- entries on every refresh (decided 2026-09-24) ----------

def test_next_morning_refresh_places_only_signals_still_below_trigger(clk, quotes):
    """Signals from 21 Sep placed by a refresh on 22 Sep during market hours: rank order, live-price check."""
    a = add_signal(symbol="AAA", sector="S1", close=500, trigger=502.5, limit=505.05, stop=482.55, band="A", rr=2.6)
    b = add_signal(symbol="BBB", sector="S2", close=200, trigger=201.0, limit=202.0, stop=193.0, band="A", rr=2.7)
    c = add_signal(symbol="CCC", sector="S3", close=100, trigger=100.5, limit=101.0, stop=96.5, band="B")
    clk.set(datetime(2026, 9, 22, 10, 30))
    quotes["AAA"] = Quote(499, 501, 497)      # still below 502.5 by more than 0.25%
    quotes["BBB"] = Quote(201.5, 202, 199)    # breakout already happened this morning → missed
    # CCC: no live price → not placed (fail closed)
    out = manager.refresh_entries("2026-09-21")
    assert out["placed"] == {"P30": 1, "P60": 1, "P100": 1} and out["missed"]["P30"] == 2
    assert {t["symbol"] for t in trades("P100")} == {"AAA"}
    t = trade("P100", "AAA")
    assert t["placed_on"] == "2026-09-21"      # 2-session expiry still counts from the signal day
    with connect() as conn:
        why = dict(conn.execute("SELECT signal_id, skip_reason FROM candidates WHERE account_id = 'P100'").fetchall())
    assert why[b].startswith("Missed: price 201.5 already past the trigger 201")
    assert why[c].startswith("No live price") and why[a] is None
    # a later refresh: BBB fell back below its trigger → placed now; AAA is not placed twice
    quotes["BBB"] = Quote(198, 202, 197)
    out = manager.refresh_entries("2026-09-21")
    assert out["placed"]["P100"] == 1 and {t["symbol"] for t in trades("P100")} == {"AAA", "BBB"}
    assert len(trades("P100")) == 2


def test_refresh_same_evening_uses_the_close(clk, quotes):
    add_signal()                                # 21 Sep signal, refreshed at 21 Sep 16:00 (EOD / after upload)
    out = manager.refresh_entries("2026-09-21")
    assert out["placed"]["P100"] == 1 and not out["missed"]["P100"]


def test_refresh_cancels_pending_buy_whose_signal_dropped(clk, quotes):
    sid = add_signal()
    manager.refresh_entries("2026-09-21")
    assert trade("P60")["state"] == "BUY_PENDING"
    with connect() as conn:                     # e.g. the stock was removed from the watchlist, then re-scanned
        conn.execute("UPDATE signals SET eligible = 0, block_reasons = ? WHERE id = ?",
                     (json.dumps(["Not on the watchlist"]), sid))
    out = manager.refresh_entries("2026-09-21")
    t = trade("P60")
    assert out["dropped"] == 3 and t["state"] == "CANCELLED" and t["exit_reason"] == "signal_dropped"
    assert get_broker().gtts("P60", "active") == []
    # back on the watchlist: the signal may be placed again
    with connect() as conn:
        conn.execute("UPDATE signals SET eligible = 1, block_reasons = '[]' WHERE id = ?", (sid,))
    manager.refresh_entries("2026-09-21")
    assert [x["state"] for x in trades("P60")] == ["CANCELLED", "BUY_PENDING"]


def test_dropped_signal_never_touches_a_triggered_buy(clk, quotes):
    sid = add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))   # buy GTT triggered and filled
    with connect() as conn:
        conn.execute("UPDATE signals SET eligible = 0 WHERE id = ?", (sid,))
    assert manager.cancel_dropped_buys() == 0
    assert trade("P60")["state"] in ("FILLED", "PROTECTED")


def test_day_order_left_by_a_missed_eod_does_not_fill_the_next_day(clk, quotes):
    """The buy GTT triggers on a gap above the limit (order rests unfilled); the laptop is off at the EOD run.
    Next morning the monitor expires that day order first, as Kite would have at the close: no fill at 504."""
    add_signal()
    manager.place_entries("2026-09-21")
    clk.set(datetime(2026, 9, 22, 9, 16))
    quotes["ABC"] = Quote(507, 507, 506, open=507)                 # gapped over the 505.05 limit
    manager.on_cycle()
    assert get_broker().orders("P100")[0].status == "open"
    clk.set(datetime(2026, 9, 23, 10, 0))                          # no EOD run on the 22nd
    quotes["ABC"] = Quote(504, 508, 503)
    manager.on_cycle()
    assert get_broker().orders("P100")[0].status == "cancelled"
    assert trade("P100")["state"] == "CANCELLED" and not get_broker().positions("P100")
