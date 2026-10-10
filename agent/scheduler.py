"""Scheduled jobs (spec section 4), run by APScheduler inside the backend process.

Each job goes through `run_job`, which skips market holidays (except backups),
records the run in audit_log and in app_state['job_runs'] for the Overview.
Jobs of later phases are listed with `built=False` so the Overview shows the
whole daily cycle from day one.
"""
from __future__ import annotations

import logging
import threading
import time as _time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import audit
from .broker import kite_session
from .clock import IST, now_ist, ts
from .db import backup_db, get_state, set_state
from .events import publish
from .market_calendar import MarketCalendar
from .settings import RUNTIME_DIR, get_app_config, get_secrets

log = logging.getLogger("agent")
RUNS_KEY = "job_runs"


class Skip(Exception):
    """Raised by a job to record a deliberate skip (not an error)."""


@dataclass
class JobDef:
    name: str
    label: str
    when: str
    phase: int
    built: bool
    trading_days_only: bool = True
    func: Callable[[], str] | None = None
    trigger: dict[str, Any] | None = None
    quiet: bool = False              # runs every minute: audit only errors, not every run
    misfire_grace: int = 3 * 3600


# ---------- job bodies ----------

# ---------- login gate (decided 2026-09-30) ----------
# The login check runs every minute, every day, from startup. Until Kite has confirmed today's session, every other
# scheduled job is held: it does not run and is not recorded, so the catch-up that follows verification runs it.
# Manual "Run now" from the Overview is not held.

VERIFIED_KEY = "login_verified"
ALERT_KEY = "login_alerted_on"
ALERT_FROM = (8, 30)             # before this, a missing login is not alerted (Kite tokens expire around 06:00)
_verify_lock = threading.Lock()
_sched: list = [None]            # the running scheduler, for the catch-up after verification


def login_verified() -> bool:
    """True when Kite has confirmed the current session (checked once per login, not every minute)."""
    st = kite_session.status()
    v = get_state(VERIFIED_KEY) or {}
    return bool(st["valid"] and v.get("login_at") and v.get("login_at") == st.get("login_at"))


def reset_verification() -> None:
    set_state(VERIFIED_KEY, None)


def _verify() -> tuple[bool, str]:
    st = kite_session.status()
    if not st["valid"]:
        return False, st.get("reason") or "Not logged in"
    try:
        prof = kite_session.get_kite().profile()
    except kite_session.SessionUnavailable as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        if kite_session.is_token_error(exc):
            kite_session.invalidate("Kite rejected the session token")
            return False, "Kite rejected the session: log in again"
        return False, f"Could not reach Kite ({type(exc).__name__}); retrying every minute"
    set_state(VERIFIED_KEY, {"login_at": st["login_at"], "at": ts(), "user_id": prof.get("user_id") or st.get("user_id")})
    return True, f"Login verified as {prof.get('user_id') or st.get('user_id')}"


def _park() -> None:
    """Verified: the login check sleeps until the session expires (Kite tokens end around 06:00), then checks
    every minute again. A session ending earlier (token rejected, logout) wakes it at once (wake_login_check)."""
    sched = _sched[0]
    st = kite_session.status()
    if sched is None or not st.get("expires_at"):
        return
    try:
        job = sched.get_job("login_check")
        if job is not None:
            job.modify(next_run_time=datetime.fromisoformat(st["expires_at"]))
    except Exception:  # noqa: BLE001 - worst case it keeps checking every minute
        log.exception("could not park the login check")


def wake_login_check() -> None:
    sched = _sched[0]
    if sched is None:
        return
    try:
        job = sched.get_job("login_check")
        if job is not None:
            job.modify(next_run_time=datetime.now(IST) + timedelta(seconds=1))
    except Exception:  # noqa: BLE001
        log.exception("could not wake the login check")
    publish("jobs")


kite_session.on_session_end.append(wake_login_check)


def job_login_check() -> str:
    """Every minute until today's login is verified; then asleep until the session expires."""
    if login_verified():
        _park()
        raise Quiet("Verified")
    with _verify_lock:
        if login_verified():
            _park()
            raise Quiet("Verified")
        ok, msg = _verify()
        if not ok:
            now = now_ist()
            if now >= _at(now.date(), ALERT_FROM) and get_state(ALERT_KEY) != now.date().isoformat():
                set_state(ALERT_KEY, now.date().isoformat())
                url = get_secrets().public_url.rstrip("/")
                audit.alert("login_missed", f"Kite login needed ({msg}). All scheduled jobs are on hold until you log "
                            "in; the login check retries every minute. Paper GTTs are not checked until then."
                            + (f"\nLog in: {url}/#/login" if url else ""), level="error")
            raise Skip(f"Waiting for login: {msg}")
        audit.record("system", "login_verified", f"{msg}. Scheduled jobs released.")
        _after_verified()
        _park()
        publish("session")
        return msg


