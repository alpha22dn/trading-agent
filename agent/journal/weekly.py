"""Weekly review (spec section 10): Saturday, per portfolio and combined.

- Win rate, expectancy in R, net P&L after charges, the week's return and its largest drawdown from peak.
- Breakdown by RR band and by sizing case (the week, and to date since each portfolio's last reset).
- Signals the agent skipped or that were outranked, with what they would have done (journal/counterfactual.py).
- Manual overrides, with the actual result against what the original plan would have done.
- An LLM narrative: what worked, what failed and one concrete suggested fix (template text until it succeeds).
Stored in the journal table as kind 'week', ref = the Monday.
"""
from __future__ import annotations

import json
import statistics
from datetime import date, timedelta
from typing import Any

from ..clock import ts
from ..db import connect
from ..events import publish
from . import counterfactual, trades as trade_journal


def week_of(d: date) -> tuple[date, date]:
    monday = d - timedelta(days=d.weekday())
    return monday, monday + timedelta(days=6)


def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    rs = [r["r_multiple"] for r in rows if r["r_multiple"] is not None]
    return {"closed": len(rows), "wins": sum(1 for r in rows if (r["net_pnl"] or 0) > 0),
            "win_pct": round(100 * sum(1 for r in rows if (r["net_pnl"] or 0) > 0) / len(rows), 1) if rows else None,
            "expectancy_r": round(statistics.mean(rs), 3) if rs else None,
            "net_pnl": round(sum(r["net_pnl"] or 0 for r in rows), 2), "charges": round(sum(r["charges"] or 0 for r in rows), 2)}


def _groups(rows: list[dict[str, Any]], key: str) -> dict[str, Any]:
    g: dict[str, list] = {}
    for r in rows:
        g.setdefault(str(r[key]), []).append(r)
    return {k: _stats(v) for k, v in sorted(g.items())}


def build(monday: date) -> dict[str, Any]:
    start, end = monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    with connect() as conn:
        accts = [dict(r) for r in conn.execute("SELECT id, name, capital, reset_at, strategy_version, archived FROM accounts "
                                               "ORDER BY capital")]
        closed = [dict(r) for r in conn.execute(
            "SELECT * FROM trades WHERE state = 'CLOSED' AND substr(closed_at, 1, 10) BETWEEN ? AND ?", (start, end))]
        eq = [dict(r) for r in conn.execute("SELECT account_id, date, equity, peak_equity FROM equity_daily WHERE date <= ? "
                                            "ORDER BY date", (end,))]
        skipped = [dict(r) for r in conn.execute(
            """SELECT c.account_id, c.skip_reason, s.id AS signal_id, s.date, s.symbol, s.entry_trigger, s.entry_limit, s.stop,
                      s.target, s.rr, s.rr_band, s.strategy_version
               FROM candidates c JOIN signals s ON s.id = c.signal_id
               WHERE c.taken = 0 AND s.date BETWEEN ? AND ? ORDER BY s.date, s.symbol""", (start, end))]
        overrides = [dict(r) | {"before": json.loads(r["before"]) if r["before"] else None,
                                "after": json.loads(r["after"]) if r["after"] else None}
                     for r in conn.execute("""SELECT o.*, t.account_id, t.symbol, t.entry_price, t.stop AS final_stop,
                                                     t.initial_stop, t.target, t.state, t.net_pnl, t.r_multiple
                                              FROM overrides o JOIN trades t ON t.id = o.trade_id
                                              WHERE substr(o.detected_at, 1, 10) BETWEEN ? AND ? ORDER BY o.id""",
                                           (start, end))]
    per: dict[str, Any] = {}
    for a in accts:
        if a["archived"] and not any(t["account_id"] == a["id"] for t in closed):
            continue
        mine = [t for t in closed if t["account_id"] == a["id"]]
        curve = [e for e in eq if e["account_id"] == a["id"]]
        before = [e for e in curve if e["date"] < start]
        week = [e for e in curve if e["date"] >= start]
        base = before[-1]["equity"] if before else (week[0]["equity"] if week else None)
        dd = max([(e["peak_equity"] - e["equity"]) / e["peak_equity"] * 100 for e in week if e["peak_equity"]] or [0])
        with connect() as conn:
            to_date = [dict(r) for r in conn.execute("SELECT * FROM trades WHERE account_id = ? AND state = 'CLOSED' AND "
                                                     "created_at >= COALESCE(?, '') AND substr(closed_at, 1, 10) <= ?",
                                                     (a["id"], a["reset_at"], end))]
        per[a["id"]] = {"name": a["name"], "capital": a["capital"], "strategy_version": a["strategy_version"],
                        "week": _stats(mine), "to_date": _stats(to_date),
                        "equity_start": base, "equity_end": week[-1]["equity"] if week else base,
                        "week_return_pct": round((week[-1]["equity"] / base - 1) * 100, 2) if week and base else None,
                        "max_drawdown_pct": round(dd, 2),
                        "by_band": _groups(mine, "rr_band"), "by_sizing": _groups(mine, "sizing_case"),
                        "by_band_to_date": _groups(to_date, "rr_band"), "by_sizing_to_date": _groups(to_date, "sizing_case")}
    # skipped / outranked: one counterfactual per signal (several portfolios may have skipped it)
    seen: dict[int, dict[str, Any]] = {}
    for s in skipped:
        if s["signal_id"] not in seen:
            seen[s["signal_id"]] = {k: s[k] for k in ("date", "symbol", "rr", "rr_band", "strategy_version")} | {
                "reasons": {}, "would_have": counterfactual.entry_plan(s["symbol"], s["date"], s["entry_trigger"],
                                                                         s["entry_limit"], s["stop"], s["target"])}
        seen[s["signal_id"]]["reasons"][s["account_id"]] = s["skip_reason"]
    skipped_out = list(seen.values())
    ov_out = []
    for o in overrides:
        system = None
        if o["change_type"] in ("stop_moved", "target_moved", "exit_cancelled", "manual_sell") and o["entry_price"]:
            orig_stop = o["before"] if o["change_type"] == "stop_moved" and isinstance(o["before"], (int, float)) else o["initial_stop"]
            orig_target = o["before"] if o["change_type"] == "target_moved" and isinstance(o["before"], (int, float)) else o["target"]
            system = counterfactual.hold_from(o["symbol"], o["detected_at"][:10], o["entry_price"], orig_stop, orig_target)
        ov_out.append({"account_id": o["account_id"], "symbol": o["symbol"], "change": o["change_type"], "before": o["before"],
                       "after": o["after"], "at": o["detected_at"], "actual": {"state": o["state"], "net_pnl": o["net_pnl"],
                                                                               "r": o["r_multiple"]},
                       "system_plan": system})
    combined = _stats(closed)
    combined["by_band"], combined["by_sizing"] = _groups(closed, "rr_band"), _groups(closed, "sizing_case")
    return {"week_start": start, "week_end": end, "accounts": per, "combined": combined,
            "skipped": skipped_out, "overrides": ov_out,
            "notes": "Skipped-signal and override outcomes are estimates on daily candles (stop first when both are "
                     "touched, no trailing or half exit), before charges."}


