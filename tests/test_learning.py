"""Learning loop: evidence rules, the walk-forward verdict, and proposal → challenger → approve / reject / rollback.
Backtests are replaced by fixed results here (the backtest engine has its own tests)."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from agent import settings
from agent.db import connect, current_db_path
from agent.learning import evidence as ev, loop
from agent.strategy import strategies, versions

from .test_phase2 import add_candle, clk  # noqa: F401 - fixtures


@pytest.fixture(autouse=True)
def template_llm(monkeypatch, tmp_path):
    monkeypatch.setenv("LLM_PROVIDER", "template")
    settings.get_secrets.cache_clear()
    monkeypatch.setattr(strategies, "STRATEGY_DIR", tmp_path)   # promote() writes YAML here, not into config/


def _closed(i, symbol="ABC", reason="stop", r=-1.0, band="B", target_hit=False, day="2026-06-01", exit_day="2026-06-03"):
    return {"symbol": symbol, "state": "CLOSED", "entry_price": 100.0, "initial_stop": 95.0, "target": 110.0,
            "entry_at": f"{day}T10:00:00", "closed_at": f"{exit_day}T11:00:00", "exit_reason": reason, "rr_band": band,
            "r_multiple": r, "target_hit_on": exit_day if target_hit else None,
            "exits": [{"at": f"{exit_day}T11:00:00", "reason": reason}], "id": i}


def _stopped_then_ran():
    """6 stop-outs on ABC; afterwards the price runs to 111, above the 110 target."""
    add_candle("ABC", "2026-06-01", 100, 103, 99, 101)
    add_candle("ABC", "2026-06-02", 101, 102, 97, 98)
    add_candle("ABC", "2026-06-03", 98, 98, 94, 95)
    add_candle("ABC", "2026-06-04", 96, 104, 95, 103)
    add_candle("ABC", "2026-06-05", 103, 111, 102, 110)
    return [_closed(i) for i in range(6)]


def test_evidence_picks_the_strongest_rule():
    f = ev.facts(_stopped_then_ran())
    assert len(f["closed"]) == 6 and f["closed"][0]["after_high"] == 111 and f["closed"][0]["to_target"] == 0.3
    c = ev.best(f, versions.champion().params)
    assert c["rule"] == "stop_too_tight" and c["evidence"] == "strong"
    assert c["key"] == "stop_buffer_pct" and c["to"] == pytest.approx(c["from"] + 0.5)
    assert "6 of 6 stop-outs" in c["text"]


def test_evidence_weak_band_and_nothing_to_learn():
    params = versions.champion().params
    assert ev.best({"closed": [], "unfilled": 0}, params) is None
    closed = [{"symbol": "X", "band": "C", "exit_reason": "stop", "target_reached": False, "r": -1.0, "entry": 100,
               "target": 110, "risk": 5, "mfe_r": 0.2, "to_target": 0.1, "after_high": 100} for _ in range(10)]
    rules = {c["rule"]: c for c in ev.candidates({"closed": closed, "unfilled": 0}, params)}
    assert rules["weak_band"]["met"] and rules["weak_band"]["to"] == 2.0 and rules["weak_band"]["numbers"]["avg_r"] == -1.0


def _fold(ch_pnl, cp_pnl, ch_r=0.5, cp_r=0.2, n=10):
    side = lambda pnl, r: {"return_pct": 1, "max_drawdown_pct": 1, "closed": n, "win_pct": 50, "avg_r": r,  # noqa: E731
                           "net_pnl": pnl, "charges": 10, "run_id": 1, "rs": [r] * n}
    return {"start": "a", "end": "b", "challenger": side(ch_pnl, ch_r), "champion": side(cp_pnl, cp_r)}


def test_judge():
    ok = loop.judge([_fold(500, 100), _fold(300, 400), _fold(200, 100), _fold(100, 50)])
    assert ok["passed"] and ok["periods_better"] == 3 and ok["challenger"]["closed"] == 40
    assert "rs" not in ok["folds"][0]["challenger"]
    few = loop.judge([_fold(500, 100, n=5)] * 4)
    assert not few["passed"] and "only 20 challenger trades" in few["reasons"][0]
    half = loop.judge([_fold(100, 500), _fold(100, 500), _fold(1900, 100), _fold(100, 50)])
    assert half["periods_better"] == 2 and half["passed"]                  # half the periods is enough
    once = loop.judge([_fold(100, 500), _fold(100, 500), _fold(100, 500), _fold(1900, 100)])
    assert not once["passed"] and any("better in 1 of 4" in r for r in once["reasons"])
    assert any("not above" in r for r in loop.judge([_fold(100, 500)] * 4)["reasons"])


@pytest.fixture
def fake_backtests(monkeypatch):
    calls = []

    def bt(live_db, start, end, overrides, pid, label):
        calls.append((start, end, overrides, label))
        if label == "baseline":
            return {"run_id": 1, "account": {"closed": 6}, "trades": _stopped_then_ran(), "sessions": 100}
        better = bool(overrides)
        n = 10
        r = 0.4 if better else 0.1
        return {"run_id": len(calls), "sessions": 60,
                "account": {"return_pct": 2.0 if better else 1.0, "max_drawdown_pct": 3.0, "closed": n, "win_pct": 50.0,
                            "avg_r": r, "net_pnl": 2000.0 if better else 1000.0, "charges": 100.0},
                "trades": [{"r_multiple": r}] * n}
    monkeypatch.setattr(loop, "_backtest", bt)
    monkeypatch.setattr(loop, "_coverage", lambda live_db: (date(2025, 9, 1), date(2026, 9, 25)))
    return calls


def test_proposal_to_challenger_then_approve(clk, fake_backtests):  # noqa: F811
    clk.set(datetime(2026, 9, 26, 10, 0))                     # Saturday
    pid = loop.create("weekly")
    with pytest.raises(loop.LearningError):
        loop.create("on demand")                              # one at a time
    loop.analyse(pid, current_db_path())
    p = loop._row(pid)
    assert p["status"] == "challenger", p["message"]
    assert (p["param"], p["from_value"]) == ("stop_buffer_pct", versions.champion().params["levels"]["stop_buffer_pct"])
    assert p["evidence"]["source"]["kind"] == "live + baseline backtest" and p["evidence"]["rule"] == "stop_too_tight"
    assert p["validation"]["passed"] and len(p["validation"]["folds"]) == loop.FOLDS
    assert [c[3] for c in fake_backtests][0] == "baseline" and len(fake_backtests) == 1 + 2 * loop.FOLDS
    assert fake_backtests[-1][2] == {"stop_buffer_pct": p["to_value"]}
    assert p["version"] == "momentum_v1.1" and p["challenger_account"] == f"CH{pid}"
    assert p["challenger_started"] == "2026-09-28" and p["explanation"].startswith("Stop below signal low (%)")
    assert versions.load("momentum_v1.1").params["levels"]["stop_buffer_pct"] == p["to_value"]
    with connect() as conn:
        acc = dict(conn.execute("SELECT * FROM accounts WHERE id = ?", (f"CH{pid}",)).fetchone())
    assert acc["capital"] == 100000 and acc["strategy_version"] == "momentum_v1.1"
    assert set(versions.trading_versions()) == {"momentum_v1", "momentum_v1.1"}

    clk.set(datetime(2026, 10, 2, 21, 0))                     # 28 Sep - 1 Oct (2 Oct is a market holiday)
    assert loop.update_challenger() == f"challenger at 4 of {loop.CHALLENGER_SESSIONS} sessions"
    clk.set(datetime(2026, 10, 20, 21, 0))
    assert loop.update_challenger() == "ready for decision" and loop._row(pid)["status"] == "decision"
    assert loop.listing()[0]["comparison"]["sessions"] >= loop.CHALLENGER_SESSIONS

    loop.approve(pid)
    assert versions.champion_version() == "momentum_v1.1" and loop._row(pid)["status"] == "approved"
    assert (strategies.STRATEGY_DIR / "momentum_v1.1.yaml").exists()
    with connect() as conn:
        assert conn.execute("SELECT archived FROM accounts WHERE id = ?", (f"CH{pid}",)).fetchone()[0] == 1
    assert loop.active() is None
    versions.rollback("momentum_v1")                          # one click back
    assert versions.champion_version() == "momentum_v1"


def test_failed_validation_and_reject(clk, fake_backtests, monkeypatch):  # noqa: F811
    monkeypatch.setattr(loop, "judge", lambda folds: {"folds": folds, "passed": False, "reasons": ["net P&L lower"]})
    pid = loop.create("on demand")
    loop.analyse(pid, current_db_path())
    p = loop._row(pid)
    assert p["status"] == "failed" and "net P&L lower" in p["message"] and not p["challenger_account"]
    assert loop.active() is None                              # a new proposal may start
    loop.reject(pid, "noted")
    assert loop._row(pid)["status"] == "rejected"
    with pytest.raises(loop.LearningError):
        loop.approve(pid)


def test_reject_challenger_archives_it(clk, fake_backtests):  # noqa: F811
    clk.set(datetime(2026, 9, 26, 10, 0))
    pid = loop.create("weekly")
    loop.analyse(pid, current_db_path())
    loop.reject(pid)
    with connect() as conn:
        assert conn.execute("SELECT archived FROM accounts WHERE id = ?", (f"CH{pid}",)).fetchone()[0] == 1
        assert conn.execute("SELECT status FROM config_versions WHERE version = 'momentum_v1.1'").fetchone()[0] == "retired"
    assert versions.trading_versions() == ["momentum_v1"] and versions.champion_version() == "momentum_v1"


def test_interrupted_analysis_is_marked():
    pid = loop.create("weekly")
    loop.mark_interrupted()
    assert loop._row(pid)["status"] == "error" and loop.active() is None


def test_api_endpoints(clk, fake_backtests, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient

    import agent.api.app as app_module
    from agent.api.app import create_app
    monkeypatch.setattr(app_module, "_background", lambda name, fn, *args: None)
    launched = []
    monkeypatch.setattr(loop, "launch", lambda pid: launched.append(pid))
    clk.set(datetime(2026, 9, 26, 10, 0))
    with TestClient(create_app(start_scheduler=False)) as c:
        assert c.post("/api/learning/propose", json={}).status_code == 400          # needs confirm
        pid = c.post("/api/learning/propose", json={"confirm": True}).json()["id"]
        assert launched == [pid]
        assert c.post("/api/learning/propose", json={"confirm": True}).status_code == 409
        loop.analyse(pid, current_db_path())                                        # what the worker does
        v = c.get("/api/learning").json()
        assert v["active"] == pid and v["proposals"][0]["status"] == "challenger" and v["champion"] == "momentum_v1"
        assert v["proposals"][0]["comparison"]["portfolios"][f"CH{pid}"]["challenger"]
        assert [x["version"] for x in v["versions"]] == ["momentum_v1", "momentum_v1.1"]
        assert c.post(f"/api/learning/{pid}/approve", json={"confirm": True}).json()["status"] == "approved"
        assert c.get("/api/learning").json()["champion"] == "momentum_v1.1"
        assert c.post("/api/learning/rollback", json={"confirm": True, "version": "momentum_v1.1"}).status_code == 400
        assert c.post("/api/learning/rollback", json={"confirm": True, "version": "momentum_v1"}).json() == \
            {"champion": "momentum_v1"}
        j = c.get("/api/journal").json()
        assert j["entries"] == [] and j["llm"]["provider"] == "template"
        csv = c.get("/api/journal/export.csv")
        assert csv.status_code == 200 and csv.text.lstrip("﻿").startswith("trade_id,account_id,symbol")
        assert c.get("/api/journal/weekly").json() == {"reviews": []}
        i = c.get("/api/integrations").json()
        assert i["llm"]["provider"] == "template" and "configured" in i["telegram"]
        assert c.post("/api/llm/test", json={"confirm": True}).status_code == 400
