"""Strategy versions: champion in the database, per-portfolio versions, per-trade rules, promote/rollback."""
from __future__ import annotations

import copy
import json
from datetime import datetime

from agent.db import connect
from agent.orders import manager
from agent.risk import portfolios
from agent.settings import STRATEGY_DIR
from agent.strategy import strategies, versions

from .test_phase2 import add_candle, add_signal, clk, cycle, quotes, trade  # noqa: F401 - fixtures


def _challenger(**exits):
    params = copy.deepcopy(versions.champion().params)
    params["exits"].update(exits)
    v = versions.next_version()
    versions.register(v, params)
    return v


def test_next_version_and_register():
    assert versions.champion_version() == "momentum_v1"
    v = _challenger(trail_buffer_pct=1.5)
    assert v == "momentum_v1.1" and versions.next_version() == "momentum_v1.2"
    assert versions.load(v).params["exits"]["trail_buffer_pct"] == 1.5 and versions.load(v).params["version"] == v


def test_each_portfolio_trades_its_own_version(clk, quotes):  # noqa: F811
    v = _challenger(trail_buffer_pct=2.0)
    portfolios.create("CH1", "Challenger", 100000)
    with connect() as conn:
        conn.execute("UPDATE accounts SET strategy_version = ? WHERE id = 'CH1'", (v,))
    sid = add_signal()                                         # the champion's signal (momentum_v1)
    with connect() as conn:                                    # same stock, the challenger's own signal row
        conn.execute("INSERT INTO signals (date, symbol, strategy_version, entry_trigger, entry_limit, stop, target, "
                     "rr, rr_band, indicators, eligible, source, created_at) SELECT date, symbol, ?, entry_trigger, "
                     "entry_limit, stop, target, rr, rr_band, indicators, eligible, source, created_at FROM signals "
                     "WHERE id = ?", (v, sid))
    out = manager.place_entries("2026-09-21")
    assert out["placed"] == {"P30": 1, "P60": 1, "P100": 1, "CH1": 1}
    assert trade("CH1")["strategy_version"] == v and trade("P100")["strategy_version"] == "momentum_v1"
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))     # bought
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(504, 505, 499))     # exits attached
    cycle(clk, datetime(2026, 9, 22, 11, 0), quotes, ABC=(561, 562, 499))     # target leg fills
    cycle(clk, datetime(2026, 9, 22, 11, 1), quotes, ABC=(561, 562, 499))     # stop on the rest
    assert trade("P100")["state"] == "TRAILING" and trade("CH1")["state"] == "TRAILING"
    add_candle("ABC", "2026-09-23", 561, 580, 570, 578)
    clk.set(datetime(2026, 9, 23, 21, 0))
    manager.trail_stops("2026-09-23", manager._strategy())
    assert trade("P100")["stop"] == 567.15       # 570 × (1 − 0.5%), rounded down to the tick
    assert trade("CH1")["stop"] == 558.6         # 570 × (1 − 2%): the challenger's own trail buffer


def test_promote_writes_yaml_and_rollback_restores(tmp_path, monkeypatch):
    monkeypatch.setattr(strategies, "STRATEGY_DIR", tmp_path)
    v = _challenger(trail_buffer_pct=1.0)
    versions.promote(v)
    assert versions.champion_version() == v and (tmp_path / f"{v}.yaml").exists()
    st = {r["version"]: r["status"] for r in versions.listing()}
    assert st == {"momentum_v1": "retired", v: "champion"}
    versions.rollback("momentum_v1")
    assert versions.champion_version() == "momentum_v1"
    assert {r["version"]: r["status"] for r in versions.listing()} == {"momentum_v1": "champion", v: "retired"}
    assert STRATEGY_DIR != tmp_path
