"""Strategies (decided 2026-09-30): the Strategies screen adds, edits, turns on/off and deletes them, and ticks the
portfolios each one trades.

- A strategy = a name, a rule type (`kind`) and its current version (config_versions). Editing its settings creates
  a new immutable version (e.g. tight_stop_v1 -> tight_stop_v1.1); open trades keep the version that opened them.
- Rule types: only "momentum_breakout" (spec section 6) exists. A new rule type is built when you describe its rules;
  the agent never invents trading logic.
- Several ON strategies may share a portfolio: their signals go into ONE ranking (RR band, then price, then
  liquidity) and a stock is bought once (orders/manager.place_entries).
- OFF, unticking a portfolio, or deleting: that strategy's pending buys there are cancelled; open trades keep their
  stops and trail to exit under their own rules. Delete keeps the history (the row is only hidden).
- A challenger portfolio (learning loop) names its own version in accounts.strategy_version and trades only that.
"""
from __future__ import annotations

import copy
import re
from typing import Any

import yaml

from .. import audit
from ..clock import ts
from ..db import connect
from ..events import publish
from ..settings import STRATEGY_DIR, StrategyConfig

KINDS: dict[str, dict[str, Any]] = {
    "momentum_breakout": {
        "label": "Momentum breakout",
        "description": "Close ≥ +3% and ≥ upper Bollinger Band, ATR rising, ADX above its minimum; buy GTT above the "
                       "signal close, stop under the signal low, target from the swing range (spec section 6).",
        "built": True, "timeframe": "Daily (scanned at the EOD run)",
    },
    "intraday_pullback": {
        "label": "Intraday pullback",
        "description": "5-minute candles, 09:30–14:30: a stock up on the day and above VWAP dips to its 20-EMA or VWAP "
                       "and closes above it; buy GTT above that candle's high, stop under its low, target the day's "
                       "high before the dip. Unfilled buys cancelled at 15:15; one trade per stock per day; a position "
                       "keeps its stop/target GTT until one fills (overnight if needed).",
        "built": True, "timeframe": "5-minute (scanned every 5 minutes)",
    },
}

# Settings each rule type may change: (path in params, label, min, max, step). Momentum's indicator periods
# (Bollinger, ATR, ADX) are shared by every momentum strategy and not editable here.
SETTINGS_BY_KIND: dict[str, dict[str, tuple[tuple[str, str], str, float, float, float]]] = {
    "momentum_breakout": {
        "min_change_pct": (("signal", "min_change_pct"), "Signal day: close up at least (%)", 0.5, 20.0, 0.1),
        "adx_min": (("signal", "adx_min"), "Signal day: ADX above", 0, 60, 1),
        "swing_lookback": (("levels", "swing_lookback"), "Swing-low lookback (sessions)", 5, 60, 1),
        "entry_trigger_buffer_pct": (("levels", "entry_trigger_buffer_pct"), "Entry trigger above close (%)", 0.0, 3.0, 0.05),
        "entry_limit_buffer_pct": (("levels", "entry_limit_buffer_pct"), "Limit above trigger (%)", 0.0, 3.0, 0.05),
        "stop_buffer_pct": (("levels", "stop_buffer_pct"), "Stop below signal low (%)", 0.0, 5.0, 0.05),
        "target_extension": (("levels", "target_extension"), "Target extension (× range)", 0.5, 5.0, 0.1),
        "min_rr": (("levels", "min_rr"), "Minimum reward:risk", 1.0, 5.0, 0.1),
        "cancel_after_sessions": (("entry", "cancel_after_sessions"), "Cancel unfilled buy after (sessions)", 1, 10, 1),
        "trail_buffer_pct": (("exits", "trail_buffer_pct"), "Trail below prior low (%)", 0.0, 5.0, 0.05),
    },
    "intraday_pullback": {
        "up_min_pct": (("signal", "up_min_pct"), "Up on the day at least (%)", 0.0, 10.0, 0.1),
        "touch_pct": (("signal", "touch_pct"), "Dip counts within (%) of EMA/VWAP", 0.0, 1.0, 0.05),
        "ema_period": (("signal", "ema_period"), "EMA period (5-min candles)", 5, 50, 1),
        "top_liquid": (("universe", "top_liquid"), "Also watch the N most liquid NSE500", 0, 200, 10),
        "entry_trigger_buffer_pct": (("levels", "entry_trigger_buffer_pct"), "Trigger above the candle high (%)", 0.0, 1.0, 0.01),
        "entry_limit_buffer_pct": (("levels", "entry_limit_buffer_pct"), "Limit above trigger (%)", 0.0, 2.0, 0.05),
        "stop_buffer_pct": (("levels", "stop_buffer_pct"), "Stop below the candle low (%)", 0.0, 2.0, 0.05),
        "min_rr": (("levels", "min_rr"), "Minimum reward:risk", 1.0, 5.0, 0.1),
    },
}
SETTINGS = SETTINGS_BY_KIND["momentum_breakout"]      # backwards-compatible name


