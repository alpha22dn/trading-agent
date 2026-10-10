"""NSE500 constituents from NSE's CSV (ind_nifty500list.csv).

Expected columns (case-insensitive): Company Name, Industry, Symbol, Series, ISIN Code.
Sector comes from the Industry column (NSE's industry classification).

Automatic update (decided 2026-10-01): every trading day right after the instruments sync (09:00 and after the
day's login), `auto_update()` downloads the list from NSE (nsearchives, then niftyindices as a fallback) and
applies it when it differs. Safeguards: the file must parse (about 500 rows); more than `max_changes` stocks in or
out at once is not applied (alert, the old list stays); a failed download keeps the old list (warning alert once a
day). The dashboard upload remains as a manual fallback.

A stock that leaves the list: pending (untriggered) buys are cancelled, as for any signal that is no longer
tradeable (decided 2026-09-24); open positions keep their stops and exit under their own rules. A stock that joins:
the next EOD run downloads its candles, and it can signal from then on.
"""
from __future__ import annotations

import csv
import io
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from .. import audit
from ..clock import now_ist, ts
from ..db import connect, get_state, set_state
from ..events import publish
from ..settings import get_app_config

log = logging.getLogger("agent")
AUTO_KEY = "nse500_auto"
DEFAULTS = {
    "auto_update": True,
    "urls": ["https://nsearchives.nseindia.com/content/indices/ind_nifty500list.csv",
             "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"],
    "max_changes": 100,
    "timeout_sec": 20,
}

REQUIRED = {"symbol", "industry"}


class Nse500FormatError(ValueError):
    pass


@dataclass
class Constituent:
    symbol: str
    series: str
    industry: str
    company: str = ""
    isin: str = ""

    @property
    def kite_symbol(self) -> str:
        # Kite lists non-EQ series as SYMBOL-SERIES (e.g. XYZ-BE)
        return self.symbol if self.series in ("", "EQ") else f"{self.symbol}-{self.series}"


@dataclass
class UploadResult:
    rows: int
    matched: int
    unmatched: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    added: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def parse_csv(content: bytes | str) -> list[Constituent]:
    text = content.decode("utf-8-sig") if isinstance(content, bytes) else content.lstrip("﻿")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        raise Nse500FormatError("The file is empty")
    header = {h.strip().lower(): h for h in reader.fieldnames if h}
    missing = REQUIRED - set(header)
    if missing:
        raise Nse500FormatError(
            f"Missing column(s): {', '.join(sorted(missing))}. Use NSE's Nifty 500 list CSV "
            "(columns: Company Name, Industry, Symbol, Series, ISIN Code).")

    def col(row: dict[str, str], name: str) -> str:
        key = header.get(name)
        return (row.get(key) or "").strip() if key else ""

    out: list[Constituent] = []
    seen: set[str] = set()
    for row in reader:
        sym = col(row, "symbol").upper()
        if not sym or sym in seen:
            continue
        seen.add(sym)
        out.append(Constituent(symbol=sym, series=col(row, "series").upper(), industry=col(row, "industry"),
                               company=col(row, "company name"), isin=col(row, "isin code")))
    if len(out) < 400:
        raise Nse500FormatError(f"Only {len(out)} symbols found; expected about 500. Wrong file?")
    return out


