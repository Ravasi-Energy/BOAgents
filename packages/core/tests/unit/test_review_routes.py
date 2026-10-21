"""Unit tests for the /review API surface: curation and bulk approve."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")

from fastapi.testclient import TestClient  # noqa: E402

from openexecutive.api.main import create_app  # noqa: E402
from openexecutive.api.routes import review as review_routes  # noqa: E402
from openexecutive.knowledge.review_store import (  # noqa: E402
    ContentType,
    ReviewStatus,
    ReviewStore,
)


@pytest.fixture(autouse=True)
def _isolate_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep audit writes out of the default ./episodic_memory.db.

    `audit.log_event` writes to the module default unless patched, and the
    leaked rows only break *other* modules' assertions in a full-suite run.
    """
    import openexecutive.audit as audit

    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ReviewStore:
    db = tmp_path / "review.db"
    ReviewStore.initialize_db(db)
    monkeypatch.setattr(review_routes, "_store", lambda: ReviewStore(db_path=db))
    return ReviewStore(db_path=db)


@pytest.fixture
def client() -> TestClient:
    return TestClient(create_app())


def _default(store: ReviewStore, filename: str, domain: str = "finance") -> str:
    """Register an item in its shipped state: approved, trusted, never reviewed."""
    import sqlite3

    item_id = f"builtin:{domain}:{filename}"
    store.register(
        item_id=item_id,
        content_type=ContentType.BUILTIN,
        domain=domain,
        filename=filename,
    )
    with sqlite3.connect(store._db_path) as conn:
        conn.execute(
            "UPDATE review_items SET status = 'approved', trusted_default = 1 "
            "WHERE item_id = ?",
            (item_id,),
        )
    return item_id


# ---------------------------------------------------------------------------
# POST /review/curate
# ---------------------------------------------------------------------------


def test_curate_start_queues_a_domain(client: TestClient, store: ReviewStore) -> None:
    _default(store, "a.md")
    _default(store, "b.md")
    _default(store, "hr.md", domain="hr")

    res = client.post("/review/curate", json={"domain": "finance", "action": "start"})

    assert res.status_code == 200
    assert res.json() == {"domain": "finance", "action": "start", "affected_count": 2}
    # Queued items are withheld from retrieval; other domains are untouched.
    assert store.get_withheld_keys(ContentType.BUILTIN) == {("finance", "a.md"), ("finance", "b.md")}


def test_curate_stop_restores_a_domain(client: TestClient, store: ReviewStore) -> None:
    _default(store, "a.md")
    client.post("/review/curate", json={"domain": "finance", "action": "start"})

    res = client.post("/review/curate", json={"domain": "finance", "action": "stop"})

    assert res.status_code == 200
    assert res.json()["affected_count"] == 1
    assert store.get_withheld_keys(ContentType.BUILTIN) == set()


def test_curate_requires_a_domain(client: TestClient, store: ReviewStore) -> None:
    """No 'curate everything' selector — one click must not dark the whole KB."""
    assert client.post("/review/curate", json={"action": "start"}).status_code == 422
    assert (
        client.post("/review/curate", json={"domain": "", "action": "start"}).status_code
        == 422
    )


def test_curate_rejects_an_unknown_action(client: TestClient, store: ReviewStore) -> None:
    res = client.post("/review/curate", json={"domain": "finance", "action": "nuke"})
    assert res.status_code == 422


def test_curate_unknown_domain_is_a_noop(client: TestClient, store: ReviewStore) -> None:
    _default(store, "a.md")
    res = client.post("/review/curate", json={"domain": "nope", "action": "start"})
    assert res.status_code == 200
    assert res.json()["affected_count"] == 0


# ---------------------------------------------------------------------------
# GET /review/trusted-defaults
# ---------------------------------------------------------------------------


