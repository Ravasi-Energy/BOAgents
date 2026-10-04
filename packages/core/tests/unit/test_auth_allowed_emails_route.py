"""GET /auth/allowed-emails — typed {administered, emails} contract (R4)."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import auth as auth_route
from openexecutive.people import store as people_store


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    people_store.initialize_db()
    return path


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(auth_route.router)
    return TestClient(app)


def test_empty_roster_reports_not_administered(db: Path) -> None:
    """Fresh install — explicit ``administered: false``, not inferred."""
    resp = _client().get("/auth/allowed-emails")
    assert resp.status_code == 200
    assert resp.json() == {"administered": False, "emails": []}


def test_returns_only_people_with_email(db: Path) -> None:
    pid_with = people_store.upsert_person(full_name="Alex", email="alex@example.com")
    people_store.upsert_person(full_name="No Email")  # email is None
    resp = _client().get("/auth/allowed-emails")
    body = resp.json()
    assert body["administered"] is True
    assert body["emails"] == [{"email": "alex@example.com", "person_id": pid_with}]


def test_excludes_archived_people(db: Path) -> None:
    pid_active = people_store.upsert_person(full_name="Active", email="active@example.com")
    pid_archived = people_store.upsert_person(full_name="Ex Hire", email="ex@example.com")
    people_store.archive_person(pid_archived)
    body = _client().get("/auth/allowed-emails").json()
    assert {r["email"] for r in body["emails"]} == {"active@example.com"}
    assert body["emails"][0]["person_id"] == pid_active


def test_emails_normalized_to_lowercase(db: Path) -> None:
    """Mixed-case emails on Person rows are returned lowercased so
    the UI's lowercase comparison succeeds."""
    people_store.upsert_person(full_name="Alex", email="Alex@Example.COM")
    body = _client().get("/auth/allowed-emails").json()
    assert body["emails"][0]["email"] == "alex@example.com"


# --------------------------------------------------------------------------- #
# administered lifecycle — durable, independent of the live list (R4)
# --------------------------------------------------------------------------- #


def test_administered_survives_archiving_everyone(db: Path) -> None:
    """An empty administered roster stays authoritative — it revokes,
    it never falls back to env."""
    pid = people_store.upsert_person(full_name="Only", email="only@example.com")
    people_store.archive_person(pid)
    body = _client().get("/auth/allowed-emails").json()
    assert body == {"administered": True, "emails": []}


def test_administered_durable_across_connections(db: Path, tmp_path: Path) -> None:
    """The flag lives in the DB file — a second connection (restart) sees it."""
    people_store.upsert_person(full_name="A", email="a@example.com")
    assert people_store.roster_administered() is True
    # Fresh read path (as after a process restart):
    assert people_store.roster_administered(db_path=db) is True


def test_reset_roster_administered_returns_flag_state(db: Path) -> None:
    assert people_store.reset_roster_administered() is False  # nothing to clear
    people_store.upsert_person(full_name="A", email="a@example.com")
    assert people_store.reset_roster_administered() is True
    assert people_store.roster_administered() is False


# --------------------------------------------------------------------------- #
# POST /auth/roster/recover-env — explicit, gated, audited recovery
# --------------------------------------------------------------------------- #


@pytest.fixture()
def auth_env(db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "svc-secret")
    monkeypatch.setenv("BACKEND_PROXY_SECRET", "proxy-secret")
    monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@example.com")
    monkeypatch.setenv("BO_TENANT_ID", "t1")


def _service() -> dict[str, str]:
    return {"x-api-key": "svc-secret"}


def _admin() -> dict[str, str]:
    return {
        "x-caller-email": "admin@example.com",
        "x-caller-proxy-secret": "proxy-secret",
    }


def _viewer() -> dict[str, str]:
    return {
        "x-caller-email": "viewer@example.com",
        "x-caller-proxy-secret": "proxy-secret",
    }


def test_recovery_requires_identity(auth_env: None) -> None:
    people_store.upsert_person(full_name="A", email="a@example.com")
    resp = _client().post("/auth/roster/recover-env")
    assert resp.status_code == 403
    assert people_store.roster_administered() is True  # unchanged


def test_recovery_refuses_viewer(auth_env: None) -> None:
    people_store.upsert_person(full_name="A", email="a@example.com")
    resp = _client().post("/auth/roster/recover-env", headers=_viewer())
    assert resp.status_code == 403
    assert people_store.roster_administered() is True


def test_recovery_service_identity_resets_and_audits(auth_env: None) -> None:
    people_store.upsert_person(full_name="A", email="a@example.com")
    resp = _client().post("/auth/roster/recover-env", headers=_service())
    assert resp.status_code == 200
    assert resp.json() == {"administered": False, "cleared": True}
    assert _client().get("/auth/allowed-emails").json()["administered"] is False
    # Second call is an audited no-op:
    resp2 = _client().post("/auth/roster/recover-env", headers=_service())
    assert resp2.json() == {"administered": False, "cleared": False}


def test_recovery_delegated_admin_also_allowed(auth_env: None) -> None:
    people_store.upsert_person(full_name="A", email="a@example.com")
    resp = _client().post("/auth/roster/recover-env", headers=_admin())
    assert resp.status_code == 200
    assert resp.json()["administered"] is False
