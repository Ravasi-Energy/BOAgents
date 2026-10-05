"""CONTROL R12 — nested trust[] merge, durable profile CAS, People clear.

Regression probes for the three contract gaps the R12 board assigns to
A01 after the UI patch integration:

- ``bo.packages.trust_store_json`` — a keyed row update must not wipe the
  row's nested lists (``allowedKinds: []`` on a partial update was a
  silent 3→0 clear on ``4ec3ae7``). Nested removal is only via
  ``<field>_remove`` ops, consumed by the merge — never persisted.
- ``PATCH /company-profile`` — durable CAS: ``version`` persists in
  profile.yaml (survives reload), ``expected_version`` is required (422
  absent, 409 stale), a matched write bumps exactly once.
- ``PATCH /people/{id}`` — ``clear_*`` ops are the only way to null a
  contact field (JSON null → 422 ambiguous), scope/availability updates
  ride the same fenced transaction, every mutating write bumps the CAS
  version once and emits ``people_person_updated`` to the audit log.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
import yaml

from openexecutive.bo.settings import store as settings_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.people import store as people_store

from .test_http_mutation_gates import ADMIN, VIEWER, _client
from .test_pd_data_loss import _trust

TENANT = "probe"


# --------------------------------------------------------------------------- #
# Nested trust: keyed-row updates must not wipe nested lists
# --------------------------------------------------------------------------- #


def _set(db: Path, doc: dict, version: int) -> None:
    settings_store.set_value(
        TENANT, "bo.packages.trust_store_json", json.dumps(doc),
        expected_version=version, actor="admin@probe.local", db_path=db,
    )


def _read(db: Path) -> dict:
    return json.loads(settings_store.get_effective_value(
        TENANT, "bo.packages.trust_store_json", db_path=db,
    ))


def test_r12_trust_nested_empty_list_preserves(bo_db: Path) -> None:
    """The R8 defect: a keyed row delta carrying ``allowedKinds: []`` wiped
    the stored list. ``[]`` must now be a no-op, never an implicit clear."""
    _set(bo_db, json.loads(_trust(["pub-1", "pub-2", "pub-3"])), 0)
    delta = {"publishers": [{"publisherId": "pub-1", "allowedKinds": []}]}
    _set(bo_db, delta, 1)
    pubs = {p["publisherId"]: p for p in _read(bo_db)["publishers"]}
    assert pubs["pub-1"]["allowedKinds"] == ["bobot"]
    assert pubs["pub-1"]["keyIds"] == ["key-pub-1"]
    assert len(pubs) == 3


def test_r12_trust_nested_list_union_add(bo_db: Path) -> None:
    _set(bo_db, json.loads(_trust(["pub-1"])), 0)
    _set(bo_db, {"publishers": [
        {"publisherId": "pub-1", "allowedKinds": ["k2"]},
    ]}, 1)
    row = _read(bo_db)["publishers"][0]
    assert row["allowedKinds"] == ["bobot", "k2"]


def test_r12_trust_nested_remove_explicit(bo_db: Path) -> None:
    _set(bo_db, json.loads(_trust(["pub-1"])), 0)
    _set(bo_db, {"publishers": [
        {"publisherId": "pub-1", "keyIds_remove": ["key-pub-1"]},
    ]}, 1)
    row = _read(bo_db)["publishers"][0]
    assert row["keyIds"] == []
    assert "keyIds_remove" not in row  # op consumed, never persisted


def test_r12_trust_nested_remove_missing_idempotent(bo_db: Path) -> None:
    _set(bo_db, json.loads(_trust(["pub-1"])), 0)
    _set(bo_db, {"publishers": [
        {"publisherId": "pub-1", "keyIds_remove": ["ghost"]},
    ]}, 1)
    row = _read(bo_db)["publishers"][0]
    assert row["keyIds"] == ["key-pub-1"]


def test_r12_trust_policy_nested_remove(bo_db: Path) -> None:
    _set(bo_db, json.loads(_trust(["pub-1"])), 0)
    _set(bo_db, {"policy": {"allowedKinds_remove": ["bobot"],
                            "capabilityCatalog": ["bots:run"]}}, 1)
    pol = _read(bo_db)["policy"]
    assert pol["allowedKinds"] == []
    assert pol["capabilityCatalog"] == ["bots:simulate", "bots:run"]
    assert "allowedKinds_remove" not in pol


def test_r12_trust_new_row_drops_remove_ops(bo_db: Path) -> None:
    _set(bo_db, json.loads(_trust(["pub-1"])), 0)
    _set(bo_db, {
        "publishers": [{
            "publisherId": "pub-2", "status": "active",
            "allowedKinds": ["bobot"], "keyIds": ["key-pub-2"],
            "keyIds_remove": ["nothing-to-remove"],
        }],
        "keys": [{
            "keyId": "key-pub-2", "algorithm": "ed25519",
            "publicKey": "AQID", "status": "active",
            "notBefore": "2026-01-01T00:00:00Z", "notAfter": None,
        }],
    }, 1)
    pubs = {p["publisherId"]: p for p in _read(bo_db)["publishers"]}
    assert "keyIds_remove" not in pubs["pub-2"]
    assert pubs["pub-2"]["keyIds"] == ["key-pub-2"]


def test_r12_trust_independent_fields_survive(bo_db: Path) -> None:
    """A delta touching ``status`` must leave both nested lists alone on the
    same row, and the row's siblings untouched."""
    _set(bo_db, json.loads(_trust(["pub-1", "pub-2"])), 0)
    _set(bo_db, {"publishers": [
        {"publisherId": "pub-1", "status": "suspended"},
    ]}, 1)
    pubs = {p["publisherId"]: p for p in _read(bo_db)["publishers"]}
    assert pubs["pub-1"]["status"] == "suspended"
    assert pubs["pub-1"]["keyIds"] == ["key-pub-1"]
    assert pubs["pub-1"]["allowedKinds"] == ["bobot"]
    assert pubs["pub-2"]["keyIds"] == ["key-pub-2"]


