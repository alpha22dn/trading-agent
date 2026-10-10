"""Scenario suite (spec section 13), run through replay mode: the live engine stepped session by session over
synthetic daily candles (intraday path open → low → high → close on up days, open → high → low → close on down
days). Each test names the spec scenario it covers."""
from __future__ import annotations

from datetime import datetime

import pytest

from agent import audit, scheduler
from agent.broker import feed, paper
from agent.broker.base import Leg, Quote
from agent.broker.paper import get_broker
from agent.db import connect
from agent.orders import manager, paper_edits
from agent.replay import Replay, day_path
from agent.risk import controls
from agent.strategy import scanner

from .test_phase2 import add_signal, trade, trades


class World:
    """Synthetic market: bars[day][symbol] = (open, high, low, close[, upper circuit, lower circuit])."""

    def __init__(self):
        self.bars: dict[str, dict[str, tuple]] = {}

    def candles(self, day):
        return self.bars.get(day, {})

    def day(self, rp: Replay, day: str, entries: bool = True, before_eod=None, **bars):
        self.bars[day] = bars
        with connect() as conn:
            for sym, b in bars.items():
                conn.execute("INSERT OR REPLACE INTO candles_daily (symbol, date, open, high, low, close, volume, "
                             "fetched_at) VALUES (?, ?, ?, ?, ?, ?, 100000, 'x')", (sym, day, *b[:4]))
        return rp.run_day(day, entries=entries, before_eod=before_eod)


@pytest.fixture
def world():
    w = World()
    with Replay(w.candles) as rp:
        w.rp = rp
        yield w


def D(w, day, **bars):
    return w.day(w.rp, day, **bars)


FLAT = (500, 501, 499, 500)       # ABC doing nothing: below the 502.5 trigger


def _entered(w):
    """ABC signal on Mon 21 Sep (trigger 502.5, limit 505.05, stop 482.55, target 560); bought Tue 22 Sep."""
    add_signal()
    D(w, "2026-09-21", ABC=FLAT)
    D(w, "2026-09-22", ABC=(503, 506, 501, 504))
    assert trade("P100")["state"] == "PROTECTED"


def test_intraday_path_is_open_low_high_close_on_up_days():
    assert day_path(100, 110, 95, 108) == [(100, 100, 100), (95, 100, 95), (110, 110, 95), (108, 110, 95)]
    assert day_path(100, 110, 95, 97) == [(100, 100, 100), (110, 110, 100), (95, 110, 95), (97, 110, 95)]


# 1. Buy GTT triggers but the limit order does not fill
def test_s01_buy_triggers_but_limit_never_fills(world):
    add_signal()
    D(world, "2026-09-21", ABC=FLAT)
    D(world, "2026-09-22", ABC=(505.5, 512, 505.2, 511))        # opens past the trigger and just above the limit
    t = trade("P100")
    assert (t["state"], t["exit_reason"]) == ("CANCELLED", "buy_cancelled")
    assert get_broker().positions("P100") == []


def test_s01b_resting_buy_fills_at_limit_when_price_comes_back(world):
    add_signal()
    D(world, "2026-09-21", ABC=FLAT)
    D(world, "2026-09-22", ABC=(506, 509, 504, 505))            # opens above the limit, later trades at 504
    t = trade("P100")
    assert t["state"] in ("FILLED", "PROTECTED") and t["entry_price"] == 505.05


def test_s01c_crossing_fills_at_the_trigger_not_the_later_price(world):
    add_signal()
    D(world, "2026-09-21", ABC=FLAT)
    D(world, "2026-09-22", ABC=(500, 513, 499, 512))            # crosses 502.5 on the way up
    assert trade("P100")["entry_price"] == 502.5                 # the limit order went in at the crossing


# 2a. Gap-up above the entry limit
def test_s02a_gap_up_above_limit_no_chase(world):
    add_signal()
    D(world, "2026-09-21", ABC=FLAT)
    D(world, "2026-09-22", ABC=(520, 525, 515, 522))
    assert trade("P100")["exit_reason"] == "buy_cancelled" and get_broker().cash("P100") == 100000


