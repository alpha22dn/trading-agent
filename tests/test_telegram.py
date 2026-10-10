"""Telegram: critical/error alerts and the EOD summary; the token never leaks into logs."""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from agent import audit, settings
from agent.notify import telegram
from agent.orders import manager, views

from .test_phase2 import add_signal, clk, cycle, quotes  # noqa: F401 - fixtures


@pytest.fixture
def bot(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:SECRET")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    settings.get_secrets.cache_clear()
    telegram._recent.clear()
    from agent.db import current_db_path
    monkeypatch.setattr(telegram, "_live_db", [current_db_path()])
    sent = []

    class Resp:
        status_code = 200

    monkeypatch.setattr(telegram.requests, "post", lambda url, **k: sent.append((url, k["json"])) or Resp())
    return sent


def _wait(sent, n):
    for _ in range(100):
        if len(sent) >= n:
            return
        time.sleep(0.02)


def test_only_critical_and_error_alerts_go_out_once(bot):
    audit.alert("order_monitor", "no quotes", level="warning")
    audit.alert("exit_attach", "P60 ABC: no stop 7 min after the fill.", level="critical")
    audit.alert("exit_attach", "P60 ABC: no stop 7 min after the fill.", level="critical")   # duplicate
    _wait(bot, 1)
    time.sleep(0.1)
    assert len(bot) == 1 and bot[0][1]["chat_id"] == "42" and "no stop" in bot[0][1]["text"]
    assert bot[0][0] == "https://api.telegram.org/bot123:SECRET/sendMessage"


def test_errors_never_show_the_token(monkeypatch, bot):
    def boom(url, **k):
        raise telegram.requests.ConnectionError(f"failed to reach {url}")
    monkeypatch.setattr(telegram.requests, "post", boom)
    with pytest.raises(RuntimeError) as e:
        telegram.send_now("hi")
    assert "SECRET" not in str(e.value)


def test_not_configured_is_silent(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    settings.get_secrets.cache_clear()
    assert telegram.send("x") is False and telegram.eod_summary("2026-09-22") is False


def test_eod_summary(clk, quotes, bot):  # noqa: F811
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    clk.set(datetime(2026, 9, 22, 21, 0))
    manager.eod_maintenance("2026-09-22")
    text = telegram.eod_text("2026-09-22")
    assert text.startswith("📊 EOD 2026-09-22") and "P100: ₹" in text and "1 open" in text
    assert telegram.eod_summary("2026-09-22") is True and telegram.eod_summary("2026-09-22") is False   # once a day
    assert views.accounts_summary()["accounts"]


def test_alerts_from_backtest_copies_are_not_sent(monkeypatch, tmp_path):
    from agent import db
    from agent.notify import telegram
    sent = []
    monkeypatch.setattr(telegram, "send", lambda text: sent.append(text) or True)
    telegram.on_alert("x", "not bound: a worker process", "critical")
    monkeypatch.setattr(telegram, "_live_db", [db.current_db_path()])
    telegram.on_alert("x", "live", "critical")
    prev = db.current_db_path()
    db.set_db_path(tmp_path / "bt_1.db")
    try:
        telegram.on_alert("x", "from a backtest", "critical")
    finally:
        db.set_db_path(prev)
    assert [("live" in t) for t in sent] == [True]