# --------------------------------------------------------------------------- #
# Company profile: durable CAS
# --------------------------------------------------------------------------- #


def _write_profile(path: Path) -> None:
    CompanyProfile(
        name="Probe SRL", industry="alt",
        vendors=["unu", "doi", "trei"],
    ).save_to_yaml(path)


def test_r12_profile_version_persists_reload(tmp_path: Path) -> None:
    from openexecutive.api.models import CompanyProfileUpdateRequest
    from openexecutive.api.routes.company_profile import update_company_profile

    path = tmp_path / "profile.yaml"
    _write_profile(path)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("COMPANY_PROFILE_PATH", str(path))
        asyncio.run(update_company_profile(
            CompanyProfileUpdateRequest(industry="nou", expected_version=1)))
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["company"]["version"] == 2
    # Readback after a fresh load: version and content both durable.
    loaded = CompanyProfile.load_from_yaml(path)
    assert loaded.version == 2
    assert loaded.industry == "nou"
    assert loaded.vendors == ["unu", "doi", "trei"]


def test_r12_profile_legacy_yaml_migrates_to_v1(tmp_path: Path) -> None:
    path = tmp_path / "profile.yaml"
    path.write_text(yaml.safe_dump(
        {"company": {"name": "Veche SRL"}}, allow_unicode=True),
        encoding="utf-8")
    loaded = CompanyProfile.load_from_yaml(path)
    assert loaded.version == 1


def test_r12_profile_patch_missing_version_422(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(tmp_path / "p.yaml"))
    _write_profile(Path(str(tmp_path / "p.yaml")))
    client = _client(
        __import__("openexecutive.api.routes.company_profile",
                   fromlist=["router"]).router)
    resp = client.patch("/company-profile", headers=ADMIN,
                        json={"industry": "nou"})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "expected_version_required"