# 2b. Gap-down through the stop: stop order unfilled, sold at the next open
def test_s02b_gap_down_through_stop_sells_next_open(world):
    _entered(world)
    D(world, "2026-09-23", ABC=(460, 468, 455, 465))            # opens below the stop leg's limit 472.85
    t = trade("P100")
    assert (t["state"], t["exit_at_open"]) == ("PROTECTED", "2026-09-23")
    D(world, "2026-09-24", ABC=(458, 462, 455, 460))
    t = trade("P100")
    assert (t["state"], t["exit_reason"]) == ("CLOSED", "gap_exit") and t["r_multiple"] < -1.5


# 3. Target leg fills and a new stop is placed on the remainder; then trailing
def test_s03_target_then_new_stop_then_trail(world):
    _entered(world)
    D(world, "2026-09-23", ABC=(540, 562, 538, 558))
    t = trade("P100")
    assert (t["state"], t["open_qty"], t["stop"]) == ("TRAILING", 22, 503.0)   # EOD moved it to entry
    g = get_broker().gtts("P100", "active")[0]
    assert g.type == "single" and g.orders[0].qty == 22
    D(world, "2026-09-24", ABC=(558, 575, 556, 570))
    assert trade("P100")["stop"] == 553.2                        # 556 × 0.995 = 553.22 → tick 553.20
    D(world, "2026-09-25", ABC=(565, 566, 548, 550))            # falls through the trailed stop
    t = trade("P100")
    assert (t["state"], t["exit_reason"]) == ("CLOSED", "stop") and t["net_pnl"] > 0


# 4. Qty 1–3 (trail-only) and qty 0 (skip)
def test_s04_trail_only_and_qty_zero(world):
    add_signal(stop=300.0, target=900.0, rr=1.9, band="C")      # risk 205/share: P30 1, P60 2, P100 4
    add_signal(symbol="WIDE", sector="Energy", close=5000, trigger=5025, limit=5050.1, stop=1000, target=15000,
               rr=2.5, band="A")                                 # risk 4,050/share: qty 0 in every account
    D(world, "2026-09-21", ABC=FLAT, WIDE=(5000, 5001, 4999, 5000))
    assert [t["symbol"] for t in trades("P30")] == ["ABC"]
    with connect() as conn:
        reasons = {r[0] for r in conn.execute("SELECT skip_reason FROM candidates WHERE taken = 0")}
    assert any("qty 0" in r for r in reasons)
    D(world, "2026-09-22", ABC=(503, 506, 501, 504), WIDE=(5000, 5001, 4999, 5000))
    assert trade("P30")["sizing_case"] == "trail_only"
    assert get_broker().gtts("P30", "active")[0].type == "single"
    assert get_broker().gtts("P100", "active")[0].type == "oco"


# 5. Several buy GTTs trigger the same morning with limited cash
def test_s05_buys_same_morning_limited_cash(world):
    syms = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    for i, sym in enumerate(syms):
        add_signal(symbol=sym, sector=f"S{i}", close=100, trigger=100.5, limit=101.0, stop=98, target=110, rr=3.0,
                   band="A")
    quiet = (100, 100.2, 99.8, 100)
    D(world, "2026-09-21", **{s: quiet for s in syms})
    # P30: each position is capped at 25% (74 × ₹101 = ₹7,474), so cash covers four; the fifth is not placed
    assert len(trades("P30")) == 4
    with connect() as conn:
        r = conn.execute("SELECT skip_reason FROM candidates WHERE account_id = 'P30' AND taken = 0").fetchone()[0]
    assert "Not enough free cash" in r
    # P100's cash drops after the EOD run (as if spent elsewhere): the funds check at trigger time rejects
    with connect() as conn:
        conn.execute("UPDATE accounts SET cash = 30000 WHERE id = 'P100'")
    bar = (100, 101, 99.5, 100.8)
    D(world, "2026-09-22", **{s: bar for s in syms})
    assert [t["state"] for t in trades("P30")] == ["PROTECTED"] * 4        # all four fill the same morning
    p100 = [t["state"] + ":" + str(t["exit_reason"]) for t in trades("P100")]
    assert p100 == ["PROTECTED:None"] + ["CANCELLED:buy_rejected"] * 3     # ₹30,000 covers one ₹24,947 buy
    assert get_broker().cash("P30") >= 0 and get_broker().cash("P100") >= 0
    assert manager.check_invariants() == []


