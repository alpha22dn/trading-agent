"""Daily manual Kite login and access-token lifecycle.

Rules enforced here:
- No automated 2FA: the user always logs in on Zerodha's page; we only
  exchange the request_token Kite hands back for an access token.
- Fail closed: `get_kite()` raises SessionUnavailable unless a token exists
  and has not passed its 06:00 IST expiry. Callers must not place orders
  without it.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Callable

from .. import audit
from ..clock import IST, now_ist
from ..db import delete_state, get_state, set_state
from ..events import publish
from ..settings import get_app_config, get_secrets

STATE_KEY = "kite_session"


class SessionUnavailable(RuntimeError):
    """No valid Kite session. Nothing that needs the broker may proceed."""


def _default_factory(api_key: str, timeout: int):
    from kiteconnect import KiteConnect
    return KiteConnect(api_key=api_key, timeout=timeout)


# Tests swap this for a fake client.
kite_factory: Callable[[str, int], Any] = _default_factory


def compute_expiry(login_at: datetime, expiry_hour: int) -> datetime:
    """Kite tokens die at `expiry_hour`:00 IST on the next morning (same day if login is earlier)."""
    login_at = login_at.astimezone(IST)
    cut = login_at.replace(hour=expiry_hour, minute=0, second=0, microsecond=0)
    return cut if login_at < cut else cut + timedelta(days=1)


def _client(access_token: str | None = None):
    secrets = get_secrets()
    if not secrets.kite_configured:
        raise SessionUnavailable("KITE_API_KEY / KITE_API_SECRET are not set in .env")
    kite = kite_factory(secrets.kite_api_key, int(get_app_config()["kite"]["request_timeout_sec"]))
    if access_token:
        kite.set_access_token(access_token)
    return kite


def login_url() -> str:
    return _client().login_url()


def complete_login(request_token: str) -> dict[str, Any]:
    """Exchange the request_token from Kite's redirect for today's access token."""
    secrets = get_secrets()
    kite = _client()
    data = kite.generate_session(request_token, api_secret=secrets.kite_api_secret)
    login_at = now_ist()
    expiry_hour = int(get_app_config()["kite"]["token_expiry_hour"])
    session = {
        "access_token": data["access_token"],
        "user_id": data.get("user_id"),
        "user_name": data.get("user_name"),
        "login_at": login_at.isoformat(timespec="seconds"),
        "expires_at": compute_expiry(login_at, expiry_hour).isoformat(timespec="seconds"),
    }
    set_state(STATE_KEY, session)
    audit.record("user", "kite_login", f"Logged in as {session['user_id']}",
                 details={"expires_at": session["expires_at"]})
    publish("session")
    return status()


def _raw_session() -> dict[str, Any] | None:
    """The stored session including the access token: only for opening the price stream. Never log or return it."""
    return get_state(STATE_KEY)


def status() -> dict[str, Any]:
    """Public session status (never includes the token)."""
    s = get_state(STATE_KEY)
    secrets = get_secrets()
    base = {"configured": secrets.kite_configured, "redirect_url": secrets.redirect_url}
    if not s:
        return base | {"valid": False, "reason": "Not logged in today"}
    expires_at = datetime.fromisoformat(s["expires_at"])
    now = now_ist()
    valid = now < expires_at
    return base | {
        "valid": valid,
        "reason": None if valid else "Session expired",
        "user_id": s.get("user_id"),
        "user_name": s.get("user_name"),
        "login_at": s["login_at"],
        "expires_at": s["expires_at"],
        "seconds_left": max(0, int((expires_at - now).total_seconds())),
    }


def get_kite():
    """Authenticated client, or SessionUnavailable. The only way code reaches Kite with a token."""
    s = get_state(STATE_KEY)
    if not s:
        raise SessionUnavailable("Not logged in to Kite today")
    if now_ist() >= datetime.fromisoformat(s["expires_at"]):
        raise SessionUnavailable("Kite session expired; log in again")
    return _client(s["access_token"])


# Called when a session ends before its expiry (token rejected, logout): the scheduler wakes the login check.
on_session_end: list = []


def _ended() -> None:
    for fn in on_session_end:
        try:
            fn()
        except Exception:  # noqa: BLE001 - a hook must never block the session change
            pass


def invalidate(reason: str) -> None:
    """Called when Kite rejects the token (TokenException) or on logout."""
    if get_state(STATE_KEY) is None:
        return
    delete_state(STATE_KEY)
    audit.alert("kite_session", f"Kite session ended: {reason}. New orders are blocked until you log in.")
    publish("session")
    _ended()


def logout() -> None:
    s = get_state(STATE_KEY)
    if s:
        try:
            _client(s["access_token"]).invalidate_access_token(s["access_token"])
        except Exception:  # token may already be dead; we drop it either way
            pass
    delete_state(STATE_KEY)
    audit.user_action("kite_logout", "Logged out of Kite")
    publish("session")
    _ended()


def is_token_error(exc: BaseException) -> bool:
    try:
        from kiteconnect.exceptions import TokenException
    except ImportError:  # pragma: no cover
        return False
    return isinstance(exc, TokenException)