def kind_of(params: dict[str, Any]) -> str:
    return params.get("kind") or "momentum_breakout"


class StrategyError(ValueError):
    pass


# ---------- reading ----------

def _row(sid: int) -> dict[str, Any]:
    with connect() as conn:
        r = conn.execute("SELECT * FROM strategies WHERE id = ? AND deleted_at IS NULL", (sid,)).fetchone()
    if not r:
        raise StrategyError(f"No strategy {sid}")
    return dict(r)


def portfolios_of(sid: int) -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT sp.account_id FROM strategy_portfolios sp JOIN accounts a ON "
                                           "a.id = sp.account_id WHERE sp.strategy_id = ? AND COALESCE(a.archived, 0) = 0 "
                                           "ORDER BY a.capital", (sid,))]


def versions_of(sid: int) -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT version FROM config_versions WHERE strategy_id = ? ORDER BY rowid",
                                           (sid,))]


def active_rows() -> list[dict[str, Any]]:
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM strategies WHERE deleted_at IS NULL ORDER BY position, id")]


def primary_version() -> str | None:
    """The strategy other code falls back to (Signals screen default, backtest default): the first ON strategy,
    else the first one listed."""
    rows = active_rows()
    mom = [r for r in rows if r["kind"] == "momentum_breakout"]   # the daily scan and backtests need a momentum one
    on = [r for r in mom if r["enabled"]]
    return (on or mom or rows or [{}])[0].get("version")


def of_version(version: str) -> dict[str, Any] | None:
    with connect() as conn:
        r = conn.execute("SELECT s.* FROM strategies s JOIN config_versions c ON c.strategy_id = s.id "
                         "WHERE c.version = ?", (version,)).fetchone()
    return dict(r) if r else None