# 6. Duplicate GTT prevention and orphan GTT cleanup
def test_s06_duplicates_and_orphans(world):
    add_signal()
    b = get_broker()
    stale = b.place_gtt("P100", "ABC", "single", [502.5], 500, [Leg("BUY", 44, 505.05)])   # crash leftover
    D(world, "2026-09-21", ABC=FLAT)
    assert b.get_gtt("P100", stale).status == "cancelled"         # cancelled before placing the tracked one
    assert len([g for g in b.gtts("P100", "active") if g.symbol == "ABC"]) == 1
    orphan = b.place_gtt("P30", "ZZZ", "single", [90], 100, [Leg("SELL", 5, 88)])          # stop on nothing held
    D(world, "2026-09-22", ABC=(503, 506, 501, 504))
    assert b.get_gtt("P30", orphan).status == "cancelled"
    assert all(len([g for g in b.gtts(a, "active") if g.symbol == "ABC"]) == 1 for a in ("P30", "P60", "P100"))
    assert manager.check_invariants() == []


# 7. All five manual-change cases from section 8 (P60)
def test_s07_five_manual_changes(world):
    add_signal()
    add_signal(symbol="XYZ", sector="Energy", close=200, trigger=201, limit=202.0, stop=190, target=230, rr=2.33,
               band="B")
    D(world, "2026-09-21", ABC=FLAT, XYZ=(200, 200.5, 199.5, 200))
    xyz = trade("P60", "XYZ")
    paper_edits.edit_trade(xyz["id"], "cancel_buy")                          # (2) user cancels a buy GTT
    D(world, "2026-09-22", ABC=(503, 506, 501, 504), XYZ=(201, 203, 200, 202))
    assert trade("P60", "XYZ")["exit_reason"] == "manual_cancel" and len(trades("P60")) == 2
    abc = trade("P60")
    paper_edits.edit_trade(abc["id"], "move_stop", 495)                      # (1) user moves the stop
    assert trade("P60")["stop"] == 495
    paper_edits.buy_outside("P60", "XYZ", 5)                                 # (5) user buys outside the system
    paper_edits.edit_trade(abc["id"], "cancel_exit")                         # (3) user cancels the exit GTT
    D(world, "2026-09-23", ABC=(505, 508, 503, 506), XYZ=(202, 204, 201, 203))
    assert trade("P60")["awaiting_user"] == "exit_cancelled" and trade("P60")["state"] == "FILLED"
    manager.confirm_new_exit(abc["id"])
    D(world, "2026-09-24", ABC=(506, 509, 504, 507), XYZ=(203, 204, 202, 203))
    assert trade("P60")["state"] == "PROTECTED" and trade("P60")["stop"] == 495
    paper_edits.edit_trade(abc["id"], "sell", qty=26)                        # (4) user sells by hand
    assert trade("P60")["exit_reason"] == "manual_sell"
    assert get_broker().gtts("P60", "active") == []
    with connect() as conn:
        kinds = [r[0] for r in conn.execute("SELECT change_type FROM overrides ORDER BY id")]
    assert kinds == ["buy_cancelled", "stop_moved", "exit_cancelled", "manual_sell"]
    assert manager.check_invariants() == []                                 # 5 XYZ held outside: expected
    assert trades("P100")[0]["state"] == "PROTECTED"                         # baselines untouched


# 8. Expired session, missed login, stale data, API error
def test_s08_session_login_stale_and_api_error(world, monkeypatch, fake_kite):
    _entered(world)
    # API error during market hours: no prices, nothing simulated, alert once, then recovery
    def boom(symbols):
        raise RuntimeError("Kite API 503")
    good = feed.quote_source
    monkeypatch.setattr(feed, "quote_source", boom)
    world.rp.clock.set(datetime(2026, 9, 23, 10, 0))
    with pytest.raises(scheduler.Skip):
        manager.on_cycle()
    assert trade("P100")["state"] == "PROTECTED"
    assert any(a["name"] == "order_monitor" for a in audit.active_alerts())
    monkeypatch.setattr(feed, "quote_source", good)
    # stale data: no candle for ABC today → no trailing, no crash; equity marked at cost
    world.rp.clock.set(datetime(2026, 9, 23, 21, 0))
    out = manager.eod_maintenance("2026-09-23")
    assert out["trails"] == [] and trade("P100")["stop"] == 482.55
    # expired session / missed login: the EOD job and the login check skip and alert; GTTs stay in place
    world.rp.clock.set(datetime(2026, 9, 24, 21, 0))
    run = scheduler.run_job("eod_run")
    assert run["status"] == "skipped" and "Kite" in run["message"]
    world.rp.clock.set(datetime(2026, 9, 24, 9, 10))
    assert scheduler.run_job("login_check")["status"] == "skipped"
    assert any(a["name"] == "login_missed" for a in audit.active_alerts())
    assert len(get_broker().gtts("P100", "active")) == 1


