"""Journal: per-trade records (code computes), explanations (LLM or template), retries, CSV."""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from agent import settings
from agent.db import connect
from agent.journal import llm, trades as journal
from agent.orders import manager

from .test_phase2 import add_candle, add_signal, clk, cycle, quotes, trade  # noqa: F401 - fixtures


def _stopped_trade(clk, quotes):  # noqa: F811
    add_signal()
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499))
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(510, 512, 499))
    add_candle("ABC", "2026-09-22", 503, 512, 499, 505)
    cycle(clk, datetime(2026, 9, 23, 11, 0), quotes, ABC=(480, 512, 479))     # stop 482.55 hit
    add_candle("ABC", "2026-09-23", 505, 506, 479, 481)
    assert trade("P100")["state"] == "CLOSED"


@pytest.fixture
def template_llm(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "template")
    settings.get_secrets.cache_clear()


def test_record_and_template(clk, quotes, template_llm):  # noqa: F811
    _stopped_trade(clk, quotes)
    out = journal.sync()
    assert out == {"journaled": 3, "explained": 0, "llm_failed": 0}               # one per portfolio
    [e] = journal.entries(account="P100")
    m = e["metrics"]
    assert (m["symbol"], m["exit_reason"], m["rr_band"], m["qty"], m["holding_days"]) == ("ABC", "stop", "B", 44, 1)
    assert m["entry"]["price"] == 503 and m["excursion"]["source"] == "daily"   # quote without an open: fill at LTP
    assert m["excursion"]["mfe"] == pytest.approx(512 - 503) and m["excursion"]["mae"] == pytest.approx(503 - 479)
    assert m["excursion"]["mfe_r"] == pytest.approx((512 - 503) / (503 - 482.55), abs=0.01)
    assert e["narrative_source"] == "template" and "stop-loss after 1 session" in e["narrative"]
    assert journal.sync()["journaled"] == 0                                      # not journaled twice
    rows = journal.csv_rows(account="P100")
    assert len(rows) == 1 and rows[0][journal.CSV_COLUMNS.index("exit_reason")] == "stop"
    assert journal.entries(result="win") == [] and len(journal.entries(result="loss")) == 3


def test_claude_writes_the_explanation(clk, quotes, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-key")
    settings.get_secrets.cache_clear()
    sent = []

    class Resp:
        status_code = 200

        def json(self):
            return {"model": "claude-sonnet-5-5", "content": [{"type": "text", "text": "A clean breakout that failed."}]}

    monkeypatch.setattr(llm.requests, "post", lambda url, **k: sent.append((url, k)) or Resp())
    _stopped_trade(clk, quotes)
    assert journal.sync() == {"journaled": 3, "explained": 3, "llm_failed": 0}
    url, k = sent[0]
    assert url == "https://api.anthropic.com/v1/messages" and k["json"]["model"] == "claude-sonnet-5-5"
    assert k["headers"]["x-api-key"] == "sk-test-key" and k["headers"]["anthropic-version"] == "2023-06-01"
    assert '"symbol": "ABC"' in k["json"]["messages"][0]["content"]                # the computed numbers go in
    [e] = journal.entries(account="P60")
    assert e["narrative"] == "A clean breakout that failed." and e["narrative_source"] == "llm"
    with connect() as conn:                                                      # the key is never stored
        assert not conn.execute("SELECT 1 FROM audit_log WHERE message LIKE '%sk-test%' OR details LIKE '%sk-test%'").fetchone()


def test_llm_down_keeps_template_and_retries(clk, quotes, monkeypatch):  # noqa: F811
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    settings.get_secrets.cache_clear()

    def down(*a, **k):
        raise llm.requests.ConnectionError("no network")
    monkeypatch.setattr(llm.requests, "post", down)
    _stopped_trade(clk, quotes)
    out = journal.sync()
    assert out["journaled"] == 3 and out["explained"] == 0 and out["llm_failed"] == 1
    e = journal.entries(account="P30")[0]
    assert e["narrative_source"] == "template" and e["narrative"] and "not reachable" in (
        journal.entries()[-1]["last_error"] or e["last_error"] or "not reachable")
    monkeypatch.setattr(llm.requests, "post", lambda url, **k: type("R", (), {
        "status_code": 200, "json": lambda self: {"content": [{"type": "text", "text": "Later."}]}})())
    assert journal.sync()["explained"] == 3                                     # retried at the next run
    assert {e["narrative"] for e in journal.entries()} == {"Later."}