def _after_verified() -> None:
    """Released: sync instruments first (other jobs use them), then run everything that was missed or held."""
    if (get_state("instruments_synced_at") or "")[:10] < now_ist().date().isoformat():
        run_job("instruments_sync", catch_up=True)
    if _sched[0] is not None:
        catch_up(_sched[0])


class Quiet(Exception):
    """A routine outcome of a frequent job: not recorded as a run."""


def job_instruments_sync() -> str:
    from .data.instruments import sync_instruments
    if not kite_session.status()["valid"]:
        raise Skip("No Kite session")
    r = sync_instruments()
    from .data import nse500
    n5 = nse500.auto_update()
    tail = {"updated": f"; NSE500 updated ({len(n5.get('added', []))} joined, {len(n5.get('removed', []))} left)",
            "unchanged": "; NSE500 unchanged", "failed": "; NSE500 download failed (list kept)",
            "blocked": "; NSE500 change too large, not applied"}.get(n5["status"], "")
    return f"{r['stored']} instruments{tail}"


def job_intraday_scan() -> str:
    from .strategy import intraday
    r = intraday.scan()
    if not r["strategies"]:
        raise Quiet("No intraday strategy is ON")
    if not r["signals"]:
        raise Quiet(f"{r['checked']} candle(s) checked, no pullback")
    placed = ", ".join(f"{a} {n}" for a, n in (r["placed"].get("placed") or {}).items() if n) if r["placed"] else ""
    return (f"{len(r['signals'])} pullback signal(s): " + ", ".join(f"{x['symbol']} {x['at']}" for x in r["signals"])
            + (f"; buy GTTs placed: {placed}" if placed else ""))


def job_intraday_cutoff() -> str:
    from .strategy import intraday
    n = intraday.cutoff()
    return f"{n} unfilled intraday buy GTT(s) cancelled"


def job_order_monitor() -> str:
    from .orders import manager
    return manager.on_cycle()


EOD_SESSION_KEY = "eod_last_session"


def eod_done_session() -> str | None:
    """The last session whose EOD run completed. Older installs: derived from the last successful run's time."""
    done = get_state(EOD_SESSION_KEY)
    if done:
        return done
    last = get_state(RUNS_KEY, {}).get("eod_run") or {}
    if last.get("status") == "ok" and last.get("at"):
        return MarketCalendar(get_app_config()).last_completed_session(datetime.fromisoformat(last["at"])).isoformat()
    return None


def job_eod_run() -> str:
    """EOD run for the last completed session (today at 21:00; the missed session when run as a catch-up):
    candles -> indicators -> results calendar -> trade maintenance (session end, exits, buy expiry, trailing,
    equity) -> signal scan -> sizing, ranking and buy GTTs. Phase 3 adds the journal."""
    from .data import events
    from .data.candles import run_backfill
    from .orders import manager
    from .settings import load_strategy_config
    from .strategy import indicators, scanner
    if not kite_session.status()["valid"]:
        audit.alert("eod_run", "EOD run skipped: not logged in to Kite. No candles fetched, no new orders.",
                    level="error")
        raise Skip("No Kite session")
    from .strategy import versions
    strategy = versions.champion()
    candles = run_backfill()
    ind = indicators.compute_all(strategy.params)
    ev = events.fetch_daily()
    if ev["ok"] and not (events.status().get("history") or {}).get("ok"):
        events.fetch_history()  # one-off: needed for last year's result dates
    day = MarketCalendar(get_app_config()).last_completed_session(now_ist()).isoformat()
    if candles.get("adjusted"):
        manager.pause_for_adjustment(candles["adjusted"])  # split/bonus on a held stock: pause and alert
    maint = manager.eod_maintenance(day)
    scan = scan_all()
    entries = manager.refresh_entries(scan["date"]) if scan.get("date") == day else None
    placed = ", ".join(f"{a} {n}" for a, n in entries["placed"].items()) if entries else "none (no scan for today)"
    set_state(EOD_SESSION_KEY, day)
    journaled = _after_eod(day)
    _resume_minute_download()
    return (f"Candles: {candles['updated']} updated, {candles['error_count']} failed. Indicators: {ind['symbols']}. "
            f"Results calendar: {'ok' if ev['ok'] else 'FAILED'}. Trades: {maint['buys_expired']} buys expired, "
            f"{len(maint['trails'])} trail updates. Signals {scan['date']}: {scan['triggers']} "
            f"triggers, {scan['eligible']} tradeable. Buy GTTs: {placed}. {journaled}")


