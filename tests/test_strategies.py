"""Strategies screen: add / edit / on-off / delete, portfolio checkboxes, one combined ranking per portfolio."""
from __future__ import annotations

from datetime import datetime

import pytest

from agent.db import connect
from agent.learning import loop
from agent.orders import manager
from agent.strategy import strategies, versions

from .test_phase2 import add_signal, clk, cycle, quotes, trade, trades  # noqa: F401 - fixtures


def _signal_for(version: str, **kw) -> int:
    sid = add_signal(**kw)
    with connect() as conn:
        conn.execute("UPDATE signals SET strategy_version = ? WHERE id = ?", (version, sid))
    return sid


def _tight(**kw):
    return strategies.create("Tight stop", "momentum_breakout", kw.pop("settings", {"stop_buffer_pct": 1.0}),
                             kw.pop("portfolios", ["P60", "P100"]), **kw)


def test_first_run_seeds_the_existing_setup():
    [s] = strategies.listing()
    assert (s["name"], s["version"], s["enabled"], s["portfolios"]) == ("Momentum v1", "momentum_v1", 1, ["P30", "P60", "P100"])
    assert strategies.versions_for_account("P60") == ["momentum_v1"]
    assert versions.champion_version() == "momentum_v1"


def test_create_validates_and_writes_its_version():
    t = _tight()
    assert t["version"] == "tight_stop_v1"
    p = versions.load("tight_stop_v1").params
    assert p["levels"]["stop_buffer_pct"] == 1.0 and p["levels"]["target_extension"] == 2.0      # the rest copied
    assert (strategies.STRATEGY_DIR / "tight_stop_v1.yaml").exists()
    assert strategies.versions_for_account("P100") == ["momentum_v1", "tight_stop_v1"]
    assert strategies.versions_for_account("P30") == ["momentum_v1"]
    assert versions.trading_versions() == ["momentum_v1", "tight_stop_v1"]
    for bad, msg in [({"stop_buffer_pct": 9}, "between"), ({"min_rr": "x"}, "number")]:
        with pytest.raises(strategies.StrategyError, match=msg):
            strategies.create("Bad", "momentum_breakout", bad, [])
    with pytest.raises(strategies.StrategyError, match="already exists"):
        strategies.create("tight STOP", "momentum_breakout", {}, [])
    with pytest.raises(strategies.StrategyError, match="Unknown rule type"):
        strategies.create("Pullback", "pullback", {}, [])
    low = strategies.create("Low RR", "momentum_breakout", {"min_rr": 1.2}, [])
    assert {"band": "D", "min": 1.2} in versions.load(low["version"]).params["ranking"]["rr_bands"]


def test_one_ranking_per_portfolio_and_a_stock_bought_once(clk, quotes):  # noqa: F811
    _tight()
    _signal_for("tight_stop_v1")                                           # tight: ABC
    add_signal()                                                           # momentum: ABC too
    _signal_for("tight_stop_v1", symbol="XYZ", sector="Energy", close=100, trigger=100.5, limit=101.0, stop=96.0,
                target=110.0, rr=2.2)
    out = manager.place_entries("2026-09-21")
    assert out["placed"] == {"P30": 1, "P60": 2, "P100": 2}
    assert {(t["symbol"], t["strategy_version"]) for t in trades("P100")} == {("ABC", "momentum_v1"), ("XYZ", "tight_stop_v1")}
    assert [t["symbol"] for t in trades("P30")] == ["ABC"]                  # P30 is not ticked for Tight stop
    with connect() as conn:
        why = conn.execute("SELECT c.skip_reason FROM candidates c JOIN signals s ON s.id = c.signal_id WHERE "
                           "c.account_id = 'P100' AND s.symbol = 'ABC' AND s.strategy_version = 'tight_stop_v1'").fetchone()[0]
    assert "bought once" in why


def test_off_untick_and_delete_cancel_only_pending_buys(clk, quotes):  # noqa: F811
    t = _tight()
    _signal_for("tight_stop_v1")
    _signal_for("tight_stop_v1", symbol="XYZ", sector="Energy", close=100, trigger=100.5, limit=101.0, stop=96.0,
                target=110.0, rr=2.2)
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499), XYZ=(99, 99.5, 98))   # ABC bought, XYZ pending
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(504, 505, 499), XYZ=(99, 99.5, 98))
    assert trade("P100")["state"] == "PROTECTED" and trade("P100", "XYZ")["state"] == "BUY_PENDING"
    r = strategies.set_portfolios(t["id"], ["P100"])                       # untick P60
    assert r["removed"] == ["P60"] and r["cancelled_buys"] == 1
    assert trade("P60", "XYZ")["state"] == "CANCELLED" and trade("P60", "XYZ")["exit_reason"] == "strategy_off"
    assert trade("P60")["state"] == "PROTECTED"                            # the open trade runs on
    assert strategies.set_enabled(t["id"], False)["cancelled_buys"] == 1
    assert trade("P100", "XYZ")["state"] == "CANCELLED" and trade("P100")["state"] == "PROTECTED"
    assert strategies.versions_for_account("P100") == ["momentum_v1"]
    cycle(clk, datetime(2026, 9, 22, 11, 0), quotes, ABC=(561, 562, 499), XYZ=(99, 99.5, 98))     # still managed
    assert trade("P100")["target_hit_on"] or trade("P100")["state"] in ("TRAILING", "PROTECTED")
    strategies.delete(t["id"])
    assert [s["name"] for s in strategies.listing()] == ["Momentum v1"]
    assert trade("P100")["strategy_version"] == "tight_stop_v1"            # history kept