def test_trusted_defaults_counts_by_domain(client: TestClient, store: ReviewStore) -> None:
    _default(store, "a.md")
    _default(store, "b.md")
    _default(store, "h.md", domain="hr")

    res = client.get("/review/trusted-defaults")

    assert res.status_code == 200
    assert res.json() == {"finance": 2, "hr": 1}


def test_trusted_defaults_excludes_reviewed_items(
    client: TestClient, store: ReviewStore
) -> None:
    signed_off = _default(store, "a.md")
    store.set_status(signed_off, ReviewStatus.APPROVED, "our CFO checked this")

    assert client.get("/review/trusted-defaults").json() == {}


# ---------------------------------------------------------------------------
# POST /review/bulk-approve
# ---------------------------------------------------------------------------


def test_bulk_approve_requires_a_selector(client: TestClient, store: ReviewStore) -> None:
    _default(store, "a.md")
    client.post("/review/curate", json={"domain": "finance", "action": "start"})

    res = client.post("/review/bulk-approve", json={})

    assert res.status_code == 400
    assert store.count_by_status()["pending"] == 1  # nothing was touched


def test_bulk_approve_all_pending_needs_the_explicit_flag(
    client: TestClient, store: ReviewStore
) -> None:
    _default(store, "a.md")
    client.post("/review/curate", json={"domain": "finance", "action": "start"})

    res = client.post("/review/bulk-approve", json={"all_pending": True})

    assert res.status_code == 200
    assert res.json()["approved_count"] == 1
    assert store.count_by_status()["pending"] == 0


def test_bulk_approve_by_item_ids(client: TestClient, store: ReviewStore) -> None:
    a = _default(store, "a.md")
    _default(store, "b.md")
    client.post("/review/curate", json={"domain": "finance", "action": "start"})

    res = client.post("/review/bulk-approve", json={"item_ids": [a]})

    assert res.status_code == 200
    assert res.json()["item_ids"] == [a]
    assert store.get_item("builtin:finance:b.md").status == ReviewStatus.PENDING  # type: ignore[union-attr]


def test_bulk_approve_caps_the_id_list(client: TestClient, store: ReviewStore) -> None:
    res = client.post(
        "/review/bulk-approve", json={"item_ids": [f"builtin:x:{i}.md" for i in range(501)]}
    )
    assert res.status_code == 422


def test_bulk_approve_rejects_an_empty_domain(client: TestClient, store: ReviewStore) -> None:
    """An empty domain must not masquerade as a scoped selector.

    The store treats a falsy domain as "no filter", so `{"domain": ""}` used to
    pass the selector guard and then widen to every pending item in every
    domain — while the audit trail recorded a scoped approve.
    """
    _default(store, "a.md", domain="finance")
    _default(store, "b.md", domain="hr")
    client.post("/review/curate", json={"domain": "finance", "action": "start"})
    client.post("/review/curate", json={"domain": "hr", "action": "start"})
    assert store.count_by_status()["pending"] == 2

    for bad in ("", "   "):
        res = client.post("/review/bulk-approve", json={"domain": bad})
        assert res.status_code == 422, f"domain={bad!r} should be rejected"

    assert store.count_by_status()["pending"] == 2  # nothing was approved


def test_curate_rejects_an_absurdly_long_domain(client: TestClient, store: ReviewStore) -> None:
    res = client.post("/review/curate", json={"domain": "x" * 5000, "action": "start"})
    assert res.status_code == 422


# ---------------------------------------------------------------------------
# F02: mutation authorization — a viewer with valid service headers could
# PATCH review items on 28fabe7 (routes had zero identity dependencies).
# ---------------------------------------------------------------------------

PROXY = "test-proxy-secret"


def _pending(store: ReviewStore, filename: str, domain: str = "finance") -> str:
    """A pending item — refusal is only provable when the store would change."""
    item_id = f"builtin:{domain}:{filename}"
    store.register(
        item_id=item_id,
        content_type=ContentType.BUILTIN,
        domain=domain,
        filename=filename,
    )
    return item_id



