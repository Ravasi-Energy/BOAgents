"""Regresii adverse pentru contractul aprobat CONTROL R2 (R3 corrective).

Acoperă explicit scenariile din decizie: revenire intenționată 20→10, bază
mai veche decât v−1, două taburi cu a treia scriere între ele, adăugare
concurentă, ștergere explicită, câmp/listă omisă păstrată, colecție goală =
no-op, refuz 409 înainte de orice efect, publish cu CAS obligatoriu.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from openexecutive.bo.bots import service as bot_service
from openexecutive.bo.bots import store as bot_store
from openexecutive.bo.routing import store as routing_store
from openexecutive.bo.settings import store as settings_store

TENANT = "probe"


def _content(steps: list[dict[str, Any]], *, cron: str | None = None) -> dict[str, Any]:
    trigger: dict[str, Any] = {"type": "manual"} if cron is None else {
        "type": "schedule", "cron": cron,
    }
    return {
        "schema_version": "bo.bobot.v1",
        "trigger": trigger,
        "steps": steps,
        "capability_refs": ["cap-unu"],
        "policy_refs": ["pol-unu"],
    }


def _step(sid: str, message: str) -> dict[str, str]:
    return {"id": sid, "type": "note", "message": message}


def _draft_steps(tenant: str, def_id: str) -> list[dict[str, Any]]:
    return list(bot_store.get_version(tenant, def_id, status="draft")["content"]["steps"])


def test_revert_20_to_10_is_a_legitimate_write(bo_db: Path) -> None:
    """Echo-repro CONTROL R2: valoare 20, scriitorul trimite 10 la versiunea
    citită — se APLICĂ (răspuns v3, valoare 10, nu echo-ignored)."""
    settings_store.set_value(
        TENANT, "bo.bobot.simulation.max_steps", 10, expected_version=0,
        actor="a@probe.local", db_path=bo_db,
    )
    settings_store.set_value(
        TENANT, "bo.bobot.simulation.max_steps", 20, expected_version=1,
        actor="a@probe.local", db_path=bo_db,
    )
    out = settings_store.set_value(
        TENANT, "bo.bobot.simulation.max_steps", 10, expected_version=2,
        actor="a@probe.local", db_path=bo_db,
    )
    assert out["version"] == 3
    assert out["value"] == 10


def test_omission_probe_step_edit_preserves_siblings(bo_db: Path) -> None:
    """Omission-repro CONTROL R3: [s1,s2,s3] + request [{s1,edit}] →
    s1 editat, s2+s3 păstrate (fără steps_remove)."""
    created = bot_service.create(TENANT, "a@probe.local", {
        "name": "Probe",
        "content": _content([_step("s1", "s1"), _step("s2", "s2"), _step("s3", "s3")]),
    }, db_path=bo_db)
    bot_service.update_draft(TENANT, "a@probe.local", created["id"], {
        "expected_version": created["draft_version"],
        "content": _content([_step("s1", "edit")]),
    }, db_path=bo_db)
    steps = _draft_steps(TENANT, created["id"])
    assert [s["id"] for s in steps] == ["s1", "s2", "s3"]
    assert steps[0]["message"] == "edit"


def test_base_older_than_vminus1_refused(bo_db: Path) -> None:
    """Scriitor cu bază v1 după încă două scrieri → 409 înainte de efect;
    starea rămâne intactă și clientul rebază explicit."""
    created = bot_service.create(TENANT, "a@probe.local", {
        "name": "Probe", "content": _content([_step("s1", "unu")]),
    }, db_path=bo_db)
    v2 = bot_service.update_draft(TENANT, "a@probe.local", created["id"], {
        "expected_version": created["draft_version"],
        "content": {"steps": [_step("s2", "doi")]},
    }, db_path=bo_db)
    bot_service.update_draft(TENANT, "a@probe.local", created["id"], {
        "expected_version": v2["draft_version"],
        "content": {"steps": [_step("s3", "trei")]},
    }, db_path=bo_db)
    with pytest.raises(bot_store.ConflictError):
        bot_service.update_draft(TENANT, "b@probe.local", created["id"], {
            "expected_version": created["draft_version"],  # v1 < v-2
            "content": {"trigger": {"type": "schedule", "cron": "0 6 * * *"}},
        }, db_path=bo_db)
    steps = _draft_steps(TENANT, created["id"])
    assert [s["id"] for s in steps] == ["s1", "s2", "s3"]


def test_two_tabs_third_write_between_rebase_flow(bo_db: Path) -> None:
    """Tab A scrie la v1→v2; tab B (baza v1) refuzat 409; B reîncarcă și
    aplică doar delta proprie — ambele câmpuri supraviețuiesc."""
    created = bot_service.create(TENANT, "a@probe.local", {
        "name": "Probe", "content": _content([_step("s1", "unu")]),
    }, db_path=bo_db)
    tab_a = bot_service.update_draft(TENANT, "a@probe.local", created["id"], {
        "expected_version": created["draft_version"],
        "content": {"steps": [_step("s1", "mesaj-A")]},
    }, db_path=bo_db)
    with pytest.raises(bot_store.ConflictError):
        bot_service.update_draft(TENANT, "b@probe.local", created["id"], {
            "expected_version": created["draft_version"],
            "content": _content([_step("s1", "unu")], cron="0 6 * * *"),
        }, db_path=bo_db)
    bot_service.update_draft(TENANT, "b@probe.local", created["id"], {
        "expected_version": tab_a["draft_version"],
        "content": {"trigger": {"type": "schedule", "cron": "0 6 * * *"}},
    }, db_path=bo_db)
    content = bot_store.get_version(TENANT, created["id"], status="draft")["content"]
    assert content["steps"][0]["message"] == "mesaj-A"
    assert content["trigger"]["cron"] == "0 6 * * *"


def test_concurrent_adds_and_explicit_delete(bo_db: Path) -> None:
    """Adăugări concurente supraviețuiesc; steps_remove șterge doar id-ul
    numit și este idempotent pe id inexistent."""
    created = bot_service.create(TENANT, "a@probe.local", {
        "name": "Probe", "content": _content([_step("s1", "unu"), _step("s2", "doi")]),
    }, db_path=bo_db)
    v = bot_service.update_draft(TENANT, "a@probe.local", created["id"], {
        "expected_version": created["draft_version"],
        "content": {"steps": [_step("s3", "trei")]},
    }, db_path=bo_db)
    v = bot_service.update_draft(TENANT, "b@probe.local", created["id"], {
        "expected_version": v["draft_version"],
        "content": {"steps": [_step("s4", "patru")]},
    }, db_path=bo_db)
    bot_service.update_draft(TENANT, "b@probe.local", created["id"], {
        "expected_version": v["draft_version"],
        "steps_remove": ["s2", "id-inexistent"],
    }, db_path=bo_db)
    assert [s["id"] for s in _draft_steps(TENANT, created["id"])] == ["s1", "s3", "s4"]


def test_empty_collections_are_noops_everywhere(bo_db: Path) -> None:
    """steps=[], capability_refs=[] și câmp omis — nimic nu se șterge."""
    created = bot_service.create(TENANT, "a@probe.local", {
        "name": "Probe", "content": _content([_step("s1", "unu")]),
    }, db_path=bo_db)
    bot_service.update_draft(TENANT, "a@probe.local", created["id"], {
        "expected_version": created["draft_version"],
        "content": {"steps": [], "capability_refs": []},
    }, db_path=bo_db)
    content = bot_store.get_version(TENANT, created["id"], status="draft")["content"]
    assert len(content["steps"]) == 1
    assert content["capability_refs"] == ["cap-unu"]
    assert content["policy_refs"] == ["pol-unu"]


def test_publish_requires_expected_version(bo_db: Path) -> None:
    """CONTROL R2: publish fără expected_version = refuz (fără fallback la
    «publică ce e acum»); versiune stale = 409 fără efect."""
    created = bot_service.create(TENANT, "a@probe.local", {
        "name": "Probe", "content": _content([_step("s1", "unu")]),
    }, db_path=bo_db)
    with pytest.raises(TypeError):
        bot_service.publish(TENANT, "a@probe.local", created["id"], db_path=bo_db)  # type: ignore[call-arg]
    with pytest.raises(bot_service.ValidationFailure):
        bot_service.publish(
            TENANT, "a@probe.local", created["id"],
            expected_version=None, db_path=bo_db,  # type: ignore[arg-type]
        )
    with pytest.raises(bot_store.ConflictError):
        bot_service.publish(
            TENANT, "a@probe.local", created["id"],
            expected_version=99, db_path=bo_db,
        )
    definition = bot_store.get_definition(TENANT, created["id"], db_path=bo_db)
    assert definition["active_version_no"] is None


def test_publish_http_missing_version_is_422(bo_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """HTTP: body-ul publish fără expected_version → 422, nu publish."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive.api.routes.bo import register_error_handlers
    from openexecutive.api.routes.bo import router as bo_router

    app = FastAPI()
    register_error_handlers(app)
    app.include_router(bo_router)
    client = TestClient(app)
    admin = {"x-caller-email": "admin@probe.local",
             "x-caller-proxy-secret": "proxy-secret-probe"}
    created = client.post("/bo/bots", headers=admin, json={
        "name": "Probe", "content": _content([_step("s1", "unu")]),
    })
    assert created.status_code == 201
    bot_id = created.json()["bot"]["id"]
    assert client.post(f"/bo/bots/{bot_id}/publish", headers=admin,
                       ).status_code == 422
    assert client.post(f"/bo/bots/{bot_id}/publish", headers=admin,
                       json={}).status_code == 422
    ok = client.post(f"/bo/bots/{bot_id}/publish", headers=admin,
                     json={"expected_version": created.json()["bot"]["draft_version"]})
    assert ok.status_code == 200


