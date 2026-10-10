"""Paper trading inside a replay (decided 2026-10-01): practice only, never touches the agent's portfolios.

The dashboard simulates the orders against the replayed prices; each closed trade is recorded here with Zerodha
charges, as the paper portfolios charge them: shares bought and sold on the same day at intraday rates on both
sides, otherwise delivery rates (with the DP charge on the sell). Shorts are intraday only (Zerodha does not allow
an equity short to be carried overnight), so the replay squares them off at the session's last price.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from ..clock import IST, ts
from ..db import connect
from ..orders.charges import compute, compute_intraday


class TradeError(ValueError):
    pass


def _day(ms: int):
    return datetime.fromtimestamp(ms / 1000, IST).date()


def round_trip(side: str, qty: int, entry_price: float, exit_price: float, entry_ts: int, exit_ts: int) -> dict[str, Any]:
    """Gross, charges and net of one closed trade."""
    if side not in ("long", "short") or qty <= 0 or entry_price <= 0 or exit_price <= 0:
        raise TradeError("Bad trade")
    buy_px, sell_px = (entry_price, exit_price) if side == "long" else (exit_price, entry_price)
    intraday = side == "short" or _day(entry_ts) == _day(exit_ts)
    if intraday:
        ch = compute_intraday("BUY", qty, buy_px).total + compute_intraday("SELL", qty, sell_px).total
    else:
        ch = compute("BUY", qty, buy_px).total + compute("SELL", qty, sell_px, first_sell_today=True).total
    gross = round((sell_px - buy_px) * qty, 2)
    return {"gross": gross, "charges": round(ch, 2), "net": round(gross - ch, 2), "intraday": intraday}


def record(body: dict[str, Any]) -> dict[str, Any]:
    try:
        row = {k: body[k] for k in ("session", "symbol", "side", "qty", "entry_ts", "entry_price", "exit_ts", "exit_price")}
        row["qty"] = int(row["qty"])
        row["entry_ts"], row["exit_ts"] = int(row["entry_ts"]), int(row["exit_ts"])
        row["entry_price"], row["exit_price"] = float(row["entry_price"]), float(row["exit_price"])
    except (KeyError, TypeError, ValueError) as exc:
        raise TradeError(f"Missing or bad field: {exc}") from exc
    if row["exit_ts"] < row["entry_ts"]:
        raise TradeError("Exit before entry")
    pnl = round_trip(row["side"], row["qty"], row["entry_price"], row["exit_price"], row["entry_ts"], row["exit_ts"])
    row |= pnl | {"exit_reason": str(body.get("exit_reason") or "closed")[:40], "created_at": ts()}
    with connect() as conn:
        cur = conn.execute(f"INSERT INTO replay_trades ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                           [int(v) if isinstance(v, bool) else v for v in row.values()])
        row["id"] = cur.lastrowid
    return row


def listing(session: str | None = None) -> dict[str, Any]:
    with connect() as conn:
        if session:
            rows = [dict(r) for r in conn.execute("SELECT * FROM replay_trades WHERE session = ? ORDER BY id", (session,))]
        else:
            rows = [dict(r) for r in conn.execute("SELECT * FROM replay_trades ORDER BY id DESC LIMIT 500")]
        sessions = [dict(r) for r in conn.execute(
            "SELECT session, COUNT(*) AS trades, SUM(net > 0) AS won, ROUND(SUM(gross), 2) AS gross, "
            "ROUND(SUM(charges), 2) AS charges, ROUND(SUM(net), 2) AS net, MIN(created_at) AS started "
            "FROM replay_trades GROUP BY session ORDER BY started DESC LIMIT 50")]
    n = len(rows)
    summary = {"trades": n, "won": sum(1 for r in rows if r["net"] > 0),
               "gross": round(sum(r["gross"] for r in rows), 2), "charges": round(sum(r["charges"] for r in rows), 2),
               "net": round(sum(r["net"] for r in rows), 2)}
    return {"trades": rows, "summary": summary, "sessions": sessions}


def clear(session: str) -> int:
    with connect() as conn:
        return conn.execute("DELETE FROM replay_trades WHERE session = ?", (session,)).rowcount


# ---------- the agent's own signals and trades, for the chart ----------

def agent_overlay(symbol: str) -> dict[str, Any]:
    import json
    with connect() as conn:
        sigs = [dict(r) for r in conn.execute(
            "SELECT date, strategy_version, entry_trigger, entry_limit, stop, target, rr, rr_band, eligible, source, "
            "indicators FROM signals WHERE symbol = ? ORDER BY date DESC LIMIT 300", (symbol,))]
        trades = [dict(r) for r in conn.execute(
            "SELECT id, account_id, strategy_version, state, qty, entry_price, entry_at, exits, stop, initial_stop, "
            "target, exit_reason, created_at, closed_at, placed_on FROM trades WHERE symbol = ? AND entry_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 200", (symbol,))]
    for s in sigs:
        ind = json.loads(s.pop("indicators") or "{}")
        s["candle_at"] = ind.get("candle_at")          # intraday signals: the pullback candle
    for t in trades:
        t["exits"] = [{k: e.get(k) for k in ("at", "price", "qty", "reason")} for e in json.loads(t["exits"] or "[]")]
    return {"symbol": symbol, "signals": sigs, "trades": trades}
