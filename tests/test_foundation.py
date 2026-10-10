"""Phase 0: config, database, calendar, controls, backups."""
from __future__ import annotations

from datetime import date, datetime

import pytest
import yaml

from agent import db, settings
from agent.clock import IST
from agent.db import SPEC_TABLES, connect
from agent.risk import controls
from agent.settings import ConfigError, load_app_config, load_strategy_config


# ---------- config ----------

def test_app_config_loads_spec_values():
    app = settings.get_app_config()
    assert app["broker"]["mode"] == "paper" and app["broker"]["live_enabled"] is False
    assert [(a["id"], a["capital"]) for a in app.accounts] == [("P30", 30000), ("P60", 60000), ("P100", 100000)]
    assert [a["id"] for a in app.accounts if a["manual_edits"]] == ["P60"]
    r = app["risk"]
    assert (r["risk_per_trade_pct"], r["max_position_pct"], r["max_open_positions"], r["max_per_sector"],
            r["drawdown_halt_pct"]) == (1.0, 25.0, 5, 2, 6.0)
    assert app["universe"]["min_traded_value_cr"] == 10
    assert "Average return on equity 5Years > 10" in app.screener_query
    assert app.holidays["2026-10-02"] == "Mahatma Gandhi Jayanti"


def test_strategy_config_has_every_section6_number():
    s = load_strategy_config("momentum_v1").params
    assert s["signal"] == {"min_change_pct": 3.0, "bb_period": 20, "bb_std": 2.0, "atr_period": 14,
                           "adx_period": 14, "adx_min": 20}
    lv = s["levels"]
    assert (lv["entry_trigger_buffer_pct"], lv["entry_limit_buffer_pct"], lv["stop_buffer_pct"],
            lv["swing_lookback"], lv["target_extension"], lv["min_rr"]) == (0.5, 0.5, 0.5, 20, 2.0, 1.5)
    assert s["entry"]["cancel_after_sessions"] == 2
    assert s["exits"] == {"split_min_qty": 4, "stop_leg_limit_pct": 2.0, "trail_buffer_pct": 0.5}


def test_live_mode_is_refused(tmp_path):
    raw = yaml.safe_load(open(settings.CONFIG_DIR / "app.yaml", encoding="utf-8"))
    raw["broker"]["mode"] = "live"
    p = tmp_path / "app.yaml"
    p.write_text(yaml.safe_dump(raw), encoding="utf-8")
    with pytest.raises(ConfigError):
        load_app_config(p)


def test_strategy_version_is_immutable(tmp_path):
    src = yaml.safe_load(open(settings.STRATEGY_DIR / "momentum_v1.yaml", encoding="utf-8"))
    src["signal"]["adx_min"] = 25   # edit in place without a new version tag
    (tmp_path / "momentum_v1.yaml").write_text(yaml.safe_dump(src), encoding="utf-8")
    edited = load_strategy_config("momentum_v1", tmp_path)
    with connect() as conn, pytest.raises(ConfigError, match="immutable"):
        db.register_strategy_version(conn, edited)


# ---------- database ----------

def test_schema_has_all_spec_tables_and_seeded_accounts():
    with connect() as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        accounts = conn.execute("SELECT id, capital, peak_equity, manual_edits, mode FROM accounts "
                                "ORDER BY capital").fetchall()
        champion = conn.execute("SELECT version, status FROM config_versions").fetchall()
    assert set(SPEC_TABLES) <= names
    assert [tuple(a) for a in accounts] == [("P30", 30000, 30000, 0, "paper"), ("P60", 60000, 60000, 1, "paper"),
                                            ("P100", 100000, 100000, 0, "paper")]
    assert [tuple(c) for c in champion] == [("momentum_v1", "champion")]


def test_init_is_idempotent():
    app = settings.get_app_config()
    strat = load_strategy_config("momentum_v1")
    db.init_db(app, strat)
    db.init_db(app, strat)
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 3


def test_backup_copies_and_prunes(tmp_path, fixed_clock):
    folder = tmp_path / "backups"
    folder.mkdir()
    old = folder / "agent_2026-01-01_183000.db"
    old.write_bytes(b"x")
    path = db.backup_db(folder, keep_days=30)
    assert path.exists() and not old.exists()
    import sqlite3
    with sqlite3.connect(path) as c:
        assert c.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 3


# ---------- calendar ----------

def test_trading_days_and_holidays(calendar):
    assert not calendar.is_trading_day(date(2026, 10, 2))        # Gandhi Jayanti (Fri)
    assert not calendar.is_trading_day(date(2026, 9, 26))        # Saturday
    assert calendar.is_trading_day(date(2026, 9, 25))
    assert calendar.next_trading_day(date(2026, 10, 1)) == date(2026, 10, 5)
    assert calendar.previous_trading_day(date(2026, 9, 15)) == date(2026, 9, 11)  # skips 14th holiday


def test_last_completed_session_respects_eod_cutoff(calendar):
    tue = date(2026, 9, 22)
    assert calendar.last_completed_session(datetime(2026, 9, 22, 20, 50, tzinfo=IST)) == date(2026, 9, 21)
    assert calendar.last_completed_session(datetime(2026, 9, 22, 20, 55, tzinfo=IST)) == tue
    assert calendar.last_completed_session(datetime(2026, 10, 3, 12, 0, tzinfo=IST)) == date(2026, 10, 1)


def test_market_open_hours(calendar):
    assert calendar.is_market_open(datetime(2026, 9, 22, 9, 15, tzinfo=IST))
    assert not calendar.is_market_open(datetime(2026, 9, 22, 15, 30, tzinfo=IST))
    assert not calendar.is_market_open(datetime(2026, 9, 14, 11, 0, tzinfo=IST))  # holiday


# ---------- controls ----------

def test_kill_switch_blocks_entries_until_rearmed(fixed_clock):
    assert controls.entries_blocked("P30") is None
    controls.activate_kill_switch("test")
    assert controls.entries_blocked("P30") == "Kill switch is on"
    controls.rearm_kill_switch()
    assert controls.entries_blocked("P30") is None


def test_account_halt_and_rearm(fixed_clock):
    controls.halt_account("P60", "6% drawdown")
    assert "halted" in controls.entries_blocked("P60")
    assert controls.entries_blocked("P30") is None
    assert controls.rearm_account("P60")
    assert not controls.rearm_account("P60")
    assert controls.entries_blocked("P60") is None
