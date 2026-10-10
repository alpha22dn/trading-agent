"""SQLite storage: schema (spec section 12), seeding, and daily backups.

Table names match the spec. Two additions: `app_state` (small key-value store
for the Kite session, kill switch and progress markers) and extra columns on
`audit_log` so alerts can be acknowledged.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

from .clock import now_ist, ts
from .settings import AppConfig, ConfigError, StrategyConfig, db_path

SCHEMA_VERSION = 13

SCHEMA = """
CREATE TABLE IF NOT EXISTS instruments (
    instrument_token INTEGER PRIMARY KEY,
    exchange_token   INTEGER,
    exchange         TEXT NOT NULL,
    segment          TEXT,
    instrument_type  TEXT,
    symbol           TEXT NOT NULL,
    name             TEXT,
    tick_size        REAL,
    lot_size         INTEGER,
    isin             TEXT,
    sector           TEXT,
    is_nse500        INTEGER NOT NULL DEFAULT 0,
    upper_circuit    REAL,
    lower_circuit    REAL,
    price_band_date  TEXT,
    updated_at       TEXT NOT NULL,
    UNIQUE (exchange, symbol)
);

CREATE TABLE IF NOT EXISTS candles_daily (
    symbol           TEXT NOT NULL,
    date             TEXT NOT NULL,
    open             REAL NOT NULL,
    high             REAL NOT NULL,
    low              REAL NOT NULL,
    close            REAL NOT NULL,
    volume           INTEGER NOT NULL,
    instrument_token INTEGER,
    fetched_at       TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS indicators_daily (
    symbol           TEXT NOT NULL,
    date             TEXT NOT NULL,
    bb_upper         REAL,
    atr14            REAL,
    adx14            REAL,
    traded_value_20d REAL,
    computed_at      TEXT NOT NULL,
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS watchlist (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol        TEXT NOT NULL,
    eps           REAL NOT NULL,
    profit_growth REAL NOT NULL,
    pasted_on     TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS ix_watchlist_active ON watchlist (active, symbol);

CREATE TABLE IF NOT EXISTS events (
    symbol     TEXT NOT NULL,
    event_date TEXT NOT NULL,
    purpose    TEXT,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, event_date, source)
);

CREATE TABLE IF NOT EXISTS signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    date             TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    entry_trigger    REAL NOT NULL,
    entry_limit      REAL NOT NULL,
    stop             REAL NOT NULL,
    target           REAL NOT NULL,
    swing_low        REAL,
    rr               REAL NOT NULL,
    rr_band          TEXT,
    indicators       TEXT,
    eligible         INTEGER NOT NULL DEFAULT 0,
    gates            TEXT,
    block_reasons    TEXT,
    source           TEXT NOT NULL DEFAULT 'eod',
    created_at       TEXT NOT NULL,
    UNIQUE (date, symbol, strategy_version)
);
CREATE INDEX IF NOT EXISTS ix_signals_date ON signals (strategy_version, date);

CREATE TABLE IF NOT EXISTS candidates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id   INTEGER NOT NULL REFERENCES signals(id),
    account_id  TEXT NOT NULL REFERENCES accounts(id),
    qty         INTEGER NOT NULL,
    rank        INTEGER,
    taken       INTEGER NOT NULL DEFAULT 0,
    skip_reason TEXT,
    created_at  TEXT NOT NULL,
    UNIQUE (signal_id, account_id)
);

CREATE TABLE IF NOT EXISTS accounts (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    mode         TEXT NOT NULL CHECK (mode IN ('paper', 'live')),
    capital      REAL NOT NULL,
    peak_equity  REAL NOT NULL,
    manual_edits INTEGER NOT NULL DEFAULT 0,
    halted       INTEGER NOT NULL DEFAULT 0,
    halt_reason  TEXT,
    halted_at    TEXT,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id       TEXT NOT NULL REFERENCES accounts(id),
    signal_id        INTEGER REFERENCES signals(id),
    symbol           TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    state            TEXT NOT NULL,
    qty              INTEGER NOT NULL,
    sizing_case      TEXT,
    entry_trigger    REAL,
    entry_limit      REAL,
    stop             REAL,
    target           REAL,
    entry_price      REAL,
    entry_at         TEXT,
    exits            TEXT,
    charges          REAL,
    net_pnl          REAL,
    r_multiple       REAL,
    exit_reason      TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    closed_at        TEXT
);
CREATE INDEX IF NOT EXISTS ix_trades_account_state ON trades (account_id, state);