def apply_constituents(items: list[Constituent], source: str = "upload") -> UploadResult:
    """Flag NSE500 members in `instruments`; needs instruments synced first. source: 'upload' or 'nse'."""
    now = ts()
    with connect() as conn:
        n_instruments = conn.execute(
            "SELECT COUNT(*) FROM instruments WHERE segment = 'NSE'").fetchone()[0]
        if n_instruments == 0:
            raise Nse500FormatError("No instruments yet. Log in to Kite first so instruments can sync.")
        before = {r[0] for r in conn.execute("SELECT symbol FROM instruments WHERE is_nse500 = 1")}
        conn.execute("UPDATE instruments SET is_nse500 = 0")
        matched, unmatched, now_members = 0, [], set()
        for c in items:
            cur = conn.execute(
                "UPDATE instruments SET is_nse500 = 1, sector = ?, isin = COALESCE(NULLIF(?, ''), isin), "
                "updated_at = ? WHERE exchange = 'NSE' AND segment = 'NSE' AND symbol IN (?, ?)",
                (c.industry, c.isin, now, c.kite_symbol, c.symbol))
            if cur.rowcount:
                matched += 1
                row = conn.execute("SELECT symbol FROM instruments WHERE exchange='NSE' AND segment='NSE' "
                                   "AND symbol IN (?, ?) LIMIT 1", (c.kite_symbol, c.symbol)).fetchone()
                now_members.add(row[0])
            else:
                unmatched.append(c.symbol)
    result = UploadResult(rows=len(items), matched=matched, unmatched=unmatched,
                          removed=sorted(before - now_members), added=sorted(now_members - before) if before else [])
    set_state("nse500_uploaded", {"at": now, "rows": len(items), "matched": matched, "source": source})
    level = "warning" if unmatched else "info"
    audit.record("user" if source == "upload" else "system", "nse500_upload",
                 f"NSE500 list ({'from NSE' if source == 'nse' else 'uploaded'}): {matched}/{len(items)} matched",
                 level=level, details=result.as_dict())
    cancelled = 0
    if result.removed:
        from ..orders import manager
        cancelled = manager.cancel_symbol_buys(result.removed, "the stock left the NSE500", "left_nse500")
        audit.alert("nse500_change", f"{len(result.removed)} stock(s) left NSE500: "
                    f"{', '.join(result.removed[:10])}{'…' if len(result.removed) > 10 else ''}. Held positions stay "
                    f"managed; no new entries" + (f"; {cancelled} pending buy(s) cancelled." if cancelled else "."),
                    level="info", details={"removed": result.removed, "cancelled_buys": cancelled})
    if result.added:
        audit.alert("nse500_change", f"{len(result.added)} stock(s) joined NSE500: "
                    f"{', '.join(result.added[:10])}{'…' if len(result.added) > 10 else ''}. Their candles download "
                    "at the next EOD run; they can signal from then on.", level="info", details={"added": result.added})
    if result.removed or result.added:
        try:
            from ..broker import feed
            feed.subscribe_universe()
        except Exception:  # noqa: BLE001 - the next connection subscribes them anyway
            log.exception("could not subscribe the new NSE500 members")
    publish("data")
    return result


# ---------- automatic update from NSE ----------

def _cfg() -> dict[str, Any]:
    return DEFAULTS | (get_app_config().get("nse500") or {})


def download(urls: list[str], timeout: int = 20) -> tuple[bytes, str]:
    """The CSV from the first URL that answers with a usable file. NSE's archive host wants a browser-like
    client with the site's cookies, so the home page is visited first."""
    import requests
    from .events import HEADERS
    sess = requests.Session()
    sess.headers.update(HEADERS | {"Accept": "text/csv,application/octet-stream,*/*"})
    errors = []
    for url in urls:
        try:
            if "nseindia.com" in url:
                try:
                    sess.get("https://www.nseindia.com/", timeout=timeout)
                except Exception:  # noqa: BLE001 - try the file anyway
                    pass
            r = sess.get(url, timeout=timeout)
            r.raise_for_status()
            parse_csv(r.content)                       # must be a real list
            return r.content, url
        except Exception as exc:  # noqa: BLE001 - next source
            errors.append(f"{url.split('/')[2]}: {type(exc).__name__}: {str(exc)[:120]}")
    raise RuntimeError("; ".join(errors) or "no source configured")


# Tests swap this for a fake: (urls, timeout) -> (csv bytes, url)
downloader: Callable[[list[str], int], tuple[bytes, str]] = download


