"""Controlled learning loop (spec section 10), decided 2026-09-29.

1. Propose: with the weekly review (Saturday) or on demand, at most one proposal at a time, for one strategy
   (weekly: each ON strategy in turn, decided 2026-09-30). Evidence comes from the journal's closed trades on
   that strategy's current version (the "champion" below); while there are fewer than MIN_LIVE_TRADES, a baseline backtest of the
   last months with the live settings supplies the trades (learning/evidence.py picks one parameter change).
2. Validate: walk-forward on the 1-minute store: FOLDS consecutive periods, champion vs challenger, ₹1,00,000 each.
   Passes when the challenger closes at least MIN_TRADES trades in total, beats the champion's net P&L and average R
   overall, and does better in at least half of the periods. The change was not fitted to these periods; they test
   whether it holds up over time.
3. Challenger: a ₹1,00,000 challenger portfolio trades the new version (paper) for at least CHALLENGER_SESSIONS.
4. Decision: you approve (the version becomes the strategy's current version, with its own YAML file) or reject.
5. Rollback: any earlier version of a strategy can be made current again with one click (strategy/versions.py).

Analysis and validation run in their own process (python -m agent.cli learning-run), like backtests.
"""
from __future__ import annotations

import copy
import json
import statistics
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .. import audit
from ..clock import now_ist, ts
from ..db import connect, current_db_path, set_db_path
from ..events import publish
from ..market_calendar import MarketCalendar
from ..settings import get_app_config
from . import evidence as ev

MIN_LIVE_TRADES = 20
MIN_TRADES = 30
FOLDS = 4
VALIDATION_MONTHS = 12
BASELINE_MONTHS = 6
CAPITAL = 100000.0
CHALLENGER_SESSIONS = 15          # three weeks of sessions
ACTIVE = ("analysing", "validating", "challenger", "decision")


class LearningError(ValueError):
    pass


# ---------- bookkeeping ----------

def _row(pid: int) -> dict[str, Any]:
    with connect() as conn:
        r = conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone()
    if not r:
        raise LearningError(f"No proposal {pid}")
    d = dict(r)
    for k in ("evidence", "validation"):
        d[k] = json.loads(d[k]) if d[k] else None
    return d


def _update(pid: int, **fields: Any) -> None:
    for k in ("evidence", "validation"):
        if k in fields and not isinstance(fields[k], (str, type(None))):
            fields[k] = json.dumps(fields[k], default=str)
    fields["updated_at"] = ts()
    with connect() as conn:
        conn.execute(f"UPDATE proposals SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", (*fields.values(), pid))
    publish("learning")


def active() -> dict[str, Any] | None:
    with connect() as conn:
        r = conn.execute(f"SELECT id FROM proposals WHERE status IN ({','.join('?' * len(ACTIVE))}) ORDER BY id DESC LIMIT 1",
                         ACTIVE).fetchone()
    return _row(r[0]) if r else None


def pick_strategy() -> dict[str, Any] | None:
    """Each ON strategy in turn: the one with the most trades closed since its last proposal (all its closed trades
    if it never had one); ties go to the one never reviewed or reviewed longest ago, then list order."""
    from ..strategy import strategies
    ranked = []
    for r in strategies.active_rows():
        if not r["enabled"] or r["kind"] != "momentum_breakout":
            continue   # the evidence rules and validation backtests are built for the momentum rule type
        vs = strategies.versions_of(r["id"])
        with connect() as conn:
            last = conn.execute("SELECT MAX(created_at) FROM proposals WHERE strategy_id = ?", (r["id"],)).fetchone()[0]
            n = conn.execute(f"SELECT COUNT(*) FROM trades WHERE state = 'CLOSED' AND entry_price IS NOT NULL AND "
                             f"strategy_version IN ({','.join('?' * len(vs))}) AND closed_at > ?",
                             (*vs, last or "")).fetchone()[0] if vs else 0
        ranked.append(((-n, last or "", r["position"], r["id"]), r))
    return min(ranked, key=lambda x: x[0])[1] if ranked else None


