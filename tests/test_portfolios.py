"""Portfolios: add, edit (capital only when flat), reset (close everything, keep history)."""
from __future__ import annotations

from datetime import datetime

import pytest

from agent import settings
from agent.broker.base import Quote
from agent.broker.paper import get_broker
from agent.db import connect, init_db
from agent.orders import manager, paper_edits, views
from agent.risk import portfolios

from .test_phase2 import add_candle, add_signal, clk, cycle, quotes, trade, trades  # noqa: F401 - fixtures


def _summary(acc):
    return {a["id"]: a for a in views.accounts_summary()["accounts"]}[acc]


def test_add_portfolio_and_it_trades_like_the_others(clk, quotes):  # noqa: F811
    p = portfolios.create("P200", "Two lakh", 200000, manual_edits=True)
    assert (p["capital"], p["cash"], p["peak_equity"], p["manual_edits"]) == (200000, 200000, 200000, 1)
    with pytest.raises(portfolios.PortfolioError, match="already exists"):
        portfolios.create("p200", "dup", 5000)
    with pytest.raises(portfolios.PortfolioError, match="Id:"):
        portfolios.create("2x", "bad", 5000)
    with pytest.raises(portfolios.PortfolioError, match="between"):
        portfolios.create("P1", "tiny", 10)
    add_signal()
    out = manager.place_entries("2026-09-21")
    assert out["placed"]["P200"] == 1 and trade("P200")["qty"] > trade("P100")["qty"]   # sized on ₹2 lakh
    assert _summary("P200")["pending_buys"] == 1


def test_capital_changes_only_when_flat(clk, quotes):  # noqa: F811
    add_signal()
    manager.place_entries("2026-09-21")
    with pytest.raises(portfolios.PortfolioError, match="only be changed when it holds nothing"):
        portfolios.edit("P30", capital=50000)
    out = portfolios.edit("P30", name="Thirty", manual_edits=True)          # name and switch: any time
    assert out["name"] == "Thirty" and out["manual_edits"] == 1 and _summary("P30")["capital"] == 30000
    portfolios.create("P5", "Five", 5000)
    portfolios.edit("P5", capital=8000)                                    # flat: starts again at ₹8,000
    a = _summary("P5")
    assert (a["capital"], a["cash"], a["peak"], a["return_pct"]) == (8000, 8000, 8000, 0)
    assert [h["kind"] for h in portfolios.history("P5")] == ["capital_changed", "created"]


def test_reset_closes_everything_and_restarts_keeping_history(clk, quotes):  # noqa: F811
    add_signal()                                                            # ABC: will be bought and protected
    add_signal(symbol="XYZ", sector="Energy", close=200, trigger=201, limit=202.0, stop=190, target=230, rr=2.33)
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499), XYZ=(199, 200, 198))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(504, 505, 499), XYZ=(199, 200, 198))
    assert trade("P60")["state"] == "PROTECTED" and trade("P60", "XYZ")["state"] == "BUY_PENDING"
    quotes["QQQ"] = Quote(100, 101, 99)
    add_candle("QQQ", "2026-09-21", 99, 101, 98, 100)
    with connect() as conn:
        conn.execute("INSERT INTO instruments (instrument_token, exchange, symbol, tick_size, sector, is_nse500, "
                     "updated_at) VALUES (8888, 'NSE', 'QQQ', 0.05, 'IT', 1, 'x')")
    paper_edits.buy_outside("P60", "QQQ", 10)                               # held outside the system
    manager.reconcile_now()
    before_closed = _summary("P60")
    clk.set(datetime(2026, 9, 22, 11, 0))
    quotes["ABC"] = Quote(510, 511, 499)
    out = portfolios.reset("P60", 75000)
    assert out["closed"] == {"cancelled_buys": 1, "closed": 1, "outside_sold": 1, "proceeds": pytest.approx(out["closed"]["proceeds"])}
    abc, xyz = trade("P60"), trade("P60", "XYZ")
    assert abc["state"] == "CLOSED" and abc["exit_reason"] == "reset" and abc["exits"][-1:] and abc["net_pnl"] > 0
    assert xyz["state"] == "CANCELLED" and xyz["exit_reason"] == "reset"
    b = get_broker()
    assert b.positions("P60") == [] and b.gtts("P60", "active") == [] and all(o.status != "open" for o in b.orders("P60"))
    a = _summary("P60")
    assert (a["capital"], a["cash"], a["peak"], a["closed_trades"], a["return_pct"]) == (75000, 75000, 75000, 0, 0)
    assert a["restarts"] == 1 and a["equity_curve"][-1]["equity"] == 75000 and len(a["equity_curve"]) == 1
    # history is kept, marked as before the reset; other portfolios are untouched
    closed = views.positions("P60")["closed"]
    assert {t["symbol"] for t in closed} == {"ABC", "XYZ"} and all(t["before_reset"] for t in closed)
    assert trade("P100")["state"] == "PROTECTED" and before_closed["capital"] == 60000
    manager.reconcile_now()                                                 # the reset sells are not re-read as hand sells
    assert trade("P60")["exit_reason"] == "reset"


def test_reset_with_default_capital_and_config_no_longer_pins_capital(clk, quotes):  # noqa: F811
    portfolios.edit("P100", capital=150000)
    app = settings.get_app_config()
    init_db(app, manager._strategy())                                       # config still says 100000: no error
    assert _summary("P100")["capital"] == 150000
    portfolios.reset("P100")
    assert _summary("P100")["capital"] == 150000


def test_reset_trusts_the_broker_over_the_outside_holdings_record(clk, quotes):  # noqa: F811
    with connect() as conn:   # a stale record with no shares behind it at the broker
        conn.execute("INSERT INTO unmanaged_holdings (account_id, symbol, qty, avg_price, detected_at) "
                     "VALUES ('P60', 'GHOST', 10, 100, 'x')")
    out = portfolios.reset("P60")
    assert out["closed"]["outside_sold"] == 0 and views.positions("P60")["unmanaged"] == []