def _plan(items: list[Constituent]) -> dict[str, Any]:
    """What applying `items` would change: membership and sectors."""
    with connect() as conn:
        cur = {r[0]: r[1] for r in conn.execute("SELECT symbol, sector FROM instruments WHERE is_nse500 = 1")}
        known = {r[0] for r in conn.execute("SELECT symbol FROM instruments WHERE exchange = 'NSE' AND segment = 'NSE'")}
    new: dict[str, str] = {}
    for c in items:
        sym = c.kite_symbol if c.kite_symbol in known else c.symbol if c.symbol in known else None
        if sym:
            new[sym] = c.industry
    return {"added": sorted(new.keys() - cur.keys()), "removed": sorted(cur.keys() - new.keys()),
            "sectors": sorted(s for s in new.keys() & cur.keys() if (new[s] or None) != (cur[s] or None)),
            "had_list": bool(cur)}


def auto_update(force: bool = False) -> dict[str, Any]:
    """Download NSE's list and apply it if it changed. Never raises: the result says what happened."""
    cfg = _cfg()
    today = now_ist().date().isoformat()
    st = get_state(AUTO_KEY) or {}
    if not cfg.get("auto_update", True) and not force:
        return {"status": "off"}
    with connect() as conn:
        if not conn.execute("SELECT 1 FROM instruments WHERE segment = 'NSE' LIMIT 1").fetchone():
            return {"status": "skipped", "detail": "No instruments yet"}
    try:
        content, url = downloader(list(cfg["urls"]), int(cfg.get("timeout_sec", 20)))
        items = parse_csv(content)
    except Exception as exc:  # noqa: BLE001
        msg = str(exc)[:400]
        log.warning("NSE500 list download failed: %s", msg)
        first_today = (st.get("failed_alert_on") != today)
        set_state(AUTO_KEY, st | {"last_attempt": ts(), "last_error": msg,
                                  "failed_alert_on": today if first_today else st.get("failed_alert_on")})
        if first_today:
            audit.alert("nse500_download", "Could not download the NSE500 list from NSE; the current list stays. "
                        f"It retries at the next sync. ({msg[:160]})", level="warning")
        publish("data")
        return {"status": "failed", "detail": msg}
    plan = _plan(items)
    n_changes = len(plan["added"]) + len(plan["removed"])
    base = {"last_attempt": ts(), "last_ok": ts(), "last_error": None, "source": url, "rows": len(items)}
    if plan["had_list"] and n_changes > int(cfg["max_changes"]) and not force:
        set_state(AUTO_KEY, st | base | {"blocked": {"added": len(plan["added"]), "removed": len(plan["removed"])}})
        audit.alert("nse500_download", f"NSE's NSE500 list would change {n_changes} stocks at once (limit "
                    f"{cfg['max_changes']}); not applied, the current list stays. Check it, then upload the CSV "
                    "or use Check now with Apply anyway.", level="warning",
                    details={"added": plan["added"][:50], "removed": plan["removed"][:50]})
        publish("data")
        return {"status": "blocked", "added": plan["added"], "removed": plan["removed"]}
    if not (plan["added"] or plan["removed"] or plan["sectors"]) and not force:
        set_state(AUTO_KEY, st | base | {"blocked": None})
        publish("data")
        return {"status": "unchanged", "rows": len(items)}
    res = apply_constituents(items, source="nse")
    set_state(AUTO_KEY, st | base | {"blocked": None, "last_change": ts(), "added": res.added, "removed": res.removed})
    log.info("NSE500 list updated from NSE: %d stocks, %d joined, %d left", res.matched, len(res.added),
             len(res.removed))
    return {"status": "updated", "sectors_changed": len(plan["sectors"])} | res.as_dict()


def auto_status() -> dict[str, Any]:
    cfg = _cfg()
    return {"enabled": bool(cfg.get("auto_update", True))} | (get_state(AUTO_KEY) or {})
