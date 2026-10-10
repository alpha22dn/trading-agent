"""Phase 0: API endpoints and scheduled jobs."""
from __future__ import annotations

from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from agent import audit, scheduler
from agent.broker import kite_session
from agent.api.app import create_app
from agent.db import get_state

from .conftest import nse500_csv


@pytest.fixture
def client(fake_kite, fixed_clock, monkeypatch):
    import agent.api.app as app_module
    from agent.data import events
    from .test_phase1 import FakeNse
    monkeypatch.setattr(app_module, "_background", lambda name, fn, *args: fn(*args))  # run inline
    monkeypatch.setattr(events, "client_factory", lambda: FakeNse([]))              # never call NSE
    with TestClient(create_app(start_scheduler=False)) as c:
        yield c


def test_overview_on_fresh_install(client):
    r = client.get("/api/overview").json()
    assert r["session"]["valid"] is False
    assert r["broker_mode"] == "paper"
    assert r["market_filter"]["enabled"] is False and r["market_filter"]["state"] == "no_data"
    assert [a["id"] for a in r["accounts"]] == ["P30", "P60", "P100"]
    assert {j["name"] for j in r["jobs"]} >= {"eod_run", "order_monitor", "backup"}
    assert r["data"]["coverage"]["nse500_symbols"] == 0


def test_screener_query_endpoint(client):
    q = client.get("/api/screener-query").json()["query"]
    assert q.startswith("Sales growth > Sales growth 3Years AND") and q.endswith("Market Capitalization > 5000")


def test_login_redirect_and_callback(client, fake_kite):
    r = client.get("/api/kite/login", follow_redirects=False)
    assert r.status_code == 307 and "kite.zerodha.com/connect/login" in r.headers["location"]
    r = client.get("/api/kite/callback?request_token=abc&status=success", follow_redirects=False)
    assert r.headers["location"].startswith("/#/login?ok=1")
    assert client.get("/api/session").json()["valid"] is True
    assert get_state("job_runs")["instruments_sync"]["status"] == "ok"   # synced right after login


def test_failed_callback_does_not_log_in(client):
    r = client.get("/api/kite/callback?request_token=bad&status=success", follow_redirects=False)
    assert "error" in r.headers["location"]
    assert client.get("/api/session").json()["valid"] is False


def test_state_changes_need_confirmation(client):
    assert client.post("/api/kill-switch", json={}).status_code == 400
    assert client.post("/api/kill-switch", json={"confirm": True}).json()["active"] is True
    assert client.get("/api/overview").json()["kill_switch"]["active"] is True
    assert client.post("/api/kill-switch/rearm", json={"confirm": True}).json()["active"] is False


def test_backfill_endpoint_requires_login(client):
    r = client.post("/api/data/backfill", json={"confirm": True})
    assert r.status_code == 409