# 9. Crash and restart during market hours
def test_s09_crash_and_restart(world, monkeypatch):
    add_signal()
    D(world, "2026-09-21", ABC=FLAT)
    # the buy fills at 09:16, then the process dies before the next cycle
    world.bars["2026-09-22"] = {"ABC": (503, 506, 501, 504)}
    world.rp.clock.set(datetime(2026, 9, 22, 9, 16))
    world.rp._book.clear()
    world.rp._book["ABC"] = Quote(503, 503, 503)
    manager.on_cycle()
    assert trade("P100")["state"] == "FILLED"
    paper._broker = None                                         # restart: fresh broker object, same database
    world.rp.clock.set(datetime(2026, 9, 22, 9, 40))
    manager.on_cycle()
    t = trade("P100")
    assert t["state"] == "PROTECTED" and len(get_broker().gtts("P100", "active")) == 1
    assert any(a["name"] == "exit_attach_late" for a in audit.active_alerts())   # 24 min > 5 min limit
    # the missed EOD run is caught up after a restart past its time
    from agent.db import set_state
    set_state(scheduler.EOD_SESSION_KEY, "2026-09-21")             # last EOD done: 21 Sep
    world.rp.clock.set(datetime(2026, 9, 22, 21, 30))
    s = scheduler.build_scheduler()
    assert scheduler.catch_up(s) == []                 # held until Kite confirms the login
    monkeypatch.setattr(scheduler, "login_verified", lambda: True)     # Kite confirmed the login
    assert "eod_run" in scheduler.catch_up(s) and s.get_job("catch_up") is not None


# 10. Upper and lower circuit; market holiday
def test_s10_circuits_and_holiday(world):
    add_signal()
    add_signal(symbol="XYZ", sector="Energy", close=200, trigger=201, limit=202.0, stop=190, target=230, rr=2.33,
               band="B")
    D(world, "2026-09-21", ABC=FLAT, XYZ=(200, 200.5, 199.5, 200))
    # ABC locked at its upper circuit all day: the buy triggers but can't fill, and expires at the close
    D(world, "2026-09-22", ABC=(503, 503, 503, 503, 503, 450), XYZ=(201, 203, 200, 202))
    assert trade("P100")["exit_reason"] == "buy_cancelled"
    assert trade("P100", "XYZ")["state"] == "PROTECTED"
    # XYZ locked at its lower circuit (188, above the stop leg's 186.20 limit): the stop can't fill
    D(world, "2026-09-23", XYZ=(188, 188, 188, 188, 230, 188))
    t = trade("P100", "XYZ")
    assert t["state"] == "PROTECTED" and t["exit_at_open"] == "2026-09-23"    # sells at the next open
    world.rp.clock.set(datetime(2026, 10, 2, 10, 0))             # Gandhi Jayanti
    with pytest.raises(scheduler.Skip):
        manager.on_cycle()
    assert scheduler.run_job("eod_run")["status"] == "skipped"