def test_catalog_upsert_and_explicit_remove(bo_db: Path) -> None:
    """Catalog: capabilities/regions fac union; doar *_remove șterge;
    câmp omis = păstrat; versiune stale = 409."""
    entry = routing_store.create_entry(TENANT, {
        "provider": "anthropic", "model_id": "m", "model_version": "v1",
        "state": "ACTIVE", "capabilities": ["a", "b"], "regions": ["eu"],
        "cost": {"input_per_million": "1.00", "output_per_million": "2.00",
                 "currency": "EUR", "valid_until": "2027-01-01"},
        "quality": None, "purpose": "p1", "source": "admin",
    }, actor="a@probe.local", db_path=bo_db)
    v = routing_store.update_entry(
        TENANT, entry.entry_id, {"capabilities": ["c"]},
        expected_version=entry.version, actor="b@probe.local", db_path=bo_db,
    )
    saved = routing_store.get_entry(TENANT, entry.entry_id, db_path=bo_db)
    assert sorted(saved.capabilities) == ["a", "b", "c"]
    routing_store.update_entry(
        TENANT, entry.entry_id, {"capabilities_remove": ["a"]},
        expected_version=v.version, actor="b@probe.local", db_path=bo_db,
    )
    saved = routing_store.get_entry(TENANT, entry.entry_id, db_path=bo_db)
    assert sorted(saved.capabilities) == ["b", "c"]
    with pytest.raises(routing_store.ConflictError):
        routing_store.update_entry(
            TENANT, entry.entry_id, {"purpose": "stale"},
            expected_version=entry.version, actor="b@probe.local", db_path=bo_db,
        )


