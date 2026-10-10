"""Sync NSE equity instruments (and the Nifty 50 index) from Kite's daily dump."""
from __future__ import annotations

from typing import Any

from .. import audit
from ..broker.kite_session import get_kite, invalidate, is_token_error
from ..clock import ts
from ..db import connect, set_state
from ..events import publish
from ..settings import get_app_config


def _keep(row: dict[str, Any], nifty_symbol: str) -> bool:
    if row.get("exchange") != "NSE":
        return False
    if row.get("segment") == "NSE" and row.get("instrument_type") == "EQ":
        return True
    return row.get("segment") == "INDICES" and row.get("tradingsymbol") == nifty_symbol


def store_instruments(rows: list[dict[str, Any]]) -> dict[str, int]:
    """Upsert instruments. Keeps sector / NSE500 flag set by the NSE500 upload."""
    nifty_symbol = get_app_config()["kite"]["nifty50_symbol"]
    kept = [r for r in rows if _keep(r, nifty_symbol)]
    now = ts()
    moved: list[str] = []
    with connect() as conn:
        for r in kept:
            # A symbol can move to a new token (e.g. after a corporate action): drop the stale row
            # but carry over what the NSE500 upload set on it.
            token = int(r["instrument_token"])
            stale = conn.execute(
                "SELECT sector, is_nse500, isin FROM instruments WHERE exchange = ? AND symbol = ? "
                "AND instrument_token != ?", (r["exchange"], r["tradingsymbol"], token)).fetchone()
            if stale:
                conn.execute("DELETE FROM instruments WHERE exchange = ? AND symbol = ? AND instrument_token != ?",
                             (r["exchange"], r["tradingsymbol"], token))
                moved.append(f"{r['tradingsymbol']}->{token}")
            conn.execute(
                """INSERT INTO instruments (instrument_token, exchange_token, exchange, segment, instrument_type,
                                            symbol, name, tick_size, lot_size, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(instrument_token) DO UPDATE SET
                       exchange_token = excluded.exchange_token, segment = excluded.segment,
                       instrument_type = excluded.instrument_type, symbol = excluded.symbol,
                       name = excluded.name, tick_size = excluded.tick_size,
                       lot_size = excluded.lot_size, updated_at = excluded.updated_at""",
                (int(r["instrument_token"]), r.get("exchange_token"), r["exchange"], r.get("segment"),
                 r.get("instrument_type"), r["tradingsymbol"], r.get("name"), r.get("tick_size"),
                 r.get("lot_size"), now),
            )
            if stale:
                conn.execute("UPDATE instruments SET sector = ?, is_nse500 = ?, isin = ? WHERE instrument_token = ?",
                             (stale["sector"], stale["is_nse500"], stale["isin"], token))
        total = conn.execute("SELECT COUNT(*) FROM instruments").fetchone()[0]
    if moved:  # recorded after the transaction closes (never write the audit log mid-transaction)
        audit.record("system", "instrument_token_changed", f"{len(moved)} symbol(s) moved to a new token",
                     level="warning", details={"moved": moved})
    return {"received": len(rows), "stored": len(kept), "total": int(total)}


def sync_instruments() -> dict[str, int]:
    kite = get_kite()
    try:
        rows = kite.instruments("NSE")
    except Exception as exc:
        if is_token_error(exc):
            invalidate("token rejected while syncing instruments")
        audit.alert("instruments_sync", f"Instrument sync failed: {exc}", "error")
        raise
    result = store_instruments(rows)
    set_state("instruments_synced_at", ts())
    audit.record("api", "instruments_sync", f"{result['stored']} NSE instruments stored", details=result)
    publish("data")
    return result


def search(q: str, limit: int = 10) -> list[dict[str, Any]]:
    """Symbol lookup for the dashboard: NSE equities whose symbol starts with / name contains the text.
    Exact and prefix symbol matches first, NSE500 members before others."""
    q = (q or "").strip().upper()
    if not q:
        return []
    with connect() as conn:
        rows = conn.execute(
            """SELECT symbol, name, is_nse500, sector FROM instruments
               WHERE exchange = 'NSE' AND instrument_type = 'EQ' AND segment = 'NSE'
                 AND (symbol LIKE ? OR UPPER(name) LIKE ?)
               ORDER BY (symbol = ?) DESC, (symbol LIKE ?) DESC, is_nse500 DESC, symbol
               LIMIT ?""", (q + "%", "%" + q + "%", q, q + "%", int(limit))).fetchall()
    return [{"symbol": r["symbol"], "name": r["name"], "nse500": bool(r["is_nse500"]), "sector": r["sector"]}
            for r in rows]
