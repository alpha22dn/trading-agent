"""FastAPI backend: REST for the dashboard, a WebSocket for live updates, and
the built dashboard served at /. Binds to 127.0.0.1 only (see .env)."""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
import threading
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from .. import audit, scheduler as jobs
from ..broker import kite_session
from ..clock import now_ist
from ..data import candles, events, instruments, nse500, watchlist
from . import auth
from .. import backtest, procs
from ..db import connect, current_db_path, get_state, init_db
from ..events import broadcaster, publish
from ..market_calendar import MarketCalendar
from ..orders import manager as trade_manager, paper_edits, views as trade_views  # manager registers the kill-switch hook
from ..risk import controls, portfolios
from ..settings import ROOT, RUNTIME_DIR, ConfigError, get_app_config, load_strategy_config
from ..strategy import indicators, market_filter, scanner, strategies, versions
from ..journal import llm, trades as journal, weekly
from ..learning import loop as learning
from ..notify import telegram  # registers the alert listener (critical / error alerts to Telegram)

log = logging.getLogger("agent")
def launch_backtest(run_id: int, live_db) -> None:
    """Start a backtest in its own process (it never shares the live agent's clock, quotes or orders)."""
    procs.spawn(f"backtest-{run_id}", ["backtest-run", "--run-id", str(run_id), "--live-db", str(live_db)],
                f"backtest_{run_id}")


def _backtest_alive(run_id: int) -> bool:
    return procs.alive(f"backtest-{run_id}")
DIST = ROOT / "dashboard" / "dist"


class PortfolioRequest(BaseModel):
    confirm: bool = False
    id: str | None = None
    name: str | None = None
    capital: float | None = None
    manual_edits: bool | None = None
    strategy_ids: list[int] | None = None


class BacktestRequest(BaseModel):
    confirm: bool = False
    name: str | None = None
    start: str
    end: str
    capital: float | None = None
    overrides: dict[str, float] = {}
    bands: list[str] = []
    strategy: str | None = None


class Confirm(BaseModel):
    confirm: bool = False


class StrategyRequest(BaseModel):
    confirm: bool = False
    name: str | None = None
    kind: str = "momentum_breakout"
    settings: dict[str, float] | None = None
    portfolios: list[str] | None = None
    enabled: bool | None = None
    copy_from: int | None = None


class ProposeRequest(BaseModel):
    confirm: bool = False
    strategy_id: int | None = None


class RejectRequest(BaseModel):
    confirm: bool = False
    note: str = ""


class RollbackRequest(BaseModel):
    confirm: bool = False
    version: str


class WatchlistPaste(BaseModel):
    text: str
    confirm: bool = False


class TradeEdit(BaseModel):
    action: str
    value: float | None = None
    qty: int | None = None
    confirm: bool = False


class OutsideBuy(BaseModel):
    symbol: str
    qty: int
    confirm: bool = False


class FetchEvents(BaseModel):
    confirm: bool = False
    history: bool = False


def _require_confirm(body: Confirm) -> None:
    if not body.confirm:
        raise HTTPException(400, "This action changes state and needs confirm: true")


def _background(name: str, fn, *args) -> None:
    def runner() -> None:
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            log.exception("background task %s failed", name)
            audit.alert(name, f"{name} failed: {exc}", level="error")
    threading.Thread(target=runner, name=name, daemon=True).start()


def _price_feed() -> dict[str, Any]:
    from ..broker import feed
    from ..orders import fastloop
    try:
        return feed.status() | fastloop.stats
    except Exception as exc:  # noqa: BLE001 - the Overview must still load
        return {"mode": "unknown", "error": str(exc)}


def _minute_status() -> dict[str, Any]:
    from ..data import minute
    try:
        return minute.status()
    except Exception as exc:  # noqa: BLE001 - the Overview must still load
        return {"error": str(exc)}


