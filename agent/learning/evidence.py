"""Journal evidence for a parameter change (spec section 10, step 1). Code only: each rule looks at closed trades and
states its numbers; the strongest becomes the proposal. Excursions and what happened after an exit use daily candles.

Rules (each changes exactly one what-if setting, within the backtest's allowed range):
- stop_too_tight:   stop-outs whose price reached the target within 10 sessions after the stop → wider stop buffer
- target_too_far:   stop-outs that got at least 60% of the way to the target first → shorter target extension
- weak_band:        the lowest RR band traded loses on average → raise the minimum RR to the next band
- unfilled_buys:    many buy GTTs expire unfilled → wider entry limit buffer
- trail_gives_back: after the target, trades give back a lot of their best run → tighter trail buffer
"""
from __future__ import annotations

import statistics
from typing import Any

from ..db import connect

POST_EXIT_SESSIONS = 10
NEXT_BAND = {"C": ("min_rr", 2.0), "B": ("min_rr", 2.5)}


def _bars(conn, symbol: str, start: str, end: str | None = None, limit: int | None = None) -> list[tuple]:
    if end:
        return conn.execute("SELECT date, high, low, close FROM candles_daily WHERE symbol = ? AND date BETWEEN ? AND ? "
                            "ORDER BY date", (symbol, start, end)).fetchall()
    return conn.execute("SELECT date, high, low, close FROM candles_daily WHERE symbol = ? AND date > ? ORDER BY date "
                        "LIMIT ?", (symbol, start, limit or POST_EXIT_SESSIONS)).fetchall()


def facts(trades: list[dict[str, Any]]) -> dict[str, Any]:
    """Normalise closed / unfilled trades (live trades rows or backtest report rows) and add excursions."""
    import json
    closed, unfilled = [], 0
    with connect() as conn:
        for t in trades:
            if t.get("entry_price") is None:
                if t.get("state") == "CANCELLED" and t.get("exit_reason") in ("expired", "buy_cancelled"):
                    unfilled += 1
                continue
            if t.get("state") != "CLOSED":
                continue
            exits = t["exits"] if isinstance(t["exits"], list) else json.loads(t["exits"] or "[]")
            if not exits or not t.get("initial_stop"):
                continue
            entry, stop0, target = float(t["entry_price"]), float(t["initial_stop"]), float(t["target"])
            d0, d1 = t["entry_at"][:10], (exits[-1].get("at") or t.get("closed_at") or "")[:10]
            held = _bars(conn, t["symbol"], d0, d1)
            after = _bars(conn, t["symbol"], d1)
            risk = entry - stop0
            hi = max((b[1] for b in held), default=entry)
            closed.append({
                "symbol": t["symbol"], "band": t.get("rr_band"), "exit_reason": t.get("exit_reason"),
                "target_reached": any(e.get("reason") == "target" for e in exits) or bool(t.get("target_hit_on")),
                "r": t.get("r_multiple"), "entry": entry, "target": target, "risk": risk,
                "mfe_r": round((hi - entry) / risk, 2) if risk > 0 else None,
                "to_target": round((hi - entry) / (target - entry), 2) if target > entry else None,
                "after_high": max((b[1] for b in after), default=None),
            })
    return {"closed": closed, "unfilled": unfilled}