def scan_all() -> dict[str, Any]:
    """Scan every ON momentum strategy that has a portfolio (and challenger versions). The first is the primary
    scan: its result is the Signals screen's status and what this function returns. Intraday strategies scan
    during market hours instead (job_intraday_scan)."""
    from .strategy import scanner, strategies, versions
    vs = [v for v in versions.trading_versions() if strategies.kind_of(versions.load(v).params) == "momentum_breakout"]
    if not vs:
        p = strategies.primary_version()
        if not p or strategies.kind_of(versions.load(p).params) != "momentum_breakout":
            return {}
        vs = [p]
    scan = scanner.scan_eod(versions.load(vs[0]))
    for v in vs[1:]:
        scanner.scan_eod(versions.load(v), primary=False)
    return scan


def _after_eod(day: str) -> str:
    """Journal (and the EOD summary) after the trading work is done; a failure here never fails the EOD run."""
    from .journal import trades as journal
    try:
        j = journal.sync()
        text = f"Journal: {j['journaled']} new, {j['explained']} explained" + (" (LLM unavailable)" if j["llm_failed"] else "")
    except Exception as exc:  # noqa: BLE001
        log.exception("journal sync failed")
        text = f"Journal failed: {exc}"
    try:
        from .notify import telegram
        telegram.eod_summary(day)
    except Exception:  # noqa: BLE001 - a notification must never break the run
        log.exception("EOD summary failed")
    try:
        from .learning import loop
        ch = loop.update_challenger()
        if ch:
            text += f". Learning: {ch}"
    except Exception:  # noqa: BLE001
        log.exception("challenger update failed")
    return text


def _resume_minute_download() -> None:
    """After the EOD run's own Kite calls are done: add today's 1-minute bars and finish any paused or
    interrupted download, in the background. The 22:30 job stays as a fallback (skips while this runs)."""
    from .data import minute
    if get_state(minute.STATE_KEY) is None or minute.is_running():
        return  # never downloaded (started from the Overview) or already going
    threading.Thread(target=run_job, args=("minute_candles",), kwargs={"catch_up": True}, name="minute_candles",
                     daemon=True).start()


def job_weekly_review() -> str:
    """Saturday: review the week just ended (Mon–Sun), then let the learning loop look for one proposal."""
    from datetime import timedelta as _td
    from .journal import trades as journal, weekly
    journal.sync()                                    # trades closed since the last EOD run are in the review
    today = now_ist().date()
    monday = weekly.week_of(today - _td(days=1 if today.weekday() == 6 else 0))[0]
    if today.weekday() < 5:                           # run on a weekday (catch-up): the previous full week
        monday -= _td(days=7)
    out = weekly.run(monday)
    msg = f"Week of {out['week_start']} reviewed" + (" (explained)" if out["explained"] else " (template text)")
    try:
        from .learning import loop
        p = loop.weekly_step()
        msg += f". Learning: {p}"
    except Exception as exc:  # noqa: BLE001 - the review stands even if the learning step fails
        log.exception("learning step failed")
        msg += f". Learning step failed: {exc}"
    return msg


def job_minute_candles() -> str:
    """Add today's 1-minute bars (and any missed sessions) after the close."""
    from .data import minute
    if not kite_session.status()["valid"]:
        raise Skip("No Kite session")
    if minute.is_running():
        raise Skip("A 1-minute download is already running")
    st = minute.run()
    return f"{st['done']} symbols, {st['rows_added']:,} bars added, {st.get('error_count', 0)} failed"


def job_backup() -> str:
    cfg = get_app_config()["backup"]
    path = backup_db(RUNTIME_DIR / "backups", int(cfg["keep_days"]))
    return f"Saved {path.name}"


def _hm(value: str) -> tuple[int, int]:
    h, m = value.split(":")
    return int(h), int(m)


