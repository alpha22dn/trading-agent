"""NSE500 list downloaded from NSE every trading day (decided 2026-10-01): applied when it differs, kept on a failed
download or an implausibly large change, and a stock that leaves loses its pending buys but keeps its position."""
from __future__ import annotations

from datetime import datetime

from agent import audit
from agent.data import instruments, nse500
from agent.db import connect, get_state
from agent.orders import manager

from .conftest import nse500_csv
from .test_phase2 import add_signal, clk, cycle, quotes, trade  # noqa: F401 - fixtures


def _serve(monkeypatch, *lists):
    """Fake NSE: each call returns the next CSV (or raises it if it's an exception)."""
    calls = []
    queue = list(lists)

    def fake(urls, timeout):
        calls.append(list(urls))
        item = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(item, Exception):
            raise item
        return nse500_csv(item).encode(), urls[0]
    monkeypatch.setattr(nse500, "downloader", fake)
    return calls


def members():
    with connect() as conn:
        return dict(conn.execute("SELECT symbol, sector FROM instruments WHERE is_nse500 = 1").fetchall())


def test_daily_download_applies_changes_only(logged_in, monkeypatch):
    instruments.sync_instruments()
    calls = _serve(monkeypatch, [("RELIANCE", "EQ", "Energy"), ("TCS", "EQ", "IT")],
                   [("RELIANCE", "EQ", "Energy"), ("TCS", "EQ", "IT")],
                   [("RELIANCE", "EQ", "Energy"), ("INFY", "EQ", "IT")],
                   [("RELIANCE", "EQ", "Oil & Gas"), ("INFY", "EQ", "IT")])
    r = nse500.auto_update()
    assert r["status"] == "updated" and members() == {"RELIANCE": "Energy", "TCS": "IT"}
    assert "nsearchives.nseindia.com" in calls[0][0]                         # NSE's archive first
    assert get_state("nse500_uploaded")["source"] == "nse"
    assert nse500.auto_update()["status"] == "unchanged"
    r = nse500.auto_update()
    assert r["status"] == "updated" and r["added"] == ["INFY"] and r["removed"] == ["TCS"]
    alerts = [a["message"] for a in audit.active_alerts() if a["name"] == "nse500_change"]
    assert any("joined" in m and "INFY" in m for m in alerts) and any("left" in m and "TCS" in m for m in alerts)
    r = nse500.auto_update()                                                 # a sector reclassified: applied too
    assert r["status"] == "updated" and members()["RELIANCE"] == "Oil & Gas"
    st = nse500.auto_status()
    assert st["enabled"] and st["last_ok"] and st["last_error"] is None


def test_failed_download_keeps_the_list_and_alerts_once_a_day(logged_in, monkeypatch):
    instruments.sync_instruments()
    _serve(monkeypatch, [("RELIANCE", "EQ", "Energy"), ("TCS", "EQ", "IT")])
    nse500.auto_update()
    _serve(monkeypatch, RuntimeError("nsearchives.nseindia.com: ConnectTimeout"))
    assert nse500.auto_update()["status"] == "failed"
    assert nse500.auto_update()["status"] == "failed"
    assert members() == {"RELIANCE": "Energy", "TCS": "IT"}
    assert len([a for a in audit.active_alerts() if a["name"] == "nse500_download"]) == 1
    assert "ConnectTimeout" in nse500.auto_status()["last_error"]


def test_too_many_changes_at_once_wait_for_you(logged_in, monkeypatch):
    instruments.sync_instruments()
    _serve(monkeypatch, [("RELIANCE", "EQ", "Energy"), ("TCS", "EQ", "IT"), ("INFY", "EQ", "IT")])
    nse500.auto_update()
    monkeypatch.setitem(nse500.DEFAULTS, "max_changes", 1)
    monkeypatch.setattr(nse500, "_cfg", lambda: nse500.DEFAULTS)
    _serve(monkeypatch, [("RELIANCE", "EQ", "Energy")])                       # 2 leave at once > 1
    r = nse500.auto_update()
    assert r["status"] == "blocked" and set(members()) == {"RELIANCE", "TCS", "INFY"}
    assert nse500.auto_status()["blocked"] == {"added": 0, "removed": 2}
    assert nse500.auto_update(force=True)["status"] == "updated" and set(members()) == {"RELIANCE"}
    assert nse500.auto_status()["blocked"] is None


def test_no_instruments_yet_skips():
    assert nse500.auto_update()["status"] == "skipped"


def test_leaving_the_nse500_cancels_pending_buys_only(clk, quotes, monkeypatch):  # noqa: F811
    add_signal()                                                             # ABC
    add_signal(symbol="XYZ", sector="Energy", close=100, trigger=100.5, limit=101.0, stop=96.0, target=110.0, rr=2.2)
    with connect() as conn:
        conn.execute("UPDATE instruments SET segment = 'NSE'")
    manager.place_entries("2026-09-21")
    cycle(clk, datetime(2026, 9, 22, 10, 0), quotes, ABC=(503, 504, 499), XYZ=(99, 99.5, 98))   # ABC bought
    cycle(clk, datetime(2026, 9, 22, 10, 1), quotes, ABC=(504, 505, 499), XYZ=(99, 99.5, 98))
    assert trade("P100")["state"] == "PROTECTED" and trade("P100", "XYZ")["state"] == "BUY_PENDING"
    _serve(monkeypatch, [("ABC", "EQ", "Finance")])                          # XYZ left
    r = nse500.auto_update()
    assert r["removed"] == ["XYZ"]
    for acct in ("P30", "P60", "P100"):
        x = trade(acct, "XYZ")
        if x:
            assert x["state"] == "CANCELLED" and x["exit_reason"] == "left_nse500"
    assert trade("P100")["state"] == "PROTECTED"
    _serve(monkeypatch, [])                                                  # ABC leaves too: position kept
    with connect() as conn:
        conn.execute("UPDATE instruments SET is_nse500 = 1 WHERE symbol = 'ABC'")
    nse500.auto_update(force=True)
    assert trade("P100")["state"] == "PROTECTED"
    alert = [a for a in audit.active_alerts() if a["name"] == "nse500_change" and "left" in a["message"]]
    assert any("pending buy" in a["message"] for a in alert)


def test_instruments_job_runs_the_update(logged_in, monkeypatch):
    from agent import scheduler
    _serve(monkeypatch, [("RELIANCE", "EQ", "Energy"), ("TCS", "EQ", "IT")])
    out = scheduler.job_instruments_sync()
    assert "NSE500 updated" in out and set(members()) == {"RELIANCE", "TCS"}
    assert "NSE500 unchanged" in scheduler.job_instruments_sync()


def test_download_falls_back_to_the_next_source(tmp_path):
    import threading
    from functools import partial
    from http.server import HTTPServer, SimpleHTTPRequestHandler
    (tmp_path / "good.csv").write_text(nse500_csv([("RELIANCE", "EQ", "Energy")]))
    (tmp_path / "bad.csv").write_text("not,a,list\n")

    class Quiet(SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(tmp_path)))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        content, url = nse500.download([f"{base}/missing.csv", f"{base}/bad.csv", f"{base}/good.csv"], 5)
        assert url.endswith("good.csv") and b"RELIANCE" in content
        try:
            nse500.download([f"{base}/missing.csv"], 5)
            raise AssertionError("expected a failure")
        except RuntimeError as exc:
            assert "404" in str(exc)
    finally:
        srv.shutdown()