CREATE TABLE IF NOT EXISTS gtts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id        INTEGER NOT NULL REFERENCES trades(id),
    gtt_id          TEXT,
    purpose         TEXT NOT NULL CHECK (purpose IN ('buy', 'exit')),
    type            TEXT NOT NULL CHECK (type IN ('single', 'oco')),
    legs            TEXT NOT NULL,
    status          TEXT NOT NULL,
    trigger_history TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_gtts_trade ON gtts (trade_id, status);

CREATE TABLE IF NOT EXISTS overrides (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id    INTEGER NOT NULL REFERENCES trades(id),
    change_type TEXT NOT NULL,
    before      TEXT,
    after       TEXT,
    detected_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS config_versions (
    version     TEXT PRIMARY KEY,
    parameters  TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('champion', 'challenger', 'retired')),
    approved_on TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    kind         TEXT NOT NULL CHECK (kind IN ('job', 'api', 'alert', 'user', 'system')),
    name         TEXT NOT NULL,
    level        TEXT NOT NULL DEFAULT 'info',
    message      TEXT,
    details      TEXT,
    acknowledged INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_audit_kind_name ON audit_log (kind, name, id);

-- Phase 2: trade events, daily equity, and the PaperBroker's simulated broker-side state
CREATE TABLE IF NOT EXISTS trade_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id   INTEGER NOT NULL REFERENCES trades(id),
    ts         TEXT NOT NULL,
    from_state TEXT,
    to_state   TEXT,
    message    TEXT NOT NULL,
    data       TEXT
);
CREATE INDEX IF NOT EXISTS ix_trade_events_trade ON trade_events (trade_id, id);

CREATE TABLE IF NOT EXISTS equity_daily (
    account_id  TEXT NOT NULL REFERENCES accounts(id),
    date        TEXT NOT NULL,
    cash        REAL NOT NULL,
    holdings    REAL NOT NULL,
    equity      REAL NOT NULL,
    peak_equity REAL NOT NULL,
    PRIMARY KEY (account_id, date)
);

CREATE TABLE IF NOT EXISTS paper_gtts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id     TEXT NOT NULL,
    symbol         TEXT NOT NULL,
    type           TEXT NOT NULL CHECK (type IN ('single', 'oco')),
    trigger_values TEXT NOT NULL,
    orders         TEXT NOT NULL,
    last_price     REAL NOT NULL,
    status         TEXT NOT NULL,
    armed_at       TEXT NOT NULL,
    seen_high      REAL,
    seen_low       REAL,
    triggered_leg  INTEGER,
    order_id       INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_paper_gtts_status ON paper_gtts (status, account_id);

CREATE TABLE IF NOT EXISTS paper_orders (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id   TEXT NOT NULL,
    gtt_id       INTEGER,
    symbol       TEXT NOT NULL,
    side         TEXT NOT NULL CHECK (side IN ('BUY', 'SELL')),
    qty          INTEGER NOT NULL,
    price        REAL NOT NULL,
    trigger      REAL,
    status       TEXT NOT NULL,
    trade_date   TEXT NOT NULL,
    placed_at    TEXT NOT NULL,
    filled_price REAL,
    filled_at    TEXT,
    charges      REAL,
    reason       TEXT
);
CREATE INDEX IF NOT EXISTS ix_paper_orders_status ON paper_orders (status, account_id);

CREATE TABLE IF NOT EXISTS paper_positions (
    account_id TEXT NOT NULL,
    symbol     TEXT NOT NULL,
    qty        INTEGER NOT NULL,
    avg_price  REAL NOT NULL,
    PRIMARY KEY (account_id, symbol)
);

-- Phase 2b: holdings bought outside the system (left unmanaged; they count toward cash and sector limits)
CREATE TABLE IF NOT EXISTS unmanaged_holdings (
    account_id  TEXT NOT NULL,
    symbol      TEXT NOT NULL,
    qty         INTEGER NOT NULL,
    avg_price   REAL,
    detected_at TEXT NOT NULL,
    PRIMARY KEY (account_id, symbol)
);