def create(trigger: str, strategy_id: int | None = None) -> int:
    if active():
        raise LearningError("A proposal is already being analysed, validated or run as a challenger")
    from ..strategy import strategies
    if strategy_id is None:
        st = pick_strategy()
        if not st:
            raise LearningError("No momentum strategy is ON: turn one on on the Strategies screen")
    else:
        try:
            st = strategies._row(strategy_id)
        except strategies.StrategyError as exc:
            raise LearningError(str(exc)) from exc
        if st["kind"] != "momentum_breakout":
            raise LearningError(f"Learning for {st['name']} is not built yet: its evidence rules and validation "
                                "backtests exist only for momentum strategies")
    with connect() as conn:
        cur = conn.execute("INSERT INTO proposals (trigger, status, message, base_version, strategy_id, created_at, "
                           "updated_at) VALUES (?, 'analysing', 'Gathering evidence…', ?, ?, ?, ?)",
                           (trigger, st["version"], st["id"], ts(), ts()))
        pid = int(cur.lastrowid)
    audit.record("job", "learning", f"Proposal {pid} started for {st['name']} ({trigger})")
    publish("learning")
    return pid


def launch(pid: int) -> None:
    from .. import procs
    procs.spawn(f"learning-{pid}", ["learning-run", "--proposal-id", str(pid), "--live-db", str(current_db_path())],
                f"learning_{pid}")


def running() -> bool:
    from .. import procs
    return procs.any_alive("learning-")


# ---------- analysis (in its own process) ----------

def _coverage(live_db: Path) -> tuple[date, date]:
    from .. import backtest
    cov = backtest.coverage(live_db)
    if not cov.get("from"):
        raise LearningError("No 1-minute data: download it on the Overview screen first")
    return date.fromisoformat(cov["from"]), date.fromisoformat(cov["to"])


def _backtest(live_db: Path, start: date, end: date, overrides: dict[str, float], pid: int, label: str) -> dict[str, Any]:
    """One in-process backtest (this is already the learning worker's own process)."""
    from .. import backtest
    cfg = backtest.validate({"start": start.isoformat(), "end": end.isoformat(), "capital": CAPITAL,
                             "overrides": overrides, "name": f"Learning #{pid}: {label}",
                             "strategy": _row(pid)["base_version"]}, live_db)
    cfg["purpose"], cfg["proposal_id"] = "learning", pid
    rid = backtest.create_run(live_db, cfg)
    backtest.run(rid, live_db)
    r = backtest.get_run(live_db, rid)
    if r["status"] != "done":
        raise LearningError(f"Backtest for {label} did not finish: {r['message']}")
    rep = r["report"]
    return {"run_id": rid, "account": rep["accounts"]["Backtest"], "trades": rep["trades"], "sessions": rep["sessions"]}


def _live_trades(champion: str) -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM trades WHERE strategy_version = ? AND "
                                              "(state = 'CLOSED' OR (state = 'CANCELLED' AND entry_price IS NULL))",
                                              (champion,))]


def gather(pid: int, live_db: Path, data_db: Path | None = None) -> dict[str, Any]:
    from ..strategy import versions
    champ = versions.load(_row(pid)["base_version"])
    trades = _live_trades(champ.version)
    source = {"kind": "live", "trades": sum(1 for t in trades if t["state"] == "CLOSED")}
    if source["trades"] < MIN_LIVE_TRADES:
        lo, hi = _coverage(data_db or live_db)
        start = max(lo, hi - timedelta(days=30 * BASELINE_MONTHS))
        _update(pid, message=f"Only {source['trades']} closed live trades: running a baseline backtest "
                             f"{start} → {hi} for evidence…")
        bt = _backtest(data_db or live_db, start, hi, {}, pid, "baseline")
        set_db_path(live_db)
        trades = trades + bt["trades"]
        source = {"kind": "live + baseline backtest", "live_trades": source["trades"], "backtest_run": bt["run_id"],
                  "backtest_period": [start.isoformat(), hi.isoformat()], "backtest_trades": bt["account"]["closed"]}
    return {"facts": ev.facts(trades), "source": source}