@pytest.fixture()
def auth_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> dict[str, str]:
    """Deployment-like auth material: proxy secret + admin roster."""
    monkeypatch.setenv("BACKEND_PROXY_SECRET", PROXY)
    monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@example.com")
    from openexecutive.people import store as people_store

    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    people_store.initialize_db()
    people_store.upsert_person(full_name="Vic", email="viewer@example.com")
    return {"x-caller-proxy-secret": PROXY}


def _patch(client: TestClient, item_id: str, headers: dict[str, str]):
    return client.patch(
        f"/review/items/{item_id}",
        json={"status": "approved"},
        headers=headers,
    )


def test_review_mutation_denied_to_viewer(
    client: TestClient, store: ReviewStore, auth_env: dict[str, str]
) -> None:
    item_id = _pending(store, "a.md")
    resp = _patch(
        client, item_id, {**auth_env, "x-caller-email": "viewer@example.com"}
    )
    assert resp.status_code == 403
    assert store.get_item(item_id).status != ReviewStatus.APPROVED


def test_review_mutation_allowed_to_admin(
    client: TestClient, store: ReviewStore, auth_env: dict[str, str]
) -> None:
    item_id = _pending(store, "a.md")
    resp = _patch(
        client, item_id, {**auth_env, "x-caller-email": "admin@example.com"}
    )
    assert resp.status_code == 200


def test_review_mutation_denied_to_service_key(
    client: TestClient, store: ReviewStore, auth_env: dict[str, str]
) -> None:
    """The shared service key resolves to operator — can read, cannot
    mutate the review queue."""
    item_id = _pending(store, "a.md")
    resp = _patch(client, item_id, {"x-api-key": "svc"})
    assert resp.status_code == 403
    assert store.get_item(item_id).status != ReviewStatus.APPROVED


def test_review_mutation_denied_without_identity(
    client: TestClient, store: ReviewStore, auth_env: dict[str, str]
) -> None:
    item_id = _pending(store, "a.md")
    resp = _patch(client, item_id, {})
    assert resp.status_code == 403
    assert store.get_item(item_id).status != ReviewStatus.APPROVED


def test_review_read_allowed_to_viewer(
    client: TestClient, store: ReviewStore, auth_env: dict[str, str]
) -> None:
    item_id = _default(store, "a.md")
    resp = client.get(
        f"/review/items/{item_id}",
        headers={**auth_env, "x-caller-email": "viewer@example.com"},
    )
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# F02 inchidere: matricea completa mutatii x roluri prin proxy real +
# audit persistent (fixture-ul autouse no-opeaza log_event; proba de mai
# jos instaleaza un logger real si verifica randul scris).
# ---------------------------------------------------------------------------

VIEWER_H = {"x-caller-email": "viewer@example.com"}
ADMIN_H = {"x-caller-email": "admin@example.com"}
SERVICE_H = {"x-api-key": "svc"}
ANON_H: dict[str, str] = {}


def _matrix_calls(store: ReviewStore) -> list[tuple[str, str, dict | None]]:
    item_id = _pending(store, "matrix.md")
    ann = store.add_annotation(item_id, "finance", "seed note")
    return [
        ("PATCH", f"/review/items/{item_id}", {"status": "approved"}),
        ("POST", "/review/bulk-approve", {"item_ids": [item_id]}),
        ("POST", "/review/curate", {"domain": "finance", "action": "start"}),
        ("POST", f"/review/items/{item_id}/annotations",
         {"correction": "matrix"}),
        ("PATCH", f"/review/annotations/{ann.annotation_id}",
         {"correction": "edited"}),
        ("DELETE", f"/review/annotations/{ann.annotation_id}", None),
    ]


def _do(client: TestClient, method: str, path: str,
        body: dict | None, headers: dict[str, str]):
    req = {"PATCH": client.patch, "POST": client.post,
           "DELETE": client.delete, "GET": client.get}[method]
    return req(path, headers=headers, **({"json": body} if body else {}))


