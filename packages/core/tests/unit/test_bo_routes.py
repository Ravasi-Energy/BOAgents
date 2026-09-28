"""HTTP-level tests for the /bo routes: authz, CAS, validation, simulation."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import bo as bo_route
from openexecutive.bo.bots import examples
from openexecutive.bo.settings.registry import REGISTRY

from .bo_testkit import capture_audit, use_tmp_db

ADMIN = {"x-caller-email": "admin@test", "x-caller-proxy-secret": "test-proxy-only"}
VIEWER = {"x-caller-email": "viewer@test", "x-caller-proxy-secret": "test-proxy-only"}
OPERATOR = {"x-api-key": "svc-key"}  # service identity, no user email
OTHER_TENANT = {**ADMIN, "x-bo-tenant": "tenant-b"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setenv("BO_TENANT_ID", "tenant-a")
    monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@test")
    monkeypatch.setenv("BACKEND_PROXY_SECRET", "test-proxy-only")
    capture_audit(monkeypatch)
    app = FastAPI()
    app.include_router(bo_route.router)
    bo_route.register_error_handlers(app)
    return TestClient(app)


def _make_bot(client: TestClient) -> str:
    resp = client.post("/bo/bots", headers=ADMIN, json=dict(examples.HEARTBEAT_STALE))
    assert resp.status_code == 201, resp.text
    return resp.json()["bot"]["id"]


def _publish(client: TestClient, bot_id: str) -> None:
    resp = client.post(f"/bo/bots/{bot_id}/publish", headers=ADMIN)
    assert resp.status_code == 200, resp.text


# --------------------------------------------------------------------------- #
# Settings API
# --------------------------------------------------------------------------- #

def test_settings_list_viewer_ok(client: TestClient) -> None:
    resp = client.get("/bo/settings", headers=VIEWER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["tenant"] == "tenant-a"
    assert body["role"] == "viewer"
    assert len(body["settings"]) == len(REGISTRY)
    assert all(s["origin"] == "default" for s in body["settings"])


def test_settings_write_requires_admin(client: TestClient) -> None:
    resp = client.put("/bo/settings/bo.ui.language", headers=VIEWER,
                      json={"value": "en", "expected_version": 0})
    assert resp.status_code == 403
    assert resp.json()["error"] == "forbidden"

    resp = client.put("/bo/settings/bo.ui.language", headers=ADMIN,
                      json={"value": "en", "expected_version": 0})
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == "SAVED"
    assert body["applied"] is True  # IMMEDIATE
    assert body["setting"]["version"] == 1

    # Service identity cannot write configuration either.
    resp = client.put("/bo/settings/bo.ui.language", headers=OPERATOR,
                      json={"value": "ro", "expected_version": 1})
    assert resp.status_code == 403


def test_settings_invalid_and_conflict(client: TestClient) -> None:
    resp = client.put("/bo/settings/bo.ui.language", headers=ADMIN,
                      json={"value": "fr", "expected_version": 0})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_value"

    resp = client.put("/bo/settings/no.such.key", headers=ADMIN,
                      json={"value": 1, "expected_version": 0})
    assert resp.status_code == 404
    assert resp.json()["error"] == "unknown_setting"

    client.put("/bo/settings/bo.ui.language", headers=ADMIN,
               json={"value": "en", "expected_version": 0})
    resp = client.put("/bo/settings/bo.ui.language", headers=ADMIN,
                      json={"value": "ro", "expected_version": 0})
    assert resp.status_code == 409
    assert resp.json()["error"] == "version_conflict"


def test_tenant_mismatch_refused_without_disclosure(client: TestClient) -> None:
    resp = client.get("/bo/settings", headers=OTHER_TENANT)
    assert resp.status_code == 403
    assert resp.json()["error"] == "tenant_mismatch"
    # The refusal must not reveal the configured tenant id.
    assert "tenant-a" not in resp.text


def test_unauthenticated_public_deployment(client: TestClient,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", "1")
    resp = client.get("/bo/settings")
    assert resp.status_code == 401


# --------------------------------------------------------------------------- #
# BoBots API
# --------------------------------------------------------------------------- #

def test_bot_lifecycle_over_http(client: TestClient) -> None:
    # viewer cannot create
    assert client.post("/bo/bots", headers=VIEWER,
                       json=dict(examples.HEARTBEAT_STALE)).status_code == 403

    bot_id = _make_bot(client)
    detail = client.get(f"/bo/bots/{bot_id}", headers=VIEWER).json()
    assert detail["bot"]["status"] == "draft"
    assert detail["active"] is None
    assert detail["draft"]["version_no"] == 1

    # simulation requires a published version
    assert client.post(f"/bo/bots/{bot_id}/simulate", headers=ADMIN,
                       json={"input": {}}).status_code == 422

    _publish(client, bot_id)
    detail = client.get(f"/bo/bots/{bot_id}", headers=VIEWER).json()
    assert detail["bot"]["active_version_no"] == 1

    resp = client.post(f"/bo/bots/{bot_id}/simulate", headers=OPERATOR, json={
        "input": {"service": {"name": "billing",
                              "last_heartbeat_age_minutes": 45}}})
    assert resp.status_code == 201, resp.text
    run = resp.json()["run"]
    assert run["result"]["findings"][0]["finding_key"] == "heartbeat-stale"
    assert run["result"]["external_effects"] == 0

    # same input + same version → same plan hash (deterministic)
    again = client.post(f"/bo/bots/{bot_id}/simulate", headers=OPERATOR, json={
        "input": {"service": {"name": "billing",
                              "last_heartbeat_age_minutes": 45}}})
    assert again.json()["run"]["plan_hash"] == run["plan_hash"]

    runs = client.get(f"/bo/bots/{bot_id}/runs", headers=VIEWER).json()["runs"]
    assert len(runs) == 2
    timeline = client.get(f"/bo/runs/{run['id']}", headers=VIEWER).json()["run"]
    assert len(timeline["timeline"]) == 4


def test_viewer_cannot_simulate(client: TestClient) -> None:
    bot_id = _make_bot(client)
    _publish(client, bot_id)
    resp = client.post(f"/bo/bots/{bot_id}/simulate", headers=VIEWER,
                       json={"input": {}})
    assert resp.status_code == 403


def test_draft_patch_cas_over_http(client: TestClient) -> None:
    bot_id = _make_bot(client)
    resp = client.patch(f"/bo/bots/{bot_id}", headers=ADMIN,
                        json={"expected_version": 99, "name": "x"})
    assert resp.status_code == 409

    resp = client.patch(f"/bo/bots/{bot_id}", headers=ADMIN,
                        json={"expected_version": 1, "name": "Nou nume"})
    assert resp.status_code == 200
    assert resp.json()["bot"]["name"] == "Nou nume"
    assert resp.json()["bot"]["draft_version"] == 2


def test_invalid_bot_rejected_over_http(client: TestClient) -> None:
    bad = dict(examples.HEARTBEAT_STALE)
    bad["content"] = {"schema_version": "bo.bobot.v1",
                      "trigger": {"type": "manual"},
                      "steps": [{"id": "s", "type": "check"}]}
    resp = client.post("/bo/bots", headers=ADMIN, json=bad)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_definition"


def test_cross_tenant_bot_invisible(client: TestClient) -> None:
    # A bot id that doesn't exist in this tenant is 404, never a leak.
    resp = client.get("/bo/bots/bot_nonexistent", headers=VIEWER)
    assert resp.status_code == 404


# --------------------------------------------------------------------------- #
# Telemetry API
# --------------------------------------------------------------------------- #

def test_telemetry_status_disabled_by_default(client: TestClient) -> None:
    resp = client.get("/bo/telemetry/status", headers=VIEWER)
    assert resp.status_code == 200
    body = resp.json()
    assert body["enabled"] is False
    assert body["schema_version"] == "bo.telemetry.v1"


def test_telemetry_validate_endpoint(client: TestClient) -> None:
    fixtures = Path(__file__).resolve().parents[4] / "fixtures/bo/telemetry"
    good = json.loads((fixtures / "valid/heartbeat.json").read_text())
    resp = client.post("/bo/telemetry/validate", headers=VIEWER, json=good)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    bad = json.loads((fixtures / "invalid/extra_envelope_field.json").read_text())
    resp = client.post("/bo/telemetry/validate", headers=VIEWER, json=bad)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_telemetry"