def test_nse500_upload_endpoint(client):
    client.get("/api/kite/callback?request_token=abc&status=success", follow_redirects=False)
    client.post("/api/data/instruments/sync", json={"confirm": True})
    csv_bytes = nse500_csv([("RELIANCE", "EQ", "Energy"), ("TCS", "EQ", "IT")]).encode()
    r = client.post("/api/data/nse500", files={"file": ("ind_nifty500list.csv", csv_bytes, "text/csv")})
    assert r.status_code == 200 and r.json()["matched"] == 2
    bad = client.post("/api/data/nse500", files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")})
    assert bad.status_code == 400


def test_alert_acknowledge(client):
    aid = audit.alert("test", "something")
    assert any(a["id"] == aid for a in client.get("/api/overview").json()["alerts"])
    assert client.post(f"/api/alerts/{aid}/ack").status_code == 200
    assert not any(a["id"] == aid for a in client.get("/api/overview").json()["alerts"])


def test_websocket_receives_updates(client):
    with client.websocket_connect("/ws") as ws:
        assert ws.receive_json()["topic"] == "hello"
        client.post("/api/kill-switch", json={"confirm": True})
        topics = {ws.receive_json()["topic"] for _ in range(2)}
        assert topics & {"controls", "alerts"}


# ---------- jobs ----------

def test_jobs_skip_on_holiday(fixed_clock):
    fixed_clock.set(datetime(2026, 10, 2, 16, 0))
    run = scheduler.run_job("eod_run")
    assert run["status"] == "skipped" and "Gandhi" in run["message"]


def test_eod_without_login_alerts_and_skips(fixed_clock):
    fixed_clock.set(datetime(2026, 9, 22, 15, 50))
    run = scheduler.run_job("eod_run")
    assert run["status"] == "skipped"
    assert any(a["name"] == "eod_run" for a in audit.active_alerts())


def test_login_check_alerts_when_missed(fixed_clock):
    run = scheduler.run_job("login_check")
    assert run["status"] == "skipped"
    assert any(a["name"] == "login_missed" for a in audit.active_alerts())


def test_backup_job_runs_on_holidays(fixed_clock, tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "RUNTIME_DIR", tmp_path)
    fixed_clock.set(datetime(2026, 10, 2, 18, 30))
    assert scheduler.run_job("backup")["status"] == "ok"
    assert get_state("job_runs")["backup"]["status"] == "ok"


def test_job_error_is_recorded_not_raised(fixed_clock, monkeypatch):
    def boom():
        raise RuntimeError("disk full")
    defs = scheduler.job_definitions()
    for j in defs:
        if j.name == "backup":
            j.func = boom
    monkeypatch.setattr(scheduler, "job_definitions", lambda: defs)
    run = scheduler.run_job("backup")
    assert run["status"] == "error" and "disk full" in run["message"]
    assert any(a["name"] == "job_backup" for a in audit.active_alerts())


def _ran(name, at, status="ok"):
    from agent.db import set_state
    runs = get_state(scheduler.RUNS_KEY, {})
    runs[name] = {"at": at, "status": status, "message": ""}
    set_state(scheduler.RUNS_KEY, runs)


def test_catch_up_schedules_missed_eod(fixed_clock, fake_kite):
    _ran("eod_run", "2026-09-21T21:00:05+05:30")                  # Monday's EOD ran
    fixed_clock.set(datetime(2026, 9, 22, 21, 30))                 # Tuesday's 21:00 run was missed
    sched = scheduler.build_scheduler()
    assert scheduler.catch_up(sched) == []                         # held until the login is verified
    kite_session.complete_login("req")
    assert scheduler.run_job("login_check")["status"] == "ok"      # verifies, syncs instruments, schedules the catch-up
    assert sched.get_job("catch_up") is not None
    assert "eod_run" in scheduler.catch_up(sched)


def test_login_gate_holds_every_other_job_until_verified(fixed_clock, fake_kite):
    runs_before = dict(get_state(scheduler.RUNS_KEY, {}))
    for name in ("order_monitor", "backup", "eod_run", "instruments_sync"):
        assert scheduler.run_scheduled(name) is None                # held: not run, not recorded
    assert get_state(scheduler.RUNS_KEY, {}) == runs_before
    assert all(j["held"] for j in scheduler.jobs_overview() if j["name"] != "login_check")
    assert scheduler.run_scheduled("login_check")["status"] == "skipped"
    kite_session.complete_login("req")
    assert not scheduler.login_verified()                           # logged in, not yet confirmed by Kite
    assert scheduler.run_scheduled("login_check")["status"] == "ok" and scheduler.login_verified()
    assert get_state("instruments_synced_at")                       # released: instruments first
    n = sum(1 for c in fake_kite.calls if c[0] == "profile")
    assert scheduler.run_scheduled("login_check")["quiet"]          # verified: later minutes do nothing
    assert sum(1 for c in fake_kite.calls if c[0] == "profile") == n
    assert not any(j["held"] for j in scheduler.jobs_overview())
    assert scheduler.run_scheduled("backup") is not None


def test_login_check_rejected_token_and_one_alert_a_day(fixed_clock, fake_kite):
    kite_session.complete_login("req")
    fake_kite.token_error = True
    r = scheduler.run_job("login_check")
    assert r["status"] == "skipped" and "rejected" in r["message"] and not kite_session.status()["valid"]
    for minute in range(3):                                          # retried every minute, alerted once
        fixed_clock.set(datetime(2026, 9, 22, 10, 1 + minute))
        assert scheduler.run_job("login_check")["status"] == "skipped"
    assert sum(1 for a in audit.active_alerts() if a["name"] == "login_missed") == 1
    fixed_clock.set(datetime(2026, 9, 23, 6, 30))                   # before 08:30: checked, not alerted
    scheduler.run_job("login_check")
    assert sum(1 for a in audit.active_alerts() if a["name"] == "login_missed") == 1
    fixed_clock.set(datetime(2026, 9, 23, 8, 31))
    scheduler.run_job("login_check")
    assert sum(1 for a in audit.active_alerts() if a["name"] == "login_missed") == 2


def test_restart_reverifies_the_session(fixed_clock, fake_kite):
    kite_session.complete_login("req")
    scheduler.run_job("login_check")
    assert scheduler.login_verified()
    scheduler.reset_verification()                                   # what startup does
    assert not scheduler.login_verified() and scheduler.run_scheduled("backup") is None
    assert scheduler.run_job("login_check")["status"] == "ok" and scheduler.login_verified()


def test_missed_jobs_after_the_laptop_was_off_overnight(fixed_clock, fake_kite):
    """Off at 21:00 on Tue 22 Sep, back at 10:00 on Wed: Tuesday's EOD and backup run now, in that order."""
    _ran("eod_run", "2026-09-21T21:00:05+05:30")
    _ran("backup", "2026-09-21T21:30:00+05:30")
    _ran("weekly_review", "2026-09-19T10:00:05+05:30")
    fixed_clock.set(datetime(2026, 9, 23, 10, 0))
    assert scheduler.missed_jobs() == ["eod_run", "backup"]    # not logged in yet (the login gate holds them)
    kite_session.complete_login("req")
    from agent.db import set_state
    set_state("instruments_synced_at", "2026-09-23T09:36:00+05:30")
    assert scheduler.missed_jobs() == ["eod_run", "backup"]
    set_state(scheduler.EOD_SESSION_KEY, "2026-09-22")
    _ran("backup", "2026-09-23T10:00:30+05:30")
    assert scheduler.missed_jobs() == []


def _verified():
    from agent.db import set_state
    kite_session.complete_login("req")
    set_state(scheduler.VERIFIED_KEY, {"login_at": kite_session.status()["login_at"]})


def test_missed_eod_runs_on_a_weekend_for_fridays_session(fixed_clock, fake_kite, monkeypatch):
    """Off from Friday 20:00 to Saturday: Friday's EOD runs on Saturday (a catch-up ignores the weekend rule)."""
    _ran("eod_run", "2026-09-24T21:00:05+05:30")                   # Thursday done
    _ran("backup", "2026-09-26T09:00:00+05:30")
    fixed_clock.set(datetime(2026, 9, 26, 11, 0))                  # Saturday
    assert scheduler.missed_jobs() == ["eod_run", "weekly_review"]     # Saturday's review is due too
    seen = []
    monkeypatch.setattr(scheduler, "job_definitions", lambda real=scheduler.job_definitions: [
        j if j.name != "eod_run" else scheduler.JobDef(**{**j.__dict__, "func": lambda: seen.append(1) or "ok"})
        for j in real()])
    assert scheduler.run_job("eod_run")["status"] == "skipped"     # the normal schedule skips weekends
    scheduler.run_missed(["eod_run"])
    assert seen == []                          # held: the login is not verified
    _verified()
    scheduler.run_missed(["eod_run"])
    assert seen == [1]


def test_no_catch_up_on_a_fresh_install(fixed_clock):
    fixed_clock.set(datetime(2026, 9, 22, 22, 0))
    assert "eod_run" not in scheduler.missed_jobs()


# ---------- Phase 1 endpoints ----------

def test_phase1_endpoints(client, monkeypatch):
    from datetime import date as _date
    from agent.data import events
    from .test_phase1 import FakeNse, _bm, _breakout, _flat, _insert_candles
    from agent.strategy import indicators
    from agent.settings import load_strategy_config

    client.get("/api/kite/callback?request_token=abc&status=success", follow_redirects=False)
    csv_bytes = nse500_csv([("TCS", "EQ", "IT"), ("INFY", "EQ", "IT")]).encode()
    client.post("/api/data/nse500", files={"file": ("n.csv", csv_bytes, "text/csv")})
    _insert_candles("TCS", *_breakout())
    _insert_candles("INFY", *_flat())
    nifty = [20000 * 1.001 ** t for t in range(160)]
    _insert_candles("NIFTY 50", nifty, nifty, nifty, vol=0)
    monkeypatch.setattr(events, "client_factory", lambda: FakeNse([_bm("INFY", _date(2026, 9, 24))]))

    assert client.post("/api/watchlist", json={"text": "TCS,10,1000"}).status_code == 400   # needs confirm
    r = client.post("/api/watchlist", json={"text": "TCS,10,1000\nZZZ,1,1", "confirm": True}).json()
    assert r["symbols"] == ["TCS"] and r["not_in_nse500"] == ["ZZZ"]
    assert client.post("/api/results-calendar/fetch", json={"confirm": True}).json()["started"]
    cal = client.get("/api/results-calendar").json()
    assert cal["status"]["fetched_today"] and cal["upcoming"][0]["symbol"] == "INFY"

    assert client.post("/api/signals/scan", json={"confirm": True}).json()["started"]
    sig = client.get("/api/signals").json()
    assert sig["date"] == "2026-09-21"            # 10:00 on the 22nd: last completed session
    wl = client.get("/api/watchlist").json()
    assert [r["symbol"] for r in wl["rows"]] == ["TCS"] and wl["pasted_on"]
    assert len(client.get("/api/watchlist?all=1").json()["rows"]) == 2

    assert client.post("/api/history/run", json={"confirm": True}).json()["started"]
    h = client.get("/api/history").json()
    assert h["summary"]["totals"]["triggers"] >= 1 and not h["running"]
    month = h["summary"]["months"][-1]["month"]
    assert client.get(f"/api/signals?month={month}").json()["signals"]
    c = client.get("/api/candles/TCS?days=30").json()
    assert len(c["candles"]) == 30 and c["candles"][-1]["bb_upper"] is not None
    assert client.get("/api/candles/NOPE").status_code == 404


def test_intraday_chart_candles(client, logged_in):
    from agent.data import instruments, intraday
    instruments.sync_instruments()
    r = client.get("/api/candles/TCS/intraday?tf=15m")
    assert r.status_code == 200
    c = r.json()["candles"]
    assert len(c) > 20 and all(k in c[0] for k in ("time", "open", "high", "low", "close"))
    assert c[1]["time"] - c[0]["time"] == 15 * 60
    four = client.get("/api/candles/TCS/intraday?tf=4h").json()["candles"]
    # two bars per session: 09:15 and 13:15 IST (times are IST wall-clock seconds)
    assert {(x["time"] % 86400) // 60 for x in four[:4]} == {9 * 60 + 15, 13 * 60 + 15}
    assert client.get("/api/candles/TCS/intraday?tf=7m").status_code == 409


def test_intraday_needs_login(client):
    intraday_r = client.get("/api/candles/TCS/intraday?tf=5m")
    assert intraday_r.status_code == 409                       # no login (and no instruments yet)


def test_symbol_search(client, logged_in):
    from agent.data import instruments, nse500
    from .conftest import nse500_csv
    instruments.sync_instruments()
    nse500.apply_constituents(nse500.parse_csv(nse500_csv([("TCS", "EQ", "IT"), ("INFY", "EQ", "IT")])))
    r = client.get("/api/instruments/search?q=in").json()
    assert r[0]["symbol"] == "INFY" and r[0]["nse500"]
    assert [x["symbol"] for x in client.get("/api/instruments/search?q=tc").json()] == ["TCS"]
    assert client.get("/api/instruments/search?q=").json() == []
    assert all(x["symbol"] != "NIFTY 50" for x in client.get("/api/instruments/search?q=nifty").json())


def test_daily_chart_adds_todays_live_bar(client, fixed_clock, monkeypatch):
    from datetime import datetime, timedelta
    from agent.broker import feed
    from agent.broker.base import Quote
    from .test_phase1 import _breakout, _insert_candles
    _insert_candles("TCS", *_breakout())
    last = client.get("/api/candles/TCS?days=30").json()["candles"][-1]
    fixed_clock.set(datetime.fromisoformat(last["date"]).replace(hour=10) + timedelta(days=1))
    today = fixed_clock.now().date().isoformat()
    monkeypatch.setattr(feed, "quote_source", lambda syms: {"TCS": Quote(ltp=101.0, high=102.0, low=99.5, open=100.0)})
    c = client.get("/api/candles/TCS?days=30&live=1").json()["candles"]
    assert c[-1]["date"] == today and c[-1]["live"]
    assert (c[-1]["open"], c[-1]["high"], c[-1]["low"], c[-1]["close"]) == (100.0, 102.0, 99.5, 101.0)
    assert len(c) == 31
    # no quote (not logged in, API down): stored candles only, no error
    monkeypatch.setattr(feed, "quote_source", lambda syms: (_ for _ in ()).throw(RuntimeError("down")))
    assert len(client.get("/api/candles/TCS?days=30&live=1").json()["candles"]) == 30
    # a historical window never gets today's bar
    monkeypatch.setattr(feed, "quote_source", lambda syms: {"TCS": Quote(ltp=101.0, high=102.0, low=99.5)})
    assert client.get(f"/api/candles/TCS?days=30&live=1&until={last['date']}").json()["candles"][-1]["date"] == last["date"]


def _count_scans(monkeypatch):
    from agent.strategy import indicators, scanner
    calls = []
    monkeypatch.setattr(scanner, "scan_eod", lambda strategy: calls.append(1) or {})
    monkeypatch.setattr(indicators, "compute_all", lambda params: {"symbols": 0})
    return calls


def test_login_fetches_results_calendar_and_rescans(client, monkeypatch):
    from agent.data import events
    from .test_phase1 import _breakout, _insert_candles
    _insert_candles("TCS", *_breakout())                 # candles up to the last completed session
    scans = _count_scans(monkeypatch)
    fetches = []
    real = events.fetch_daily
    monkeypatch.setattr(events, "fetch_daily", lambda: fetches.append(1) or real())
    client.get("/api/kite/callback?request_token=abc&status=success", follow_redirects=False)
    assert fetches == [1] and scans == [1]
    assert events.status()["fetched_today"] and events.status()["fresh"]
    client.get("/api/kite/callback?request_token=abc&status=success", follow_redirects=False)
    assert fetches == [1] and scans == [1, 1]             # already fetched today: re-scan only


def test_watchlist_upload_rescans_signals(client, monkeypatch):
    from agent.db import connect
    from .test_phase1 import _breakout, _insert_candles
    client.get("/api/kite/callback?request_token=abc&status=success", follow_redirects=False)
    client.post("/api/data/nse500", files={"file": ("n.csv", nse500_csv([("TCS", "EQ", "IT")]).encode(), "text/csv")})
    scans = _count_scans(monkeypatch)
    # no candles for the last session yet: skipped (the EOD run scans them)
    r = client.post("/api/watchlist", json={"text": "TCS,10,1000", "confirm": True}).json()
    assert r["rescan"] and scans == []
    assert "skipped" in [a for a in audit.recent(20) if a["name"] == "signal_scan"][0]["message"]
    _insert_candles("TCS", *_breakout())
    client.post("/api/watchlist", json={"text": "TCS,10,1000", "confirm": True})
    assert scans == [1]
    with connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM audit_log WHERE message = 'Automatic re-scan after watchlist update'"
                            ).fetchone()[0] == 1


def test_dismiss_all_alerts_of_one_kind(client):
    for i in range(3):
        audit.alert("price_adjustment", f"stock {i}: past prices changed")
    audit.alert("order_monitor", "no quotes")
    assert client.post("/api/alerts/ack-all/price_adjustment", json={}).status_code == 400      # needs confirm
    assert client.post("/api/alerts/ack-all/price_adjustment", json={"confirm": True}).json() == {"dismissed": 3}
    assert [a["name"] for a in audit.active_alerts()] == ["order_monitor"]


def test_login_check_sleeps_until_the_session_expires(fixed_clock, fake_kite):
    sched = scheduler.build_scheduler()
    sched.start(paused=True)
    try:
        assert sched.get_job("login_check").next_run_time.second == 0          # every minute while waiting
        kite_session.complete_login("req")
        assert scheduler.run_job("login_check")["status"] == "ok"               # verified
        expires = datetime.fromisoformat(kite_session.status()["expires_at"])
        assert sched.get_job("login_check").next_run_time == expires            # asleep until 06:00 tomorrow
        assert scheduler.run_job("login_check")["quiet"]                        # a stray run parks it again
        assert sched.get_job("login_check").next_run_time == expires
        kite_session.logout()                                                   # the session ends early
        nxt = sched.get_job("login_check").next_run_time
        assert (nxt - datetime.now(nxt.tzinfo)).total_seconds() < 5            # checking again right away
        view = {j["name"]: j for j in scheduler.jobs_overview(sched)}
        assert "session expiry" in view["login_check"]["when"]
    finally:
        sched.shutdown(wait=False)
        scheduler._sched[0] = None


def test_dashboard_page_is_always_rechecked(client):
    from agent.api.app import DIST
    if not (DIST / "index.html").exists():
        pytest.skip("dashboard not built")
    r = client.get("/")
    assert r.status_code == 200 and r.headers.get("cache-control") == "no-cache"
    assert client.get("/#/overview").headers.get("cache-control") == "no-cache"
