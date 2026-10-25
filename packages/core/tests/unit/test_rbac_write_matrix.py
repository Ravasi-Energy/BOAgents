"""BO-RBAC: deny-by-default pe toate scrierile REST legacy.

Fiecare ruta de scriere din agents/personas/sessions/artifacts/decisions/
alerts/skills/workflows/episodic/watchlist/knowledge/scheduled cere rolul
admin si refuza INAINTE de orice efect. Viewer-ul citeste, nu scrie.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import (
    agents, alerts, artifacts, bo as bo_route, decisions, episodic,
    knowledge, personas, scheduled, sessions, skills, watchlist, workflows,
)

from .bo_testkit import capture_audit, use_tmp_db

ADMIN = {"x-caller-email": "admin@test", "x-caller-proxy-secret": "test-proxy-only"}
VIEWER = {"x-caller-email": "viewer@test", "x-caller-proxy-secret": "test-proxy-only"}

ROUTERS = [agents, personas, sessions, artifacts, decisions, alerts,
           skills, workflows, episodic, watchlist, knowledge, scheduled]

# Toate scrierile vizate de control: (metoda, cale) — efectul trebuie
# sa lipseasca inainte ca ruta sa existe; 400/404/422 nu dovedesc nimic.
WRITES = [
    ("patch", "/agents/x"),
    ("delete", "/agents/x/override"),
    ("post", "/agents/x/test"),
    ("post", "/agents/x/rollback/1"),
    ("post", "/personas"),
    ("put", "/personas/x"),
    ("post", "/personas/x/reset"),
    ("delete", "/personas/x"),
    ("delete", "/sessions/x"),
    ("post", "/artifacts/x/archive"),
    ("post", "/artifacts/x/restore"),
    ("delete", "/artifacts/x"),
    ("post", "/decisions/1/reject"),
    ("post", "/alerts/1/ack"),
    ("post", "/alerts/bulk-ack"),
    ("post", "/alerts/1/reopen"),
    ("post", "/skills"),
    ("put", "/skills/x"),
    ("delete", "/skills/x"),
    ("delete", "/workflows/runs/1"),
    ("post", "/workflows/custom"),
    ("put", "/workflows/custom/x"),
    ("delete", "/workflows/custom/x"),
    ("post", "/workflows/custom/x/activate"),
    ("patch", "/memories/decisions/1"),
    ("delete", "/memories/decisions/1"),
    ("patch", "/memories/initiatives/1"),
    ("delete", "/memories/initiatives/1"),
    ("patch", "/memories/advice/1"),
    ("delete", "/memories/advice/1"),
    ("patch", "/watchlist/x"),
    ("delete", "/watchlist/x"),
    ("post", "/watchlist/x/approve"),
    ("post", "/watchlist/x/decline"),
    ("post", "/knowledge/builtin"),
    ("put", "/knowledge/builtin/d/f"),
    ("delete", "/knowledge/builtin/d/f"),
    ("post", "/knowledge/failures"),
    ("put", "/knowledge/failures/d/f"),
    ("delete", "/knowledge/failures/d/f"),
]


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setenv("BO_TENANT_ID", "tenant-a")
    monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@test")
    monkeypatch.setenv("BACKEND_PROXY_SECRET", "test-proxy-only")
    capture_audit(monkeypatch)
    app = FastAPI()
    for r in ROUTERS:
        app.include_router(r.router)
    bo_route.register_error_handlers(app)
    return TestClient(app)


def test_viewer_refuzat_pe_toate_scrierile(client):
    """Viewer primeste 403 pe FIECARE ruta de scriere — nu pe 400/404."""
    greseli = []
    for method, path in WRITES:
        r = client.request(method, path, headers=VIEWER,
                           json={}, params={})
        if r.status_code != 403:
            greseli.append((method, path, r.status_code))
    assert greseli == []


def test_anonim_refuzat_inainte_de_rol(client):
    """Fara identitate: 401/403, nu efect."""
    r = client.post("/personas", json={})
    assert r.status_code in (401, 403)


def test_citirile_raman_deschise_viewerului(client):
    """Citirile nu se strang: GET /personas merge pentru viewer."""
    r = client.get("/personas", headers=VIEWER)
    assert r.status_code == 200


def test_admin_pozitiv_scriere(client):
    """Adminul trece de gard — scrierea nu mai cade pe rol."""
    r = client.post("/personas", headers=ADMIN,
                    json={"display_name": "Test", "body": "x"})
    assert r.status_code in (200, 201, 400, 404, 422)
    assert r.status_code != 403
