"""BO-AUDIT-FORGE: viewer-ul nu poate falsifica intrari de audit.

Ruta POST /audit/log cere audit:write (admin), refuza evenimentele de
securitate (auth_* — producatori interni) si orice actor diferit de
identitatea rezolvata de server. Fara rand nou la refuz.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import audit as audit_route
from openexecutive.api.routes import bo as bo_route

from .bo_testkit import capture_audit, use_tmp_db

ADMIN = {"x-caller-email": "admin@test", "x-caller-proxy-secret": "test-proxy-only"}
VIEWER = {"x-caller-email": "viewer@test", "x-caller-proxy-secret": "test-proxy-only"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setenv("BO_TENANT_ID", "tenant-a")
    monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@test")
    monkeypatch.setenv("BACKEND_PROXY_SECRET", "test-proxy-only")
    capture_audit(monkeypatch)
    app = FastAPI()
    app.include_router(audit_route.router)
    bo_route.register_error_handlers(app)
    return TestClient(app)


def test_viewer_nu_scrie_audit(client):
    r = client.post("/audit/log", headers=VIEWER,
                    json={"event_type": "alert", "summary": "x"})
    assert r.status_code == 403


def test_eveniment_auth_refuzate_si_admin(client):
    r = client.post("/audit/log", headers=ADMIN,
                    json={"event_type": "auth_login", "summary": "x"})
    assert r.status_code == 403


def test_actor_fals_refuzat(client):
    r = client.post("/audit/log", headers=ADMIN,
                    json={"event_type": "alert", "summary": "x",
                          "actor": "admin"})
    assert r.status_code == 403


def test_flux_legitim_admin(client):
    r = client.post("/audit/log", headers=ADMIN,
                    json={"event_type": "alert", "summary": "nota onesta"})
    assert r.status_code == 201
    assert r.json()["id"] is not None