def test_edit_creates_a_new_version_and_open_trades_keep_theirs(clk, quotes):  # noqa: F811
    t = _tight()
    _signal_for("tight_stop_v1")
    manager.place_entries("2026-09-21")
    out = strategies.edit(t["id"], name="Tight stop 1%", settings={"stop_buffer_pct": 1.0, "trail_buffer_pct": 1.5})
    assert out["version"] == "tight_stop_v1.1" and out["name"] == "Tight stop 1%"
    assert any("trail_buffer_pct 0.5 → 1.5" in c for c in out["changes"])
    st = {r["version"]: r["status"] for r in versions.listing()}
    assert st["tight_stop_v1"] == "retired" and st["tight_stop_v1.1"] == "champion"
    assert trade("P100")["strategy_version"] == "tight_stop_v1"            # the pending buy keeps its version
    assert strategies.edit(t["id"], settings={"stop_buffer_pct": 1.0, "trail_buffer_pct": 1.5})["changes"] == []
    assert strategies.versions_of(t["id"]) == ["tight_stop_v1", "tight_stop_v1.1"]


def test_challenger_portfolio_cannot_be_ticked_and_new_portfolios_get_on_strategies():
    from agent.risk import portfolios
    t = _tight()
    strategies.set_enabled(t["id"], False)
    portfolios.create("P200", "Two lakh", 200000)
    assert strategies.versions_for_account("P200") == ["momentum_v1"]      # every ON strategy, not the OFF one
    portfolios.create("P300", "Three", 300000, strategy_ids=[t["id"]])
    assert strategies.portfolios_of(t["id"]) == ["P60", "P100", "P300"]
    portfolios.create("CH9", "Challenger", 100000, strategy_ids=[])
    with connect() as conn:
        conn.execute("UPDATE accounts SET strategy_version = 'momentum_v1' WHERE id = 'CH9'")
    with pytest.raises(strategies.StrategyError, match="challenger"):
        strategies.set_portfolios(t["id"], ["CH9"])


def test_learning_takes_strategies_in_turn(clk):  # noqa: F811
    t = _tight()
    with connect() as conn:                                                # Tight stop has more closed trades
        for i in range(3):
            conn.execute("INSERT INTO trades (account_id, symbol, strategy_version, state, qty, open_qty, entry_price, "
                         "exits, charges, created_at, updated_at, closed_at) VALUES ('P100', ?, 'tight_stop_v1', 'CLOSED', "
                         "1, 0, 100, '[]', 0, 'x', 'x', '2026-09-20')", (f"S{i}",))
    assert loop.pick_strategy()["id"] == t["id"]
    pid = loop.create("weekly")
    assert loop._row(pid)["base_version"] == "tight_stop_v1" and loop._row(pid)["strategy_id"] == t["id"]
    loop.mark_interrupted()
    clk.set(datetime(2026, 9, 26, 10, 0))
    assert loop.pick_strategy()["name"] == "Momentum v1"                   # Tight stop was just reviewed
    with pytest.raises(strategies.StrategyError, match="Learning proposal"):
        pid2 = loop.create("on demand", t["id"])
        strategies.delete(t["id"])
    loop.mark_interrupted()
    assert loop._row(pid2)["strategy_id"] == t["id"]


def test_api(clk, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    import agent.api.app as app_module
    from agent.api.app import create_app
    rescans = []
    monkeypatch.setattr(app_module, "_background", lambda name, fn, *args: rescans.append(args))
    with TestClient(create_app(start_scheduler=False)) as c:
        v = c.get("/api/strategies").json()
        assert [s["name"] for s in v["strategies"]] == ["Momentum v1"] and v["kinds"][0]["kind"] == "momentum_breakout"
        assert {s["key"] for s in v["settings"]} >= {"stop_buffer_pct", "min_change_pct", "adx_min"}
        assert c.post("/api/strategies", json={"name": "X"}).status_code == 400                       # confirm
        r = c.post("/api/strategies", json={"confirm": True, "name": "Wide target", "settings": {"target_extension": 3},
                                             "portfolios": ["P100"]})
        assert r.status_code == 200 and rescans                                                       # it trades now: re-scan
        sid = r.json()["id"]
        assert c.post("/api/strategies", json={"confirm": True, "name": "Bad", "settings": {"min_rr": 99}}).status_code == 400
        assert c.post(f"/api/strategies/{sid}/toggle", json={"confirm": True, "enabled": False}).json()["enabled"] == 0
        assert c.post(f"/api/strategies/{sid}/portfolios", json={"confirm": True, "portfolios": ["P30", "P100"]}).json()["added"] == ["P30"]
        assert c.post(f"/api/strategies/{sid}/edit", json={"confirm": True, "settings": {"target_extension": 2.5}}).json()["version"] == "wide_target_v1.1"
        assert c.post(f"/api/strategies/{sid}/delete", json={"confirm": True}).json()["deleted"] == sid
        assert c.post(f"/api/strategies/{sid}/toggle", json={"confirm": True, "enabled": True}).status_code == 400
        lv = c.get("/api/learning").json()
        assert [s["name"] for s in lv["strategies"]] == ["Momentum v1"] and lv["next_pick"] == 1
