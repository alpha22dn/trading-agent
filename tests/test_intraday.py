"""Intraday pullback (decided 2026-09-30): rules, the 5-minute scan, GTT placement, full-quantity OCO exit,
same-day charges, the 15:15 cutoff and the guards (no daily scan, backtest or learning for it yet)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from agent import scheduler
from agent.clock import IST, ts
from agent.db import connect
from agent.orders import charges
from agent.strategy import intraday, intraday_pullback as rules, strategies

from .test_phase2 import clk, cycle, quotes, trade  # noqa: F401 - fixtures

BASE = [(100, 101, 99.8, 100.8, 5000), (100.8, 101.8, 100.7, 101.6, 4000), (101.6, 102.6, 101.5, 102.4, 4000),
        (102.4, 105.0, 102.3, 104.6, 4000), (104.6, 106.0, 104.2, 105.2, 3000), (105.2, 105.3, 103.6, 103.9, 3000)]
PULLBACK = (103.2, 103.3, 102.75, 103.2, 2000)       # dips to VWAP (~102.68), closes above it


def bars(day, rows, start=(9, 15)):
    t = datetime(*day, *start, tzinfo=IST)
    out = []
    for o, h, lo, c, v in rows:
        out.append({"date": t, "open": o, "high": h, "low": lo, "close": c, "volume": v})
        t += timedelta(minutes=5)
    return out


PREV = bars((2026, 9, 21), [(100, 100.2, 99.8, 100, 1000)] * 75)


def test_rules():
    params = rules.template("x")
    today = bars((2026, 9, 22), BASE + [PULLBACK])
    s, why = rules.evaluate(PREV, today, 6, 100.0, 0.05, params)
    assert why is None and s.rr_ok and s.touched == "VWAP"
    assert (s.entry_trigger, s.entry_limit, s.stop, s.target, s.rr_band) == (103.4, 103.65, 102.6, 106.0, "B")
    assert rules.evaluate(PREV, today, 5, 100.0, 0.05, params)[0] is None          # 09:40: no dip to EMA/VWAP
    assert "window" in rules.evaluate(PREV, today, 1, 100.0, 0.05, params)[1]       # ends 09:25
    assert "up" in rules.evaluate(PREV, today, 6, 102.5, 0.05, params)[1]           # only +0.7% on the day
    late = bars((2026, 9, 22), BASE + [PULLBACK], start=(14, 5))                    # candle ends 14:40
    assert "window" in rules.evaluate(PREV, late, 6, 100.0, 0.05, params)[1]
    low_rr = bars((2026, 9, 22), BASE + [(103.9, 103.95, 102.7, 103.1, 3000)])
    assert not rules.evaluate(PREV, low_rr, 6, 100.0, 0.05, params)[0].rr_ok        # target too close: RR 0.97


@pytest.fixture
def world(monkeypatch):
    from agent.data import events
    monkeypatch.setattr(events, "fresh_for", lambda d: True)
    intraday.reset_day_state()
    with connect() as conn:
        conn.execute("INSERT INTO instruments (instrument_token, exchange, symbol, tick_size, sector, is_nse500, updated_at) "
                     "VALUES (7001, 'NSE', 'ABC', 0.05, 'Finance', 1, ?)", (ts(),))
        conn.execute("INSERT INTO candles_daily (symbol, date, open, high, low, close, volume, fetched_at) VALUES "
                     "('ABC', '2026-09-21', 99, 101, 98, 100, 5000000, ?)", (ts(),))
        conn.execute("INSERT INTO indicators_daily (symbol, date, traded_value_20d, computed_at) VALUES "
                     "('ABC', '2026-09-21', 5e9, ?)", (ts(),))
    st = strategies.create("Pullback", "intraday_pullback", {}, ["P100"])
    feed = {"today": bars((2026, 9, 22), BASE + [PULLBACK]), "calls": 0}

    def fetch(token, start, end):
        feed["calls"] += 1
        if start.date().isoformat() == "2026-09-21":
            return PREV
        return [b for b in feed["today"] if b["date"] < end]
    return {"strategy": st, "fetch": fetch, "feed": feed}


def test_scan_places_a_buy_then_the_whole_position_exits_at_the_target(clk, quotes, world):  # noqa: F811
    assert world["strategy"]["version"] == "pullback_v1"
    assert [s.version for s in intraday.strategies_on()] == ["pullback_v1"]
    clk.set(datetime(2026, 9, 22, 9, 50, 20))
    from agent.broker.base import Quote
    quotes["ABC"] = Quote(ltp=103.0, high=103.3, low=102.75)
    out = intraday.scan(fetch=world["fetch"])
    assert [(x["symbol"], x["at"], x["eligible"]) for x in out["signals"]] == [("ABC", "09:45", True)]
    t = trade("P100")
    assert (t["state"], t["sizing_case"], t["strategy_version"], t["entry_trigger"], t["stop"], t["target"]) == \
        ("BUY_PENDING", "full", "pullback_v1", 103.4, 102.6, 106.0)
    with pytest.raises(StopIteration):
        trade("P30")                                                     # P30 is not ticked for Pullback
    # the next pass: one trade per stock per day
    world["feed"]["today"] = bars((2026, 9, 22), BASE + [PULLBACK, PULLBACK])
    clk.set(datetime(2026, 9, 22, 9, 55, 20))
    assert intraday.scan(fetch=world["fetch"])["signals"] == []
    # buy triggers, exits attach as one OCO for the whole quantity, the target sells everything
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(103.5, 103.6, 102.9))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(103.6, 103.7, 102.9))
    t = trade("P100")
    assert t["state"] == "PROTECTED"
    with connect() as conn:
        legs = conn.execute("SELECT legs FROM gtts WHERE trade_id = ? AND purpose = 'exit' AND status = 'active'",
                            (t["id"],)).fetchone()[0]
    qty = t["open_qty"]
    assert f'"qty": {qty}' in legs and legs.count(f'"qty": {qty}') == 2         # stop and target legs: full quantity
    cycle(clk, datetime(2026, 9, 22, 11, 0), quotes, ABC=(106.1, 106.2, 102.9))
    cycle(clk, datetime(2026, 9, 22, 11, 1), quotes, ABC=(106.1, 106.2, 102.9))
    t = trade("P100")
    assert (t["state"], t["exit_reason"], t["open_qty"]) == ("CLOSED", "target", 0)
    # bought and sold the same day: charged as intraday on both sides
    buy_px = t["entry_price"]
    expected = charges.compute_intraday("BUY", qty, buy_px).total + charges.compute_intraday("SELL", qty, 106.0).total
    assert t["charges"] == pytest.approx(expected, abs=0.1)
    assert t["charges"] < charges.compute("BUY", qty, buy_px).total + charges.compute("SELL", qty, 106.0).total


def test_cutoff_at_1515_and_catch_up(clk, quotes, world):  # noqa: F811
    from agent.broker.base import Quote
    clk.set(datetime(2026, 9, 22, 9, 50, 20))
    quotes["ABC"] = Quote(ltp=103.0, high=103.3, low=102.75)
    intraday.scan(fetch=world["fetch"])
    assert trade("P100")["state"] == "BUY_PENDING"
    clk.set(datetime(2026, 9, 22, 12, 0))
    assert intraday.cutoff() == 0 and "intraday_cutoff" not in scheduler.missed_jobs()   # today's buy stays
    clk.set(datetime(2026, 9, 22, 15, 20))
    assert "intraday_cutoff" in scheduler.missed_jobs()
    assert scheduler.run_job("intraday_cutoff")["status"] == "ok"
    t = trade("P100")
    assert (t["state"], t["exit_reason"]) == ("CANCELLED", "intraday_cutoff")
    assert "intraday_cutoff" not in scheduler.missed_jobs()


def test_a_buy_left_from_yesterday_is_cancelled_before_the_open_session_continues(clk, quotes, world):  # noqa: F811
    from agent.broker.base import Quote
    clk.set(datetime(2026, 9, 22, 9, 50, 20))
    quotes["ABC"] = Quote(ltp=103.0, high=103.3, low=102.75)
    intraday.scan(fetch=world["fetch"])
    clk.set(datetime(2026, 9, 23, 9, 20))                               # laptop was off at 15:15 and at the EOD run
    assert "intraday_cutoff" in scheduler.missed_jobs()
    assert intraday.cutoff() == 1 and trade("P100")["exit_reason"] == "intraday_cutoff"


def test_guards(clk, world, monkeypatch):  # noqa: F811
    from agent import backtest
    from agent.learning import loop
    from agent.strategy import scanner
    scanned = []
    monkeypatch.setattr(scanner, "scan_eod", lambda s, primary=True: scanned.append(s.version) or {"date": "x"})
    scheduler.scan_all()
    assert scanned == ["momentum_v1"]                                    # the evening scan is momentum-only
    assert backtest._base("pullback_v1").params["kind"] == "intraday_pullback"          # backtests: built
    wi = {w["key"] for x in backtest.setup(__import__("agent.db", fromlist=["x"]).current_db_path())["strategies"]
          if x["version"] == "pullback_v1" for w in x["whatifs"]}
    assert {"up_min_pct", "touch_pct", "min_rr"} <= wi
    with pytest.raises(loop.LearningError, match="not built yet"):
        loop.create("on demand", world["strategy"]["id"])
    assert loop.pick_strategy()["kind"] == "momentum_breakout"
    setup = strategies.form_setup()
    keys = {f["key"] for f in setup["settings_by_kind"]["intraday_pullback"]}
    assert {"up_min_pct", "touch_pct", "top_liquid", "min_rr"} <= keys
    assert setup["defaults_by_kind"]["intraday_pullback"]["up_min_pct"] == 1.0
    edited = strategies.edit(world["strategy"]["id"], settings={"up_min_pct": 1.5, "top_liquid": 20})
    assert edited["version"] == "pullback_v1.1"
    from agent.strategy import versions
    p = versions.load("pullback_v1.1").params
    assert p["kind"] == "intraday_pullback" and p["signal"]["up_min_pct"] == 1.5 and p["universe"]["top_liquid"] == 20
    with pytest.raises(strategies.StrategyError, match="between"):
        strategies.create("Bad", "intraday_pullback", {"touch_pct": 5}, [])


def test_same_day_charges():
    booked = charges.compute("BUY", 100, 100.0)
    ch = charges.same_day_sell(100, 102.0, 100.0, 0, True)
    assert booked.total + ch.total == pytest.approx(charges.compute_intraday("BUY", 100, 100.0).total
                                                    + charges.compute_intraday("SELL", 100, 102.0).total, abs=0.01)
    assert ch.dp == 0 and charges.compute_intraday("BUY", 100000, 100.0).brokerage == 20      # capped at Rs 20
    mixed = charges.same_day_sell(40, 102.0, 100.0, 60, True)        # 60 held from before: delivery, DP charged
    assert mixed.dp == pytest.approx(15.34)


# ---------- backtest on 1-minute data ----------

def _minutes(token, day, candles, tail=None):
    """5-minute candles as 1-minute rows: the candle's range in its first minute, then flat at its close.
    tail: {"HH:MM": (o, h, l, c)} extra minutes after the candles; other minutes repeat the last close."""
    from agent.data import minute
    t0 = datetime.fromisoformat(f"{day}T09:15").replace(tzinfo=IST)
    rows, last = [], None
    for k, (o, h, lo, c, v) in enumerate(candles):
        for m in range(5):
            t = t0 + timedelta(minutes=5 * k + m)
            px = (o, h, lo, c) if m == 0 else (c, c, c, c)
            rows.append((token, int(t.timestamp()), *[round(x * 100) for x in px], v if m == 0 else 0))
        last = c
    t = t0 + timedelta(minutes=5 * len(candles))
    end = t0.replace(hour=15, minute=30)
    while t < end:
        px = (tail or {}).get(t.strftime("%H:%M"))
        if px:
            last = px[3]
        rows.append((token, int(t.timestamp()), *[round(x * 100) for x in (px or (last,) * 4)], 100))
        t += timedelta(minutes=1)
    with minute.connect() as conn:
        conn.executemany("INSERT OR REPLACE INTO candles_1m VALUES (?, ?, ?, ?, ?, ?, ?)", rows)


def test_backtest_replays_the_pullback_on_minute_data(fixed_clock):
    """22 Sep: ABC pulls back to VWAP in the 09:45 candle; the buy goes on at 09:50, triggers at 09:52 (103.40)
    and the target (the day's high, 106.00) sells everything at 10:30. Same-day: intraday charges."""
    from agent import backtest
    from agent.db import current_db_path
    from .conftest import NIFTY_TOKEN
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO instruments (instrument_token, exchange, symbol, tick_size, sector, "
                     "is_nse500, updated_at) VALUES (?, 'NSE', 'NIFTY 50', 0.05, NULL, 0, 'x')", (NIFTY_TOKEN,))
        conn.execute("INSERT INTO instruments (instrument_token, exchange, symbol, tick_size, sector, is_nse500, updated_at) "
                     "VALUES (7001, 'NSE', 'ABC', 0.05, 'Finance', 1, 'x')")
        for d, c in (("2026-09-21", 100), ("2026-09-22", 106.1)):
            conn.execute("INSERT INTO candles_daily (symbol, date, open, high, low, close, volume, fetched_at) VALUES "
                         "('ABC', ?, 100, 106.2, 99.8, ?, 5000000, 'x')", (d, c))
            conn.execute("INSERT INTO candles_daily (symbol, date, open, high, low, close, volume, fetched_at) VALUES "
                         "('NIFTY 50', ?, 20000, 20000, 20000, 20000, 1, 'x')", (d,))
        conn.execute("INSERT INTO indicators_daily (symbol, date, traded_value_20d, computed_at) VALUES "
                     "('ABC', '2026-09-21', 5e9, 'x')")
    for d in ("2026-09-21", "2026-09-22"):
        _minutes(NIFTY_TOKEN, d, [(20000, 20000, 20000, 20000, 1)])
    _minutes(7001, "2026-09-21", [(100, 100.2, 99.8, 100, 1000)] * 75)
    _minutes(7001, "2026-09-22", BASE + [(103.2, 103.3, 102.75, 103.0, 2000)],
             tail={"09:52": (103.0, 103.5, 102.95, 103.45), "10:30": (104.0, 106.2, 103.9, 106.1)})
    st = strategies.create("Pullback", "intraday_pullback", {}, [])
    cfg = backtest.validate({"start": "2026-09-22", "end": "2026-09-22", "capital": 100000,
                             "strategy": st["version"]}, current_db_path())
    assert cfg["strategy"] == "pullback_v1" and cfg["base_params"]["kind"] == "intraday_pullback"
    rid = backtest.create_run(current_db_path(), cfg)
    backtest.run(rid, current_db_path())
    r = backtest.get_run(current_db_path(), rid)
    assert r["status"] == "done", r["message"]
    rep = r["report"]
    assert rep["signals"] == {"triggers": 1, "tradeable": 1}
    [t] = rep["trades"]
    assert (t["symbol"], t["sizing_case"], t["exit_reason"], t["entry_price"], t["target"]) == \
        ("ABC", "full", "target", 103.4, 106.0)
    assert t["entry_at"].startswith("2026-09-22T09:52") and t["exits"][0]["at"].startswith("2026-09-22T10:30")
    qty = t["qty"]
    expected = charges.compute_intraday("BUY", qty, 103.4).total + charges.compute_intraday("SELL", qty, 106.0).total
    assert t["charges"] == pytest.approx(expected, abs=0.1)
    assert rep["accounts"]["Backtest"]["closed"] == 1 and rep["accounts"]["Backtest"]["net_pnl"] > 0
    # a what-if: needing a 4% rise on the day finds nothing
    cfg2 = backtest.validate({"start": "2026-09-22", "end": "2026-09-22", "capital": 100000, "strategy": "pullback_v1",
                              "overrides": {"up_min_pct": 4}}, current_db_path())
    assert cfg2["overrides"] == {"up_min_pct": 4.0}
    rid2 = backtest.create_run(current_db_path(), cfg2)
    backtest.run(rid2, current_db_path())
    assert backtest.get_run(current_db_path(), rid2)["report"]["signals"]["triggers"] == 0


def test_backtest_cancels_an_untriggered_buy_at_1515(fixed_clock):
    from agent import backtest
    from agent.db import current_db_path
    from .conftest import NIFTY_TOKEN
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO instruments (instrument_token, exchange, symbol, tick_size, sector, "
                     "is_nse500, updated_at) VALUES (?, 'NSE', 'NIFTY 50', 0.05, NULL, 0, 'x')", (NIFTY_TOKEN,))
        conn.execute("INSERT INTO instruments (instrument_token, exchange, symbol, tick_size, sector, is_nse500, updated_at) "
                     "VALUES (7001, 'NSE', 'ABC', 0.05, 'Finance', 1, 'x')")
        for d, c in (("2026-09-21", 100), ("2026-09-22", 103.5)):
            conn.execute("INSERT INTO candles_daily (symbol, date, open, high, low, close, volume, fetched_at) VALUES "
                         "('ABC', ?, 100, 106, 99.8, ?, 5000000, 'x')", (d, c))
            conn.execute("INSERT INTO candles_daily (symbol, date, open, high, low, close, volume, fetched_at) VALUES "
                         "('NIFTY 50', ?, 20000, 20000, 20000, 20000, 1, 'x')", (d,))
        conn.execute("INSERT INTO indicators_daily (symbol, date, traded_value_20d, computed_at) VALUES "
                     "('ABC', '2026-09-21', 5e9, 'x')")
    for d in ("2026-09-21", "2026-09-22"):
        _minutes(NIFTY_TOKEN, d, [(20000, 20000, 20000, 20000, 1)])
    _minutes(7001, "2026-09-21", [(100, 100.2, 99.8, 100, 1000)] * 75)
    _minutes(7001, "2026-09-22", BASE + [(103.2, 103.3, 102.75, 103.0, 2000)],
             tail={"15:20": (103.0, 103.6, 102.9, 103.5)})                      # would trigger after 15:15
    st = strategies.create("Pullback", "intraday_pullback", {}, [])
    cfg = backtest.validate({"start": "2026-09-22", "end": "2026-09-22", "capital": 100000,
                             "strategy": st["version"]}, current_db_path())
    rid = backtest.create_run(current_db_path(), cfg)
    backtest.run(rid, current_db_path())
    [t] = backtest.get_run(current_db_path(), rid)["report"]["trades"]
    assert (t["state"], t["exit_reason"], t["entry_price"]) == ("CANCELLED", "intraday_cutoff", None)
