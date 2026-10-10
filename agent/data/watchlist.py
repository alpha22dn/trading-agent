"""Fundamental watchlist from Screener.in (spec section 5, gate 2).

Three ways in, all ending in (NSE code, EPS, profit growth) rows:
- Excel upload: Screener's results table copied into Excel. Screener shows company
  names, not codes, but every name is a link to /company/<NSE code>/, and Excel
  keeps those links; the code is read from the link.
- Paste straight from the Screener page: the dashboard reads the links from the
  copied table (browser clipboard HTML) and sends code/EPS/growth lines.
- Plain text: one stock per line, NSE code, EPS, profit growth (tabs or commas).

Columns are found by header: "EPS 12M" (or "EPS") and "Profit growth" (not the
3-year column). Repeated header rows (Screener repeats them every page) are
skipped. Stocks whose link has a numeric BSE code (not NSE-listed) are dropped.
Each upload replaces the active watchlist; tickers outside NSE500 are dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .. import audit
from ..clock import ts
from ..db import connect
from ..events import publish


class WatchlistFormatError(ValueError):
    pass


_LINK = re.compile(r"/company/([^/?#]+)/", re.I)


def symbol_from_link(url: str | None) -> str | None:
    m = _LINK.search(url or "")
    return m.group(1).upper() if m else None


def _find_columns(header: list[str]) -> tuple[int | None, int | None, int | None]:
    """(company/code column, EPS column, profit-growth column) from a header row."""
    h = [(c or "").strip().lower() for c in header]
    code = next((i for i, c in enumerate(h) if c in ("nse code", "symbol", "ticker")), None)
    name = next((i for i, c in enumerate(h) if c.startswith("company") or c == "name"), None)
    eps = next((i for i, c in enumerate(h) if c.startswith("eps") and "12m" in c), None)
    if eps is None:
        eps = next((i for i, c in enumerate(h) if c.startswith("eps") and "var" not in c and "gr" not in c), None)
    growth = next((i for i, c in enumerate(h) if c.startswith("profit growth") and "3" not in c and "5" not in c), None)
    return (code if code is not None else name), eps, growth


def parse_xlsx(content: bytes) -> tuple[list[tuple[str, float, float]], list[str], list[str]]:
    """Rows from a Screener table pasted into Excel. Returns (rows, bad_lines, bse_only)."""
    import io
    import openpyxl
    try:
        wb = openpyxl.load_workbook(io.BytesIO(content), data_only=True)
    except Exception as exc:  # noqa: BLE001
        raise WatchlistFormatError(f"Could not read the Excel file: {exc}") from exc
    ws = wb.active
    rows, bad, bse_only = [], [], []
    cols: tuple[int | None, int | None, int | None] | None = None
    for r in ws.iter_rows():
        values = [c.value for c in r]
        texts = [str(v).strip() if v is not None else "" for v in values]
        if not any(texts):
            continue
        found = _find_columns(texts)
        if found[1] is not None and found[2] is not None and found[0] is not None:
            cols = found  # header row (Screener repeats it on every page)
            continue
        if cols is None:
            continue
        ci, ei, gi = cols
        cell = r[ci]
        sym = symbol_from_link(cell.hyperlink.target if cell.hyperlink else None)
        if sym is None:
            text = texts[ci]
            sym = text.upper() if re.fullmatch(r"[A-Za-z0-9&\-]+", text or "") else None
        if sym is None:
            bad.append(texts[ci] or " ".join(texts))
            continue
        if sym.isdigit():  # BSE scrip code: not listed on NSE
            bse_only.append(texts[ci])
            continue
        eps, growth = _num(texts[ei]), _num(texts[gi])
        if eps is None or growth is None:
            bad.append(f"{sym}: missing EPS or profit growth")
            continue
        rows.append((sym, eps, growth))
    if cols is None:
        raise WatchlistFormatError("No header row with Company, EPS 12M and Profit growth columns found. "
                                   "Add those columns to the Screener results before copying.")
    return rows, bad, bse_only


@dataclass
class PasteResult:
    accepted: list[dict[str, Any]] = field(default_factory=list)
    not_in_nse500: list[str] = field(default_factory=list)
    bad_lines: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    bse_only: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"accepted": len(self.accepted), "symbols": [a["symbol"] for a in self.accepted],
                "not_in_nse500": self.not_in_nse500, "bad_lines": self.bad_lines[:20],
                "duplicates": self.duplicates, "bse_only": self.bse_only}


_NUM = re.compile(r"^[+-]?\d+(\.\d+)?$")


def _num(text: str) -> float | None:
    t = text.strip().replace("%", "").replace("₹", "").replace(",", "")
    return float(t) if _NUM.match(t) else None


def parse(text: str) -> tuple[list[tuple[str, float, float]], list[str]]:
    rows, bad = [], []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in (line.split("\t") if "\t" in line else line.split(","))]
        parts = [p for p in parts if p != ""]
        if len(parts) < 3:
            bad.append(line)
            continue
        sym, eps, growth = parts[0].upper(), _num(parts[1]), _num(parts[2])
        if eps is None or growth is None:
            if not rows and not bad:  # header row
                continue
            bad.append(line)
            continue
        rows.append((sym, eps, growth))
    return rows, bad


def replace_watchlist(text: str) -> PasteResult:
    rows, bad = parse(text)
    if not rows:
        raise WatchlistFormatError("No stocks found. Paste one stock per line: NSE code, EPS, profit growth.")
    return replace_rows(rows, bad, source="paste")


def replace_from_xlsx(content: bytes) -> PasteResult:
    rows, bad, bse_only = parse_xlsx(content)
    if not rows:
        raise WatchlistFormatError("No stocks with an NSE code, EPS and profit growth found in the file.")
    return replace_rows(rows, bad, source="excel", bse_only=bse_only)


def replace_rows(rows: list[tuple[str, float, float]], bad: list[str], source: str,
                 bse_only: list[str] | None = None) -> PasteResult:
    res = PasteResult(bad_lines=bad, bse_only=bse_only or [])
    now = ts()
    with connect() as conn:
        members = {r[0]: r[1] for r in conn.execute(
            "SELECT symbol, instrument_token FROM instruments WHERE is_nse500 = 1")}
        # NSE500 file symbols may carry a -BE style suffix in Kite; accept the bare code too.
        bare = {s.split("-")[0]: s for s in members}
        seen: set[str] = set()
        for sym, eps, growth in rows:
            kite_sym = sym if sym in members else bare.get(sym)
            if not kite_sym:
                res.not_in_nse500.append(sym)
                continue
            if kite_sym in seen:
                res.duplicates.append(sym)
                continue
            seen.add(kite_sym)
            res.accepted.append({"symbol": kite_sym, "eps": eps, "profit_growth": growth})
        if not res.accepted:
            raise WatchlistFormatError("None of the pasted stocks are in the NSE500 list.")
        conn.execute("UPDATE watchlist SET active = 0 WHERE active = 1")
        conn.executemany("INSERT INTO watchlist (symbol, eps, profit_growth, pasted_on, active) VALUES (?, ?, ?, ?, 1)",
                         [(a["symbol"], a["eps"], a["profit_growth"], now) for a in res.accepted])
    audit.user_action("watchlist_paste", f"Watchlist replaced from {source}: {len(res.accepted)} stocks",
                      details=res.as_dict())
    publish("watchlist")
    return res


def active() -> dict[str, dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute("SELECT symbol, eps, profit_growth, pasted_on FROM watchlist WHERE active = 1").fetchall()
    return {r["symbol"]: dict(r) for r in rows}
