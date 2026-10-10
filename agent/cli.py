"""Command-line entry point.

    python -m agent.cli init      create/upgrade the database and check config
    python -m agent.cli serve     run backend + scheduler + dashboard on 127.0.0.1:8000
    python -m agent.cli status    print session, data coverage and job status
    python -m agent.cli backup    back up the database now
    python -m agent.cli history   recompute indicators and run the 2-year signal scan (Phase 1 check)
    python -m agent.cli results   fetch NSE results calendar (upcoming + 3-year history)
"""
from __future__ import annotations

import argparse
import time
import json
import logging
import sys
from logging.handlers import RotatingFileHandler

from .settings import RUNTIME_DIR, ConfigError, get_app_config, get_secrets, load_strategy_config


def setup_logging() -> None:
    (RUNTIME_DIR / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    fh = RotatingFileHandler(RUNTIME_DIR / "logs" / "agent.log", maxBytes=5_000_000, backupCount=5,
                             encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.handlers = [fh, sh]
    # kiteconnect logs full request URLs at DEBUG; keep it quiet so tokens never reach the log
    logging.getLogger("kiteconnect").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def cmd_init() -> int:
    from .db import current_db_path, init_db
    if sys.version_info < (3, 12):
        print(f"Note: Python {sys.version.split()[0]} detected; the spec targets 3.12 or 3.13.")
    app = get_app_config()
    strategy = load_strategy_config(app["strategy"]["active_version"])
    init_db(app, strategy)
    secrets = get_secrets()
    print(f"Database ready: {current_db_path()}")
    print(f"Strategy: {strategy.version} ({strategy.fingerprint})   Broker mode: {app['broker']['mode']}")
    if not secrets.kite_configured:
        print("\nNext: put KITE_API_KEY and KITE_API_SECRET in .env")
    print(f"Kite redirect URL to register in the Kite developer console:\n  {secrets.redirect_url}")
    return 0


def cmd_serve() -> int:
    import uvicorn
    from .api.app import create_app
    secrets = get_secrets()
    if secrets.host not in ("127.0.0.1", "localhost"):
        print("Refusing to bind to a non-local address. Set HOST=127.0.0.1 in .env.")
        return 2
    # On a server the dashboard is reached through a proxy (Tailscale): never without a sign-in (fail closed).
    from .api import auth
    if secrets.public_url and not auth.required():
        print("PUBLIC_URL is set, so the dashboard is reachable from other devices: set DASHBOARD_AUTH=required in .env.")
        return 2
    if auth.required() and not auth.configured():
        print("DASHBOARD_AUTH=required but no password is set yet. Run: python -m agent.cli set-password")
        return 2
    app = create_app()
    print(f"\n  Dashboard: http://{secrets.host}:{secrets.port}\n")
    uvicorn.run(app, host=secrets.host, port=secrets.port, log_level="warning")
    return 0


def cmd_set_password() -> int:
    """Set (or change) the dashboard password. Changing it signs every device out."""
    import getpass
    from .api import auth
    while True:
        p1 = getpass.getpass("New dashboard password (10+ characters): ")
        if p1 != getpass.getpass("Again: "):
            print("They don't match; try again.")
            continue
        try:
            auth.set_password(p1)
        except ValueError as exc:
            print(exc)
            continue
        break
    print("Password set. Restart the agent if it is running (every device is signed out).")
    return 0


def cmd_status() -> int:
    from .broker.kite_session import status
    from .data.candles import coverage
    from .db import get_state, init_db
    app = get_app_config()
    init_db(app, load_strategy_config(app["strategy"]["active_version"]))
    cov = coverage()
    cov["stale"], cov["missing"] = len(cov["stale"]), len(cov["missing"])
    print(json.dumps({"session": status(), "coverage": cov, "jobs": get_state("job_runs", {})}, indent=2))
    return 0


def cmd_backup() -> int:
    from .db import backup_db, init_db
    app = get_app_config()
    init_db(app, load_strategy_config(app["strategy"]["active_version"]))
    path = backup_db(RUNTIME_DIR / "backups", int(app["backup"]["keep_days"]))
    print(f"Backup written: {path}")
    return 0


def _ready():
    from .db import init_db
    app = get_app_config()
    strategy = load_strategy_config(app["strategy"]["active_version"])
    init_db(app, strategy)
    return strategy


def cmd_history() -> int:
    from .strategy import indicators, scanner
    strategy = _ready()
    print("Computing indicators...")
    print(indicators.compute_all(strategy.params))
    s = scanner.scan_history(strategy)
    print(f"\n{'Month':<9}{'Triggers':>9}{'RR>=1.5':>9}{'Liquid':>8}{'Market':>8}{'Tradeable':>10}")
    for m in s["months"]:
        print(f"{m['month']:<9}{m['triggers']:>9}{m['rr_ok']:>9}{m['liquid']:>8}{m['market_ok']:>8}{m['tradeable']:>10}")
    t = s["totals"]
    print(f"{'Total':<9}{t['triggers']:>9}{t['rr_ok']:>9}{t['liquid']:>8}{t['market_ok']:>8}{t['tradeable']:>10}")
    if not s.get("results_history_from"):
        print("\nResults history not loaded: the results blackout was not applied. Run: python -m agent.cli results")
    return 0


def cmd_results() -> int:
    from .data import events
    _ready()
    print("Upcoming:", events.fetch_daily())
    print("History:", events.fetch_history())
    return 0


def cmd_replay(start: str | None, end: str | None, learn: bool = False) -> int:
    """Replay a date range through the live engine on a separate database and print the results."""
    from datetime import date, timedelta

    from . import replay
    from .db import current_db_path
    _ready()
    source = current_db_path()
    end = end or (date.today() - timedelta(days=1)).isoformat()
    start = start or (date.fromisoformat(end) - timedelta(days=365)).isoformat()
    print(f"Replaying {start} → {end} (live database is only read; results go to runtime/replays/)")
    t0 = time.monotonic()

    def progress(day: str, i: int, n: int) -> None:
        if i == 1 or i % 20 == 0 or i == n:
            print(f"  {day}  session {i}/{n}")

    rep = replay.run(start, end, source_db=source, progress=progress)
    print(f"\nDone in {time.monotonic() - t0:.0f}s. {rep['sessions']} sessions. Report: {rep['db'].replace('.db', '.json')}")
    print("Watchlist and valuation gates are skipped (no historical fundamentals). Intraday path assumed "
          "open → low → high → close on up days, open → high → low → close on down days.\n")
    print("A drawdown halt is re-armed the same evening in a replay (nobody is there to do it); halts are counted.\n")
    print(f"{'Account':<8}{'Return':>9}{'Max DD':>8}{'Closed':>8}{'Win %':>7}{'Avg R':>8}{'Net P&L':>12}{'Charges':>10}  Halts")
    for acc, a in rep["accounts"].items():
        print(f"{acc:<8}{a['return_pct']:>8.2f}%{a['max_drawdown_pct']:>7.2f}%{a['closed']:>8}"
              f"{(a['win_pct'] if a['win_pct'] is not None else 0):>7.1f}{(a['avg_r'] if a['avg_r'] is not None else 0):>8.2f}"
              f"{a['net_pnl']:>12,.0f}{a['charges']:>10,.0f}  {len(a['halts'])}")
    j = rep.get("journal")
    if j:
        print(f"\nJournal: {j['trades']} of {j['closed_trades']} closed trades journaled, {j['weeks']} weekly review(s).")
    if learn:
        from pathlib import Path

        from .db import set_db_path
        from .learning import loop
        out = Path(rep["db"])
        set_db_path(out)
        pid = loop.create("replay")
        print(f"\nLearning proposal {pid} from the replay's trades (backtests use the live 1-minute store)…")
        try:
            loop.analyse(pid, out, data_db=source)
        except Exception as exc:  # noqa: BLE001 - recorded on the proposal
            print(f"  failed: {exc}")
        p = loop._row(pid)
        print(f"Proposal {pid}: {p['status']} - {p['message']}")
        if p.get("param"):
            print(f"  {p['param']} {p['from_value']:g} -> {p['to_value']:g} ({p['evidence']['rule']}, "
                  f"{p['evidence']['evidence']} evidence): {p['evidence']['text']}")
        if p.get("validation"):
            v = p["validation"]
            print(f"  Walk-forward: challenger {v['challenger']['closed']} trades, net {v['challenger']['net_pnl']:,.0f}, "
                  f"avg R {v['challenger']['avg_r']} vs champion {v['champion']['closed']} trades, net "
                  f"{v['champion']['net_pnl']:,.0f}, avg R {v['champion']['avg_r']}; better in {v['periods_better']} "
                  f"of {len(v['folds'])} periods")
        set_db_path(source)
    return 0


def cmd_backtest_run(run_id: int, live_db: str) -> int:
    """Started by the dashboard's Backtest tab in its own process; results go to the backtest_runs table."""
    from pathlib import Path

    from . import backtest
    from .db import set_db_path
    set_db_path(Path(live_db))
    try:
        backtest.run(run_id, Path(live_db))
    except Exception as exc:  # noqa: BLE001 - recorded on the run by backtest.run; exit non-zero for the log
        print(f"Backtest {run_id} failed: {exc}")
        return 1
    return 0


def cmd_learning_run(proposal_id: int, live_db: str) -> int:
    """Started by the Learning screen / weekly review in its own process; results go to the proposals table."""
    from pathlib import Path

    from .learning import loop
    try:
        loop.analyse(proposal_id, Path(live_db))
    except Exception as exc:  # noqa: BLE001 - recorded on the proposal by loop.analyse
        print(f"Learning proposal {proposal_id} failed: {exc}")
        return 1
    return 0


def cmd_learn() -> int:
    """Look for a proposal now, in this console (the dashboard's "Look for a proposal now" does the same in the
    background). Prints the outcome."""
    from .db import current_db_path
    from .learning import loop
    _ready()
    pid = loop.create("manual (console)")
    code = cmd_learning_run(pid, str(current_db_path()))
    p = loop._row(pid)
    print(f"Proposal {pid}: {p['status']} - {p['message']}")
    if p.get("param"):
        print(f"  {p['param']} {p['from_value']:g} -> {p['to_value']:g} ({p['evidence']['evidence']} evidence)")
    return code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent")
    parser.add_argument("command", choices=["init", "serve", "status", "backup", "history", "results", "replay",
                                            "backtest-run", "learning-run", "learn", "set-password"])
    parser.add_argument("--run-id", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--live-db", help=argparse.SUPPRESS)
    parser.add_argument("--proposal-id", type=int, help=argparse.SUPPRESS)
    parser.add_argument("--from", dest="start", help="replay: first date (YYYY-MM-DD), default one year back")
    parser.add_argument("--to", dest="end", help="replay: last date (YYYY-MM-DD), default yesterday")
    parser.add_argument("--learn", action="store_true", help="replay: then look for one learning proposal")
    args = parser.parse_args(argv)
    setup_logging()
    try:
        if args.command == "replay":
            return cmd_replay(args.start, args.end, args.learn)
        if args.command == "backtest-run":
            return cmd_backtest_run(args.run_id, args.live_db)
        if args.command == "learning-run":
            return cmd_learning_run(args.proposal_id, args.live_db)
        if args.command == "learn":
            return cmd_learn()
        return {"init": cmd_init, "serve": cmd_serve, "status": cmd_status, "backup": cmd_backup,
                "history": cmd_history, "results": cmd_results, "set-password": cmd_set_password}[args.command]()
    except ConfigError as exc:
        print(f"\nConfig problem: {exc}\n")
        return 2


if __name__ == "__main__":
    sys.exit(main())