CREATE TABLE IF NOT EXISTS app_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Journal (spec section 10): one entry per closed trade and one per weekly review. Code computes `metrics`;
-- the LLM only writes `narrative` (template text until it succeeds).
CREATE TABLE IF NOT EXISTS journal (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    kind             TEXT NOT NULL CHECK (kind IN ('trade', 'week')),
    ref              TEXT NOT NULL,          -- trade id, or the Monday of the reviewed week
    account_id       TEXT,                   -- trade entries; NULL for a weekly review (covers all portfolios)
    metrics          TEXT NOT NULL,          -- JSON
    narrative        TEXT,
    narrative_source TEXT NOT NULL DEFAULT 'template',   -- llm / template
    model            TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    UNIQUE (kind, ref)
);

-- Portfolio capital history: created, capital changed (only when flat), reset
CREATE TABLE IF NOT EXISTS capital_changes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id    TEXT NOT NULL REFERENCES accounts(id),
    at            TEXT NOT NULL,
    kind          TEXT NOT NULL,            -- created / capital_changed / reset
    capital_before REAL,
    capital_after REAL NOT NULL,
    equity_before REAL,
    note          TEXT
);

-- Strategies screen (decided 2026-09-30): named strategies, each with a rule type and its current version
CREATE TABLE IF NOT EXISTS strategies (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,                -- rule type, e.g. momentum_breakout
    version     TEXT NOT NULL,                -- current version (config_versions)
    enabled     INTEGER NOT NULL DEFAULT 1,
    position    INTEGER NOT NULL DEFAULT 0,   -- list order; also the tie-break between strategies
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    deleted_at  TEXT                          -- deleted strategies stay for their trades' history
);

-- Which portfolios each strategy trades (checkboxes on the Strategies screen)
CREATE TABLE IF NOT EXISTS strategy_portfolios (
    strategy_id INTEGER NOT NULL REFERENCES strategies(id),
    account_id  TEXT NOT NULL REFERENCES accounts(id),
    PRIMARY KEY (strategy_id, account_id)
);

-- Learning loop (spec section 10): one row per proposal, from evidence to your decision
CREATE TABLE IF NOT EXISTS proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger       TEXT NOT NULL,              -- weekly / manual
    status        TEXT NOT NULL,              -- analysing, validating, failed, no_proposal, challenger, decision, approved, rejected, error
    message       TEXT,
    base_version  TEXT,                       -- the champion it was measured against
    param         TEXT,                       -- the what-if setting changed
    from_value    REAL,
    to_value      REAL,
    evidence      TEXT,                       -- JSON (rule, numbers, source)
    explanation   TEXT,
    validation    TEXT,                       -- JSON (folds, totals, pass/fail reasons)
    version       TEXT,                       -- the challenger version, once it runs
    challenger_account TEXT,
    challenger_started TEXT,                  -- first session it traded
    decided_at    TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- Research tab: saved chart layouts. key 'global' = indicators and chart settings shared by every stock;
-- key 'sym:<SYMBOL>' = that stock's drawings.
CREATE TABLE IF NOT EXISTS research_layouts (
    key         TEXT PRIMARY KEY,
    data        TEXT NOT NULL,              -- JSON
    updated_at  TEXT NOT NULL
);

