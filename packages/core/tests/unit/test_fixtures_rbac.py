"""RBAC for the /fixtures routes (F-1, REM-AUDIT-18).

Every mutating fixture route swaps, wipes or generates company state —
the same blast radius as client slots — so each requires ``fixtures:write``
(admin) and must refuse BEFORE any write, generation or external call.
Read routes (list/status) stay viewer-safe.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import bo as bo_route
from openexecutive.api.routes import fixtures as fixtures_route

from .bo_testkit import capture_audit, use_tmp_db

ADMIN = {"x-caller-email": "admin@test", "x-caller-proxy-secret": "test-proxy-only"}
VIEWER = {"x-caller-email": "viewer@test", "x-caller-proxy-secret": "test-proxy-only"}
OPERATOR = {"x-api-key": "svc-key"}  # service identity, no user email


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setenv("BO_TENANT_ID", "tenant-a")
    monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@test")
    monkeypatch.setenv("BACKEND_PROXY_SECRET", "test-proxy-only")
    capture_audit(monkeypatch)
    app = FastAPI()
    app.include_router(fixtures_route.router)
    bo_route.register_error_handlers(app)
    return TestClient(app)


def _spy_async(monkeypatch: pytest.MonkeyPatch, module: str, name: str) -> list:
    """Replace an async callable with a recorder; returns the call list."""
    calls: list = []

    async def _fake(*args, **kwargs):
        calls.append((args, kwargs))
        return {"ok": True}

    monkeypatch.setattr(f"{module}.{name}", _fake)
    return calls


# --------------------------------------------------------------------------- #
# Mutating routes — refusal happens before any effect
# --------------------------------------------------------------------------- #

MUTATING = [
    ("post", "/fixtures/snapshot", {}),
    ("post", "/fixtures/reset", {}),
    ("post", "/fixtures/unload", {}),
    ("post", "/fixtures/demo/load", {}),
    ("post", "/fixtures/generate", {"description": "acme"}),
    ("post", "/fixtures", {"bundle": {"profile": {}}}),
    ("delete", "/fixtures/demo", {}),
]


@pytest.mark.parametrize("method,path,payload", MUTATING)
@pytest.mark.parametrize("headers", [VIEWER, OPERATOR])
def test_mutating_routes_refuse_non_admin(
    client: TestClient,
    method: str,
    path: str,
    payload: dict,
    headers: dict,
) -> None:
    # Well-formed bodies: validation must pass so the refusal observed is
    # the authorization check inside the handler, not pydantic's 422.
    resp = client.request(method, path, headers=headers, json=payload)
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert body.get("error") == "forbidden"


def test_generate_refusal_happens_before_llm_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused /fixtures/generate must never reach the LLM."""
    calls = _spy_async(
        monkeypatch,
        "openexecutive.fixtures.generator",
        "generate_fixture_bundle",
    )
    resp = client.post(
        "/fixtures/generate", headers=VIEWER, json={"description": "acme"}
    )
    assert resp.status_code == 403
    assert calls == []


@pytest.mark.parametrize(
    "route_call",
    [
        ("post", "/fixtures/snapshot", "snapshot_user_state_async"),
        ("post", "/fixtures/reset", "reset_all_state"),
        ("post", "/fixtures/unload", "unload_fixture"),
        ("post", "/fixtures/demo/load", "load_fixture_any"),
    ],
)
def test_mutating_routes_refuse_before_any_write(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    route_call: tuple[str, str, str],
) -> None:
    method, path, fn = route_call
    calls = _spy_async(
        monkeypatch, "openexecutive.cli.fixture_loader", fn
    )
    resp = client.request(method, path, headers=VIEWER, json={})
    assert resp.status_code == 403
    assert calls == []  # nothing wrote, nothing loaded


def test_create_refusal_happens_before_store_write(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list = []
    from openexecutive.fixtures import store as fixtures_store

    def _fake_insert(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(fixtures_store, "insert_fixture", _fake_insert)
    resp = client.post(
        "/fixtures", headers=OPERATOR, json={"bundle": {"profile": {}}}
    )
    assert resp.status_code == 403
    assert calls == []


# --------------------------------------------------------------------------- #
# Admin still reaches the real logic
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "method,path,fn",
    [
        ("post", "/fixtures/snapshot", "snapshot_user_state_async"),
        ("post", "/fixtures/reset", "reset_all_state"),
        ("post", "/fixtures/unload", "unload_fixture"),
        ("post", "/fixtures/demo/load", "load_fixture_any"),
    ],
)
def test_admin_passes_the_gate(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    fn: str,
) -> None:
    calls = _spy_async(
        monkeypatch, "openexecutive.cli.fixture_loader", fn
    )
    resp = client.request(method, path, headers=ADMIN, json={})
    # The spy returned {"ok": True} — the handler ran past authorization.
    assert resp.status_code == 200, resp.text
    assert calls != []


def test_admin_generate_reaches_the_model_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admin passes authz; a GenerationError surfaces as 422, not 403."""
    from openexecutive.fixtures import generator

    async def _raise(*args, **kwargs):
        raise generator.GenerationError("model output unusable")

    monkeypatch.setattr(generator, "generate_fixture_bundle", _raise)
    resp = client.post(
        "/fixtures/generate", headers=ADMIN, json={"description": "acme"}
    )
    assert resp.status_code == 422
    assert "unusable" in resp.json()["detail"]


def test_admin_delete_reaches_the_store(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.fixtures import store as fixtures_store

    monkeypatch.setattr(fixtures_store, "delete_fixture", lambda name: False)
    resp = client.request("delete", "/fixtures/demo", headers=ADMIN)
    assert resp.status_code == 404  # authz passed; the fixture just isn't there


# --------------------------------------------------------------------------- #
# Read routes stay viewer-safe; unauthenticated is refused on public deploys
# --------------------------------------------------------------------------- #

def test_read_routes_viewer_ok(client: TestClient) -> None:
    resp = client.get("/fixtures/status", headers=VIEWER)
    assert resp.status_code == 200, resp.text
    resp = client.get("/fixtures", headers=VIEWER)
    assert resp.status_code == 200, resp.text


def test_unauthenticated_refused_on_public_deployment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", "1")
    resp = client.request("post", "/fixtures/reset", json={})
    assert resp.status_code == 401


def test_wrong_proxy_secret_refused(client: TestClient) -> None:
    headers = {"x-caller-email": "admin@test", "x-caller-proxy-secret": "wrong"}
    resp = client.request("post", "/fixtures/unload", headers=headers)
    assert resp.status_code == 401


def test_tenant_mismatch_refused_without_disclosure(
    client: TestClient,
) -> None:
    headers = {**ADMIN, "x-bo-tenant": "tenant-b"}
    resp = client.request("post", "/fixtures/reset", headers=headers)
    assert resp.status_code == 403
    assert "tenant-a" not in resp.text