def candidates(f: dict[str, Any], params: dict[str, Any], bands_traded: list[str] | None = None) -> list[dict[str, Any]]:
    closed = f["closed"]
    lv, ex, en = params["levels"], params["exits"], params["entry"]
    out: list[dict[str, Any]] = []
    stopped = [t for t in closed if t["exit_reason"] == "stop" and not t["target_reached"]]

    later = [t for t in stopped if t["after_high"] is not None and t["after_high"] >= t["target"]]
    if stopped:
        share = len(later) / len(stopped)
        cur = float(lv["stop_buffer_pct"])
        out.append({"rule": "stop_too_tight", "key": "stop_buffer_pct", "from": cur, "to": round(min(5.0, cur + 0.5), 2),
                    "share": share, "n": len(stopped), "met": len(stopped) >= 5 and share >= 0.25,
                    "numbers": {"stop_outs": len(stopped), "reached_target_after": len(later)},
                    "text": f"{len(later)} of {len(stopped)} stop-outs ({share:.0%}) reached the target within "
                            f"{POST_EXIT_SESSIONS} sessions after the stop: the stop may sit too close."})

    near = [t for t in stopped if (t["to_target"] or 0) >= 0.6]
    if stopped:
        share = len(near) / len(stopped)
        cur = float(lv["target_extension"])
        out.append({"rule": "target_too_far", "key": "target_extension", "from": cur, "to": round(max(0.5, cur - 0.25), 2),
                    "share": share, "n": len(stopped), "met": len(stopped) >= 5 and share >= 0.25,
                    "numbers": {"stop_outs": len(stopped), "got_60pct_to_target": len(near)},
                    "text": f"{len(near)} of {len(stopped)} stop-outs ({share:.0%}) first got at least 60% of the way "
                            "to the target: a nearer target would have banked part of those moves."})

    by_band: dict[str, list[float]] = {}
    for t in closed:
        if t["r"] is not None and t["band"]:
            by_band.setdefault(t["band"], []).append(t["r"])
    lowest = max((b for b in by_band if b in NEXT_BAND), default=None)   # 'C' before 'B'
    if lowest:
        rs = by_band[lowest]
        avg = statistics.mean(rs)
        key, to = NEXT_BAND[lowest]
        cur = float(lv["min_rr"])
        if to > cur:
            out.append({"rule": "weak_band", "key": key, "from": cur, "to": to,
                        "share": max(0.0, min(1.0, -avg)), "n": len(rs), "met": len(rs) >= 8 and avg <= -0.25,
                        "numbers": {"band": lowest, "trades": len(rs), "avg_r": round(avg, 3),
                                    "other_bands_avg_r": {b: round(statistics.mean(v), 3) for b, v in by_band.items() if b != lowest}},
                        "text": f"Band {lowest} trades averaged {avg:+.2f}R over {len(rs)} trades: trading only RR ≥ {to:g} "
                                "drops them."})

    placed = len(closed) + f["unfilled"]
    if placed:
        share = f["unfilled"] / placed
        cur = float(lv["entry_limit_buffer_pct"])
        out.append({"rule": "unfilled_buys", "key": "entry_limit_buffer_pct", "from": cur, "to": round(min(3.0, cur + 0.25), 2),
                    "share": share, "n": placed, "met": placed >= 8 and share >= 0.35,
                    "numbers": {"buys_placed": placed, "unfilled": f["unfilled"]},
                    "text": f"{f['unfilled']} of {placed} buy GTTs ({share:.0%}) expired unfilled: a slightly higher limit "
                            "would catch more breakouts that gap past it."})

    trailed = [t for t in closed if t["target_reached"] and t["r"] is not None and t["mfe_r"] is not None]
    if trailed:
        give = statistics.mean(t["mfe_r"] - t["r"] for t in trailed)
        cur = float(ex["trail_buffer_pct"])
        if cur > 0.25:
            out.append({"rule": "trail_gives_back", "key": "trail_buffer_pct", "from": cur, "to": round(max(0.25, cur - 0.25), 2),
                        "share": max(0.0, min(1.0, give / 3)), "n": len(trailed), "met": len(trailed) >= 5 and give >= 1.5,
                        "numbers": {"trailed_trades": len(trailed), "avg_give_back_r": round(give, 2)},
                        "text": f"Trades that reached the target gave back {give:.2f}R on average from their best point "
                                "before the trailed stop closed them: a tighter trail keeps more."})
    for c in out:
        c["strength"] = round(c["share"] * min(1.0, c["n"] / 10), 3)
        c["from"] = float(c["from"])
    return sorted(out, key=lambda c: (c["met"], c["strength"]), reverse=True)


def best(f: dict[str, Any], params: dict[str, Any]) -> dict[str, Any] | None:
    """The single proposal: the strongest rule whose threshold is met, else the strongest overall (marked weak) so
    validation can decide. None when there is nothing to learn from yet."""
    cands = [c for c in candidates(f, params) if c["to"] != c["from"]]
    if not cands or not f["closed"]:
        return None
    return cands[0] | {"evidence": "strong" if cands[0]["met"] else "weak"}