# 11. Drawdown halt, kill switch, re-arm
def test_s11_halt_kill_switch_rearm(world):
    add_signal()
    with connect() as conn:
        conn.execute("UPDATE accounts SET cash = 93000 WHERE id = 'P100'")
    D(world, "2026-09-21", ABC=FLAT)
    assert [t["account_id"] for t in trades()] == ["P30", "P60"]  # P100 halted by the EOD equity check
    controls.rearm_account("P100")
    add_signal(day="2026-09-22", symbol="XYZ", sector="Energy", close=200, trigger=201, limit=202.0, stop=190,
               target=230, rr=2.33, band="B")
    controls.activate_kill_switch("scenario")
    assert all(t["state"] == "CANCELLED" for t in trades())      # pending buys cancelled
    D(world, "2026-09-22", ABC=FLAT, XYZ=(200, 200.5, 199.5, 200))
    assert not any(t["symbol"] == "XYZ" for t in trades())       # entries blocked
    controls.rearm_kill_switch()
    add_signal(day="2026-09-23", symbol="QQQ", sector="IT", close=300, trigger=301.5, limit=303.0, stop=290,
               target=340, rr=2.85, band="A")
    D(world, "2026-09-23", ABC=FLAT, XYZ=FLAT, QQQ=(300, 300.5, 299.5, 300))
    # P100 is still 7% below its old peak, but re-arming restarted the peak: it trades again
    assert {t["account_id"] for t in trades() if t["symbol"] == "QQQ"} == {"P30", "P60", "P100"}


# 12. Held stock drops out of NSE500: keep managing, no new entries
def test_s12_dropped_from_nse500(world):
    _entered(world)
    with connect() as conn:
        conn.execute("UPDATE instruments SET is_nse500 = 0 WHERE symbol = 'ABC'")
        assert "ABC" not in set(scanner.load_joined(conn)["symbol"])   # out of the scan universe
    D(world, "2026-09-23", ABC=(540, 562, 538, 558))
    D(world, "2026-09-24", ABC=(558, 575, 556, 570))
    assert trade("P100")["stop"] == 553.2                        # still trailed


# 13. Split or bonus on a held stock: pause management and alert
def test_s13_split_pauses_management(world, monkeypatch):
    _entered(world)
    from agent.data import candles, events
    from agent.strategy import indicators
    monkeypatch.setattr(candles, "run_backfill", lambda: {"updated": 1, "error_count": 0, "adjusted": ["ABC"]})
    monkeypatch.setattr(indicators, "compute_all", lambda params: {"symbols": 1})
    monkeypatch.setattr(events, "fetch_daily", lambda: {"ok": True})
    monkeypatch.setattr(events, "status", lambda: {"history": {"ok": True}})
    monkeypatch.setattr(scanner, "scan_eod", lambda s: {"date": "2026-09-23", "triggers": 0, "eligible": 0})
    world.bars["2026-09-23"] = {"ABC": (540, 562, 538, 558)}
    with connect() as conn:
        conn.execute("INSERT OR REPLACE INTO candles_daily (symbol, date, open, high, low, close, volume, fetched_at) "
                     "VALUES ('ABC', '2026-09-24', 270, 281, 269, 280, 1, 'x')")      # 1:2 split prices
    world.rp.clock.set(datetime(2026, 9, 24, 21, 0))
    from agent.broker import kite_session
    monkeypatch.setattr(kite_session, "status", lambda: {"valid": True})
    run = scheduler.run_job("eod_run")
    assert run["status"] == "ok", run
    t = trade("P100")
    assert t["paused"] and t["stop"] == 482.55                   # no trailing on the split-distorted candle
    assert any(a["name"] == "trade_paused" for a in audit.active_alerts())


# Replay is deterministic: the same inputs give the same trades, to the paisa
def test_replay_is_deterministic(world):
    def run_once():
        _entered(world)
        D(world, "2026-09-23", ABC=(540, 562, 538, 558))
        D(world, "2026-09-24", ABC=(558, 575, 556, 570))
        D(world, "2026-09-25", ABC=(565, 566, 548, 550))
        with connect() as conn:
            return [tuple(r) for r in conn.execute(
                "SELECT account_id, symbol, state, qty, entry_price, stop, net_pnl, r_multiple, exit_reason, charges "
                "FROM trades ORDER BY id")]
    first = run_once()
    with connect() as conn:
        for tbl in ("trade_events", "gtts", "candidates", "trades", "paper_gtts", "paper_orders", "paper_positions",
                    "equity_daily", "signals", "overrides", "unmanaged_holdings", "app_state"):
            conn.execute(f"DELETE FROM {tbl}")
        conn.execute("UPDATE accounts SET cash = capital, peak_equity = capital, halted = 0")
    assert run_once() == first