def job_definitions() -> list[JobDef]:
    app = get_app_config()
    bh, bm = _hm(app["backup"]["time"])
    eh, em = _hm(app["market"]["eod_after"])
    eod_m = em + 5
    eod_h, eod_m = eh + eod_m // 60, eod_m % 60
    return [
        JobDef("instruments_sync", "Instruments + NSE500 list", "09:00 and right after login", 0, True,
               func=job_instruments_sync, trigger={"day_of_week": "mon-fri", "hour": 9, "minute": 0}),
        JobDef("login_check", "Login check", "Every minute until verified, then at session expiry", 0, True,
               trading_days_only=False, func=job_login_check, trigger={"minute": "*"}, quiet=True, misfire_grace=50),
        JobDef("order_monitor", "Order monitor", "Every 1 min, 09:15–15:30", 2, True, func=job_order_monitor,
               trigger={"day_of_week": "mon-fri", "hour": "9-15", "minute": "*"}, quiet=True, misfire_grace=50),
        JobDef("intraday_scan", "Intraday pullback scan", "Every 5 min, 09:30–14:30", 3, True, func=job_intraday_scan,
               trigger={"day_of_week": "mon-fri", "hour": "9-14", "minute": "*/5", "second": 20}, quiet=True,
               misfire_grace=120),
        JobDef("intraday_cutoff", "Cancel unfilled intraday buys", "15:15", 3, True, func=job_intraday_cutoff,
               trigger={"day_of_week": "mon-fri", "hour": 15, "minute": 15}),
        JobDef("eod_run", "EOD run", f"{eod_h:02d}:{eod_m:02d}", 0, True,
               func=job_eod_run, trigger={"day_of_week": "mon-fri", "hour": eod_h, "minute": eod_m}),
        JobDef("minute_candles", "1-minute candles", "After the EOD run (fallback 22:30)", 0, True,
               func=job_minute_candles, trigger={"day_of_week": "mon-fri", "hour": 22, "minute": 30}),
        JobDef("weekly_review", "Weekly review + learning", "Saturday 10:00", 3, True, trading_days_only=False,
               func=job_weekly_review, trigger={"day_of_week": "sat", "hour": 10, "minute": 0}),
        JobDef("backup", "Database backup", f"{bh:02d}:{bm:02d} daily", 0, True, trading_days_only=False,
               func=job_backup, trigger={"hour": bh, "minute": bm}),
    ]


def run_job(name: str, catch_up: bool = False) -> dict[str, Any]:
    """catch_up: a missed run done after a restart, possibly on a weekend or holiday (the work is for the
    missed session, not for today)."""
    jobs = {j.name: j for j in job_definitions()}
    job = jobs[name]
    if not job.built or job.func is None:
        raise ValueError(f"{name} is not built yet (phase {job.phase})")
    cal = MarketCalendar(get_app_config())
    today = now_ist().date()
    started = _time.monotonic()
    status, message = "ok", ""
    if job.trading_days_only and not catch_up and not cal.is_trading_day(today):
        status, message = "skipped", f"Market holiday ({cal.holiday_name(today) or 'weekend'})"
    else:
        try:
            message = job.func() or ""
        except Quiet as q:
            return {"status": "ok", "message": str(q), "quiet": True}
        except Skip as s:
            status, message = "skipped", str(s)
        except Exception as exc:  # noqa: BLE001 - every failure is recorded and alerted
            status, message = "error", f"{type(exc).__name__}: {exc}"
            log.exception("job %s failed", name)
            audit.alert(f"job_{name}", f"{job.label} failed: {message}", level="error")
    run = {"at": ts(), "status": status, "message": message[:300],
           "seconds": round(_time.monotonic() - started, 1)}
    runs = get_state(RUNS_KEY, {})
    runs[name] = run
    set_state(RUNS_KEY, runs)
    if not job.quiet or status == "error":
        audit.record("job", name, f"{status}: {message}"[:500], "error" if status == "error" else "info")
    publish("jobs")
    return run


def jobs_overview(scheduler: BackgroundScheduler | None = None) -> list[dict[str, Any]]:
    runs = get_state(RUNS_KEY, {})
    verified = login_verified()
    out = []
    for j in job_definitions():
        next_run = None
        if scheduler and j.built:
            aps = scheduler.get_job(j.name)
            if aps and aps.next_run_time:
                next_run = aps.next_run_time.astimezone(IST).isoformat(timespec="seconds")
        out.append({"name": j.name, "label": j.label, "when": j.when, "phase": j.phase, "built": j.built,
                    "last_run": runs.get(j.name), "next_run": next_run,
                    "held": j.built and j.name != "login_check" and not verified})
    return out


