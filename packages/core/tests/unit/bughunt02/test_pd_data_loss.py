"""Clasa 1 — pierdere de date între utilizatori (PD-1…PD-6).

Aserțiunile sunt contractul sigur (3+1 → 4, lista goală rămâne, câmpurile
diferite rămân ambele). Pe codul măsurat ele pică acolo unde serverul
acceptă un document incomplet la versiunea care se potrivește.

CONTROL R2/R3 — diferența de contract față de probele originale ale
coordonatorului: serverul NU mai deduce baza scriitorului din
``expected_version − 1`` și NU mai șterge elemente prin omisiune. Contractul
aprobat este CAS strict + delta explicită: ``expected_version`` este
versiunea efectiv citită de scriitor; un scriitor stale primește 409 înainte
de orice efect și își reîncarcă/rebază schimbarea; colecțiile fac upsert,
ștergerea doar prin ops ``*_remove`` explicite; ``[]``/câmp omis = păstrat;
payload-ul gol pe documente = refuz explicit. Scrierile „stale dar la
versiune curentă" din probele originale sunt rescrise ca fluxul onest
(scrie la baza reală → 409 → rebase → retrimite delta) — aserțiunea de
ne-pierdere rămâne identică.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.models import CompanyProfileUpdateRequest
from openexecutive.api.routes.bo import register_error_handlers
from openexecutive.api.routes.bo import router as bo_router
from openexecutive.api.routes.company_profile import router as profile_router
from openexecutive.api.routes.people import router as people_router
from openexecutive.bo.bots import service as bot_service
from openexecutive.bo.bots import store as bot_store
from openexecutive.bo.routing import store as routing_store
from openexecutive.bo.settings import store as settings_store
from openexecutive.memory.company_profile import CompanyProfile

TENANT = "probe"
ADMIN = {
    "x-caller-email": "admin@probe.local",
    "x-caller-proxy-secret": "proxy-secret-probe",
}
VIEWER = {
    "x-caller-email": "viewer@probe.local",
    "x-caller-proxy-secret": "proxy-secret-probe",
}


def _note(label: str, before: object, after: object, expected: object) -> None:
    line = f"{label} inainte={before} dupa={after} asteptat={expected}"
    print(line)
    assert after == expected, line


def _bo_client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)
    app.include_router(bo_router)
    return TestClient(app)


def _trust(publisher_ids: list[str]) -> str:
    publishers = []
    keys = []
    for pid in publisher_ids:
        kid = "key-" + pid
        publishers.append({
            "publisherId": pid,
            "status": "active",
            "allowedKinds": ["bobot"],
            "keyIds": [kid],
        })
        keys.append({
            "keyId": kid,
            "algorithm": "ed25519",
            "publicKey": "AQID",
            "status": "active",
            "notBefore": "2026-01-01T00:00:00Z",
            "notAfter": None,
        })
    doc = {
        "schemaVersion": "bo.package.registry.v1",
        "registryId": "reg-probe",
        "version": "trust-1",
        "updatedAt": "2026-10-03T00:00:00Z",
        "publishers": publishers,
        "keys": keys,
        "policy": {
            "policyVersion": "pol-1",
            "allowedKinds": ["bobot"],
            "capabilityCatalog": ["bots:simulate"],
            "maxPackageBytes": 1048576,
            "rollbackRequiresApproval": True,
        },
    }
    return json.dumps(doc)


def _pub_count(raw: str) -> int:
    if not raw:
        return 0
    return len(json.loads(raw)["publishers"])


def _content(steps: list[dict[str, Any]], *, cron: str | None = None) -> dict[str, Any]:
    trigger: dict[str, Any] = {"type": "manual"} if cron is None else {
        "type": "schedule", "cron": cron,
    }
    return {
        "schema_version": "bo.bobot.v1",
        "trigger": trigger,
        "steps": steps,
        "capability_refs": ["cap-unu", "cap-doi", "cap-trei"],
        "policy_refs": ["pol-unu", "pol-doi"],
    }


def _step(sid: str, message: str) -> dict[str, str]:
    return {"id": sid, "type": "note", "message": message}


def _draft_steps(tenant: str, def_id: str) -> list[dict[str, Any]]:
    draft = bot_store.get_version(tenant, def_id, status="draft")
    return list(draft["content"]["steps"])


@pytest.fixture()
def seeded_csv(bo_db: Path) -> None:
    settings_store.set_value(
        TENANT, "bo.router.allowed_providers", "unu,doi,trei",
        expected_version=0, actor="admin@probe.local", db_path=bo_db,
    )


def test_pd1_settings_csv_three_plus_one(bo_db: Path, seeded_csv: None) -> None:
    before = settings_store.get_effective_value(
        TENANT, "bo.router.allowed_providers", db_path=bo_db,
    ).split(",")
    settings_store.set_value(
        TENANT, "bo.router.allowed_providers", "patru",
        expected_version=1, actor="b@probe.local", db_path=bo_db,
    )
    after = settings_store.get_effective_value(
        TENANT, "bo.router.allowed_providers", db_path=bo_db,
    ).split(",")
    _note("PD-1 bo.router.allowed_providers", len(before), len(after), 4)


def test_pd3_settings_csv_empty_keeps_list(bo_db: Path, seeded_csv: None) -> None:
    before = 3
    settings_store.set_value(
        TENANT, "bo.router.allowed_providers", "",
        expected_version=1, actor="b@probe.local", db_path=bo_db,
    )
    after = [
        p for p in settings_store.get_effective_value(
            TENANT, "bo.router.allowed_providers", db_path=bo_db,
        ).split(",") if p
    ]
    _note("PD-3 CSV gol allowed_providers", before, len(after), 3)


def test_pd_stale_csv_version_keeps_list(bo_db: Path, seeded_csv: None) -> None:
    from openexecutive.bo.settings.store import ConfigConflictError

    with pytest.raises(ConfigConflictError):
        settings_store.set_value(
            TENANT, "bo.router.allowed_providers", "patru",
            expected_version=0, actor="b@probe.local", db_path=bo_db,
        )
    after = settings_store.get_effective_value(
        TENANT, "bo.router.allowed_providers", db_path=bo_db,
    )
    print(f"PD versiune veche CSV dupa={after}")
    assert after == "unu,doi,trei"


def test_pd3_settings_omitted_value_rejected(bo_db: Path, seeded_csv: None) -> None:
    client = _bo_client()
    before = settings_store.get_effective_value(
        TENANT, "bo.router.allowed_providers", db_path=bo_db,
    )
    response = client.put(
        "/bo/settings/bo.router.allowed_providers",
        headers=ADMIN,
        json={"expected_version": 1},
    )
    after = settings_store.get_effective_value(
        TENANT, "bo.router.allowed_providers", db_path=bo_db,
    )
    print(f"PD-3 cheie value lipsa status={response.status_code} inainte={before} dupa={after}")
    assert response.status_code == 422
    assert after == before


def test_pd1_trust_store_three_plus_one(bo_db: Path) -> None:
    original = _trust(["pub-1", "pub-2", "pub-3"])
    settings_store.set_value(
        TENANT, "bo.packages.trust_store_json", original,
        expected_version=0, actor="admin@probe.local", db_path=bo_db,
    )
    before = _pub_count(settings_store.get_effective_value(
        TENANT, "bo.packages.trust_store_json", db_path=bo_db,
    ))
    settings_store.set_value(
        TENANT, "bo.packages.trust_store_json", _trust(["pub-4"]),
        expected_version=1, actor="b@probe.local", db_path=bo_db,
    )
    after = _pub_count(settings_store.get_effective_value(
        TENANT, "bo.packages.trust_store_json", db_path=bo_db,
    ))
    _note("PD-1 trust_store publishers", before, after, 4)


def test_pd3_trust_store_empty_string(bo_db: Path) -> None:
    # CONTROL R2: "" este payload ambiguu (vechiul client „wipe") — refuz
    # explicit, documentul stocat rămâne intact.
    settings_store.set_value(
        TENANT, "bo.packages.trust_store_json", _trust(["pub-1", "pub-2", "pub-3"]),
        expected_version=0, actor="admin@probe.local", db_path=bo_db,
    )
    with pytest.raises(settings_store.SettingValidationError):
        settings_store.set_value(
            TENANT, "bo.packages.trust_store_json", "",
            expected_version=1, actor="b@probe.local", db_path=bo_db,
        )
    after = _pub_count(settings_store.get_effective_value(
        TENANT, "bo.packages.trust_store_json", db_path=bo_db,
    ))
    _note("PD-3 trust_store sir gol refuzat", 3, after, 3)


def test_pd4_trust_store_different_fields(bo_db: Path) -> None:
    base = json.loads(_trust(["pub-1"]))
    settings_store.set_value(
        TENANT, "bo.packages.trust_store_json", json.dumps(base),
        expected_version=0, actor="seed", db_path=bo_db,
    )
    changed = json.loads(_trust(["pub-1"]))
    changed["policy"]["policyVersion"] = "pol-A"
    settings_store.set_value(
        TENANT, "bo.packages.trust_store_json", json.dumps(changed),
        expected_version=1, actor="a@probe.local", db_path=bo_db,
    )
    # B a construit documentul pe baza v1 — sub contractul CAS strict o
    # declară onest și primește 409 înainte de orice efect (CONTROL R2);
    # după reload/rebase își aplică doar delta proprie la v2.
    stale = json.loads(_trust(["pub-1"]))
    stale["version"] = "trust-B"
    with pytest.raises(settings_store.ConfigConflictError):
        settings_store.set_value(
            TENANT, "bo.packages.trust_store_json", json.dumps(stale),
            expected_version=1, actor="b@probe.local", db_path=bo_db,
        )
    settings_store.set_value(
        TENANT, "bo.packages.trust_store_json",
        json.dumps({"version": "trust-B"}),
        expected_version=2, actor="b@probe.local", db_path=bo_db,
    )
    saved = json.loads(settings_store.get_effective_value(
        TENANT, "bo.packages.trust_store_json", db_path=bo_db,
    ))
    print(
        "PD-4 trust_store "
        f"policyVersion={saved['policy']['policyVersion']} version={saved['version']}"
    )
    assert saved["policy"]["policyVersion"] == "pol-A"
    assert saved["version"] == "trust-B"


def test_pd5_viewer_cannot_empty_csv(bo_db: Path, seeded_csv: None) -> None:
    client = _bo_client()
    response = client.put(
        "/bo/settings/bo.router.allowed_providers",
        headers=VIEWER,
        json={"value": "", "expected_version": 1},
    )
    after = settings_store.get_effective_value(
        TENANT, "bo.router.allowed_providers", db_path=bo_db,
    )
    print(f"PD-5 viewer direct status={response.status_code} dupa={after}")
    assert response.status_code == 403
    assert after == "unu,doi,trei"


def test_pd1_bot_steps_three_plus_one(bo_db: Path) -> None:
    created = bot_service.create(TENANT, "admin@probe.local", {
        "name": "Probe",
        "content": _content([
            _step("s1", "unu"), _step("s2", "doi"), _step("s3", "trei"),
        ]),
    }, db_path=bo_db)
    def_id = created["id"]
    before = len(_draft_steps(TENANT, def_id))
    bot_service.update_draft(TENANT, "b@probe.local", def_id, {
        "expected_version": created["draft_version"],
        "content": _content([_step("s4", "patru")]),
    }, db_path=bo_db)
    after_steps = _draft_steps(TENANT, def_id)
    _note("PD-1 bot steps", before, len(after_steps), 4)


def test_pd2_bot_lists_saved_together(bo_db: Path) -> None:
    created = bot_service.create(TENANT, "admin@probe.local", {
        "name": "Probe",
        "content": _content([
            _step("s1", "unu"), _step("s2", "doi"), _step("s3", "trei"),
        ]),
    }, db_path=bo_db)
    short = _content([_step("s4", "patru")])
    short["capability_refs"] = ["doar-unu"]
    short.pop("policy_refs")
    bot_service.update_draft(TENANT, "b@probe.local", created["id"], {
        "expected_version": created["draft_version"],
        "content": short,
    }, db_path=bo_db)
    content = bot_store.get_version(TENANT, created["id"], status="draft")["content"]
    print(
        "PD-2 bot capability_refs="
        f"{content['capability_refs']} policy_refs={content['policy_refs']} "
        f"steps={len(content['steps'])}"
    )
    assert content["capability_refs"] == ["cap-unu", "cap-doi", "cap-trei", "doar-unu"] or (
        "cap-unu" in content["capability_refs"] and "doar-unu" in content["capability_refs"]
    )
    assert content["policy_refs"] == ["pol-unu", "pol-doi"]
    assert len(content["steps"]) == 4


def test_pd3_bot_empty_steps(bo_db: Path) -> None:
    created = bot_service.create(TENANT, "admin@probe.local", {
        "name": "Probe",
        "content": _content([
            _step("s1", "unu"), _step("s2", "doi"), _step("s3", "trei"),
        ]),
    }, db_path=bo_db)
    bot_service.update_draft(TENANT, "b@probe.local", created["id"], {
        "expected_version": created["draft_version"],
        "content": _content([]),
    }, db_path=bo_db)
    after = len(_draft_steps(TENANT, created["id"]))
    _note("PD-3 bot steps []", 3, after, 3)


def test_pd4_bot_different_fields(bo_db: Path) -> None:
    created = bot_service.create(TENANT, "admin@probe.local", {
        "name": "Probe",
        "content": _content([
            _step("s1", "unu"), _step("s2", "doi"), _step("s3", "trei"),
        ]),
    }, db_path=bo_db)
    edited = _content([
        _step("s1", "mesaj-A"), _step("s2", "doi"), _step("s3", "trei"),
    ])
    saved = bot_service.update_draft(TENANT, "a@probe.local", created["id"], {
        "expected_version": created["draft_version"],
        "content": edited,
    }, db_path=bo_db)
    # B deține baza v1 (draftul creat) — o declară onest; CAS strict → 409
    # înainte de orice efect (CONTROL R2), apoi își retrimește doar delta
    # proprie (trigger.cron) pe versiunea reîncărcată v2.
    stale = _content([
        _step("s1", "unu"), _step("s2", "doi"), _step("s3", "trei"),
    ], cron="0 6 * * *")
    with pytest.raises(bot_store.ConflictError):
        bot_service.update_draft(TENANT, "b@probe.local", created["id"], {
            "expected_version": created["draft_version"],
            "content": stale,
        }, db_path=bo_db)
    bot_service.update_draft(TENANT, "b@probe.local", created["id"], {
        "expected_version": saved["draft_version"],
        "content": {"trigger": {"type": "schedule", "cron": "0 6 * * *"}},
    }, db_path=bo_db)
    content = bot_store.get_version(TENANT, created["id"], status="draft")["content"]
    message = content["steps"][0]["message"]
    cron = content["trigger"].get("cron")
    print(f"PD-4 bot message={message} cron={cron}")
    assert message == "mesaj-A"
    assert cron == "0 6 * * *"


def test_pd6_bot_delete_one_keeps_sibling_added_by_other(bo_db: Path) -> None:
    created = bot_service.create(TENANT, "admin@probe.local", {
        "name": "Probe",
        "content": _content([
            _step("s1", "unu"), _step("s2", "doi"), _step("s3", "trei"),
        ]),
    }, db_path=bo_db)
    added = bot_service.update_draft(TENANT, "a@probe.local", created["id"], {
        "expected_version": created["draft_version"],
        "content": _content([
            _step("s1", "unu"), _step("s2", "doi"),
            _step("s3", "trei"), _step("s4", "patru"),
        ]),
    }, db_path=bo_db)
    # CONTROL R2: ștergerea prin omisiune este respinsă — omis lui s2 îl
    # păstrează; doar op-ul explicit steps_remove șterge pe id stabil.
    bot_service.update_draft(TENANT, "b@probe.local", created["id"], {
        "expected_version": added["draft_version"],
        "content": _content([_step("s1", "unu"), _step("s3", "trei")]),
    }, db_path=bo_db)
    ids = [s["id"] for s in _draft_steps(TENANT, created["id"])]
    print(f"PD-6 bot omisie s2 pastrat ids={ids}")
    assert ids == ["s1", "s2", "s3", "s4"]
    bot_service.update_draft(TENANT, "b@probe.local", created["id"], {
        "expected_version": added["draft_version"] + 1,
        "steps_remove": ["s2"],
    }, db_path=bo_db)
    ids = [s["id"] for s in _draft_steps(TENANT, created["id"])]
    print(f"PD-6 bot steps_remove s2 pastrand s4 ids={ids}")
    assert ids == ["s1", "s3", "s4"]


def _catalog_body(**over: Any) -> dict[str, Any]:
    body = {
        "provider": "anthropic",
        "model_id": "model-probe",
        "model_version": "v1",
        "state": "ACTIVE",
        "capabilities": ["unu", "doi", "trei"],
        "regions": ["eu", "us", "ro"],
        "cost": {
            "input_per_million": "1.00",
            "output_per_million": "2.00",
            "currency": "EUR",
            "valid_until": "2027-01-01",
        },
        "quality": None,
        "purpose": "scop-initial",
        "source": "admin",
    }
    body.update(over)
    return body


def test_pd1_catalog_partial_put_drops_capabilities(bo_db: Path) -> None:
    from openexecutive.api.routes.bo import _CatalogEntryPatch

    entry = routing_store.create_entry(
        TENANT, _catalog_body(), actor="admin@probe.local", db_path=bo_db,
    )
    partial = _CatalogEntryPatch.model_validate({
        "expected_version": entry.version,
        "provider": "anthropic",
        "model_id": "model-probe",
        "model_version": "v1",
        "purpose": "scop-initial",
        "source": "admin",
        "capabilities": ["patru"],
    })
    routing_store.update_entry(
        TENANT, entry.entry_id,
        partial.model_dump(exclude={"expected_version"}),
        expected_version=entry.version, actor="b@probe.local", db_path=bo_db,
    )
    saved = routing_store.get_entry(TENANT, entry.entry_id, db_path=bo_db)
    _note(
        "PD-1 catalog capabilities",
        3,
        len(saved.capabilities),
        4,
    )


def test_pd3_catalog_omitted_capabilities_default_empty(bo_db: Path) -> None:
    from openexecutive.api.routes.bo import _CatalogEntryPatch

    entry = routing_store.create_entry(
        TENANT, _catalog_body(), actor="admin@probe.local", db_path=bo_db,
    )
    partial = _CatalogEntryPatch.model_validate({
        "expected_version": entry.version,
        "provider": "anthropic",
        "model_id": "model-probe",
        "purpose": "scop-initial",
        "source": "admin",
    })
    dumped = partial.model_dump(exclude={"expected_version"})
    routing_store.update_entry(
        TENANT, entry.entry_id, dumped,
        expected_version=entry.version, actor="b@probe.local", db_path=bo_db,
    )
    saved = routing_store.get_entry(TENANT, entry.entry_id, db_path=bo_db)
    print(f"PD-3 catalog capabilities omise dupa={list(saved.capabilities)} regions={list(saved.regions)}")
    assert list(saved.capabilities) == ["unu", "doi", "trei"]
    assert list(saved.regions) == ["eu", "us", "ro"]


def test_pd4_catalog_stale_purpose(bo_db: Path) -> None:
    entry = routing_store.create_entry(
        TENANT, _catalog_body(), actor="admin@probe.local", db_path=bo_db,
    )
    first = _catalog_body(purpose="scop-A")
    updated = routing_store.update_entry(
        TENANT, entry.entry_id, first,
        expected_version=entry.version, actor="a@probe.local", db_path=bo_db,
    )
    # B a construit corpul pe baza v1 — o declară onest; CAS strict → 409
    # înainte de orice efect (CONTROL R2), apoi își retrimește doar delta
    # proprie (regions+md) pe versiunea reîncărcată.
    stale = _catalog_body(purpose="scop-initial", regions=["eu", "us", "ro", "md"])
    with pytest.raises(routing_store.ConflictError):
        routing_store.update_entry(
            TENANT, entry.entry_id, stale,
            expected_version=entry.version, actor="b@probe.local",
            db_path=bo_db,
        )
    routing_store.update_entry(
        TENANT, entry.entry_id, {"regions": ["md"]},
        expected_version=updated.version, actor="b@probe.local", db_path=bo_db,
    )
    saved = routing_store.get_entry(TENANT, entry.entry_id, db_path=bo_db)
    print(f"PD-4 catalog purpose={saved.purpose} regions={list(saved.regions)}")
    assert saved.purpose == "scop-A"
    assert "md" in saved.regions


def _write_profile(path: Path, **over: Any) -> None:
    profile = CompanyProfile(
        name="Firma Probe",
        industry="servicii",
        vendors=["unu", "doi", "trei"],
        financials={
            "burn_rate_monthly": 1234.56,
            "burn_rate_currency": "RON",
            "runway_months": 8,
            "key_metrics": {"clienti": 3},
        },
    )
    data = profile.model_dump(mode="json")
    data.update(over)
    path.write_text(yaml.safe_dump({"company": data}), encoding="utf-8")


def test_pd1_company_profile_vendors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "profile.yaml"
    _write_profile(path)
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(path))
    import asyncio

    from openexecutive.api.routes.company_profile import update_company_profile
    asyncio.run(update_company_profile(CompanyProfileUpdateRequest(vendors=["patru"], expected_version=1)))
    loaded = CompanyProfile.load_from_yaml(path)
    _note("PD-1 company.vendors", 3, len(loaded.vendors), 4)


def test_pd3_company_profile_empty_vendors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "profile.yaml"
    _write_profile(path)
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(path))
    import asyncio

    from openexecutive.api.routes.company_profile import update_company_profile
    asyncio.run(update_company_profile(CompanyProfileUpdateRequest(vendors=[], expected_version=1)))
    loaded = CompanyProfile.load_from_yaml(path)
    _note("PD-3 company.vendors []", 3, len(loaded.vendors), 3)


def test_pd4_company_profile_partial_financials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "profile.yaml"
    _write_profile(path)
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(path))
    import asyncio

    from openexecutive.api.models import FinancialsData
    from openexecutive.api.routes.company_profile import update_company_profile
    asyncio.run(update_company_profile(CompanyProfileUpdateRequest(
        financials=FinancialsData(runway_months=12), expected_version=1,
    )))
    loaded = CompanyProfile.load_from_yaml(path)
    fin = loaded.financials
    print(
        "PD-4 company.financials "
        f"burn={fin.burn_rate_monthly} ccy={fin.burn_rate_currency} "
        f"runway={fin.runway_months} metrics={fin.key_metrics}"
    )
    assert fin.runway_months == 12
    assert fin.burn_rate_monthly == Decimal("1234.56")
    assert fin.burn_rate_currency == "RON"
    assert fin.key_metrics == {"clienti": 3}


def test_pd_company_profile_omitted_vendors_stay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "profile.yaml"
    _write_profile(path)
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(path))
    import asyncio

    from openexecutive.api.routes.company_profile import update_company_profile
    asyncio.run(update_company_profile(
        CompanyProfileUpdateRequest(industry="alt", expected_version=1)
    ))
    loaded = CompanyProfile.load_from_yaml(path)
    assert loaded.industry == "alt"
    assert loaded.vendors == ["unu", "doi", "trei"]


def test_pd_profile_has_no_role_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "profile.yaml"
    _write_profile(path)
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(path))
    app = FastAPI()
    app.include_router(profile_router)
    client = TestClient(app)
    response = client.patch("/company-profile", json={"vendors": []})
    loaded = CompanyProfile.load_from_yaml(path)
    print(f"PD-5 company-profile fara rol status={response.status_code} vendors={loaded.vendors}")
    assert response.status_code == 403
    assert loaded.vendors == ["unu", "doi", "trei"]


def test_pd1_people_department_slugs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.people import store as people_store

    db = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    people_store.initialize_db(db)
    pid = people_store.upsert_person(
        full_name="Persoana", role="contabil",
        department_slugs=["unu", "doi", "trei"], db_path=db,
    )
    people_store.update_person(pid, department_slugs=["patru"], db_path=db)
    person = people_store.get_person(pid, db_path=db)
    assert person is not None
    _note("PD-1 people.department_slugs", 3, len(person.department_slugs), 4)


def test_pd5_viewer_self_principal_then_empties_list(
    bo_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded_csv: None,
) -> None:
    from starlette.requests import Request

    from openexecutive.bo import identity as bo_identity
    from openexecutive.people import store as people_store

    db = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    people_store.initialize_db(db)
    app = FastAPI()
    app.include_router(people_router)
    client = TestClient(app)
    created = client.post("/people", json={
        "full_name": "Viewer",
        "email": "viewer@probe.local",
        "is_principal": True,
    })
    print(f"PD-5 auto-principal status={created.status_code} body={created.text[:240]}")
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "http",
        "path": "/bo/settings/bo.router.allowed_providers",
        "raw_path": b"/bo/settings/bo.router.allowed_providers",
        "query_string": b"",
        "headers": [
            (b"x-caller-email", b"viewer@probe.local"),
            (b"x-caller-proxy-secret", b"proxy-secret-probe"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("127.0.0.1", 8000),
    }
    role = bo_identity.resolve_identity(Request(scope)).role
    print(f"PD-5 rol dupa auto-principal={role}")
    assert (created.status_code, role) == (403, "viewer")