def versions_for_account(account_id: str) -> list[str]:
    """The versions a portfolio trades, in strategy order: its own version if it is a challenger portfolio,
    else the current version of every ON strategy that ticks it."""
    with connect() as conn:
        a = conn.execute("SELECT strategy_version, archived FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if not a or a["archived"]:
            return []
        if a["strategy_version"]:
            return [a["strategy_version"]]
        return [r[0] for r in conn.execute(
            "SELECT s.version FROM strategies s JOIN strategy_portfolios sp ON sp.strategy_id = s.id "
            "WHERE sp.account_id = ? AND s.enabled = 1 AND s.deleted_at IS NULL ORDER BY s.position, s.id",
            (account_id,))]


def scanned_versions() -> list[str]:
    """Versions to scan each evening: every ON strategy with a portfolio, plus challenger portfolios' versions.
    With none, the first strategy is still scanned so the Signals screen has something to show."""
    out: list[str] = []
    for r in active_rows():
        if r["enabled"] and portfolios_of(r["id"]):
            out.append(r["version"])
    with connect() as conn:
        for (v,) in conn.execute("SELECT DISTINCT strategy_version FROM accounts WHERE COALESCE(archived, 0) = 0 AND "
                                 "strategy_version IS NOT NULL"):
            if v not in out:
                out.append(v)
    if not out and (p := primary_version()):
        out.append(p)
    return out


def settings_of(params: dict[str, Any]) -> dict[str, float]:
    out = {}
    for k, (path, *_rest) in SETTINGS_BY_KIND.get(kind_of(params), {}).items():
        v = params.get(path[0], {}).get(path[1])
        if v is not None:
            out[k] = v
    return out


def listing() -> list[dict[str, Any]]:
    from . import versions
    out = []
    with connect() as conn:
        counts = {(r[0], r[1]): r[2] for r in conn.execute(
            "SELECT strategy_version, CASE WHEN state = 'CLOSED' THEN 'closed' WHEN state = 'BUY_PENDING' THEN 'pending' "
            "WHEN state IN ('FILLED','PROTECTED','TRAILING') THEN 'open' ELSE 'other' END, COUNT(*) FROM trades GROUP BY 1, 2")}
        pnl = {r[0]: (r[1], r[2]) for r in conn.execute(
            "SELECT strategy_version, SUM(net_pnl), AVG(r_multiple) FROM trades WHERE state = 'CLOSED' GROUP BY 1")}
    for r in active_rows():
        vs = versions_of(r["id"])
        s = versions.load(r["version"])
        n = lambda k: sum(counts.get((v, k), 0) for v in vs)  # noqa: E731
        closed = [pnl[v] for v in vs if v in pnl]
        out.append(r | {"kind_label": KINDS.get(r["kind"], {}).get("label", r["kind"]),
                        "portfolios": portfolios_of(r["id"]), "versions": vs, "settings": settings_of(s.params),
                        "open_trades": n("open"), "pending_buys": n("pending"), "closed_trades": n("closed"),
                        "net_pnl": round(sum(c[0] or 0 for c in closed), 2)})
    return out


def form_setup() -> dict[str, Any]:
    with connect() as conn:
        accts = [dict(r) for r in conn.execute("SELECT id, name, capital, strategy_version FROM accounts WHERE "
                                               "COALESCE(archived, 0) = 0 ORDER BY capital")]
    return {"kinds": [{"kind": k, **v} for k, v in KINDS.items()],
            "settings": [{"key": k, "label": lab, "min": lo, "max": hi, "step": st}
                         for k, (_p, lab, lo, hi, st) in SETTINGS.items()],
            "settings_by_kind": {kind: [{"key": k, "label": lab, "min": lo, "max": hi, "step": st}
                                        for k, (_p, lab, lo, hi, st) in m.items()] for kind, m in SETTINGS_BY_KIND.items()},
            "defaults_by_kind": {"intraday_pullback": settings_of(_intraday_template("x"))},
            "portfolios": [{"id": a["id"], "name": a["name"], "capital": a["capital"],
                            "challenger": a["strategy_version"]} for a in accts]}


# ---------- changing ----------

def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")[:40]
    return s or "strategy"


def _intraday_template(version: str) -> dict[str, Any]:
    from . import intraday_pullback
    return intraday_pullback.template(version)


def _clean_settings(kind: str, settings: dict[str, Any]) -> dict[str, float]:
    if kind not in SETTINGS_BY_KIND or not KINDS.get(kind, {}).get("built"):
        raise StrategyError(f"Rule type {kind} is not built yet")
    spec = SETTINGS_BY_KIND[kind]
    out: dict[str, float] = {}
    for k, v in (settings or {}).items():
        if k not in spec or v in (None, ""):
            continue
        _path, label, lo, hi, step = spec[k]
        try:
            x = float(v)
        except (TypeError, ValueError) as exc:
            raise StrategyError(f"{label}: enter a number") from exc
        if not lo <= x <= hi:
            raise StrategyError(f"{label}: must be between {lo:g} and {hi:g}")
        out[k] = int(round(x)) if step == 1 else round(x, 4)
    return out


def _apply(params: dict[str, Any], settings: dict[str, float]) -> dict[str, Any]:
    p = copy.deepcopy(params)
    spec = SETTINGS_BY_KIND[kind_of(params)]
    for k, v in settings.items():
        path = spec[k][0]
        p[path[0]][path[1]] = v
    # a minimum RR below the lowest band gets its own band D, ranked last (as in backtests)
    bands = [b for b in p["ranking"]["rr_bands"] if b["band"] != "D"]
    lowest = min(float(b["min"]) for b in bands)
    if float(p["levels"]["min_rr"]) < lowest:
        bands.append({"band": "D", "min": float(p["levels"]["min_rr"])})
    p["ranking"]["rr_bands"] = bands
    return p


def _write_yaml(version: str, params: dict[str, Any], why: str) -> None:
    path = STRATEGY_DIR / f"{version}.yaml"
    if not path.exists():
        header = f"# {version}: {why} ({ts()[:10]}). Immutable like every version; a further change creates a new version.\n\n"
        path.write_text(header + yaml.safe_dump(params, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _new_version(sid: int, version: str, params: dict[str, Any], why: str) -> StrategyConfig:
    from . import versions
    s = versions.register(version, params, status="champion")
    with connect() as conn:
        conn.execute("UPDATE config_versions SET strategy_id = ?, approved_on = ? WHERE version = ?", (sid, ts(), version))
    try:
        _write_yaml(version, s.params, why)
    except OSError:   # the database copy is authoritative; the file is a readable record
        pass
    return s


def _check_portfolios(ids: list[str]) -> list[str]:
    ok = {a["id"]: a for a in form_setup()["portfolios"]}
    out = []
    for a in ids or []:
        if a not in ok:
            raise StrategyError(f"No portfolio {a}")
        if ok[a]["challenger"]:
            raise StrategyError(f"{a} is a challenger portfolio: it trades only its own version")
        out.append(a)
    return sorted(set(out), key=lambda x: ok[x]["capital"])


def create(name: str, kind: str, settings: dict[str, Any], portfolios: list[str], enabled: bool = True,
           copy_from: int | None = None) -> dict[str, Any]:
    from . import versions
    name = (name or "").strip()[:60]
    if not name:
        raise StrategyError("Give the strategy a name")
    if any(r["name"].lower() == name.lower() for r in active_rows()):
        raise StrategyError(f"A strategy called {name} already exists")
    if kind not in KINDS:
        raise StrategyError(f"Unknown rule type {kind}")
    clean = _clean_settings(kind, settings)
    accts = _check_portfolios(portfolios)
    src = _row(copy_from) if copy_from else None
    if src and src["kind"] != kind:
        src = None                                   # settings are copied only from a strategy of the same rule type
    if kind == "intraday_pullback":
        base_version = src["version"] if src else "(rule template)"
        base = versions.load(src["version"]).params if src else _intraday_template("x")
    else:
        base_version = src["version"] if src else (primary_version() or "momentum_v1")
        base = versions.load(base_version).params
    params = _apply(base, clean)
    root = _slug(name)
    with connect() as conn:
        taken = {r[0] for r in conn.execute("SELECT version FROM config_versions")}
        pos = (conn.execute("SELECT MAX(position) FROM strategies").fetchone()[0] or 0) + 1
        version = next(f"{root}_v{i}" for i in range(1, 1000) if f"{root}_v{i}" not in taken)
        cur = conn.execute("INSERT INTO strategies (name, kind, version, enabled, position, created_at, updated_at) "
                           "VALUES (?, ?, ?, ?, ?, ?, ?)", (name, kind, version, 1 if enabled else 0, pos, ts(), ts()))
        sid = int(cur.lastrowid)
        conn.executemany("INSERT INTO strategy_portfolios (strategy_id, account_id) VALUES (?, ?)", [(sid, a) for a in accts])
    _new_version(sid, version, params, f"strategy '{name}' created on the Strategies screen")
    audit.user_action("strategy_created", f"{name} ({version}): {'ON' if enabled else 'OFF'}, portfolios "
                      f"{', '.join(accts) or 'none'}", {"settings": clean, "copied_from": base_version})
    publish("strategies")
    return _row(sid)


def edit(sid: int, name: str | None = None, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    from . import versions
    r = _row(sid)
    changes = []
    if name is not None and name.strip() and name.strip() != r["name"]:
        new = name.strip()[:60]
        if any(x["name"].lower() == new.lower() and x["id"] != sid for x in active_rows()):
            raise StrategyError(f"A strategy called {new} already exists")
        with connect() as conn:
            conn.execute("UPDATE strategies SET name = ?, updated_at = ? WHERE id = ?", (new, ts(), sid))
        changes.append(f"renamed to {new}")
    if settings is not None:
        cur = versions.load(r["version"])
        clean = _clean_settings(r["kind"], settings)
        params = _apply(cur.params, clean)
        if params != cur.params:
            version = versions.next_version(r["version"])
            _new_version(sid, version, params, f"settings of '{r['name']}' changed on the Strategies screen")
            with connect() as conn:
                conn.execute("UPDATE config_versions SET status = 'retired' WHERE version = ?", (r["version"],))
                conn.execute("UPDATE strategies SET version = ?, updated_at = ? WHERE id = ?", (version, ts(), sid))
            diff = {k: (v, settings_of(params)[k]) for k, v in settings_of(cur.params).items()
                    if settings_of(params).get(k) != v}
            changes.append(f"new version {version} (" + ", ".join(f"{k} {a:g} → {b:g}" for k, (a, b) in diff.items()) + ")")
    if changes:
        audit.user_action("strategy_edited", f"{r['name']}: " + "; ".join(changes))
        publish("strategies")
    return _row(sid) | {"changes": changes}


def _cancel_pending(sid: int, accounts: list[str] | None, why: str) -> int:
    from ..orders import manager
    vs = versions_of(sid)
    if not vs:
        return 0
    return manager.cancel_buys(vs, accounts, why)


def set_enabled(sid: int, on: bool) -> dict[str, Any]:
    r = _row(sid)
    with connect() as conn:
        conn.execute("UPDATE strategies SET enabled = ?, updated_at = ? WHERE id = ?", (1 if on else 0, ts(), sid))
    n = 0 if on else _cancel_pending(sid, None, f"strategy {r['name']} turned off")
    audit.user_action("strategy_on" if on else "strategy_off",
                      f"{r['name']} turned {'ON' if on else 'OFF'}" + (f"; {n} pending buy(s) cancelled" if n else ""))
    publish("strategies")
    return _row(sid) | {"cancelled_buys": n}


def set_portfolios(sid: int, portfolios: list[str]) -> dict[str, Any]:
    r = _row(sid)
    new = _check_portfolios(portfolios)
    old = portfolios_of(sid)
    removed = [a for a in old if a not in new]
    with connect() as conn:
        conn.execute("DELETE FROM strategy_portfolios WHERE strategy_id = ?", (sid,))
        conn.executemany("INSERT INTO strategy_portfolios (strategy_id, account_id) VALUES (?, ?)", [(sid, a) for a in new])
        conn.execute("UPDATE strategies SET updated_at = ? WHERE id = ?", (ts(), sid))
    n = _cancel_pending(sid, removed, f"portfolio unticked for strategy {r['name']}") if removed else 0
    added = [a for a in new if a not in old]
    audit.user_action("strategy_portfolios", f"{r['name']}: portfolios {', '.join(new) or 'none'}"
                      + (f" (added {', '.join(added)})" if added else "") + (f" (removed {', '.join(removed)})" if removed else "")
                      + (f"; {n} pending buy(s) cancelled" if n else ""))
    publish("strategies")
    return _row(sid) | {"added": added, "removed": removed, "cancelled_buys": n}


def delete(sid: int) -> dict[str, Any]:
    r = _row(sid)
    with connect() as conn:
        busy = conn.execute("SELECT id FROM proposals WHERE strategy_id = ? AND status IN "
                            "('analysing','validating','challenger','decision')", (sid,)).fetchone()
    if busy:
        raise StrategyError(f"Learning proposal {busy[0]} is testing this strategy: reject it on the Learning screen first")
    n = _cancel_pending(sid, None, f"strategy {r['name']} deleted")
    with connect() as conn:
        conn.execute("UPDATE strategies SET enabled = 0, deleted_at = ?, updated_at = ? WHERE id = ?", (ts(), ts(), sid))
        conn.execute("DELETE FROM strategy_portfolios WHERE strategy_id = ?", (sid,))
    audit.user_action("strategy_deleted", f"{r['name']} deleted" + (f"; {n} pending buy(s) cancelled" if n else "")
                      + "; its trades and history are kept")
    publish("strategies")
    return {"deleted": sid, "cancelled_buys": n}


def set_version(sid: int, version: str) -> None:
    """Learning approve / rollback: the strategy's current version becomes `version` (one of its own)."""
    r = _row(sid)
    if version not in versions_of(sid):
        raise StrategyError(f"{version} is not a version of {r['name']}")
    with connect() as conn:
        conn.execute("UPDATE config_versions SET status = 'retired' WHERE strategy_id = ? AND version != ? AND "
                     "status = 'champion'", (sid, version))
        conn.execute("UPDATE config_versions SET status = 'champion', approved_on = ? WHERE version = ?", (ts(), version))
        conn.execute("UPDATE strategies SET version = ?, updated_at = ? WHERE id = ?", (version, ts(), sid))
    from . import versions
    try:
        _write_yaml(version, versions.load(version).params, f"made current for '{r['name']}'")
    except OSError:
        pass
    publish("strategies")
