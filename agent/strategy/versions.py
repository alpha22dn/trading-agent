"""Strategy versions (spec section 10).

- Every strategy on the Strategies screen (strategy/strategies.py) has a current version; a portfolio trades the
  current versions of the ON strategies that tick it, or its own version if it is a challenger portfolio.
- "champion" here means the primary strategy's version (the first ON strategy): the fallback for code that needs
  a single version (backtest defaults, the Signals screen's default view).
- A version's parameters come from config/strategies/<version>.yaml when that file exists, otherwise from the
  config_versions table.
- Versions are immutable: a change is always a new version (momentum_v1.1, v1.2, ...).
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..clock import ts
from ..db import connect, get_state
from ..settings import ConfigError, StrategyConfig, get_app_config, load_strategy_config

CHAMPION_KEY = "strategy_champion"


def champion_version() -> str:
    from . import strategies
    try:
        v = strategies.primary_version()
    except Exception:  # noqa: BLE001 - before the strategies table exists (first start)
        v = None
    return v or get_state(CHAMPION_KEY) or get_app_config()["strategy"]["active_version"]


def load(version: str) -> StrategyConfig:
    try:
        return load_strategy_config(version)
    except ConfigError:
        with connect() as conn:
            r = conn.execute("SELECT parameters FROM config_versions WHERE version = ?", (version,)).fetchone()
        if not r:
            raise
        return StrategyConfig(version=version, params=json.loads(r[0]))


def champion() -> StrategyConfig:
    return load(champion_version())


def trading_versions() -> list[str]:
    """Versions scanned each evening: ON strategies with a portfolio, then challenger portfolios' versions."""
    from . import strategies
    return strategies.scanned_versions()


def next_version(base: str | None = None) -> str:
    """momentum_v1 -> momentum_v1.1 -> momentum_v1.2 ... (never reuses a number, even of a rejected version)."""
    base = base or champion_version()
    root = re.sub(r"\.\d+$", "", base)
    with connect() as conn:
        known = [r[0] for r in conn.execute("SELECT version FROM config_versions WHERE version LIKE ?", (root + "%",))]
    minors = [int(m.group(1)) for v in known if (m := re.fullmatch(re.escape(root) + r"\.(\d+)", v))]
    return f"{root}.{max(minors, default=0) + 1}"


def register(version: str, params: dict[str, Any], status: str = "challenger",
             strategy_id: int | None = None) -> StrategyConfig:
    """Store a new version. It belongs to the strategy of its base version (momentum_v1.2 -> momentum_v1's)."""
    s = StrategyConfig(version=version, params=dict(params, version=version))
    with connect() as conn:
        if strategy_id is None:
            root = re.sub(r"\.\d+$", "", version)
            r = conn.execute("SELECT strategy_id FROM config_versions WHERE version = ?", (root,)).fetchone()
            strategy_id = r[0] if r else None
        conn.execute("INSERT INTO config_versions (version, parameters, fingerprint, status, approved_on, created_at, "
                     "strategy_id) VALUES (?, ?, ?, ?, NULL, ?, ?)", (version, json.dumps(s.params, sort_keys=True),
                                                                     s.fingerprint, status, ts(), strategy_id))
    return s


def promote(version: str) -> None:
    """Make `version` its strategy's current version (learning approve, rollback)."""
    from . import strategies
    st = strategies.of_version(version)
    if not st:
        raise ValueError(f"{version} belongs to no strategy")
    strategies.set_version(st["id"], version)


def rollback(version: str) -> None:
    """One-click rollback: an earlier version of a strategy becomes its current version again."""
    with connect() as conn:
        r = conn.execute("SELECT status FROM config_versions WHERE version = ?", (version,)).fetchone()
    if not r:
        raise ValueError(f"No strategy version {version}")
    promote(version)


def listing() -> list[dict[str, Any]]:
    from . import strategies
    current = {r["version"]: r for r in strategies.active_rows()}
    with connect() as conn:
        rows = [dict(r) for r in conn.execute("SELECT c.version, c.parameters, c.status, c.approved_on, c.created_at, "
                                              "c.strategy_id, s.name AS strategy_name, s.deleted_at FROM config_versions c "
                                              "LEFT JOIN strategies s ON s.id = c.strategy_id ORDER BY c.rowid")]
    for r in rows:
        r["parameters"] = json.loads(r["parameters"])
        r["is_champion"] = r["version"] in current      # the current version of a (not deleted) strategy
    return [r for r in rows if not r["deleted_at"]]
