"""Tests for GET /sessions per-caller scoping.

The Recent-chats sidebar must show each signed-in user only their own
chats. The route resolves `x-caller-email` to a Person and filters the
list; an unknown header MUST NOT fall through to the principal.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import sessions as sessions_route
from openexecutive.memory import episodic, session_store
from openexecutive.people import store as people_store


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.chdir(tmp_path)
    db_path = Path("./episodic_memory.db").resolve()
    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(session_store, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    episodic.initialize_db(db_path)
    people_store.initialize_db()

    app = FastAPI()
    app.include_router(sessions_route.router)
    return TestClient(app)


def test_sessions_list_filters_by_caller_email(client: TestClient) -> None:
    """Two users with their own sessions each see only their own."""
    alex_id = people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )
    sabin_id = people_store.upsert_person(
        full_name="Sabin", email="sabin@example.com"
    )

    session_store.create_session("alex-s1", "Alex chat", "2024-01-01T00:00:00", caller_person_id=alex_id)
    session_store.create_session("sabin-s1", "Sabin chat", "2024-01-02T00:00:00", caller_person_id=sabin_id)

    alex_resp = client.get("/sessions", headers={"x-caller-email": "alex@example.com"})
    sabin_resp = client.get("/sessions", headers={"x-caller-email": "sabin@example.com"})

    assert alex_resp.status_code == 200
    assert sabin_resp.status_code == 200
    assert [s["session_id"] for s in alex_resp.json()] == ["alex-s1"]
    assert [s["session_id"] for s in sabin_resp.json()] == ["sabin-s1"]


def test_sessions_list_unknown_email_returns_empty_not_principal(client: TestClient) -> None:
    """A signed-in user whose email isn't in the roster gets an empty list,
    not the principal's chats. Mirrors the chat-route precedence rule."""
    alex_id = people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )
    session_store.create_session("alex-only", "Alex chat", "2024-01-01T00:00:00", caller_person_id=alex_id)

    resp = client.get("/sessions", headers={"x-caller-email": "stranger@example.com"})
    assert resp.status_code == 200
    assert resp.json() == []


def test_sessions_list_no_header_falls_back_to_principal(client: TestClient) -> None:
    """CLI / direct-curl callers (no x-caller-email header) see the principal's chats."""
    alex_id = people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )
    people_store.upsert_person(full_name="Sabin", email="sabin@example.com")

    session_store.create_session("alex-s1", "Alex chat", "2024-01-01T00:00:00", caller_person_id=alex_id)

    resp = client.get("/sessions")
    assert resp.status_code == 200
    assert [s["session_id"] for s in resp.json()] == ["alex-s1"]


def test_sessions_list_hides_legacy_null_owner_rows(client: TestClient) -> None:
    """Sessions created before the migration have NULL owner and stay hidden."""
    alex_id = people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )
    # Simulate a pre-migration row.
    session_store.create_session("legacy", "Pre-migration", "2024-01-01T00:00:00", caller_person_id=None)
    session_store.create_session("modern", "Post-migration", "2024-01-02T00:00:00", caller_person_id=alex_id)

    resp = client.get("/sessions", headers={"x-caller-email": "alex@example.com"})
    assert resp.status_code == 200
    assert [s["session_id"] for s in resp.json()] == ["modern"]


def test_sessions_list_email_case_insensitive(client: TestClient) -> None:
    """Header is lowercased before lookup; mixed-case still resolves."""
    sabin_id = people_store.upsert_person(
        full_name="Sabin", email="sabin@example.com"
    )
    session_store.create_session("sabin-s1", "Sabin chat", "2024-01-01T00:00:00", caller_person_id=sabin_id)

    resp = client.get("/sessions", headers={"x-caller-email": "Sabin@Example.COM"})
    assert resp.status_code == 200
    assert [s["session_id"] for s in resp.json()] == ["sabin-s1"]


# ---------------------------------------------------------------------------
# F03: direct-read / delete ownership — the list filter alone was not enough
# ---------------------------------------------------------------------------


def _two_people_with_sessions() -> tuple[int, int]:
    alex_id = people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )
    sabin_id = people_store.upsert_person(
        full_name="Sabin", email="sabin@example.com"
    )
    session_store.create_session(
        "alex-private", "Alex private", "2024-01-01T00:00:00", caller_person_id=alex_id
    )
    session_store.save_message("alex-private", "user", "PRIVATE_CONTENT_ALEX")
    return alex_id, sabin_id


def test_session_detail_refused_to_other_person(client: TestClient) -> None:
    """F03 regression: knowing a session_id must not expose another
    person's chat — same 404 as a session that does not exist."""
    _two_people_with_sessions()
    resp = client.get(
        "/sessions/alex-private", headers={"x-caller-email": "sabin@example.com"}
    )
    assert resp.status_code == 404


def test_session_messages_refused_to_other_person(client: TestClient) -> None:
    """The IDOR the coordinator's probe reproduced on 28fabe7: GET
    /sessions/{id}/messages returned another person's private content."""
    _two_people_with_sessions()
    resp = client.get(
        "/sessions/alex-private/messages",
        headers={"x-caller-email": "sabin@example.com"},
    )
    assert resp.status_code == 404
    assert "PRIVATE_CONTENT_ALEX" not in resp.text


def test_session_detail_and_messages_served_to_owner(client: TestClient) -> None:
    _two_people_with_sessions()
    meta = client.get(
        "/sessions/alex-private", headers={"x-caller-email": "alex@example.com"}
    )
    msgs = client.get(
        "/sessions/alex-private/messages",
        headers={"x-caller-email": "alex@example.com"},
    )
    assert meta.status_code == 200
    assert meta.json()["session_id"] == "alex-private"
    assert msgs.status_code == 200
    assert "PRIVATE_CONTENT_ALEX" in msgs.text


def test_session_read_refused_for_unknown_caller(client: TestClient) -> None:
    """A signed-in email absent from the roster resolves to no person —
    it must not read anything, not even via the principal fallback."""
    _two_people_with_sessions()
    resp = client.get(
        "/sessions/alex-private/messages",
        headers={"x-caller-email": "stranger@example.com"},
    )
    assert resp.status_code == 404


def test_session_delete_refused_to_other_person(client: TestClient) -> None:
    # Contractul nou (BO-RBAC): scrierea cere admin; un non-admin primeste
    # 403 inainte de orice efect — nu divulga existenta si nu sterge.
    _two_people_with_sessions()
    resp = client.delete(
        "/sessions/alex-private", headers={"x-caller-email": "sabin@example.com"}
    )
    assert resp.status_code == 403
    assert session_store.get_session_metadata("alex-private") is not None


def test_session_detail_refused_for_legacy_null_owner(client: TestClient) -> None:
    """Legacy rows (caller_person_id NULL) are refused to everyone — the
    list already hides them; direct access now matches."""
    people_store.upsert_person(
        full_name="Alex", is_principal=True, email="alex@example.com"
    )
    session_store.create_session("legacy", "old", "2024-01-01T00:00:00")
    assert client.get("/sessions/legacy").status_code == 404
    assert client.get("/sessions/legacy/messages").status_code == 404
