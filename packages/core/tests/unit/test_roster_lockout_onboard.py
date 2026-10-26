"""ROSTER-LOCKOUT + ONBOARD-AUTH-CAS regressions.

- Seeds (fixture loader, wizard people, interview commit) must NOT flip the
  `administered` flag: loading roster DATA is not roster administration, and
  flipping it would revoke the env bootstrap and lock the operator out.
- The first roster write is an explicit authority transition: it must keep
  the acting admin's own access (409 before any write otherwise).
- Once administered, archiving/clearing the caller's own row is refused.
- /onboard/* endpoints are admin-gated and sessions are bound to their
  creator's actor; completed sessions refuse stale writes with 409.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes.onboarding import router as onboard_router
from openexecutive.api.routes.people import router as people_router
from openexecutive.people import store as people_store

ADMIN = {
    "x-caller-email": "admin@probe.local",
    "x-caller-proxy-secret": "proxy-secret-probe",
}
OTHER_ADMIN = {
    "x-caller-email": "admin2@probe.local",
    "x-caller-proxy-secret": "proxy-secret-probe",
}
VIEWER = {
    "x-caller-email": "viewer@probe.local",
    "x-caller-proxy-secret": "proxy-secret-probe",
}


def _client(*routers) -> TestClient:
    app = FastAPI()
    for r in routers:
        app.include_router(r)
    return TestClient(app)


@pytest.fixture(autouse=True)
def _probe_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BO_TENANT_ID", "probe")
    monkeypatch.setenv("BACKEND_PROXY_SECRET", "proxy-secret-probe")
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "service-secret-probe")
    monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@probe.local,admin2@probe.local")


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", path)
    people_store.initialize_db(path)
    return path


# ── seeds do not administer the roster ───────────────────────────────────────


def test_fixture_seed_people_does_not_mark_administered(
    db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import yaml

    from openexecutive.cli import fixture_loader

    people_yaml = tmp_path / "people.yaml"
    people_yaml.write_text(yaml.safe_dump({"people": [
        {"full_name": "Fixture Boss", "email": "boss@fixture.example",
         "is_principal": True}
    ]}))
    seeded = fixture_loader._seed_people(people_yaml)
    assert seeded == 1
    assert people_store.roster_administered(db_path=db) is False


def test_wizard_people_do_not_mark_administered(
    db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.onboarding import profile_builder

    monkeypatch.setattr(
        profile_builder, "build_people_from_answers",
        lambda answers: [
            {"full_name": "Founder", "email": "f@x.example",
             "is_principal": True}
        ],
    )
    profile_builder._save_wizard_people({"k": "v"})
    assert len(people_store.list_people(db_path=db)) == 1
    assert people_store.roster_administered(db_path=db) is False


# ── first roster write: explicit transition keeps the actor ──────────────────


def test_first_write_without_actor_row_refused_409(db: Path) -> None:
    client = _client(people_router)
    resp = client.post(
        "/people",
        json={"full_name": "Someone Else", "email": "other@probe.local"},
        headers=ADMIN,
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["error"] == "roster_lockout"
    assert people_store.list_people(db_path=db) == []
    assert people_store.roster_administered(db_path=db) is False


def test_first_write_with_actor_row_succeeds_and_marks(db: Path) -> None:
    client = _client(people_router)
    resp = client.post(
        "/people",
        json={"full_name": "Probe Admin", "email": "admin@probe.local"},
        headers=ADMIN,
    )
    assert resp.status_code == 201
    assert people_store.roster_administered(db_path=db) is True
    # Once administered + self in roster, adding others works.
    resp2 = client.post(
        "/people",
        json={"full_name": "Employee", "email": "e@probe.local"},
        headers=ADMIN,
    )
    assert resp2.status_code == 201


def test_archive_own_row_refused_while_administered(db: Path) -> None:
    pid = people_store.upsert_person(
        full_name="Probe Admin", email="admin@probe.local")
    people_store.upsert_person(full_name="Emp", email="e@probe.local")
    client = _client(people_router)
    resp = client.post(f"/people/{pid}/archive", headers=ADMIN)
    assert resp.status_code == 409
    p = people_store.get_person(pid, db_path=db)
    assert p is not None and not p.archived


def test_patch_clear_own_email_refused_while_administered(db: Path) -> None:
    pid = people_store.upsert_person(
        full_name="Probe Admin", email="admin@probe.local")
    client = _client(people_router)
    cur = people_store.get_person(pid, db_path=db)
    resp = client.patch(
        f"/people/{pid}",
        json={"expected_version": cur.version, "clear_email": True},
        headers=ADMIN,
    )
    assert resp.status_code == 409
    assert people_store.get_person(pid, db_path=db).email == "admin@probe.local"


def test_archive_other_person_still_allowed(db: Path) -> None:
    people_store.upsert_person(full_name="Probe Admin", email="admin@probe.local")
    pid2 = people_store.upsert_person(full_name="Emp", email="e@probe.local")
    client = _client(people_router)
    resp = client.post(f"/people/{pid2}/archive", headers=ADMIN)
    assert resp.status_code == 204


# ── onboarding authorization + session binding + stale 409 ───────────────────


def test_onboard_wizard_admin_only_and_actor_bound(db: Path) -> None:
    client = _client(onboard_router)
    assert client.get("/onboard/start", headers=VIEWER).status_code == 403

    started = client.get("/onboard/start", headers=ADMIN)
    assert started.status_code == 200
    sid = started.json()["session_id"]

    # Bound to the creating actor — another admin gets 404, not the session.
    assert (
        client.get(f"/onboard/status/{sid}", headers=OTHER_ADMIN).status_code
        == 404
    )
    assert client.get(f"/onboard/status/{sid}", headers=ADMIN).status_code == 200
    assert (
        client.post(
            "/onboard/answer",
            json={"session_id": sid, "answer": "x"},
            headers=VIEWER,
        ).status_code
        == 403
    )


def test_onboard_wizard_completed_session_refuses_409(
    db: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile = tmp_path / "profile.yaml"
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(profile))
    client = _client(onboard_router)
    sid = client.get("/onboard/start", headers=ADMIN).json()["session_id"]
    # Drive the session to completion at the state level — the route's
    # own completed-write contract is what is under test.
    from openexecutive.api.routes import onboarding as route_mod

    state = route_mod._wizard_sessions[sid]
    state.completed = True
    resp = client.post(
        "/onboard/answer",
        json={"session_id": sid, "answer": "again"},
        headers=ADMIN,
    )
    assert resp.status_code == 409