def test_trust_store_keyed_upsert_and_remove(bo_db: Path) -> None:
    """Trust store: publishers fac upsert pe publisherId; publishers_remove
    șterge explicit; câmpurile omise se păstrează."""
    from .test_pd_data_loss import _trust

    settings_store.set_value(
        TENANT, "bo.packages.trust_store_json", _trust(["p1", "p2"]),
        expected_version=0, actor="a@probe.local", db_path=bo_db,
    )
    added = json.loads(_trust(["p3"]))
    settings_store.set_value(
        TENANT, "bo.packages.trust_store_json",
        json.dumps({"publishers": added["publishers"], "keys": added["keys"],
                    "publishers_remove": ["p1"], "keys_remove": ["key-p1"]}),
        expected_version=1, actor="b@probe.local", db_path=bo_db,
    )
    saved = json.loads(settings_store.get_effective_value(
        TENANT, "bo.packages.trust_store_json", db_path=bo_db,
    ))
    assert [p["publisherId"] for p in saved["publishers"]] == ["p2", "p3"]
    assert [k["keyId"] for k in saved["keys"]] == ["key-p2", "key-p3"]
    assert saved["policy"]["policyVersion"] == "pol-1"
    assert "publishers_remove" not in saved


def test_csv_remove_op_and_empty_noop(bo_db: Path) -> None:
    """CSV: value = delta de adăugat, remove = op explicit de șters,
    gol = no-op; remove pe chei non-csv = refuz."""
    settings_store.set_value(
        TENANT, "bo.router.allowed_providers", "unu,doi,trei",
        expected_version=0, actor="a@probe.local", db_path=bo_db,
    )
    settings_store.set_value(
        TENANT, "bo.router.allowed_providers", "patru",
        expected_version=1, actor="b@probe.local", db_path=bo_db,
    )
    settings_store.set_value(
        TENANT, "bo.router.allowed_providers", "",
        expected_version=2, actor="b@probe.local", db_path=bo_db,
        remove=["unu"],
    )
    value = settings_store.get_effective_value(
        TENANT, "bo.router.allowed_providers", db_path=bo_db,
    )
    assert value == "doi,trei,patru"
    with pytest.raises(settings_store.SettingValidationError):
        settings_store.set_value(
            TENANT, "bo.ui.display_name", "x",
            expected_version=0, actor="b@probe.local", db_path=bo_db,
            remove=["unu"],
        )