def create_app(start_scheduler: bool = True) -> FastAPI:
    app_cfg = get_app_config()
    init_db(app_cfg, load_strategy_config(app_cfg["strategy"]["active_version"]))
    telegram.bind_live(current_db_path())   # alerts from backtest / replay copies never go out
    champ = versions.champion   # the champion can change (Learning screen), so it is looked up on each use
    state: dict[str, Any] = {"scheduler": None}

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        broadcaster.bind(asyncio.get_running_loop())
        candles.mark_interrupted()
        backtest.mark_interrupted(current_db_path())
        learning.mark_interrupted()
        if start_scheduler:
            sched = jobs.build_scheduler()
            sched.start()
            state["scheduler"] = sched
            # the login check runs first (then every minute); it runs the catch-up once Kite confirms the login.
            # A restart re-verifies even a session from earlier today, so everything waits for Kite's answer.
            jobs.reset_verification()
            sched.add_job(jobs.run_scheduled, "date", run_date=now_ist() + timedelta(seconds=2), args=["login_check"],
                          id="login_check_startup", replace_existing=True)
            audit.record("system", "startup", f"Backend started; strategy {champ().version}")
            from ..broker import feed
            from ..orders import fastloop
            feed.start_stream()            # Kite WebSocket prices in market hours (REST quotes as the fallback)
            fastloop.start()
            from .. import ops
            ops.start()                    # start notice to Telegram, uptime ping every 5 minutes
        yield
        if state["scheduler"]:
            from .. import ops
            from ..broker import feed
            from ..orders import fastloop
            fastloop.stop()
            feed.stop_stream()
            state["scheduler"].shutdown(wait=False)
            ops.stop()                     # marks a clean stop (a restart after a crash says so)

    api = FastAPI(title="Trading Agent", version="0.1.0", lifespan=lifespan)

    # ---------- sign-in (servers only: DASHBOARD_AUTH=required) ----------

    @api.middleware("http")
    async def require_session(request: Request, call_next):
        if not auth.allowed(request.url.path, request.cookies.get(auth.COOKIE)):
            if request.url.path == "/api/kite/callback":
                return RedirectResponse("/#/login?error=Sign in to the dashboard first, then log in to Kite again")
            return JSONResponse({"detail": "Sign in required"}, status_code=401)
        return await call_next(request)

    def _client(request: Request) -> str:
        host = request.client.host if request.client else "?"
        if host in ("127.0.0.1", "::1"):                    # behind the Tailscale proxy: the forwarded address
            host = (request.headers.get("x-forwarded-for") or host).split(",")[0].strip()
        return host

    @api.get("/api/auth/status")
    def auth_status(request: Request) -> dict[str, Any]:
        return {"required": auth.required(), "configured": auth.configured(),
                "signed_in": (not auth.required()) or auth.valid_session(request.cookies.get(auth.COOKIE))}

    @api.post("/api/auth/login")
    async def auth_login(request: Request):
        if not auth.required():
            return {"ok": True}
        who = _client(request)
        wait = auth.too_many(who)
        if wait:
            raise HTTPException(429, f"Too many wrong passwords. Try again in {max(1, wait // 60)} minute(s).")
        try:
            password = str((await request.json()).get("password") or "")
        except ValueError:
            password = ""
        if not auth.check_password(password):
            auth.failed(who)
            audit.record("user", "dashboard_sign_in", f"Wrong dashboard password from {who}", "warning")
            raise HTTPException(401, "Wrong password")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(auth.COOKIE, auth.new_session(), max_age=auth.SESSION_DAYS * 86400, httponly=True,
                        samesite="strict", secure=auth.cookie_secure(), path="/")
        audit.record("user", "dashboard_sign_in", f"Signed in from {who}")
        return resp

    @api.post("/api/auth/logout")
    def auth_logout():
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(auth.COOKIE, path="/")
        return resp

    # ---------- health & session ----------

    @api.get("/api/health")
    def health(request: Request) -> dict[str, Any]:
        out: dict[str, Any] = {"ok": True, "time": now_ist().isoformat(timespec="seconds")}
        if auth.allowed("/api/private", request.cookies.get(auth.COOKIE)):
            out |= {"strategy": champ().version, "broker_mode": app_cfg["broker"]["mode"]}
        return out

    @api.get("/api/session")
    def session() -> dict[str, Any]:
        return kite_session.status()

    @api.get("/api/kite/login")
    def kite_login():
        try:
            return RedirectResponse(kite_session.login_url())
        except kite_session.SessionUnavailable as exc:
            return RedirectResponse(f"/#/login?error={exc}")

    @api.get("/api/kite/callback")
    def kite_callback(request_token: str | None = None, status: str | None = None):
        if status != "success" or not request_token:
            audit.record("user", "kite_login", f"Login not completed (status={status})", "warning")
            return RedirectResponse("/#/login?error=Login was cancelled or failed")
        try:
            kite_session.complete_login(request_token)
        except Exception as exc:  # noqa: BLE001
            audit.alert("kite_login", f"Could not complete Kite login: {exc}", level="error")
            return RedirectResponse("/#/login?error=Could not complete login. Check the API secret in .env")

        def after_login() -> None:
            # verify with Kite first: that releases the held jobs (instruments sync, then the catch-up)
            if jobs.run_job("login_check").get("status") == "skipped":
                return   # not verified: the minute check keeps trying and does the rest once it succeeds
            ev = events.status()
            if not ev["fetched_today"]:  # results calendar: refreshed at every day's first login
                events.fetch_daily()
                if not (ev.get("history") or {}).get("ok"):
                    events.fetch_history()
            _auto_rescan("login")
        _background("after_login", after_login)
        return RedirectResponse("/#/login?ok=1")

    @api.post("/api/kite/logout")
    def kite_logout(body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        kite_session.logout()
        return kite_session.status()

    @api.get("/api/screener-query")
    def screener_query() -> dict[str, Any]:
        return {"query": get_app_config().screener_query}

    # ---------- overview ----------

    @api.get("/api/overview")
    def overview() -> dict[str, Any]:
        cal = MarketCalendar(app_cfg)
        now = now_ist()
        today = now.date()
        up = get_state("nse500_uploaded")
        return {
            "now": now.isoformat(timespec="seconds"),
            "strategy": champ().version,
            "strategies_on": sum(1 for r in strategies.active_rows() if r["enabled"]),
            "price_feed": _price_feed(),
            "broker_mode": app_cfg["broker"]["mode"],
            "session": kite_session.status(),
            "market": {
                "is_trading_day": cal.is_trading_day(today),
                "holiday": cal.holiday_name(today),
                "is_open": cal.is_market_open(now),
                "next_trading_day": cal.next_trading_day(today).isoformat(),
                "calendar_valid_through": cal.valid_through.isoformat(),
                "calendar_expiring": (cal.valid_through - today).days < 30,
            },
            "market_filter": market_filter.evaluate(),
            "results_calendar": events.status(),
            "last_scan": get_state("last_scan"),
            "kill_switch": controls.kill_switch_state(),
            "accounts": [{k: v for k, v in a.items() if k != "equity_curve"}
                         for a in trade_views.accounts_summary()["accounts"]],
            "jobs": jobs.jobs_overview(state["scheduler"]),
            "alerts": audit.active_alerts(),
            "data": {
                "instruments_synced_at": get_state("instruments_synced_at"),
                "nse500": up,
                "nse500_auto": nse500.auto_status(),
                "backfill": get_state(candles.STATE_KEY),
                "coverage": candles.coverage(),
                "minute": _minute_status(),
            },
        }

    @api.get("/api/audit")
    def audit_recent(limit: int = 100, kind: str | None = None) -> list[dict[str, Any]]:
        return audit.recent(min(limit, 500), kind)

    # ---------- controls ----------

    @api.post("/api/kill-switch")
    def kill_switch(body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        return controls.activate_kill_switch()

    @api.post("/api/kill-switch/rearm")
    def kill_switch_rearm(body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        return controls.rearm_kill_switch()

    @api.post("/api/accounts/{account_id}/rearm")
    def account_rearm(account_id: str, body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        if not controls.rearm_account(account_id):
            raise HTTPException(404, f"{account_id} is not halted")
        return {"ok": True}

    @api.post("/api/alerts/{alert_id}/ack")
    def alert_ack(alert_id: int) -> dict[str, Any]:
        if not audit.acknowledge(alert_id):
            raise HTTPException(404, "No such alert")
        return {"ok": True}

    @api.post("/api/alerts/ack-all/{name}")
    def alert_ack_all(name: str, body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        n = audit.acknowledge_all(name)
        audit.user_action("alerts_dismissed", f"Dismissed {n} '{name}' alert(s)")
        return {"dismissed": n}

    @api.post("/api/jobs/{name}/run")
    def job_run(name: str, body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        defs = {j.name: j for j in jobs.job_definitions()}
        if name not in defs or not defs[name].built:
            raise HTTPException(404, f"{name} is not a runnable job")
        audit.user_action("job_run_now", f"Ran {name} manually")
        _background(f"job_{name}", jobs.run_job, name)
        return {"started": True}

    # ---------- data setup ----------

    @api.post("/api/data/nse500")
    async def upload_nse500(file: UploadFile = File(...)) -> dict[str, Any]:
        content = await file.read()
        if len(content) > 2_000_000:
            raise HTTPException(413, "File too large for an NSE500 list")
        try:
            items = nse500.parse_csv(content)
            return nse500.apply_constituents(items).as_dict()
        except nse500.Nse500FormatError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/data/nse500/check")
    def nse500_check(body: Confirm, force: bool = False) -> dict[str, Any]:
        """Download NSE's list now; force=1 applies it even past the max-changes safeguard."""
        _require_confirm(body)
        audit.user_action("nse500_check", "Checked the NSE500 list with NSE" + (" (apply anyway)" if force else ""))
        return nse500.auto_update(force=force)

    @api.post("/api/data/instruments/sync")
    def instruments_sync(body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        try:
            return instruments.sync_instruments()
        except kite_session.SessionUnavailable as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/data/backfill")
    def backfill(body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        if not kite_session.status()["valid"]:
            raise HTTPException(409, "Log in to Kite first")
        if candles.is_running():
            raise HTTPException(409, "A candle download is already running")
        audit.user_action("candle_backfill", "Started candle download")
        _background("candle_backfill", candles.run_backfill)
        return {"started": True}

    @api.post("/api/data/minute")
    def minute_download(body: Confirm) -> dict[str, Any]:
        from ..data import minute
        _require_confirm(body)
        if not kite_session.status()["valid"]:
            raise HTTPException(409, "Log in to Kite first")
        if minute.is_running():
            raise HTTPException(409, "A 1-minute download is already running")
        audit.user_action("minute_candles", "Started 1-minute candle download")
        _background("minute_candles", minute.run)
        return {"started": True}

    @api.post("/api/data/minute/stop")
    def minute_stop(body: Confirm) -> dict[str, Any]:
        from ..data import minute
        _require_confirm(body)
        if not minute.request_stop():
            raise HTTPException(409, "No 1-minute download is running")
        audit.user_action("minute_candles", "Pause requested: stops after the stock being downloaded")
        return {"stopping": True}

    @api.get("/api/instruments/search")
    def instruments_search(q: str = "", limit: int = 10) -> list[dict[str, Any]]:
        return instruments.search(q, min(int(limit), 25))

    @api.get("/api/data/coverage")
    def coverage() -> dict[str, Any]:
        return candles.coverage()

    # ---------- Phase 1: watchlist, results calendar, signals ----------

    @api.get("/api/watchlist")
    def watchlist_view(all: bool = False) -> dict[str, Any]:
        out = scanner.gate_table(only_watchlist=not all)
        out["stale_after_days"] = int(app_cfg["watchlist"]["stale_after_days"])
        return out

    @api.post("/api/watchlist")
    def watchlist_paste(body: WatchlistPaste) -> dict[str, Any]:
        if not body.confirm:
            raise HTTPException(400, "This action changes state and needs confirm: true")
        try:
            out = watchlist.replace_watchlist(body.text).as_dict()
        except watchlist.WatchlistFormatError as exc:
            raise HTTPException(400, str(exc)) from exc
        _background("signal_scan", _auto_rescan, "watchlist update")
        return out | {"rescan": True}

    @api.post("/api/watchlist/upload")
    async def watchlist_upload(file: UploadFile = File(...)) -> dict[str, Any]:
        content = await file.read()
        if len(content) > 5_000_000:
            raise HTTPException(413, "File too large for a watchlist")
        if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
            raise HTTPException(400, "Upload the .xlsx file you pasted the Screener results into")
        try:
            out = watchlist.replace_from_xlsx(content).as_dict()
        except watchlist.WatchlistFormatError as exc:
            raise HTTPException(400, str(exc)) from exc
        _background("signal_scan", _auto_rescan, "watchlist update")
        return out | {"rescan": True}

    @api.get("/api/results-calendar")
    def results_calendar(days: int = 30) -> dict[str, Any]:
        today = now_ist().date().isoformat()
        with connect() as conn:
            rows = conn.execute(
                """SELECT e.symbol, e.event_date, e.purpose, w.symbol IS NOT NULL AS on_watchlist
                   FROM events e JOIN instruments i ON i.symbol = e.symbol AND i.is_nse500 = 1
                   LEFT JOIN watchlist w ON w.symbol = e.symbol AND w.active = 1
                   WHERE e.event_date >= ? AND e.event_date <= date(?, ?) ORDER BY e.event_date, e.symbol""",
                (today, today, f"+{int(days)} days")).fetchall()
        return {"status": events.status(), "upcoming": [dict(r) for r in rows]}

    @api.post("/api/results-calendar/fetch")
    def results_calendar_fetch(body: FetchEvents) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        audit.user_action("results_calendar_fetch", "History" if body.history else "Daily")
        _background("results_calendar", events.fetch_history if body.history else events.fetch_daily)
        return {"started": True}

    @api.get("/api/signals")
    def signals_view(date: str | None = None, month: str | None = None, version: str | None = None) -> dict[str, Any]:
        last_scan = get_state("last_scan") or {}
        rows_ = strategies.active_rows()
        v = version if version in {r["version"] for r in rows_} else champ().version
        # A scan with no triggers stores no rows, so the last scan's date wins when it is newer.
        latest = max(filter(None, [scanner.latest_signal_date(v), last_scan.get("date")]), default=None)
        day = None if month else (date or latest)
        rows = scanner.signals_for(v, day=day, month=month) if (day or month) else []
        cands = trade_views.candidates_for([r["id"] for r in rows if r.get("eligible")])
        for r in rows:
            r["accounts"] = cands.get(r["id"], [])
        return {"date": day, "month": month, "latest_date": latest, "last_scan": last_scan or None, "version": v,
                "strategies": [{"name": r["name"], "version": r["version"], "enabled": bool(r["enabled"]),
                                "portfolios": strategies.portfolios_of(r["id"])} for r in rows_],
                "signals": rows, "results_calendar": events.status(), "market_filter": market_filter.evaluate()}

    def _rescan() -> None:
        """Scan the last completed session, then place buy GTTs for its tradeable signals in rank order (and
        cancel pending buys whose signal dropped out). Used by the button, login and watchlist uploads."""
        indicators.compute_all(champ().params)       # indicator periods are shared by every strategy
        scan = jobs.scan_all() or {}
        if scan.get("date"):
            trade_manager.refresh_entries(scan["date"])

    def _auto_rescan(why: str) -> None:
        """Re-scan the last completed session from stored candles after something the gates depend on
        changed (login + results calendar, watchlist upload), then place/cancel buy GTTs (see _rescan).
        Skipped until that session's candles are in; the EOD run downloads and scans them."""
        d = MarketCalendar(get_app_config()).last_completed_session(now_ist()).isoformat()
        with connect() as conn:
            have = conn.execute("SELECT MAX(date) FROM candles_daily").fetchone()[0]
        if not have or have < d:
            audit.record("job", "signal_scan", f"Automatic re-scan after {why} skipped: candles for {d} not "
                         "downloaded yet (the EOD run fetches and scans them)")
            return
        audit.record("system", "signal_scan", f"Automatic re-scan after {why}")
        _rescan()

    @api.post("/api/signals/scan")
    def signals_scan(body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        audit.user_action("signal_scan", "Re-ran today's scan from cached data")
        _background("signal_scan", _rescan)
        return {"started": True}

    # ---------- Backtest tab ----------

    @api.get("/api/backtests/setup")
    def backtest_setup() -> dict[str, Any]:
        return backtest.setup(current_db_path())

    @api.get("/api/backtests")
    def backtest_list() -> dict[str, Any]:
        runs = backtest.list_runs(current_db_path())
        return {"runs": runs, "running": next((r["id"] for r in runs if r["status"] in ("queued", "running", "cancelling")
                                               and _backtest_alive(r["id"])), None)}

    @api.get("/api/backtests/{run_id}")
    def backtest_get(run_id: int) -> dict[str, Any]:
        r = backtest.get_run(current_db_path(), run_id)
        if not r:
            raise HTTPException(404, "No such backtest")
        return r

    @api.post("/api/backtests")
    def backtest_start(body: BacktestRequest) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        live_db = current_db_path()
        if procs.any_alive("backtest-"):
            raise HTTPException(409, "A backtest is already running: wait for it or cancel it")
        try:
            cfg = backtest.validate(body.model_dump(exclude={"confirm"}), live_db)
        except backtest.BacktestError as exc:
            raise HTTPException(400, str(exc)) from exc
        run_id = backtest.create_run(live_db, cfg)
        audit.user_action("backtest", f"Started backtest {run_id}: {cfg['name']}")
        launch_backtest(run_id, live_db)
        return {"id": run_id, "config": cfg}

    @api.post("/api/backtests/{run_id}/cancel")
    def backtest_cancel(run_id: int, body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        live_db = current_db_path()
        r = backtest.get_run(live_db, run_id)
        if not r or r["status"] not in ("queued", "running"):
            raise HTTPException(409, "That backtest is not running")
        if _backtest_alive(run_id):
            backtest.update_run(live_db, run_id, status="cancelling", message="Cancelling…")
        else:
            backtest.update_run(live_db, run_id, status="cancelled", message="Cancelled")
        return {"cancelling": True}

    @api.post("/api/backtests/{run_id}/delete")
    def backtest_delete(run_id: int, body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        if _backtest_alive(run_id):
            raise HTTPException(409, "Cancel the run before deleting it")
        with connect() as conn:
            n = conn.execute("DELETE FROM backtest_runs WHERE id = ?", (run_id,)).rowcount
        if not n:
            raise HTTPException(404, "No such backtest")
        return {"deleted": run_id}

    @api.get("/api/history")
    def history_view() -> dict[str, Any]:
        summary = scanner.history_state()
        if summary:  # month counts are re-read so "on today's watchlist" follows the current paste
            summary = summary | scanner.summarize(champ().version)
        return {"summary": summary, "running": _history_lock.locked()}

    _history_lock = threading.Lock()

    def _history() -> None:
        if not _history_lock.acquire(blocking=False):
            return
        try:
            publish("history")
            indicators.compute_all(champ().params)
            scanner.scan_history(champ())
        finally:
            _history_lock.release()
            publish("history")

    @api.post("/api/history/run")
    def history_run(body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        if _history_lock.locked():
            raise HTTPException(409, "A historical scan is already running")
        audit.user_action("history_scan", "Started 2-year historical scan")
        _background("history_scan", _history)
        return {"started": True}

    # ---------- Phase 2: accounts and positions ----------

    @api.get("/api/accounts")
    def accounts_view() -> dict[str, Any]:
        return trade_views.accounts_summary()

    @api.get("/api/positions")
    def positions_view(account: str | None = None) -> dict[str, Any]:
        return trade_views.positions(account)

    @api.get("/api/trades/{trade_id}")
    def trade_view(trade_id: int) -> dict[str, Any]:
        d = trade_views.trade_detail(trade_id)
        if d is None:
            raise HTTPException(404, "No such trade")
        return d

    # ---------- Phase 2b: paper edits on the manual-edit account, user confirmations ----------

    @api.post("/api/trades/{trade_id}/edit")
    def trade_edit(trade_id: int, body: TradeEdit) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        try:
            return paper_edits.edit_trade(trade_id, body.action, body.value, body.qty)
        except paper_edits.EditError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ---------- portfolios: add, edit, reset ----------

    @api.post("/api/accounts")
    def portfolio_create(body: PortfolioRequest) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        try:
            out = portfolios.create(body.id or "", body.name or "", body.capital, bool(body.manual_edits),
                                    body.strategy_ids)
        except portfolios.PortfolioError as exc:
            raise HTTPException(400, str(exc)) from exc
        _background("signal_scan", _auto_rescan, "new portfolio")
        return out

    @api.post("/api/accounts/{account_id}/edit")
    def portfolio_edit(account_id: str, body: PortfolioRequest) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        try:
            return portfolios.edit(account_id, body.name, body.capital, body.manual_edits)
        except portfolios.PortfolioError as exc:
            raise HTTPException(409 if "only be changed" in str(exc) else 400, str(exc)) from exc

    @api.get("/api/accounts/{account_id}/holdings")
    def portfolio_holdings(account_id: str) -> dict[str, Any]:
        try:
            portfolios._row(account_id)
        except portfolios.PortfolioError as exc:
            raise HTTPException(404, str(exc)) from exc
        return portfolios.holdings(account_id) | {"history": portfolios.history(account_id),
                                                  "market_open": MarketCalendar(get_app_config()).is_market_open(now_ist())}

    @api.post("/api/accounts/{account_id}/reset")
    def portfolio_reset(account_id: str, body: PortfolioRequest) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        try:
            return portfolios.reset(account_id, body.capital)
        except portfolios.PortfolioError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/accounts/{account_id}/buy-outside")
    def buy_outside(account_id: str, body: OutsideBuy) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        try:
            return paper_edits.buy_outside(account_id, body.symbol, body.qty)
        except paper_edits.EditError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/accounts/{account_id}/sell-outside")
    def sell_outside(account_id: str, body: OutsideBuy) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        try:
            return paper_edits.sell_outside(account_id, body.symbol, body.qty)
        except paper_edits.EditError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/trades/{trade_id}/confirm-exit")
    def trade_confirm_exit(trade_id: int, body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        try:
            return trade_manager.confirm_new_exit(trade_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/trades/{trade_id}/resume")
    def trade_resume(trade_id: int, body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        try:
            return trade_manager.resume_trade(trade_id)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    # ---------- research tab ----------

    @api.get("/api/research/symbols")
    def research_symbols(q: str = "", limit: int = 30) -> list[dict[str, Any]]:
        from ..research import data as research
        return research.symbols(q, min(int(limit), 100))

    @api.get("/api/research/bars")
    def research_bars(symbol: str, tf: str = "1D", before: int | None = None, limit: int = 500) -> dict[str, Any]:
        from ..research import data as research
        try:
            return research.bars(symbol, tf, before, limit)
        except research.ResearchError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.get("/api/research/replay")
    def research_replay(symbol: str, after: int, limit: int = 5000) -> dict[str, Any]:
        from ..research import data as research
        try:
            return research.replay_feed(symbol, after, limit)
        except research.ResearchError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.get("/api/research/seconds/days")
    def research_second_days() -> dict[str, Any]:
        from ..data import seconds
        return {"days": seconds.days()}

    @api.get("/api/research/seconds")
    def research_seconds(symbol: str, start: int, end: int) -> dict[str, Any]:
        """Recorded 1-second bars of one stock for the minutes in [start, end) (at most a day)."""
        from ..data import seconds
        from ..research import data as research
        if end <= start or end - start > 86_400_000:
            raise HTTPException(400, "Ask for at most one day of seconds")
        try:
            token = research._token(symbol)
        except research.ResearchError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {"symbol": symbol, "minutes": seconds.read(token, start, end)}

    @api.get("/api/research/trades")
    def research_trades(session: str | None = None) -> dict[str, Any]:
        from ..research import trading
        return trading.listing(session)

    @api.post("/api/research/trades")
    async def research_trade_add(request: Request) -> dict[str, Any]:
        from ..research import trading
        try:
            return trading.record(await request.json())
        except (trading.TradeError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.delete("/api/research/trades")
    def research_trades_clear(session: str) -> dict[str, Any]:
        from ..research import trading
        return {"deleted": trading.clear(session)}

    @api.get("/api/research/charges")
    def research_charges(side: str, qty: int, entry: float, exit: float, entry_ts: int, exit_ts: int) -> dict[str, Any]:
        from ..research import trading
        try:
            return trading.round_trip(side, qty, entry, exit, entry_ts, exit_ts)
        except trading.TradeError as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.get("/api/research/agent")
    def research_agent(symbol: str) -> dict[str, Any]:
        from ..research import trading
        return trading.agent_overlay(symbol)

    @api.get("/api/research/layout/{key}")
    def research_layout(key: str) -> dict[str, Any]:
        from ..research import data as research
        return research.get_layout(key) or {"data": None}

    @api.put("/api/research/layout/{key}")
    async def research_layout_put(key: str, request: Request) -> dict[str, Any]:
        from ..research import data as research
        try:
            return research.put_layout(key, await request.json())
        except (research.ResearchError, ValueError) as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.get("/api/candles/{symbol}/intraday")
    def candles_intraday(symbol: str, tf: str = "15m") -> dict[str, Any]:
        from ..data import intraday
        try:
            return {"symbol": symbol, "tf": tf, "candles": intraday.candles(symbol, tf)}
        except intraday.IntradayError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/api/candles/{symbol}")
    def candles_view(symbol: str, days: int = 160, until: str | None = None, live: bool = False) -> dict[str, Any]:
        with connect() as conn:
            rows = conn.execute(
                """SELECT c.date, c.open, c.high, c.low, c.close, c.volume, i.bb_upper
                   FROM candles_daily c LEFT JOIN indicators_daily i ON i.symbol = c.symbol AND i.date = c.date
                   WHERE c.symbol = ? AND c.date <= COALESCE(?, '9999') ORDER BY c.date DESC LIMIT ?""",
                (symbol, until, min(int(days), 600))).fetchall()
        if not rows:
            raise HTTPException(404, f"No candles for {symbol}")
        out = [dict(r) for r in reversed(rows)]
        if live and not until:
            bar = _today_bar(symbol)
            if bar and bar["date"] > out[-1]["date"]:
                out.append(bar)
        return {"symbol": symbol, "candles": out}

    def _today_bar(symbol: str) -> dict[str, Any] | None:
        """Today's forming daily bar from a live quote, while the market is open (chart display only)."""
        from ..broker import feed
        from ..market_calendar import MarketCalendar
        now = now_ist()
        if not MarketCalendar(get_app_config()).is_market_open(now):
            return None
        try:
            q = feed.quote_source([symbol]).get(symbol)
        except Exception:  # noqa: BLE001 - no live bar; the stored candles still show
            return None
        if not q:
            return None
        o = q.open if q.open else q.ltp
        return {"date": now.date().isoformat(), "open": o, "high": max(q.high, o, q.ltp), "low": min(q.low, o, q.ltp),
                "close": q.ltp, "volume": None, "bb_upper": None, "live": True}

    # ---------- Strategies screen (decided 2026-09-30) ----------

    @api.get("/api/strategies")
    def strategies_view() -> dict[str, Any]:
        return {"strategies": strategies.listing(), **strategies.form_setup(),
                "learning_active": (learning.active() or {}).get("strategy_id")}

    def _strategy_call(fn, *args, rescan: bool = False):
        try:
            out = fn(*args)
        except strategies.StrategyError as exc:
            raise HTTPException(400, str(exc)) from exc
        if rescan:   # a strategy started trading somewhere: scan and place its buys now, as after a watchlist change
            _background("signal_scan", _auto_rescan, "strategy change")
        return out

    @api.post("/api/strategies")
    def strategy_create(body: StrategyRequest) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        on = True if body.enabled is None else body.enabled
        return _strategy_call(strategies.create, body.name or "", body.kind, body.settings or {}, body.portfolios or [],
                              on, body.copy_from, rescan=on and bool(body.portfolios))

    @api.post("/api/strategies/{sid}/edit")
    def strategy_edit(sid: int, body: StrategyRequest) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        out = _strategy_call(strategies.edit, sid, body.name, body.settings)
        if any(c.startswith("new version") for c in out.get("changes", [])) and out["enabled"]:
            _background("signal_scan", _auto_rescan, "strategy settings changed")
        return out

    @api.post("/api/strategies/{sid}/toggle")
    def strategy_toggle(sid: int, body: StrategyRequest) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        return _strategy_call(strategies.set_enabled, sid, bool(body.enabled), rescan=bool(body.enabled))

    @api.post("/api/strategies/{sid}/portfolios")
    def strategy_portfolios(sid: int, body: StrategyRequest) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        out = _strategy_call(strategies.set_portfolios, sid, body.portfolios or [])
        if out["added"] and out["enabled"]:
            _background("signal_scan", _auto_rescan, "strategy portfolios changed")
        return out

    @api.post("/api/strategies/{sid}/delete")
    def strategy_delete(sid: int, body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        return _strategy_call(strategies.delete, sid)

    # ---------- Journal / Learning (Phase 3) ----------

    @api.get("/api/journal")
    def journal_list(account: str | None = None, symbol: str | None = None, result: str | None = None,
                     exit_reason: str | None = None, band: str | None = None, start: str | None = None,
                     end: str | None = None) -> dict[str, Any]:
        rows = journal.entries(account=account, symbol=symbol, result=result, exit_reason=exit_reason, band=band,
                               start=start, end=end)
        with connect() as conn:
            pending = conn.execute("SELECT COUNT(*) FROM trades t LEFT JOIN journal j ON j.kind = 'trade' AND "
                                   "j.ref = CAST(t.id AS TEXT) WHERE t.state = 'CLOSED' AND t.entry_price IS NOT NULL "
                                   "AND j.id IS NULL").fetchone()[0]
        return {"entries": rows, "not_journaled": pending, "llm": llm.status()}

    @api.get("/api/journal/export.csv")
    def journal_csv(account: str | None = None, symbol: str | None = None, result: str | None = None,
                    exit_reason: str | None = None, band: str | None = None, start: str | None = None,
                    end: str | None = None):
        import csv
        import io

        from fastapi.responses import Response
        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow(journal.CSV_COLUMNS)
        w.writerows(journal.csv_rows(account=account, symbol=symbol, result=result, exit_reason=exit_reason, band=band,
                                     start=start, end=end))
        name = f"journal_{now_ist().date().isoformat()}.csv"
        return Response(buf.getvalue().encode("utf-8-sig"), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @api.post("/api/journal/sync")
    def journal_sync(body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        _background("journal", journal.sync)
        return {"started": True}

    @api.get("/api/journal/weekly")
    def journal_weekly() -> dict[str, Any]:
        return {"reviews": weekly.reviews()}

    @api.post("/api/journal/weekly/run")
    def journal_weekly_run(body: Confirm) -> dict[str, Any]:
        """Rebuild this week's review now (the Saturday job does it automatically)."""
        _require_confirm(body)
        monday, _ = weekly.week_of(now_ist().date())
        _background("weekly_review", weekly.run, monday)
        return {"started": monday.isoformat()}

    @api.get("/api/learning")
    def learning_view() -> dict[str, Any]:
        return {"proposals": learning.listing(), "active": (learning.active() or {}).get("id"),
                "running": learning.running(), "versions": versions.listing(), "champion": versions.champion_version(),
                "strategies": [{k: r[k] for k in ("id", "name", "version", "enabled")} for r in strategies.active_rows()],
                "next_pick": (learning.pick_strategy() or {}).get("id"),
                "rules": {"min_trades": learning.MIN_TRADES, "folds": learning.FOLDS,
                          "validation_months": learning.VALIDATION_MONTHS, "challenger_sessions": learning.CHALLENGER_SESSIONS,
                          "capital": learning.CAPITAL, "min_live_trades": learning.MIN_LIVE_TRADES}}

    @api.post("/api/learning/propose")
    def learning_propose(body: ProposeRequest) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        if procs.any_alive("backtest-"):
            raise HTTPException(409, "A backtest is running: wait for it to finish first")
        try:
            pid = learning.create("on demand", body.strategy_id)
        except learning.LearningError as exc:
            raise HTTPException(409, str(exc)) from exc
        audit.user_action("learning_propose", f"Look for a proposal now (#{pid})")
        learning.launch(pid)
        return {"id": pid}

    @api.post("/api/learning/{pid}/approve")
    def learning_approve(pid: int, body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        try:
            out = learning.approve(pid)
        except learning.LearningError as exc:
            raise HTTPException(409, str(exc)) from exc
        _background("signal_scan", _auto_rescan, "new champion")
        return out

    @api.post("/api/learning/{pid}/reject")
    def learning_reject(pid: int, body: RejectRequest) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        try:
            return learning.reject(pid, body.note.strip()[:200])
        except learning.LearningError as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.post("/api/learning/rollback")
    def learning_rollback(body: RollbackRequest) -> dict[str, Any]:
        _require_confirm(Confirm(confirm=body.confirm))
        owner = strategies.of_version(body.version)
        if owner and owner["version"] == body.version:
            raise HTTPException(400, f"{body.version} is already {owner['name']}'s current version")
        try:
            versions.rollback(body.version)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from exc
        audit.user_action("strategy_rollback", f"{body.version} made current again"
                          + (f" for {owner['name']}" if owner else ""))
        publish("learning")
        _background("signal_scan", _auto_rescan, "strategy rollback")
        return {"champion": versions.champion_version()}

    @api.get("/api/integrations")
    def integrations() -> dict[str, Any]:
        return {"llm": llm.status(), "telegram": telegram.status()}

    @api.post("/api/notify/test")
    def notify_test(body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        if not telegram.configured():
            raise HTTPException(400, "Telegram is not set up: add TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID to .env and restart")
        try:
            telegram.send_now("Trading Agent: test message. Critical alerts and the daily EOD summary will arrive here.")
        except RuntimeError as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"sent": True}

    @api.post("/api/llm/test")
    def llm_test(body: Confirm) -> dict[str, Any]:
        _require_confirm(body)
        if llm.provider() == "template":
            raise HTTPException(400, "No LLM set up: explanations use the built-in template text")
        try:
            text, model = llm.complete("Reply with exactly: OK", max_tokens=10)
        except llm.LLMUnavailable as exc:
            raise HTTPException(502, str(exc)) from exc
        return {"ok": True, "model": model, "reply": text[:50]}

    # ---------- live updates ----------

    @api.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        if not auth.allowed("/ws", socket.cookies.get(auth.COOKIE)):
            await socket.close(code=1008)
            return
        await socket.accept()
        q = broadcaster.subscribe()
        try:
            await socket.send_text('{"topic":"hello"}')
            while True:
                msg = await q.get()
                await socket.send_text(msg)
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            broadcaster.unsubscribe(q)

    @api.exception_handler(ConfigError)
    async def config_error(_, exc: ConfigError):
        return JSONResponse({"detail": str(exc)}, status_code=500)

    # ---------- dashboard ----------

    if (DIST / "index.html").exists():
        api.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @api.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            candidate = (DIST / path).resolve()
            if path and candidate.is_file() and DIST.resolve() in candidate.parents and candidate.name != "index.html":
                return FileResponse(candidate)
            # the page itself is always re-checked, so a new build shows after a plain reload (its scripts have
            # new file names on every build, so they can be cached)
            return FileResponse(DIST / "index.html", headers={"Cache-Control": "no-cache"})
    else:
        @api.get("/", include_in_schema=False)
        def no_dashboard():
            return JSONResponse({"detail": "Dashboard not built. Run setup.bat (needs Node.js) or see README."})

    publish("startup")
    return api