def test_r12_profile_patch_stale_409_no_write(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "p.yaml"
    _write_profile(path)
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(path))
    from openexecutive.api.routes.company_profile import router
    client = _client(router)
    resp = client.patch("/company-profile", headers=ADMIN,
                        json={"industry": "nou", "expected_version": 99})
    assert resp.status_code == 409
    assert resp.json()["detail"]["current_version"] == 1
    loaded = CompanyProfile.load_from_yaml(path)
    assert loaded.industry == "alt"  # nothing written
    assert loaded.version == 1


def test_r12_profile_patch_viewer_403(tmp_path: Path,
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "p.yaml"
    _write_profile(path)
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(path))
    from openexecutive.api.routes.company_profile import router
    client = _client(router)
    resp = client.patch("/company-profile", headers=VIEWER,
                        json={"industry": "nou", "expected_version": 1})
    assert resp.status_code == 403
    assert CompanyProfile.load_from_yaml(path).industry == "alt"


def test_r12_profile_get_exposes_version(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "p.yaml"
    _write_profile(path)
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(path))
    from openexecutive.api.routes.company_profile import router
    client = _client(router)
    assert client.get("/company-profile").json()["version"] == 1


# --------------------------------------------------------------------------- #
# People: explicit clear, fenced scope/availability, audit
# --------------------------------------------------------------------------- #


