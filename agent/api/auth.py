"""Dashboard sign-in (decided 2026-10-01, for running on a server).

Off by default: on the laptop the server only accepts connections from the laptop itself. On a server
(`DASHBOARD_AUTH=required` in .env), every API call and the live-update socket need a signed-in session:

- One password, set with `python -m agent.cli set-password`. Stored as a PBKDF2-SHA256 hash (600,000 rounds) with a
  random salt in runtime/auth.json; the password itself is never stored or logged.
- A session is an HttpOnly, SameSite=Strict cookie signed with a random key from the same file, valid 30 days.
  Setting a new password replaces the key, which signs every device out.
- Wrong passwords: at most 5 per 10 minutes from one address and 20 per hour in total, then a wait.
- Open without a session: the page itself (no data), /api/auth/*, and a bare /api/health for uptime checks.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

COOKIE = "ta_session"
SESSION_DAYS = 30
ROUNDS = 600_000
OPEN_PATHS = ("/api/auth/", "/api/health")
_fails: dict[str, list[float]] = {}
_all_fails: list[float] = []
_lock = threading.Lock()


def _file() -> Path:
    from ..settings import RUNTIME_DIR
    return RUNTIME_DIR / "auth.json"


def required() -> bool:
    from ..settings import get_secrets
    return get_secrets().dashboard_auth.lower() == "required"


def _load() -> dict[str, Any] | None:
    f = _file()
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text())
    except (OSError, ValueError):
        return None


def configured() -> bool:
    return _load() is not None


def _hash(password: str, salt: bytes, rounds: int = ROUNDS) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode(), salt, rounds).hex()


def set_password(password: str) -> None:
    if len(password) < 10:
        raise ValueError("Use at least 10 characters")
    salt = os.urandom(16)
    data = {"salt": salt.hex(), "rounds": ROUNDS, "hash": _hash(password, salt), "key": secrets.token_hex(32),
            "set_at": int(time.time())}
    f = _file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    tmp.replace(f)


def check_password(password: str) -> bool:
    d = _load()
    if not d:
        return False
    return hmac.compare_digest(_hash(password, bytes.fromhex(d["salt"]), int(d["rounds"])), d["hash"])


def _sign(payload: bytes, key: str) -> str:
    return hmac.new(bytes.fromhex(key), payload, hashlib.sha256).hexdigest()


def new_session() -> str:
    d = _load()
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + SESSION_DAYS * 86400,
                                                   "n": secrets.token_hex(8)}).encode())
    return f"{payload.decode()}.{_sign(payload, d['key'])}"


def valid_session(token: str | None) -> bool:
    d = _load()
    if not token or not d or "." not in token:
        return False
    payload, sig = token.rsplit(".", 1)
    if not hmac.compare_digest(_sign(payload.encode(), d["key"]), sig):
        return False
    try:
        return int(json.loads(base64.urlsafe_b64decode(payload.encode()))["exp"]) > time.time()
    except (ValueError, KeyError):
        return False


def allowed(path: str, cookie: str | None) -> bool:
    if not required():
        return True
    if not path.startswith("/api/") and path != "/ws":
        return True                       # the page and its scripts carry no data
    if path.startswith(OPEN_PATHS):
        return True
    return valid_session(cookie)


def too_many(addr: str) -> int:
    """Seconds to wait before another try (0 = go ahead)."""
    now = time.time()
    with _lock:
        mine = [t for t in _fails.get(addr, []) if now - t < 600]
        _fails[addr] = mine
        _all_fails[:] = [t for t in _all_fails if now - t < 3600]
        if len(mine) >= 5:
            return int(600 - (now - mine[0])) + 1
        if len(_all_fails) >= 20:
            return int(3600 - (now - _all_fails[0])) + 1
    return 0


def failed(addr: str) -> None:
    now = time.time()
    with _lock:
        _fails.setdefault(addr, []).append(now)
        _all_fails.append(now)


def cookie_secure() -> bool:
    from ..settings import get_secrets
    return get_secrets().public_url.lower().startswith("https://")