def _snapshot(store: ReviewStore) -> tuple:
    items = sorted(
        (i.item_id, i.status, i.priority, i.reviewer_notes)
        for i in store.list_items(ContentType.BUILTIN))
    return (
        items,
        store.count_by_status(),
        store.get_withheld_keys(ContentType.BUILTIN),
    )


@pytest.mark.parametrize("idx", range(6))
def test_review_mutation_matrix_denied_roles(
    idx: int, client: TestClient, store: ReviewStore,
    auth_env: dict[str, str]
) -> None:
    """Fiecare mutatie /review x {viewer, operator(service key), anonim}
    prin proxy real → 403 si efect ZERO in store."""
    method, path, body = _matrix_calls(store)[idx]
    for role_headers in (
        {**auth_env, **VIEWER_H}, SERVICE_H, ANON_H
    ):
        before = _snapshot(store)
        resp = _do(client, method, path, body, role_headers)
        assert resp.status_code == 403, (
            f"{method} {path} pentru {role_headers}: "
            f"{resp.status_code} {resp.text}")
        assert _snapshot(store) == before, (
            f"{method} {path} refuzat dar a avut efect")


@pytest.mark.parametrize("idx", range(6))
def test_review_mutation_matrix_allowed_admin(
    idx: int, client: TestClient, store: ReviewStore,
    auth_env: dict[str, str]
) -> None:
    """Acelasi set de mutatii pentru adminul delegat → 200."""
    method, path, body = _matrix_calls(store)[idx]
    resp = _do(client, method, path, body, {**auth_env, **ADMIN_H})
    assert resp.status_code == 200, (
        f"{method} {path} pentru admin: {resp.status_code} {resp.text}")


def test_review_mutation_denied_on_tenant_mismatch(
    client: TestClient, store: ReviewStore, auth_env: dict[str, str],
    monkeypatch: pytest.MonkeyPatch
) -> None:
    """Tenantul se citeste din configurare (BO_TENANT_ID), nu din request:
    un hint `x-bo-tenant` diferit este refuz chiar si adminului."""
    monkeypatch.setenv("BO_TENANT_ID", "tenant-a")
    item_id = _pending(store, "tenant.md")
    resp = client.patch(
        f"/review/items/{item_id}", json={"status": "approved"},
        headers={**auth_env, **ADMIN_H, "x-bo-tenant": "tenant-b"})
    assert resp.status_code == 403
    assert store.get_item(item_id).status == ReviewStatus.PENDING


def test_review_denial_audit_persisted(
    client: TestClient, store: ReviewStore, auth_env: dict[str, str],
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refuzul scrie `review_mutation_denied` in auditul PERSISTENT, cu
    actorul real — nu e un no-op (fixture-ul autouse al fisierului il
    inlocuieste; aici instalam loggerul real pe un db temporar)."""
    import openexecutive.audit as audit_pkg
    from openexecutive.audit import logger as audit_mod

    audit_db = tmp_path / "audit.db"
    logger = audit_mod.AuditLogger(db_path=audit_db)
    logger.initialize_db()
    monkeypatch.setattr(audit_pkg, "log_event", audit_mod.log_event)
    prev = audit_mod._default_logger
    audit_mod.set_audit_logger(logger)
    try:
        item_id = _pending(store, "audit.md")
        resp = client.patch(
            f"/review/items/{item_id}", json={"status": "approved"},
            headers={**auth_env, **VIEWER_H})
        assert resp.status_code == 403
    finally:
        audit_mod.set_audit_logger(prev)
    rows = logger.query(event_type="review_mutation_denied")
    assert len(rows) == 1
    assert rows[0].actor == "viewer@example.com"
    assert "/review/items/" in rows[0].summary
    assert store.get_item(item_id).status == ReviewStatus.PENDING