def _person(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[int, Path]:
    db = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    people_store.initialize_db(db)
    pid = people_store.upsert_person(
        full_name="Ana Pop", role="ops", email="ana@probe.local",
        department_slugs=["finance"], db_path=db,
    )
    return pid, db


def _people_client() -> Any:
    from openexecutive.api.routes.people import router
    return _client(router)


def test_r12_people_null_field_422(tmp_path: Path,
                                   monkeypatch: pytest.MonkeyPatch) -> None:
    pid, _ = _person(tmp_path, monkeypatch)
    client = _people_client()
    resp = client.patch(f"/people/{pid}", headers=ADMIN,
                        json={"email": None, "expected_version": 1})
    assert resp.status_code == 422
    assert resp.json()["detail"]["error"] == "ambiguous_null"


def test_r12_people_clear_email_explicit(tmp_path: Path,
                                         monkeypatch: pytest.MonkeyPatch) -> None:
    pid, db = _person(tmp_path, monkeypatch)
    client = _people_client()
    resp = client.patch(f"/people/{pid}", headers=ADMIN,
                        json={"clear_email": True, "expected_version": 1})
    assert resp.status_code == 200
    assert resp.json()["email"] is None
    person = people_store.get_person(pid, db_path=db)
    assert person is not None
    assert person.email is None
    assert person.version == 2


def test_r12_people_clear_and_value_422(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    pid, _ = _person(tmp_path, monkeypatch)
    client = _people_client()
    resp = client.patch(
        f"/people/{pid}", headers=ADMIN,
        json={"clear_email": True, "email": "x@y.z", "expected_version": 1})
    assert resp.status_code == 422


def test_r12_people_stale_version_409_no_write(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid, db = _person(tmp_path, monkeypatch)
    client = _people_client()
    resp = client.patch(f"/people/{pid}", headers=ADMIN,
                        json={"role": "cfo", "expected_version": 99})
    assert resp.status_code == 409
    person = people_store.get_person(pid, db_path=db)
    assert person is not None
    assert person.role == "ops"
    assert person.version == 1


def test_r12_people_missing_version_422(tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    pid, _ = _person(tmp_path, monkeypatch)
    client = _people_client()
    resp = client.patch(f"/people/{pid}", headers=ADMIN, json={"role": "cfo"})
    assert resp.status_code == 422


def test_r12_people_scope_only_update_fenced(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A scope-only PATCH is still a CAS-fenced write and bumps version
    exactly once — the old code wrote scope outside the fence."""
    pid, db = _person(tmp_path, monkeypatch)
    from openexecutive.people.models import AuthorityScope
    people_store.set_authority_scope(pid, [AuthorityScope.WILDCARD], db_path=db)
    client = _people_client()
    stale = client.patch(f"/people/{pid}", headers=ADMIN, json={
        "authority_scope": [], "expected_version": 99})
    assert stale.status_code == 409
    assert people_store.get_person(pid, db_path=db).authority_scope  # type: ignore[union-attr]
    ok = client.patch(f"/people/{pid}", headers=ADMIN,
                      json={"authority_scope": [], "expected_version": 1})
    assert ok.status_code == 200
    person = people_store.get_person(pid, db_path=db)
    assert person is not None
    assert person.authority_scope == []
    assert person.version == 2


def test_r12_people_department_slugs_delta(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid, db = _person(tmp_path, monkeypatch)
    client = _people_client()
    add = client.patch(f"/people/{pid}", headers=ADMIN, json={
        "department_slugs": ["ops"], "expected_version": 1})
    assert add.status_code == 200
    person = people_store.get_person(pid, db_path=db)
    assert person is not None
    assert sorted(person.department_slugs) == ["finance", "ops"]
    rm = client.patch(f"/people/{pid}", headers=ADMIN, json={
        "department_slugs_remove": ["finance"], "expected_version": 2})
    assert rm.status_code == 200
    person = people_store.get_person(pid, db_path=db)
    assert person is not None
    assert person.department_slugs == ["ops"]
    assert person.version == 3


def test_r12_people_mutation_writes_audit(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pid, _ = _person(tmp_path, monkeypatch)
    client = _people_client()
    captured: list[tuple] = []
    import openexecutive.audit as audit
    monkeypatch.setattr(audit, "log_event",
                        lambda *a, **k: captured.append((a, k)))
    resp = client.patch(f"/people/{pid}", headers=ADMIN,
                        json={"role": "cfo", "expected_version": 1})
    assert resp.status_code == 200
    assert any(a and a[0] == "people_person_updated" for a, _ in captured)


def test_r12_people_clear_email_severs_roster(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Clearing the email drops the person from the auth roster — the
    admission derived from it dies on the next request (R12 email-auth)."""
    pid, db = _person(tmp_path, monkeypatch)
    client = _people_client()

    def roster() -> set[str]:
        return {
            p.email for p in people_store.list_people(db_path=db)
            if p.email and not p.archived
        }

    assert "ana@probe.local" in roster()
    resp = client.patch(f"/people/{pid}", headers=ADMIN,
                        json={"clear_email": True, "expected_version": 1})
    assert resp.status_code == 200
    assert "ana@probe.local" not in roster()


def test_r12_people_viewer_403(tmp_path: Path,
                               monkeypatch: pytest.MonkeyPatch) -> None:
    pid, db = _person(tmp_path, monkeypatch)
    client = _people_client()
    resp = client.patch(f"/people/{pid}", headers=VIEWER,
                        json={"role": "cfo", "expected_version": 1})
    assert resp.status_code == 403
    assert people_store.get_person(pid, db_path=db).role == "ops"  # type: ignore[union-attr]


def test_r12_people_bogus_channel_422_no_write(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An unknown preferred_channel used to land in the row and break every
    subsequent Person read — now refused 422 before any write."""
    pid, db = _person(tmp_path, monkeypatch)
    client = _people_client()
    resp = client.patch(f"/people/{pid}", headers=ADMIN, json={
        "preferred_channel": "carrier-pigeon", "expected_version": 1})
    assert resp.status_code == 422
    person = people_store.get_person(pid, db_path=db)
    assert person is not None
    assert person.preferred_channel == "any"
    assert person.version == 1
    bad = client.post("/people", headers=ADMIN, json={
        "full_name": "Rau", "preferred_channel": "carrier-pigeon"})
    assert bad.status_code == 422
    assert people_store.list_people(db_path=db)[0].full_name == "Ana Pop"
