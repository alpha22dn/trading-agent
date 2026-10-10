"""Running on a server: dashboard sign-in, the start notice and the uptime heartbeat."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from agent import ops, settings
from agent.api import auth
from agent.db import get_state


@pytest.fixture
def server(monkeypatch, tmp_path):
    monkeypatch.setattr(auth, "_file", lambda: tmp_path / "auth.json")
    monkeypatch.setenv("DASHBOARD_AUTH", "required")
    monkeypatch.setenv("PUBLIC_URL", "https://agent.example.ts.net")
    settings.get_secrets.cache_clear()
    auth._fails.clear()
    auth._all_fails.clear()
    yield
    settings.get_secrets.cache_clear()


def client():
    from agent.api.app import create_app
    return TestClient(create_app(start_scheduler=False), base_url="https://testserver")   # Secure cookies need https


def test_laptop_needs_no_sign_in():
    with client() as c:
        assert c.get("/api/auth/status").json() == {"required": False, "configured": False, "signed_in": True}
        assert c.get("/api/overview").status_code == 200


def test_server_requires_a_session(server):
    auth.set_password("correct horse battery")
    with client() as c:
        assert c.get("/api/overview").status_code == 401
        assert c.get("/api/health").json().keys() == {"ok", "time"}                  # bare health only
        assert c.get("/").status_code in (200, 404)                                  # the page itself is open
        assert c.get("/api/kite/callback?status=success&request_token=x", follow_redirects=False).headers["location"].startswith("/#/login?error=Sign%20in")
        with pytest.raises(Exception):
            with c.websocket_connect("/ws"):
                pass
        assert c.post("/api/auth/login", json={"password": "wrong"}).status_code == 401
        r = c.post("/api/auth/login", json={"password": "correct horse battery"})
        assert r.status_code == 200
        ck = r.headers["set-cookie"].lower()
        assert "httponly" in ck and "samesite=strict" in ck and "secure" in ck
        assert c.get("/api/overview").status_code == 200
        assert "strategy" in c.get("/api/health").json()
        # browsers send the session cookie with the socket handshake (the test client needs it spelled out)
        with c.websocket_connect("/ws", headers={"cookie": f"{auth.COOKIE}={c.cookies.get(auth.COOKIE)}"}) as ws:
            assert ws.receive_text() == '{"topic":"hello"}'
        assert c.get("/api/auth/status").json()["signed_in"]
        auth.set_password("a brand new password")                                     # signs everyone out
        assert c.get("/api/overview").status_code == 401


def test_wrong_passwords_are_limited(server):
    auth.set_password("correct horse battery")
    with client() as c:
        for _ in range(5):
            assert c.post("/api/auth/login", json={"password": "nope"}).status_code == 401
        r = c.post("/api/auth/login", json={"password": "correct horse battery"})
        assert r.status_code == 429 and "Try again" in r.json()["detail"]


def test_tampered_or_expired_sessions_fail(server, monkeypatch):
    auth.set_password("correct horse battery")
    tok = auth.new_session()
    assert auth.valid_session(tok)
    assert not auth.valid_session(tok[:-1] + ("0" if tok[-1] != "0" else "1"))
    import time
    monkeypatch.setattr(time, "time", lambda: 10 ** 12)
    assert not auth.valid_session(tok)
    with pytest.raises(ValueError):
        auth.set_password("short")


def test_serve_refuses_a_public_url_without_sign_in(monkeypatch, tmp_path):
    from agent import cli
    monkeypatch.setattr(auth, "_file", lambda: tmp_path / "auth.json")
    monkeypatch.setenv("PUBLIC_URL", "https://agent.example.ts.net")
    settings.get_secrets.cache_clear()
    assert cli.cmd_serve() == 2                                    # no DASHBOARD_AUTH=required
    monkeypatch.setenv("DASHBOARD_AUTH", "required")
    settings.get_secrets.cache_clear()
    assert cli.cmd_serve() == 2                                    # no password yet
    assert settings.get_secrets().redirect_url == "https://agent.example.ts.net/api/kite/callback"
    settings.get_secrets.cache_clear()


def test_start_notice_says_when_the_last_stop_was_not_clean(monkeypatch):
    assert ops.started().startswith("🟢")                          # first ever start
    assert "unexpected stop" in ops.started()                      # no clean stop recorded in between
    ops.stopped()
    t = ops.started()
    assert t.startswith("🟢") and "stopped normally" in t


def test_heartbeat_pings_the_uptime_url(monkeypatch):
    import requests
    calls = []
    monkeypatch.setattr(requests, "get", lambda url, timeout: calls.append(url))
    assert ops.beat() == {"pinged": False}                         # no URL: only the local record
    monkeypatch.setenv("HEALTHCHECK_URL", "https://hc-ping.com/abc")
    settings.get_secrets.cache_clear()
    assert ops.beat() == {"pinged": True} and calls == ["https://hc-ping.com/abc"]
    assert get_state(ops.STATE_KEY)["last_alive"]
    settings.get_secrets.cache_clear()
