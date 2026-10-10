"""Configuration loading: secrets from .env, everything else from config/*.yaml.

Nothing trading-related is hard-coded in Python. Code reads values from
`AppConfig` (config/app.yaml) and `StrategyConfig` (config/strategies/*.yaml).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"
STRATEGY_DIR = CONFIG_DIR / "strategies"
RUNTIME_DIR = ROOT / "runtime"


class Secrets(BaseSettings):
    """Values from .env. Never committed, never logged."""

    model_config = SettingsConfigDict(env_file=str(ROOT / ".env"), env_file_encoding="utf-8", extra="ignore")

    kite_api_key: str = ""
    kite_api_secret: str = ""
    host: str = "127.0.0.1"
    port: int = 8000
    db_path: str = ""  # optional override; default runtime/agent.db
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    llm_provider: str = "ollama"
    ollama_url: str = "http://127.0.0.1:11434"
    ollama_model: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = "claude-sonnet-5-5"   # chosen 2026-09-29 (≈ ₹20/month at this volume)
    # Running on a server (decided 2026-10-01): the dashboard's address as you open it (e.g. the Tailscale name),
    # whether a sign-in is required, and an uptime-check URL the agent pings every 5 minutes.
    public_url: str = ""
    dashboard_auth: str = "off"     # "required" on a server
    healthcheck_url: str = ""

    @property
    def kite_configured(self) -> bool:
        return bool(self.kite_api_key and self.kite_api_secret)

    @property
    def redirect_url(self) -> str:
        base = self.public_url.rstrip("/") if self.public_url else f"http://{self.host}:{self.port}"
        return f"{base}/api/kite/callback"


@dataclass(frozen=True)
class AppConfig:
    raw: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.raw[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.raw.get(key, default)

    @property
    def timezone(self) -> str:
        return self.raw["timezone"]

    @property
    def holidays(self) -> dict[str, str]:
        return {str(k): str(v) for k, v in (self.raw.get("holidays") or {}).items()}

    @property
    def screener_query(self) -> str:
        return self.raw["screener_query"].strip()

    @property
    def accounts(self) -> list[dict[str, Any]]:
        return list(self.raw["accounts"])


@dataclass(frozen=True)
class StrategyConfig:
    version: str
    params: dict[str, Any] = field(repr=False)

    @property
    def fingerprint(self) -> str:
        canonical = json.dumps(self.params, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]


class ConfigError(RuntimeError):
    pass


_REQUIRED_APP_KEYS = ["timezone", "broker", "strategy", "market", "kite", "accounts", "risk",
                      "universe", "indicators", "results_calendar", "watchlist", "charges", "paper", "data", "backup",
                      "screener_query", "holidays"]
_REQUIRED_STRATEGY_KEYS = ["version", "signal", "levels", "entry", "exits", "ranking"]


def load_app_config(path: Path | None = None) -> AppConfig:
    path = path or CONFIG_DIR / "app.yaml"
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    missing = [k for k in _REQUIRED_APP_KEYS if k not in raw]
    if missing:
        raise ConfigError(f"{path.name} is missing keys: {', '.join(missing)}")
    if raw["broker"].get("mode") == "live" and not raw["broker"].get("live_enabled"):
        raise ConfigError("broker.mode is 'live' but broker.live_enabled is false")
    if raw["broker"].get("mode") == "live":
        raise ConfigError("Live trading is out of scope for the MVP; KiteBroker is not built yet")
    ids = [a["id"] for a in raw["accounts"]]
    if len(ids) != len(set(ids)):
        raise ConfigError("Duplicate account ids in accounts")
    return AppConfig(raw=raw)


def load_strategy_config(version: str, directory: Path | None = None) -> StrategyConfig:
    directory = directory or STRATEGY_DIR
    path = directory / f"{version}.yaml"
    if not path.exists():
        raise ConfigError(f"Strategy config not found: {path}")
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    missing = [k for k in _REQUIRED_STRATEGY_KEYS if k not in raw]
    if missing:
        raise ConfigError(f"{path.name} is missing keys: {', '.join(missing)}")
    if raw["version"] != version:
        raise ConfigError(f"{path.name} declares version '{raw['version']}', expected '{version}'")
    return StrategyConfig(version=version, params=raw)


@lru_cache(maxsize=1)
def get_secrets() -> Secrets:
    return Secrets()


@lru_cache(maxsize=1)
def get_app_config() -> AppConfig:
    return load_app_config()


def db_path() -> Path:
    override = get_secrets().db_path
    return Path(override) if override else RUNTIME_DIR / "agent.db"
