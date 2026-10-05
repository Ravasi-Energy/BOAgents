"""BUGHUNT-02 P0-10 — HTTP probes for the five previously ungated mutations.

The coordinator probe asserts the ``ident`` parameter exists (a signature
check). These probes exercise the real gate over HTTP on each mutation:
no identity → 403, viewer → 403 with the target data untouched, admin →
the effect actually lands. Written against the probe environment in this
directory's conftest (proxy + service secrets configured, so the dev
fallback is disabled).
"""
from __future__ import annotations

import io
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes.company_profile import router as profile_router
from openexecutive.api.routes.departments import router as departments_router
from openexecutive.api.routes.documents import router as documents_router
from openexecutive.api.routes.onboarding import router as onboarding_router
from openexecutive.api.routes.people import router as people_router
from openexecutive.departments import store as departments_store
from openexecutive.memory.company_profile import CompanyProfile
from openexecutive.people import store as people_store
from tests.unit._fake_store import FakeStore

ADMIN = {
    "x-caller-email": "admin@probe.local",
    "x-caller-proxy-secret": "proxy-secret-probe",
}
VIEWER = {
    "x-caller-email": "viewer@probe.local",
    "x-caller-proxy-secret": "proxy-secret-probe",
}


def _client(*routers: Any, store: Any = None) -> TestClient:
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    if store is not None:
        app.state.store = store
    return TestClient(app)


def _write_profile(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    CompanyProfile(name="Probe SRL", industry="alt", vendors=["unu", "doi", "trei"]).save_to_yaml(path)


# ── PATCH /company-profile ──────────────────────────────────────────────────


def test_profile_patch_viewer_403_and_admin_200(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "profile.yaml"
    _write_profile(path)
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(path))
    client = _client(profile_router)

    assert client.patch("/company-profile", json={"vendors": ["patru"]}).status_code == 403
    assert client.patch(
        "/company-profile", json={"vendors": ["patru"]}, headers=VIEWER
    ).status_code == 403
    assert CompanyProfile.load_from_yaml(path).vendors == ["unu", "doi", "trei"]

    ok = client.patch(
        "/company-profile",
        json={"vendors": ["patru"], "expected_version": 1},
        headers=ADMIN,
    )
    assert ok.status_code == 200, ok.text
    assert CompanyProfile.load_from_yaml(path).vendors == ["unu", "doi", "trei", "patru"]


# ── POST /people ────────────────────────────────────────────────────────────


def test_people_create_viewer_403_admin_201_and_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    people_store.initialize_db(db)
    client = _client(people_router)

    body = {"full_name": "A Nou", "email": "nou@probe.local"}
    assert client.post("/people", json=body).status_code == 403
    assert client.post("/people", json=body, headers=VIEWER).status_code == 403
    assert people_store.list_people(db_path=db) == []

    created = client.post("/people", json=body, headers=ADMIN)
    assert created.status_code == 201, created.text
    assert len(people_store.list_people(db_path=db)) == 1

    principal = client.post(
        "/people",
        json={"full_name": "V", "email": "viewer@probe.local", "is_principal": True},
        headers=VIEWER,
    )
    assert principal.status_code == 403
    assert all(
        not p.is_principal for p in people_store.list_people(db_path=db)
    )


# ── POST + DELETE /departments ───────────────────────────────────────────────


def test_departments_create_delete_gated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "departments.db"
    monkeypatch.setattr(departments_store, "DB_PATH", db)
    departments_store.initialize_db(db)
    client = _client(departments_router)

    body = {"title": "Probe Dept", "mission": "sonde"}
    assert client.post("/departments", json=body).status_code == 403
    assert client.post("/departments", json=body, headers=VIEWER).status_code == 403
    assert departments_store.get_department("probe-dept", db_path=db) is None

    created = client.post("/departments", json=body, headers=ADMIN)
    assert created.status_code == 201, created.text
    assert departments_store.get_department("probe-dept", db_path=db) is not None

    assert client.delete("/departments/probe-dept", headers=VIEWER).status_code == 403
    assert departments_store.get_department("probe-dept", db_path=db) is not None

    deleted = client.delete("/departments/probe-dept", headers=ADMIN)
    assert deleted.status_code == 204
    assert departments_store.get_department("probe-dept", db_path=db) is None


# ── POST /documents ─────────────────────────────────────────────────────────


def test_documents_upload_viewer_403_admin_indexes(tmp_path: Path) -> None:
    store = FakeStore()
    client = _client(documents_router, store=store)
    files = {"file": ("plan.md", io.BytesIO(b"# Plan\nGrow revenue."), "text/markdown")}

    assert client.post("/documents", files=files).status_code == 403
    files = {"file": ("plan.md", io.BytesIO(b"# Plan\nGrow revenue."), "text/markdown")}
    assert client.post("/documents", files=files, headers=VIEWER).status_code == 403
    assert not getattr(store, "upserts", None) and not getattr(store, "chunks", None)

    files = {"file": ("plan.md", io.BytesIO(b"# Plan\nGrow revenue."), "text/markdown")}
    ok = client.post("/documents", files=files, data={"domain": "general"}, headers=ADMIN)
    assert ok.status_code == 200, ok.text


# ── POST /onboard/interview/commit ───────────────────────────────────────────


def _commit_body(session_id: str) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "profile": {"name": "Northwind Tools", "industry": "Industrial supply"},
        "people": [{"full_name": "Dana Reyes", "role": "CEO", "is_principal": True}],
        "departments": [{"title": "Operations", "head_person_name": "Dana Reyes"}],
    }


def test_onboard_commit_viewer_403_admin_writes_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.api.routes import onboarding as route

    profile_path = tmp_path / "company" / "profile.yaml"
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(profile_path))
    monkeypatch.setattr(route, "_interview_sessions", OrderedDict())
    monkeypatch.setattr(route, "_onboarding_research_fired", set())
    monkeypatch.setattr(
        route, "_get_interview", lambda sid: SimpleNamespace(saved=False)
    )
    monkeypatch.setattr(
        "openexecutive.onboarding.commit.save_onboarding_people", lambda d: {}
    )
    monkeypatch.setattr(
        "openexecutive.onboarding.commit.reconcile_onboarding_departments",
        lambda d, ids: {"updated": 0, "created": 0},
    )

    async def _no_research(session_id: str) -> None:
        return None

    monkeypatch.setattr(route, "_fire_post_onboarding_research", _no_research)
    client = _client(onboarding_router)

    assert client.post("/onboard/interview/commit", json=_commit_body("s1")).status_code == 403
    assert client.post(
        "/onboard/interview/commit", json=_commit_body("s1"), headers=VIEWER
    ).status_code == 403
    assert not profile_path.exists()

    ok = client.post(
        "/onboard/interview/commit", json=_commit_body("s1"), headers=ADMIN
    )
    assert ok.status_code == 200, ok.text
    assert profile_path.exists()