def validate(pid: int, live_db: Path, overrides: dict[str, float], data_db: Path | None = None) -> dict[str, Any]:
    data_db = data_db or live_db
    lo, hi = _coverage(data_db)
    start = max(lo, hi - timedelta(days=30 * VALIDATION_MONTHS))
    span = (hi - start).days + 1
    folds = []
    for k in range(FOLDS):
        a = start + timedelta(days=span * k // FOLDS)
        b = start + timedelta(days=span * (k + 1) // FOLDS - 1)
        set_db_path(live_db)
        _update(pid, message=f"Validating: period {k + 1} of {FOLDS} ({a} → {b})…")
        champ = _backtest(data_db, a, b, {}, pid, f"champion {a}→{b}")
        chal = _backtest(data_db, a, b, overrides, pid, f"challenger {a}→{b}")
        folds.append({"start": a.isoformat(), "end": b.isoformat(),
                      "champion": _metrics(champ), "challenger": _metrics(chal)})
    return judge(folds)


def _metrics(bt: dict[str, Any]) -> dict[str, Any]:
    a = bt["account"]
    return {k: a[k] for k in ("return_pct", "max_drawdown_pct", "closed", "win_pct", "avg_r", "net_pnl", "charges")} | {
        "run_id": bt["run_id"], "rs": [t["r_multiple"] for t in bt["trades"] if t["r_multiple"] is not None]}


def judge(folds: list[dict[str, Any]]) -> dict[str, Any]:
    def tot(side: str) -> dict[str, Any]:
        rs = [r for f in folds for r in f[side]["rs"]]
        return {"closed": sum(f[side]["closed"] for f in folds), "net_pnl": round(sum(f[side]["net_pnl"] for f in folds), 2),
                "avg_r": round(statistics.mean(rs), 3) if rs else None}
    ch, cp = tot("challenger"), tot("champion")
    better = sum(1 for f in folds if f["challenger"]["net_pnl"] > f["champion"]["net_pnl"])
    need = -(-len(folds) // 2)
    reasons = []
    if ch["closed"] < MIN_TRADES:
        reasons.append(f"only {ch['closed']} challenger trades (needs {MIN_TRADES})")
    if not ch["net_pnl"] > cp["net_pnl"]:
        reasons.append(f"net P&L ₹{ch['net_pnl']:,.0f} not above the champion's ₹{cp['net_pnl']:,.0f}")
    if ch["avg_r"] is None or (cp["avg_r"] is not None and ch["avg_r"] < cp["avg_r"]):
        reasons.append(f"average R {ch['avg_r']} below the champion's {cp['avg_r']}")
    if better < need:
        reasons.append(f"better in {better} of {len(folds)} periods (needs {need})")
    for f in folds:
        for side in ("champion", "challenger"):
            f[side].pop("rs", None)
    return {"folds": folds, "challenger": ch, "champion": cp, "periods_better": better, "passed": not reasons,
            "reasons": reasons}


def explain(p: dict[str, Any]) -> str:
    from .. import backtest
    e = p["evidence"]
    label = backtest.WHATIFS[p["param"]][1] if p["param"] in backtest.WHATIFS else p["param"]
    text = (f"{label}: {p['from_value']:g} → {p['to_value']:g}. {e['text']} The evidence is {e['evidence']}"
            + (" (the rule's threshold is met)." if e["evidence"] == "strong" else
               " (below the rule's threshold): the walk-forward validation decides."))
    try:
        from ..journal import llm
        if llm.provider() != "template":
            t, _ = llm.complete("In 2 to 4 plain sentences, explain this proposed change to a swing-trading strategy and "
                                "the evidence for it; say whether the evidence is strong or thin. Use only these numbers:\n"
                                + json.dumps({k: p[k] for k in ("param", "from_value", "to_value", "evidence")}, default=str),
                                max_tokens=300)
            return t or text
    except Exception:  # noqa: BLE001 - the template stands
        pass
    return text


def analyse(pid: int, live_db: Path, data_db: Path | None = None) -> None:
    """The learning worker: evidence → one proposal → walk-forward validation → challenger (if it passes).
    data_db: where the backtests read candles and the 1-minute store (default live_db; a replay learns from its
    own trades but backtests on the live data it was copied from)."""
    from .. import backtest
    from ..strategy import versions
    data_db = data_db or live_db
    set_db_path(live_db)
    try:
        g = gather(pid, live_db, data_db)
        set_db_path(live_db)
        champ = versions.load(_row(pid)["base_version"])
        c = ev.best(g["facts"], champ.params)
        if c is None:
            _update(pid, status="no_proposal", message="No closed trades to learn from yet")
            return
        c["source"] = g["source"]
        c["all_rules"] = [{k: x[k] for k in ("rule", "key", "from", "to", "met", "strength", "text")}
                          for x in ev.candidates(g["facts"], champ.params)]
        allowed = backtest.WHATIFS[c["key"]]
        if not allowed[2] <= c["to"] <= allowed[3]:
            _update(pid, status="no_proposal", message=f"{c['key']} is already at its limit")
            return
        _update(pid, status="validating", param=c["key"], from_value=c["from"], to_value=c["to"], evidence=c,
                message="Validating (walk-forward)…")
        _update(pid, explanation=explain(_row(pid)))
        result = validate(pid, live_db, {c["key"]: c["to"]}, data_db)
        set_db_path(live_db)
        if not result["passed"]:
            _update(pid, status="failed", validation=result, message="Did not pass validation: " + "; ".join(result["reasons"]))
            audit.record("job", "learning", f"Proposal {pid} ({c['key']} → {c['to']:g}) failed validation")
            return
        _update(pid, validation=result, message="Passed validation")
        start_challenger(pid)
    except Exception as exc:  # noqa: BLE001 - recorded on the proposal
        set_db_path(live_db)
        _update(pid, status="error", message=f"{type(exc).__name__}: {exc}"[:500])
        raise
    finally:
        set_db_path(live_db)


# ---------- challenger ----------

def start_challenger(pid: int) -> None:
    from ..risk import portfolios
    from ..strategy import versions
    p = _row(pid)
    params = copy.deepcopy(versions.load(p["base_version"]).params)
    from .. import backtest
    path = backtest.WHATIFS[p["param"]][0]
    params[path[0]][path[1]] = int(p["to_value"]) if backtest.WHATIFS[p["param"]][4] == 1 else p["to_value"]
    if p["param"] == "min_rr":
        lowest = min(float(b["min"]) for b in params["ranking"]["rr_bands"])
        if p["to_value"] < lowest:
            params["ranking"]["rr_bands"].append({"band": "D", "min": p["to_value"]})
    v = versions.next_version(p["base_version"])
    versions.register(v, params)
    acc = f"CH{pid}"
    portfolios.create(acc, f"Challenger {v}", CAPITAL, strategy_ids=[])
    with connect() as conn:
        conn.execute("UPDATE accounts SET strategy_version = ? WHERE id = ?", (v, acc))
    first = MarketCalendar(get_app_config()).next_trading_day(now_ist().date()).isoformat()
    _update(pid, status="challenger", version=v, challenger_account=acc, challenger_started=first,
            message=f"Challenger {acc} trades {v} from {first}; decision after {CHALLENGER_SESSIONS} sessions")
    audit.alert("learning", f"Proposal {pid} passed validation: challenger portfolio {acc} (₹1,00,000) now trades {v} "
                f"({p['param']} {p['from_value']:g} → {p['to_value']:g}). Decide after {CHALLENGER_SESSIONS} sessions.",
                level="info")


def comparison(p: dict[str, Any]) -> dict[str, Any] | None:
    """Challenger vs champion portfolios since the challenger started (returns in %, so capital doesn't matter)."""
    if not p.get("challenger_account"):
        return None
    start = p["challenger_started"]
    cal = MarketCalendar(get_app_config())
    today = now_ist().date()
    last = cal.last_completed_session(now_ist())
    sessions = cal.trading_days_between(date.fromisoformat(start) - timedelta(days=1), last) if last.isoformat() >= start else 0
    out = {"sessions": sessions, "needed": CHALLENGER_SESSIONS, "since": start, "portfolios": {}}
    from ..strategy import strategies
    mine = set(strategies.portfolios_of(p["strategy_id"])) if p.get("strategy_id") else set()
    with connect() as conn:
        accts = [dict(r) for r in conn.execute("SELECT id, capital, strategy_version, archived FROM accounts")]
        for a in accts:
            if a["id"] != p["challenger_account"] and (a["archived"] or a["strategy_version"] or a["id"] not in mine):
                continue
            eq = conn.execute("SELECT date, equity FROM equity_daily WHERE account_id = ? AND date >= ? ORDER BY date",
                              (a["id"], (date.fromisoformat(start) - timedelta(days=7)).isoformat())).fetchall()
            before = [e for e in eq if e[0] < start]
            base = before[-1][1] if before else (a["capital"] if a["id"] == p["challenger_account"] else (eq[0][1] if eq else None))
            now = eq[-1][1] if eq else base
            tr = [dict(r) for r in conn.execute("SELECT net_pnl, r_multiple FROM trades WHERE account_id = ? AND state = 'CLOSED' "
                                                "AND substr(closed_at, 1, 10) >= ?", (a["id"], start))]
            rs = [t["r_multiple"] for t in tr if t["r_multiple"] is not None]
            out["portfolios"][a["id"]] = {
                "challenger": a["id"] == p["challenger_account"],
                "return_pct": round((now / base - 1) * 100, 2) if base else None,
                "closed": len(tr), "avg_r": round(statistics.mean(rs), 3) if rs else None,
                "net_pnl": round(sum(t["net_pnl"] or 0 for t in tr), 2),
                "curve": [{"date": e[0], "equity": e[1]} for e in eq if e[0] >= start], "base": base}
    out["today"] = today.isoformat()
    return out


def update_challenger() -> str | None:
    """At EOD: a challenger that has run long enough is ready for your decision."""
    p = active()
    if not p or p["status"] != "challenger":
        return None
    c = comparison(p)
    if c and c["sessions"] >= CHALLENGER_SESSIONS:
        _update(p["id"], status="decision", message=f"Ran {c['sessions']} sessions: ready for your decision")
        audit.alert("learning", f"Challenger {p['challenger_account']} ({p['version']}) has run {c['sessions']} sessions: "
                    "approve or reject it on the Learning screen.", level="warning")
        return "ready for decision"
    return f"challenger at {c['sessions'] if c else 0} of {CHALLENGER_SESSIONS} sessions"


def _end_challenger(p: dict[str, Any], note: str) -> None:
    from ..orders import manager
    acc = p.get("challenger_account")
    if acc:
        manager.close_account(acc, f"Challenger ended ({note})")
        with connect() as conn:
            conn.execute("UPDATE accounts SET archived = 1 WHERE id = ?", (acc,))


def approve(pid: int) -> dict[str, Any]:
    from ..strategy import versions
    p = _row(pid)
    if p["status"] not in ("challenger", "decision"):
        raise LearningError("Only a running challenger can be approved")
    _end_challenger(p, "approved")
    versions.promote(p["version"])
    _update(pid, status="approved", decided_at=ts(), message=f"{p['version']} is now the strategy's current version")
    audit.user_action("learning_approved", f"Proposal {pid}: {p['version']} made current "
                      f"({p['param']} {p['from_value']:g} → {p['to_value']:g})")
    return _row(pid)


def reject(pid: int, note: str = "") -> dict[str, Any]:
    p = _row(pid)
    if p["status"] not in ACTIVE + ("failed", "no_proposal", "error"):
        raise LearningError("This proposal is already decided")
    if p["status"] in ("analysing", "validating") and running():
        raise LearningError("Wait for the analysis to finish")
    _end_challenger(p, "rejected")
    if p.get("version"):
        with connect() as conn:
            conn.execute("UPDATE config_versions SET status = 'retired' WHERE version = ? AND status = 'challenger'",
                         (p["version"],))
    _update(pid, status="rejected", decided_at=ts(), message=("Rejected" + (f": {note}" if note else "")))
    audit.user_action("learning_rejected", f"Proposal {pid} rejected" + (f": {note}" if note else ""))
    return _row(pid)


def weekly_step() -> str:
    upd = update_challenger()
    p = active()
    if p:
        return f"proposal {p['id']} is {p['status']}" + (f" ({upd})" if upd else "")
    pid = create("weekly")
    launch(pid)
    return f"analysing proposal {pid}"


def listing() -> list[dict[str, Any]]:
    with connect() as conn:
        ids = [r[0] for r in conn.execute("SELECT id FROM proposals ORDER BY id DESC")]
    out = []
    for i in ids:
        p = _row(i)
        with connect() as conn:
            r = conn.execute("SELECT name FROM strategies WHERE id = ?", (p.get("strategy_id"),)).fetchone()
        p["strategy_name"] = r[0] if r else None
        p["comparison"] = comparison(p) if p["status"] in ("challenger", "decision") else None
        out.append(p)
    return out


def mark_interrupted() -> None:
    with connect() as conn:
        conn.execute("UPDATE proposals SET status = 'error', message = 'Stopped when the agent restarted; start a new one', "
                     "updated_at = ? WHERE status IN ('analysing', 'validating')", (ts(),))
