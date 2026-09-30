"""HTTP-level tests for the /clients routes.

Covers the FastAPI contract (status codes, error mapping, payload shapes)
over the slot machinery; the deeper save/restore behavior is covered by
``test_client_slots.py``.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import bo as bo_route
from openexecutive.api.routes import clients as route
from openexecutive.clients import slots


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    company = tmp_path / "company"
    company.mkdir()
    settings = SimpleNamespace(
        company_profile_path=company / "profile.yaml",
        vector_store_path=tmp_path / "chroma",
        mcp_servers_config_path=company / "mcp_servers.json",
        honcho_workspace_id="default-ws",
    )
    settings.company_profile_path.write_text("name: Live Co\n")

    db_path = tmp_path / "episodic.db"
    from openexecutive import config
    from openexecutive.memory import episodic

    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    async def _no_vector(_settings: Any, _app_state: Any, *, store: Any = None) -> int:
        return 0

    monkeypatch.setattr(slots, "_rebuild_vector_state", _no_vector)
    monkeypatch.setattr(slots, "_set_honcho_client_workspace", lambda _slug: None)
    monkeypatch.setattr(slots, "_reseed_blank_defaults", lambda **kw: None)
    monkeypatch.setattr(slots, "snapshot_user_state", lambda _s: None)

    app = FastAPI()
    app.include_router(route.router)
    bo_route.register_error_handlers(app)
    return TestClient(app)


def test_list_starts_empty_with_no_active(client: TestClient) -> None:
    body = client.get("/clients").json()
    assert body == {
        "active": None,
        "fixture_active": None,
        "rotation_in_progress": False,
        "clients": [],
    }


def test_create_activate_save_delete_lifecycle(client: TestClient) -> None:
    # Create from current → becomes active.
    resp = client.post(
        "/clients", json={"display_name": "Acme Corp", "source": "current"}
    )
    assert resp.status_code == 200
    assert resp.json()["slug"] == "acme_corp"
    assert resp.json()["active"] is True

    body = client.get("/clients").json()
    assert body["active"] == "acme_corp"
    assert [c["slug"] for c in body["clients"]] == ["acme_corp"]

    # Blank second client, then switch to it.
    assert (
        client.post(
            "/clients", json={"display_name": "Beta", "source": "blank"}
        ).status_code
        == 200
    )
    resp = client.post("/clients/beta/activate")
    assert resp.status_code == 200
    assert resp.json()["slug"] == "beta"
    assert resp.json()["previous"] == "acme_corp"
    assert client.get("/clients").json()["active"] == "beta"

    # Checkpoint without switching.
    assert client.post("/clients/save").json()["slug"] == "beta"

    # Active client refuses deletion; parked one deletes.
    assert client.delete("/clients/beta").status_code == 409
    assert client.delete("/clients/acme_corp").status_code == 200
    assert [c["slug"] for c in client.get("/clients").json()["clients"]] == ["beta"]


def test_error_mapping(client: TestClient) -> None:
    # Unknown slot → 404.
    assert client.post("/clients/nope/activate").status_code == 404
    assert client.delete("/clients/nope").status_code == 404
    # No active client → save conflicts.
    assert client.post("/clients/save").status_code == 409
    # Bad input → 400.
    assert (
        client.post("/clients", json={"display_name": "", "source": "current"}).status_code
        == 400
    )
    assert (
        client.post(
            "/clients", json={"display_name": "X", "source": "weird"}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/clients", json={"display_name": "X", "slug": "Bad Slug!"}
        ).status_code
        == 400
    )
    # Duplicate → 409.
    client.post("/clients", json={"display_name": "Acme", "source": "blank"})
    assert (
        client.post(
            "/clients", json={"display_name": "Acme", "slug": "acme", "source": "blank"}
        ).status_code
        == 409
    )


def test_refuses_while_fixture_active(client: TestClient, tmp_path: Path) -> None:
    backup = tmp_path / "company" / "_user_backup"
    backup.mkdir(parents=True)
    (backup / ".fixture_active").write_text("halcyon_motors")

    body = client.get("/clients").json()
    assert body["fixture_active"] == "halcyon_motors"
    assert (
        client.post(
            "/clients", json={"display_name": "Acme", "source": "current"}
        ).status_code
        == 409
    )


# ── SEC-09: real ASGI route reproduces the blocked/identity failure ─────────


@pytest.fixture()
def refusing_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TestClient, dict[str, bool], SimpleNamespace]:
    """Same harness as ``client`` but the vector rebuild layer refuses on
    demand — the post-preflight refusal point (SEC-08's delete_company_docs
    lives inside _rebuild_vector_state). The activation path, recovery and
    marker machinery all run for real through POST /clients/{slug}/activate.
    """
    company = tmp_path / "company"
    company.mkdir()
    settings = SimpleNamespace(
        company_profile_path=company / "profile.yaml",
        vector_store_path=tmp_path / "chroma",
        mcp_servers_config_path=company / "mcp_servers.json",
        honcho_workspace_id="default-ws",
        client_rotation_enabled=False,
    )
    settings.company_profile_path.write_text("name: Live Co\n")

    db_path = tmp_path / "episodic.db"
    from openexecutive import config
    from openexecutive.memory import episodic

    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(slots, "_set_honcho_client_workspace", lambda _slug: None)
    monkeypatch.setattr(slots, "_reseed_blank_defaults", lambda **kw: None)
    monkeypatch.setattr(slots, "snapshot_user_state", lambda _s: None)

    from openexecutive.knowledge.store import PersistedEmbeddingConfigError

    refusing = {"on": False}

    async def _maybe_refuse(
        _settings: Any, _app_state: Any, *, store: Any = None
    ) -> int:
        if refusing["on"]:
            if refusing.get("once"):
                refusing["on"] = False
            raise PersistedEmbeddingConfigError(
                "collection 'company_docs': refused (synthetic)"
            )
        return 0

    monkeypatch.setattr(slots, "_rebuild_vector_state", _maybe_refuse)

    app = FastAPI()
    app.include_router(route.router)
    # The refusal is a real unhandled server error on this path — let it
    # surface as a 500 response instead of re-raising into the test.
    return TestClient(app, raise_server_exceptions=False), refusing, settings


def test_activate_route_late_refusal_auto_recovers_and_blocks(
    refusing_client: tuple[TestClient, dict[str, bool], SimpleNamespace],
) -> None:
    """Through the real POST /clients/{slug}/activate route — no test-side
    repair before inspecting the failure state."""
    client, refusing, settings = refusing_client
    assert (
        client.post(
            "/clients", json={"display_name": "Acme", "source": "blank"}
        ).status_code
        == 200
    )
    assert (
        client.post(
            "/clients", json={"display_name": "Beta", "source": "blank"}
        ).status_code
        == 200
    )
    assert client.post("/clients/acme/activate").status_code == 200

    # One-shot refusal during B's restore → 500, automatic recovery to A
    # (the flag disarms on the first raise, so the recovery restore — which
    # re-runs the same layer — succeeds).
    refusing["once"] = True
    refusing["on"] = True
    resp = client.post("/clients/beta/activate")
    assert resp.status_code == 500
    assert client.get("/clients").json()["active"] == "acme"
    assert slots.get_restore_blocked(settings) is None

    # Persistent refusal → the second failure blocks the instance.
    refusing["once"] = False
    refusing["on"] = True
    resp = client.post("/clients/beta/activate")
    assert resp.status_code == 400
    assert "restore-blocked" in resp.json()["detail"]
    marker = slots.get_restore_blocked(settings)
    assert marker is not None and marker["restore_slug"] == "acme"
    assert client.get("/clients").json()["active"] is None

    # Retrying B stays refused; only the recorded recovery slug can proceed.
    assert client.post("/clients/beta/activate").status_code == 400
    assert client.post("/clients/acme/activate").status_code == 400
    refusing["on"] = False
    assert client.post("/clients/acme/activate").status_code == 200
    assert slots.get_restore_blocked(settings) is None
    assert client.get("/clients").json()["active"] == "acme"


# ---------------------------------------------------------------- B4
# Server-side BO identity/capability gate on client-slot mutations
# (REM-AUDIT-01): the shared-secret transport gate alone cannot tell a
# viewer session from an admin one — every mutation must refuse
# viewer/operator BEFORE any slot effect runs.

_RBAC_ENV = {
    "BO_TENANT_ID": "tenant-a",
    "BO_ADMIN_EMAILS": "admin@test",
    "BACKEND_PROXY_SECRET": "test-proxy-only",
}
_RBAC_ADMIN = {
    "x-caller-email": "admin@test",
    "x-caller-proxy-secret": "test-proxy-only",
}
_RBAC_VIEWER = {
    "x-caller-email": "viewer@test",
    "x-caller-proxy-secret": "test-proxy-only",
}
_RBAC_OPERATOR = {"x-api-key": "svc-key"}  # service identity, no user


def _rbac_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in _RBAC_ENV.items():
        monkeypatch.setenv(k, v)


def _mutation_requests(client: TestClient, headers: dict[str, str]) -> list:
    """One request per mutating endpoint — the set B4 requires to refuse
    non-admins before any effect."""
    return [
        client.post(
            "/clients", headers=headers,
            json={"display_name": "X", "source": "blank"},
        ),
        client.post("/clients/save", headers=headers),
        client.post("/clients/ghost/activate", headers=headers),
        client.patch(
            "/clients/ghost", headers=headers, json={"status": "active"}
        ),
        client.delete("/clients/ghost", headers=headers),
    ]


def test_viewer_refused_before_any_effect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rbac_env(monkeypatch)
    for resp in _mutation_requests(client, _RBAC_VIEWER):
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "forbidden"
    # No slot was created as a side effect of the refused calls.
    assert client.get("/clients", headers=_RBAC_ADMIN).json()["clients"] == []


def test_operator_refused_before_any_effect(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rbac_env(monkeypatch)
    for resp in _mutation_requests(client, _RBAC_OPERATOR):
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"] == "forbidden"
    assert client.get("/clients", headers=_RBAC_ADMIN).json()["clients"] == []


def test_admin_mutations_still_work(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rbac_env(monkeypatch)
    resp = client.post(
        "/clients", headers=_RBAC_ADMIN,
        json={"display_name": "Acme", "source": "blank"},
    )
    assert resp.status_code == 200, resp.text


def test_forged_delegation_without_proxy_secret_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller asserting an admin email WITHOUT the proxy delegation
    credential must be refused — headers alone never authenticate."""
    _rbac_env(monkeypatch)
    forged = {"x-caller-email": "admin@test"}  # no proxy secret
    resp = client.post(
        "/clients", headers=forged,
        json={"display_name": "X", "source": "blank"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"] == "unauthenticated"
    assert client.get("/clients", headers=_RBAC_ADMIN).json()["clients"] == []


def test_viewer_reads_allowed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Read-only surfaces stay open to viewers — the gate is on writes."""
    _rbac_env(monkeypatch)
    assert client.get("/clients", headers=_RBAC_VIEWER).status_code == 200
    assert (
        client.get("/clients/cockpit", headers=_RBAC_VIEWER).status_code == 200
    )


def test_wrong_tenant_hint_refused(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _rbac_env(monkeypatch)
    resp = client.post(
        "/clients",
        headers={**_RBAC_ADMIN, "x-bo-tenant": "tenant-b"},
        json={"display_name": "X", "source": "blank"},
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == "tenant_mismatch"
