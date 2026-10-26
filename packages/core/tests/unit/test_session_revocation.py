"""C10 logout-replay: per-session revocation, durable across restarts,
independent per session — never a whole-roster revoke."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes.sessions import router as sessions_router
from openexecutive.memory import session_revocation

ADMIN = {
    "x-caller-email": "admin@probe.local",
    "x-caller-proxy-secret": "proxy-secret-probe",
    "x-caller-session-jti": "jti-A",
}
ADMIN_S2 = dict(ADMIN, **{"x-caller-session-jti": "jti-B"})
VIEWER = {
    "x-caller-email": "viewer@probe.local",
    "x-caller-proxy-secret": "proxy-secret-probe",
    "x-caller-session-jti": "jti-V",
}
SERVICE = {"x-api-key": "service-secret-probe"}


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BO_TENANT_ID", "probe")
    monkeypatch.setenv("BACKEND_PROXY_SECRET", "proxy-secret-probe")
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "service-secret-probe")
    monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@probe.local")
    db = tmp_path / "episodic.db"
    monkeypatch.setattr(session_revocation, "DB_PATH", db)
    session_revocation.initialize_db(db)
    return db


def _client() -> TestClient:
    from openexecutive.api.routes.bo import register_error_handlers

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(sessions_router)
    return TestClient(app)


def test_revoked_jti_refused_other_sessions_survive() -> None:
    client = _client()
    # Session A logs out → its jti dies.
    assert client.post("/sessions/revoke", json={
        "email": "admin@probe.local", "jti": "jti-A",
    }, headers=SERVICE).status_code == 204
    # Replay of the copied token is refused...
    resp = client.get("/sessions", headers=ADMIN)
    assert resp.status_code == 401
    # ...while the same user's second session stays valid — a session
    # revoke, not a roster revoke.
    assert client.get("/sessions", headers=ADMIN_S2).status_code == 200


def test_revocation_survives_restart(tmp_path: Path) -> None:
    # Durable row: a fresh process (new connection, same file) still
    # refuses the token.
    session_revocation.revoke("jti-A", "admin@probe.local")
    assert session_revocation.is_revoked("jti-A") is True
    assert session_revocation.is_revoked("jti-B") is False
    assert session_revocation.is_revoked(None) is False


def test_revoke_is_service_only_and_idempotent() -> None:
    client = _client()
    # A delegated user cannot call the revocation endpoint directly.
    assert client.post("/sessions/revoke", json={
        "email": "viewer@probe.local", "jti": "jti-V",
    }, headers=VIEWER).status_code == 403
    # Double revoke converges — replaying the signOut event is safe.
    for _ in range(2):
        assert client.post("/sessions/revoke", json={
            "email": "viewer@probe.local", "jti": "jti-V",
        }, headers=SERVICE).status_code == 204
    assert session_revocation.is_revoked("jti-V") is True


def test_revoked_readback_and_missing_jti_is_neutral() -> None:
    client = _client()
    assert client.get("/sessions/revoked/jti-A",
                      headers=SERVICE).json()["revoked"] is False
    session_revocation.revoke("jti-A", "admin@probe.local")
    assert client.get("/sessions/revoked/jti-A",
                      headers=SERVICE).json()["revoked"] is True
    # An older proxy that stamps no jti gets no check (compat).
    legacy = {k: v for k, v in ADMIN.items()
              if k != "x-caller-session-jti"}
    assert client.get("/sessions", headers=legacy).status_code == 200
