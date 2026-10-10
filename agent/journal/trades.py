"""Per-trade journal (spec section 10): every closed trade gets a record computed by code and a short explanation.

Record: account, strategy version, signal-day indicators, entry / stop / target, RR band, qty, sizing case, fills,
charges, net P&L, R-multiple, maximum favourable and adverse excursion (MFE / MAE), holding days, exit reason, the
stop's history and any manual overrides. Excursions use the 1-minute store between the fill and the last exit
when it has the bars, else daily candles.

The explanation comes from the LLM (journal/llm.py). Until that succeeds the entry carries a template text and is
retried at the next EOD run (at most MAX_ATTEMPTS times).
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from .. import audit
from ..clock import IST, ts
from ..db import connect, current_db_path
from ..events import publish
from ..market_calendar import MarketCalendar
from ..settings import get_app_config
from . import llm

MAX_ATTEMPTS = 3
REASONS = {"stop": "stop-loss", "target": "target", "manual_sell": "sold by hand", "gap_exit": "gap below the stop, "
           "sold at the open", "reset": "portfolio reset", "kill_switch": "kill switch"}


def _excursions(symbol: str, entry: float, start_iso: str, end_iso: str) -> dict[str, Any]:
    """Highest high and lowest low while the trade was open."""
    hi = lo = None
    source = None
    try:
        from ..data import minute
        mpath = current_db_path().with_name(current_db_path().stem + "_1m.db")
        if mpath.exists():
            with connect() as conn:
                tok = conn.execute("SELECT instrument_token FROM instruments WHERE symbol = ?", (symbol,)).fetchone()
            if tok:
                bars = minute.load(int(tok[0]), datetime.fromisoformat(start_iso), datetime.fromisoformat(end_iso))
                if bars:
                    hi, lo, source = max(b["high"] for b in bars), min(b["low"] for b in bars), "1-minute"
    except Exception:  # noqa: BLE001 - the daily candles below are the fallback
        hi = lo = None
    if hi is None:
        with connect() as conn:
            r = conn.execute("SELECT MAX(high), MIN(low) FROM candles_daily WHERE symbol = ? AND date BETWEEN ? AND ?",
                             (symbol, start_iso[:10], end_iso[:10])).fetchone()
        if r and r[0] is not None:
            hi, lo, source = float(r[0]), float(r[1]), "daily"
    if hi is None:
        return {"source": None}
    return {"source": source, "high": round(hi, 2), "low": round(lo, 2),
            "mfe": round(max(0.0, hi - entry), 2), "mae": round(max(0.0, entry - lo), 2),
            "mfe_pct": round(max(0.0, hi - entry) / entry * 100, 2), "mae_pct": round(max(0.0, entry - lo) / entry * 100, 2)}


def record(trade_id: int) -> dict[str, Any]:
    """The computed journal record of one closed trade (no LLM involved)."""
    with connect() as conn:
        t = dict(conn.execute("SELECT * FROM trades WHERE id = ?", (trade_id,)).fetchone())
        sig = conn.execute("SELECT date, indicators, rr, rr_band FROM signals WHERE id = ?", (t["signal_id"],)).fetchone()
        stops = [json.loads(r[0] or "[]") for r in conn.execute(
            "SELECT trigger_history FROM gtts WHERE trade_id = ? AND purpose = 'exit' ORDER BY id", (trade_id,))]
        overrides = [dict(r) for r in conn.execute("SELECT change_type, before, after, detected_at FROM overrides "
                                                   "WHERE trade_id = ? ORDER BY id", (trade_id,))]
    exits = json.loads(t["exits"] or "[]")
    entry = float(t["entry_price"])
    risk_ps = entry - float(t["initial_stop"])
    first_at, last_at = t["entry_at"], (exits[-1]["at"] if exits and exits[-1].get("at") else t["closed_at"])
    cal = MarketCalendar(get_app_config())
    holding = cal.trading_days_between(date.fromisoformat(first_at[:10]), date.fromisoformat(last_at[:10]))
    ex = _excursions(t["symbol"], entry, first_at, last_at)
    if ex.get("source") and risk_ps > 0:
        ex["mfe_r"], ex["mae_r"] = round(ex["mfe"] / risk_ps, 2), round(ex["mae"] / risk_ps, 2)
    bought = t["bought_qty"] or t["qty"]
    sold = sum(e["qty"] for e in exits)
    return {
        "trade_id": trade_id, "account_id": t["account_id"], "symbol": t["symbol"], "strategy_version": t["strategy_version"],
        "signal_date": sig["date"] if sig else None,
        "signal_indicators": json.loads(sig["indicators"] or "{}") if sig else {},
        "levels": {"entry_trigger": t["entry_trigger"], "entry_limit": t["entry_limit"], "initial_stop": t["initial_stop"],
                   "final_stop": t["stop"], "target": t["target"]},
        "rr": t["rr"], "rr_band": t["rr_band"], "qty": bought, "sizing_case": t["sizing_case"],
        "entry": {"price": entry, "at": first_at},
        "exits": [{"qty": e["qty"], "price": e["price"], "at": e.get("at"), "reason": e.get("reason"),
                   "charges": e.get("charges")} for e in exits],
        "avg_exit": round(sum(e["qty"] * e["price"] for e in exits) / sold, 2) if sold else None,
        "charges": t["charges"], "net_pnl": t["net_pnl"], "r_multiple": t["r_multiple"],
        "risk_per_share": round(risk_ps, 2), "risk_rupees": round(risk_ps * bought, 2),
        "excursion": ex, "holding_days": holding, "exit_reason": t["exit_reason"],
        "target_reached": t["target_hit_on"] is not None or any(e.get("reason") == "target" for e in exits),
        "stop_history": [h for hist in stops for h in hist],
        "overrides": overrides,
    }


def template_text(m: dict[str, Any]) -> str:
    """Plain sentences from the numbers: used until (or instead of) the LLM explanation."""
    ind = m.get("signal_indicators") or {}
    why = (f"Bought on the breakout signal of {m['signal_date']} (band {m['rr_band'] or '?'}, RR {m['rr']:.2f}"
           + (f", close {ind['close']:g}" if ind.get("close") else "") + ")") if m.get("rr") else "Bought on a breakout signal"
    ex = m["excursion"]
    path = (f" While open it went as high as +{ex['mfe_r']:.2f}R and as low as −{ex['mae_r']:.2f}R."
            if ex.get("mfe_r") is not None else "")
    how = REASONS.get(m["exit_reason"] or "", m["exit_reason"] or "closed")
    if m["target_reached"] and m["exit_reason"] == "stop":
        how = "target on half, then the trailed stop on the rest"
    res = (f"{'−' if m['net_pnl'] < 0 else ''}₹{abs(m['net_pnl']):,.0f}") if m["net_pnl"] is not None else "—"
    r = f" ({m['r_multiple']:+.2f}R)" if m["r_multiple"] is not None else ""
    ov = f" {len(m['overrides'])} manual change(s) were made to it." if m["overrides"] else ""
    return (f"{why}: {m['qty']} shares of {m['symbol']} at ₹{m['entry']['price']:,.2f}, stop ₹{m['levels']['initial_stop']:,.2f}, "
            f"target ₹{m['levels']['target']:,.2f}. It closed by {how} after {m['holding_days']} session(s) for {res} "
            f"net of ₹{m['charges'] or 0:,.0f} charges{r}.{path}{ov}")


def prompt_for(m: dict[str, Any]) -> str:
    return ("Explain this closed swing trade for the journal in 3 to 5 sentences: why it was taken (the signal), how "
            "it played out (use the excursion numbers: how far it went for and against the trade), why it closed, and "
            "one lesson if the numbers suggest one. R means multiples of the initial risk per share.\n\nTrade data "
            "(JSON):\n" + json.dumps(m, default=str))


def _upsert(m: dict[str, Any]) -> None:
    now = ts()
    with connect() as conn:
        conn.execute("""INSERT INTO journal (kind, ref, account_id, metrics, narrative, narrative_source, created_at, updated_at)
                        VALUES ('trade', ?, ?, ?, ?, 'template', ?, ?)
                        ON CONFLICT(kind, ref) DO UPDATE SET metrics = excluded.metrics, updated_at = excluded.updated_at""",
                     (str(m["trade_id"]), m["account_id"], json.dumps(m, default=str), template_text(m), now, now))


def narrate(entry_id: int) -> bool:
    with connect() as conn:
        r = conn.execute("SELECT kind, metrics, attempts FROM journal WHERE id = ?", (entry_id,)).fetchone()
    m = json.loads(r["metrics"])
    try:
        text, model = llm.complete(prompt_for(m) if r["kind"] == "trade" else m["prompt"],
                                   max_tokens=400 if r["kind"] == "trade" else 900)
    except llm.LLMUnavailable as exc:
        with connect() as conn:
            conn.execute("UPDATE journal SET attempts = attempts + 1, last_error = ?, updated_at = ? WHERE id = ?",
                         (str(exc)[:300], ts(), entry_id))
        return False
    with connect() as conn:
        conn.execute("UPDATE journal SET narrative = ?, narrative_source = 'llm', model = ?, attempts = attempts + 1, "
                     "last_error = NULL, updated_at = ? WHERE id = ?", (text, model, ts(), entry_id))
    return True


def sync(use_llm: bool = True) -> dict[str, int]:
    """Journal every closed trade not journaled yet; then (re)try LLM explanations still on the template."""
    with connect() as conn:
        todo = [r[0] for r in conn.execute("""SELECT t.id FROM trades t LEFT JOIN journal j ON j.kind = 'trade' AND j.ref = CAST(t.id AS TEXT)
                                              WHERE t.state = 'CLOSED' AND t.entry_price IS NOT NULL AND j.id IS NULL ORDER BY t.id""")]
    for tid in todo:
        _upsert(record(tid))
    written = failed = 0
    if use_llm and llm.provider() != "template":
        with connect() as conn:
            pending = [r[0] for r in conn.execute("SELECT id FROM journal WHERE narrative_source = 'template' AND "
                                                  "attempts < ? ORDER BY id", (MAX_ATTEMPTS,))]
        for jid in pending:
            if narrate(jid):
                written += 1
            else:
                failed += 1
                break   # the provider is down: stop for now, the next run retries
    if failed:
        with connect() as conn:
            err = conn.execute("SELECT last_error FROM journal WHERE last_error IS NOT NULL ORDER BY updated_at DESC "
                               "LIMIT 1").fetchone()
        audit.record("job", "journal", f"LLM explanations paused: {err[0] if err else 'unavailable'}", "warning")
    if todo or written:
        publish("journal")
    return {"journaled": len(todo), "explained": written, "llm_failed": failed}


def entries(account: str | None = None, symbol: str | None = None, result: str | None = None,
            exit_reason: str | None = None, band: str | None = None, start: str | None = None,
            end: str | None = None) -> list[dict[str, Any]]:
    """Journal entries for the Journal screen, newest first, with filters."""
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM journal WHERE kind = 'trade' ORDER BY CAST(ref AS INTEGER) DESC")]
    out = []
    for r in rows:
        m = json.loads(r["metrics"])
        last = (m["exits"][-1]["at"] if m["exits"] and m["exits"][-1].get("at") else m["entry"]["at"]) or ""
        if (account and m["account_id"] != account) or (symbol and symbol.upper() not in m["symbol"]) \
                or (exit_reason and m["exit_reason"] != exit_reason) or (band and m["rr_band"] != band) \
                or (result == "win" and not (m["net_pnl"] or 0) > 0) or (result == "loss" and (m["net_pnl"] or 0) > 0) \
                or (start and last[:10] < start) or (end and last[:10] > end):
            continue
        out.append({"id": r["id"], "trade_id": int(r["ref"]), "metrics": m, "narrative": r["narrative"],
                    "narrative_source": r["narrative_source"], "model": r["model"], "last_error": r["last_error"],
                    "closed_on": last[:10]})
    return out


CSV_COLUMNS = ["trade_id", "account_id", "symbol", "strategy_version", "signal_date", "rr_band", "rr", "sizing_case",
               "qty", "entry_at", "entry_price", "initial_stop", "final_stop", "target", "avg_exit", "closed_on",
               "exit_reason", "target_reached", "holding_days", "charges", "net_pnl", "r_multiple", "mfe_r", "mae_r",
               "mfe_pct", "mae_pct", "excursion_source", "overrides", "explanation"]


def csv_rows(**filters: Any) -> list[list[Any]]:
    out = []
    for e in entries(**filters):
        m, ex = e["metrics"], e["metrics"]["excursion"]
        out.append([m["trade_id"], m["account_id"], m["symbol"], m["strategy_version"], m["signal_date"], m["rr_band"],
                    m["rr"], m["sizing_case"], m["qty"], m["entry"]["at"], m["entry"]["price"], m["levels"]["initial_stop"],
                    m["levels"]["final_stop"], m["levels"]["target"], m["avg_exit"], e["closed_on"], m["exit_reason"],
                    m["target_reached"], m["holding_days"], m["charges"], m["net_pnl"], m["r_multiple"], ex.get("mfe_r"),
                    ex.get("mae_r"), ex.get("mfe_pct"), ex.get("mae_pct"), ex.get("source"),
                    "; ".join(o["change_type"] for o in m["overrides"]), e["narrative"]])
    return out