def template_text(m: dict[str, Any]) -> str:
    c = m["combined"]
    if not c["closed"]:
        head = "No trades closed this week."
    else:
        head = (f"{c['closed']} trade(s) closed, {c['wins']} won ({c['win_pct']}%), expectancy "
                f"{c['expectancy_r']:+.2f}R, net {'−' if c['net_pnl'] < 0 else ''}₹{abs(c['net_pnl']):,.0f} after "
                f"₹{c['charges']:,.0f} charges.")
    best = [f"{a}: {x['week_return_pct']:+.2f}%" for a, x in m["accounts"].items() if x["week_return_pct"] is not None]
    sk = [s for s in m["skipped"] if s["would_have"].get("outcome") in ("target", "stop")]
    miss = sum(1 for s in sk if s["would_have"]["outcome"] == "target")
    tail = (f" Of {len(sk)} skipped signal(s) with a known outcome, {miss} would have reached the target." if sk else "")
    ov = f" {len(m['overrides'])} manual change(s) this week." if m["overrides"] else ""
    return head + (" Week's return: " + ", ".join(best) + "." if best else "") + tail + ov


def prompt_for(m: dict[str, Any]) -> str:
    return ("Write this week's review of a rules-based swing-trading system that runs several paper portfolios on the "
            "same signals (different capital). In three short paragraphs: what worked, what failed, and exactly one "
            "concrete suggested fix (a single parameter or rule change, with the numbers that support it). Use only "
            "the numbers below; say plainly when the sample is too small to conclude anything. R = multiples of the "
            "initial risk.\n\nReview data (JSON):\n" + json.dumps(m, default=str))


def run(monday: date, use_llm: bool = True) -> dict[str, Any]:
    """Build (or rebuild) the review of the week starting `monday` and explain it."""
    m = build(monday)
    m["prompt"] = prompt_for({k: v for k, v in m.items() if k != "prompt"})
    now = ts()
    with connect() as conn:
        conn.execute("""INSERT INTO journal (kind, ref, account_id, metrics, narrative, narrative_source, attempts, created_at, updated_at)
                        VALUES ('week', ?, NULL, ?, ?, 'template', 0, ?, ?)
                        ON CONFLICT(kind, ref) DO UPDATE SET metrics = excluded.metrics, narrative = excluded.narrative,
                        narrative_source = 'template', attempts = 0, last_error = NULL, updated_at = excluded.updated_at""",
                     (m["week_start"], json.dumps(m, default=str), template_text(m), now, now))
        jid = conn.execute("SELECT id FROM journal WHERE kind = 'week' AND ref = ?", (m["week_start"],)).fetchone()[0]
    explained = trade_journal.narrate(jid) if use_llm and trade_journal.llm.provider() != "template" else False
    publish("journal")
    return {"id": jid, "week_start": m["week_start"], "explained": explained}


def reviews() -> list[dict[str, Any]]:
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM journal WHERE kind = 'week' ORDER BY ref DESC")]
    out = []
    for r in rows:
        m = json.loads(r["metrics"])
        m.pop("prompt", None)
        out.append({"id": r["id"], "week_start": r["ref"], "metrics": m, "narrative": r["narrative"],
                    "narrative_source": r["narrative_source"], "model": r["model"], "last_error": r["last_error"],
                    "created_at": r["created_at"], "updated_at": r["updated_at"]})
    return out