-- Research tab: paper trades made during a replay (practice only; separate from the agent's portfolios).
CREATE TABLE IF NOT EXISTS replay_trades (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session     TEXT NOT NULL,              -- one replay run
    symbol      TEXT NOT NULL,
    side        TEXT NOT NULL,              -- long / short
    qty         INTEGER NOT NULL,
    entry_ts    INTEGER NOT NULL,           -- replay time, epoch ms
    entry_price REAL NOT NULL,
    exit_ts     INTEGER NOT NULL,
    exit_price  REAL NOT NULL,
    exit_reason TEXT,                       -- target / stop / closed / end of day
    gross       REAL NOT NULL,
    charges     REAL NOT NULL,
    net         REAL NOT NULL,
    intraday    INTEGER NOT NULL,           -- bought and sold the same day: intraday rates
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_replay_trades_session ON replay_trades (session);

-- Backtest tab: every run with its settings and results (the run itself uses its own database)
CREATE TABLE IF NOT EXISTS backtest_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    name        TEXT NOT NULL,
    config      TEXT NOT NULL,              -- JSON: dates, accounts, what-if settings
    status      TEXT NOT NULL,              -- queued / running / done / error / cancelled / interrupted
    progress    REAL NOT NULL DEFAULT 0,    -- 0..1
    message     TEXT,
    report      TEXT,                       -- JSON, when done
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT,
    updated_at  TEXT NOT NULL
);
"""

SPEC_TABLES = ["instruments", "candles_daily", "indicators_daily", "watchlist", "events", "signals",
               "candidates", "accounts", "trades", "gtts", "overrides", "journal", "config_versions",
               "audit_log"]


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


_db_override: Path | None = None


def set_db_path(path: Path | None) -> None:
    """Used by tests to point at a temporary database."""
    global _db_override
    _db_override = path


def current_db_path() -> Path:
    return _db_override or db_path()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    conn = _connect(current_db_path())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Columns added after a table first shipped: (table, column, definition).
MIGRATIONS = [
    ("signals", "eligible", "INTEGER NOT NULL DEFAULT 0"),
    ("signals", "gates", "TEXT"),
    ("signals", "block_reasons", "TEXT"),
    ("signals", "source", "TEXT NOT NULL DEFAULT 'eod'"),
    ("accounts", "cash", "REAL"),
    ("trades", "initial_stop", "REAL"),
    ("trades", "open_qty", "INTEGER"),
    ("trades", "target_hit_on", "TEXT"),
    ("trades", "placed_on", "TEXT"),
    ("trades", "sector", "TEXT"),
    ("trades", "rr", "REAL"),
    ("trades", "rr_band", "TEXT"),
    ("trades", "filled_at_cycle", "TEXT"),
    ("trades", "last_trail_on", "TEXT"),
    ("trades", "exit_at_open", "TEXT"),
    ("trades", "bought_qty", "INTEGER"),       # shares actually bought (a buy can fill in parts)
    ("paper_orders", "filled_qty", "INTEGER NOT NULL DEFAULT 0"),
    ("trades", "paused", "TEXT"),              # management paused (split/bonus): reason
    ("accounts", "reset_at", "TEXT"),          # start of the current run (after a reset or capital change)
    ("accounts", "strategy_version", "TEXT"),  # NULL = trades the champion; a challenger portfolio names its version
    ("accounts", "archived", "INTEGER NOT NULL DEFAULT 0"),  # a finished challenger: kept for history, not traded
    ("config_versions", "strategy_id", "INTEGER"),   # the strategy a version belongs to
    ("proposals", "strategy_id", "INTEGER"),         # the strategy a learning proposal improves
    ("trades", "awaiting_user", "TEXT"),       # waiting for the user (exit GTT cancelled by the user)       # date an exit-at-next-open GTT was placed (gap below the stop)
]


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, definition in MIGRATIONS:
        cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_db(app: AppConfig, strategy: StrategyConfig) -> None:
    with connect() as conn:
        # The Phase 0 placeholder `journal` table (never written) is replaced by the Phase 3 one.
        cols = {r[1] for r in conn.execute("PRAGMA table_info(journal)")}
        if cols and "kind" not in cols:
            if conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0]:
                conn.execute("ALTER TABLE journal RENAME TO journal_phase0")
            else:
                conn.execute("DROP TABLE journal")
        # Tables first, then columns added later, then indexes that may use them.
        conn.executescript(SCHEMA.replace(
            "CREATE INDEX IF NOT EXISTS ix_signals_date ON signals (strategy_version, date);", ""))
        _migrate(conn)
        conn.execute("CREATE INDEX IF NOT EXISTS ix_signals_date ON signals (strategy_version, date)")
        conn.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
        _seed_accounts(conn, app)
        conn.execute("UPDATE accounts SET cash = capital WHERE cash IS NULL")  # paper cash starts at capital
        register_strategy_version(conn, strategy)
        _seed_strategies(conn, strategy)


def _seed_strategies(conn: sqlite3.Connection, strategy: StrategyConfig) -> None:
    """First run of the Strategies screen: the version every portfolio traded (the champion) becomes the strategy
    "Momentum v1", ON, on every portfolio that traded it."""
    if conn.execute("SELECT 1 FROM strategies").fetchone():
        return
    r = conn.execute("SELECT value FROM app_state WHERE key = 'strategy_champion'").fetchone()
    version = json.loads(r[0]) if r else strategy.version
    if not conn.execute("SELECT 1 FROM config_versions WHERE version = ?", (version,)).fetchone():
        version = strategy.version
    now = ts()
    cur = conn.execute("INSERT INTO strategies (name, kind, version, enabled, position, created_at, updated_at) "
                       "VALUES ('Momentum v1', 'momentum_breakout', ?, 1, 1, ?, ?)", (version, now, now))
    sid = cur.lastrowid
    conn.execute("UPDATE config_versions SET strategy_id = ? WHERE strategy_id IS NULL", (sid,))
    conn.execute("UPDATE proposals SET strategy_id = ? WHERE strategy_id IS NULL", (sid,))   # earlier proposals
    conn.execute("INSERT INTO strategy_portfolios (strategy_id, account_id) SELECT ?, id FROM accounts "
                 "WHERE COALESCE(archived, 0) = 0 AND strategy_version IS NULL", (sid,))


def _seed_accounts(conn: sqlite3.Connection, app: AppConfig) -> None:
    mode = app["broker"]["mode"]
    for acc in app.accounts:
        row = conn.execute("SELECT capital FROM accounts WHERE id = ?", (acc["id"],)).fetchone()
        if row is None:
            conn.execute(
                "INSERT INTO accounts (id, name, mode, capital, peak_equity, manual_edits, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (acc["id"], acc["name"], mode, float(acc["capital"]), float(acc["capital"]),
                 1 if acc.get("manual_edits") else 0, ts()),
            )
        # Once created, a portfolio lives in the database: its capital is changed on the Accounts screen
        # (only when it is flat, or by a reset), so the config value only seeds the first run.


def register_strategy_version(conn: sqlite3.Connection, strategy: StrategyConfig) -> None:
    """Store the active strategy version; refuse to run if its parameters were edited in place."""
    row = conn.execute("SELECT fingerprint FROM config_versions WHERE version = ?",
                       (strategy.version,)).fetchone()
    if row is None:
        has_champion = conn.execute(
            "SELECT 1 FROM config_versions WHERE status = 'champion'").fetchone()
        conn.execute(
            "INSERT INTO config_versions (version, parameters, fingerprint, status, approved_on, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (strategy.version, json.dumps(strategy.params, sort_keys=True), strategy.fingerprint,
             "challenger" if has_champion else "champion", ts() if not has_champion else None, ts()),
        )
    elif row["fingerprint"] != strategy.fingerprint:
        raise ConfigError(
            f"config/strategies/{strategy.version}.yaml was edited after it was registered. "
            "Strategy versions are immutable: revert the file, or create a new version through the "
            "Learning screen."
        )


# ---------- app_state ----------

def get_state(key: str, default: Any = None) -> Any:
    with connect() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_state(key: str, value: Any) -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (key, json.dumps(value), ts()),
        )


def delete_state(key: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM app_state WHERE key = ?", (key,))


# ---------- backups ----------

def backup_db(backup_dir: Path, keep_days: int) -> Path:
    """Consistent online copy via SQLite's backup API, then prune old copies."""
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_ist().strftime("%Y-%m-%d_%H%M%S")
    target = backup_dir / f"agent_{stamp}.db"
    src = _connect(current_db_path())
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    cutoff = now_ist() - timedelta(days=keep_days)
    for old in backup_dir.glob("agent_*.db"):
        try:
            stamp_part = old.stem.removeprefix("agent_")
            when = datetime.strptime(stamp_part, "%Y-%m-%d_%H%M%S").replace(tzinfo=cutoff.tzinfo)
        except ValueError:
            continue
        if when < cutoff:
            old.unlink(missing_ok=True)
    return target