def run_scheduled(name: str) -> dict[str, Any] | None:
    """What the scheduler calls: every job except the login check waits for a verified login (held, not run and
    not recorded; the catch-up after verification runs it)."""
    if name != "login_check" and not login_verified():
        return None
    return run_job(name)


def build_scheduler() -> BackgroundScheduler:
    sched = BackgroundScheduler(timezone=IST, job_defaults={"coalesce": True, "max_instances": 1,
                                                             "misfire_grace_time": 3 * 3600})
    for j in job_definitions():
        if j.built and j.trigger:
            sched.add_job(run_scheduled, CronTrigger(timezone=IST, **j.trigger), args=[j.name], id=j.name,
                          name=j.label, replace_existing=True, misfire_grace_time=j.misfire_grace)
    _sched[0] = sched
    return sched


def _at(d, hm: tuple[int, int]) -> datetime:
    return datetime.combine(d, datetime.min.time(), IST).replace(hour=hm[0], minute=hm[1])


def missed_jobs(now: datetime | None = None) -> list[str]:
    """Scheduled work that did not happen while the agent was stopped (laptop off, crash, late start), in the
    order to run it now (decided 2026-09-24: every missed job runs on the next restart or login)."""
    app = get_app_config()
    cal = MarketCalendar(app)
    now = (now or now_ist()).astimezone(IST)
    today = now.date()
    runs = get_state(RUNS_KEY, {})
    logged_in = kite_session.status()["valid"]
    ran_on = lambda name: ((runs.get(name) or {}).get("at") or "")[:10]  # noqa: E731
    todo: list[str] = []
    trading_today = cal.is_trading_day(today)
    if trading_today and logged_in and now >= _at(today, (9, 0)) \
            and (get_state("instruments_synced_at") or "")[:10] < today.isoformat():
        todo.append("instruments_sync")
    from .strategy import intraday
    # intraday buys never carry into the next session: after 15:15 (or on a closed day) any pending one, before
    # 15:15 any left from an earlier session
    if (not trading_today or now >= _at(today, (15, 15))) and intraday.pending_count():
        todo.append("intraday_cutoff")
    elif intraday.pending_count(placed_before=today.isoformat()):
        todo.append("intraday_cutoff")
    done = eod_done_session()
    if done and done < cal.last_completed_session(now).isoformat():   # never on a fresh install (no EOD yet)
        todo.append("eod_run")
    last_sat = today - timedelta(days=(today.weekday() - 5) % 7)
    if now >= _at(last_sat, (10, 0)) and done and ran_on("weekly_review") < last_sat.isoformat():
        todo.append("weekly_review")
    bt = _hm(app["backup"]["time"])
    due = _at(today, bt) if now >= _at(today, bt) else _at(today - timedelta(days=1), bt)
    last_backup = (runs.get("backup") or {}).get("at")
    if not last_backup or datetime.fromisoformat(last_backup) < due:
        todo.append("backup")
    if "eod_run" not in todo and logged_in:   # the EOD run starts the 1-minute job itself
        from .data import minute
        st = get_state(minute.STATE_KEY)
        if st and not minute.is_running() and (st.get("aborted") or st.get("running")
                                                or (st.get("upto") or "") < cal.last_completed_session(now).isoformat()):
            todo.append("minute_candles")
    return todo


def run_missed(todo: list[str]) -> None:
    if not login_verified():
        return   # the login expired in between: the next verification runs them
    for name in todo:
        if name == "minute_candles":
            _resume_minute_download()
        else:
            run_job(name, catch_up=True)


def catch_up(sched: BackgroundScheduler) -> list[str]:
    """After the login is verified: run every missed or held job, in order, in the background. Before that the
    login check holds everything (it calls this again once it verifies)."""
    if not login_verified():
        return []
    todo = missed_jobs()
    if todo:
        labels = {j.name: j.label for j in job_definitions()}
        what = ", ".join(labels.get(n, n) for n in todo)
        detail = f" (EOD for {MarketCalendar(get_app_config()).last_completed_session(now_ist())})" if "eod_run" in todo else ""
        audit.record("system", "catch_up", f"Missed while the agent was stopped: {what}{detail}. Running now.")
        sched.add_job(run_missed, "date", run_date=datetime.now(IST) + timedelta(seconds=10),
                      args=[todo], id="catch_up", replace_existing=True)
    return todo
