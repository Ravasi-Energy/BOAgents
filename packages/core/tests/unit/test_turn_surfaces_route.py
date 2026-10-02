"""HTTP-level tests for the turn-barrier operator surface (F-2/F-3,
REM-AUDIT-18): turn blockers, durable turn history, control audit, and
admin reconciliation over the /clients routes.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from openexecutive.api.routes import bo as bo_route
from openexecutive.api.routes import clients as route
from openexecutive.bo import turn_barrier as tb

from .bo_testkit import capture_audit, use_tmp_db

ADMIN = {"x-caller-email": "admin@test", "x-caller-proxy-secret": "test-proxy-only"}
VIEWER = {"x-caller-email": "viewer@test", "x-caller-proxy-secret": "test-proxy-only"}
OPERATOR = {"x-api-key": "svc-key"}


@pytest.fixture()
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setenv("BO_TENANT_ID", "tenant-a")
    monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@test")
    monkeypatch.setenv("BACKEND_PROXY_SECRET", "test-proxy-only")
    monkeypatch.setattr(tb, "_switch_wait_seconds", lambda: 0)
    capture_audit(monkeypatch)
    app = FastAPI()
    app.include_router(route.router)
    bo_route.register_error_handlers(app)
    return TestClient(app)


def _expire(turn_id: str) -> None:
    """Push the lease's expiry into the past (crash/owner-vanished model)."""
    from openexecutive.bo import db as bo_db

    with bo_db.get_conn() as conn:
        conn.execute(
            "UPDATE bo_turn_leases SET lease_expires_at='2000-01-01' "
            "WHERE turn_id=?",
            (turn_id,),
        )


def _uncertain_lease(ref: str = "m-hist") -> str:
    lease = tb.admit_turn("email", ref=ref, wait_s=0)
    _expire(lease.turn_id)
    return lease.turn_id


# --------------------------------------------------------------------------- #
# Turn history (closed leases — the durable trail)
# --------------------------------------------------------------------------- #

def test_turn_history_empty_then_lists_reconciled(
    client: TestClient,
) -> None:
    resp = client.get("/clients/turn-history", headers=VIEWER)
    assert resp.status_code == 200
    assert resp.json() == {"turns": []}

    turn_id = _uncertain_lease()
    resp = client.post(
        f"/clients/turn-blockers/{turn_id}/reconcile",
        headers=ADMIN,
        json={"resolution": "verified"},
    )
    assert resp.status_code == 200, resp.text

    resp = client.get("/clients/turn-history", headers=ADMIN)
    assert resp.status_code == 200
    rows = resp.json()["turns"]
    assert len(rows) == 1
    row = rows[0]
    assert row["turn_id"] == turn_id
    assert row["status"] == "closed"
    assert row["resolution"] == "verified"
    assert "reconciled_by:admin@test" in (row["reason"] or "")
    assert "owner" in row  # admins see the owner for forensics


def test_turn_history_redacts_owner_for_viewers(client: TestClient) -> None:
    turn_id = _uncertain_lease()
    client.post(
        f"/clients/turn-blockers/{turn_id}/reconcile",
        headers=ADMIN,
        json={"resolution": "attested"},
    )
    resp = client.get("/clients/turn-history", headers=VIEWER)
    assert resp.status_code == 200
    rows = resp.json()["turns"]
    assert len(rows) == 1
    assert "owner" not in rows[0]  # pid:nonce stays admin-only
    # ... but the resolution distinction is visible to everyone.
    assert rows[0]["resolution"] == "attested"


# --------------------------------------------------------------------------- #
# Control audit (durable bo_control_audit rows)
# --------------------------------------------------------------------------- #

def test_control_audit_lists_reconcile_with_actor(
    client: TestClient,
) -> None:
    resp = client.get("/clients/control-audit", headers=ADMIN)
    assert resp.status_code == 200
    assert resp.json() == {"audit": []}

    turn_id = _uncertain_lease()
    client.post(
        f"/clients/turn-blockers/{turn_id}/reconcile",
        headers=ADMIN,
        json={"resolution": "attested"},
    )
    resp = client.get("/clients/control-audit", headers=ADMIN)
    audit = resp.json()["audit"]
    assert len(audit) == 1
    row = audit[0]
    assert row["action"] == "turn_reconcile"
    assert row["turn_id"] == turn_id
    assert row["actor"] == "admin@test"
    assert row["resolution"] == "attested"


def test_control_audit_redacts_actor_for_viewers(client: TestClient) -> None:
    turn_id = _uncertain_lease()
    client.post(
        f"/clients/turn-blockers/{turn_id}/reconcile",
        headers=ADMIN,
        json={"resolution": "verified"},
    )
    resp = client.get("/clients/control-audit", headers=VIEWER)
    rows = resp.json()["audit"]
    assert len(rows) == 1
    assert "actor" not in rows[0]
    assert rows[0]["resolution"] == "verified"


def test_control_audit_filters_by_turn_id(client: TestClient) -> None:
    t1 = _uncertain_lease("m-a")
    t2 = _uncertain_lease("m-b")
    for t in (t1, t2):
        client.post(
            f"/clients/turn-blockers/{t}/reconcile",
            headers=ADMIN,
            json={"resolution": "attested"},
        )
    resp = client.get(f"/clients/control-audit?turn_id={t2}", headers=ADMIN)
    rows = resp.json()["audit"]
    assert len(rows) == 1
    assert rows[0]["turn_id"] == t2


# --------------------------------------------------------------------------- #
# Reconcile route — roles, refusals, auditability
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("headers", [VIEWER, OPERATOR])
def test_reconcile_requires_admin(client: TestClient, headers: dict) -> None:
    turn_id = _uncertain_lease()
    resp = client.post(
        f"/clients/turn-blockers/{turn_id}/reconcile",
        headers=headers,
        json={"resolution": "verified"},
    )
    assert resp.status_code == 403
    # Refusal must not have closed the lease.
    assert any(
        b["turn_id"] == turn_id
        for b in client.get("/clients/turn-blockers", headers=VIEWER)
        .json()["blockers"]
    )


def test_reconcile_unknown_turn_404(client: TestClient) -> None:
    resp = client.post(
        "/clients/turn-blockers/nope/reconcile",
        headers=ADMIN,
        json={"resolution": "verified"},
    )
    assert resp.status_code == 404


def test_reconcile_active_lease_refused_409(client: TestClient) -> None:
    lease = tb.admit_turn("email", ref="m-live", wait_s=0)
    resp = client.post(
        f"/clients/turn-blockers/{lease.turn_id}/reconcile",
        headers=ADMIN,
        json={"resolution": "verified"},
    )
    assert resp.status_code == 409
    assert "active" in resp.json()["detail"]


def test_reconcile_refused_while_owner_alive(client: TestClient) -> None:
    """RA15-BO01-03: an uncertain lease whose owner task is still live
    refuses reconciliation — expiry alone is not proof of absence."""
    turn_id = _uncertain_lease("m-alive")
    # Same-pid owner + a live (non-done) registered task → still apt.
    tb.register_turn_task(
        turn_id, task=SimpleNamespace(done=lambda: False)
    )
    try:
        resp = client.post(
            f"/clients/turn-blockers/{turn_id}/reconcile",
            headers=ADMIN,
            json={"resolution": "verified"},
        )
        assert resp.status_code == 409
        assert "still running" in resp.json()["detail"]
        # No audit row: a refused reconcile leaves no false evidence.
        audit = client.get("/clients/control-audit", headers=ADMIN).json()[
            "audit"
        ]
        assert audit == []
    finally:
        tb.unregister_turn_task(turn_id)


def test_reconcile_recorded_atomically_when_forward_audit_fails(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RA15-BO01-02 over HTTP: an I/O error on the episodic audit forward
    cannot lose the durable control-audit row."""
    import openexecutive.audit as audit

    def _boom(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(audit, "log_event", _boom)
    turn_id = _uncertain_lease("m-io")
    resp = client.post(
        f"/clients/turn-blockers/{turn_id}/reconcile",
        headers=ADMIN,
        json={"resolution": "verified"},
    )
    assert resp.status_code == 200, resp.text
    audit = client.get("/clients/control-audit", headers=ADMIN).json()["audit"]
    assert [r["action"] for r in audit] == ["turn_reconcile"]


def test_unauthenticated_refused_on_public_deployment(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OE_PUBLIC_DEPLOYMENT", "1")
    assert client.get("/clients/turn-history").status_code == 401
    assert client.get("/clients/control-audit").status_code == 401
    assert (
        client.post(
            "/clients/turn-blockers/x/reconcile",
            json={"resolution": "verified"},
        ).status_code
        == 401
    )


# --------------------------------------------------------------------------- #
# RA15-BO01-08 — actor identity must not leak through derived fields
# --------------------------------------------------------------------------- #

def _reconciled_lease(client: TestClient, ref: str = "priv") -> str:
    turn_id = _uncertain_lease(ref)
    resp = client.post(
        f"/clients/turn-blockers/{turn_id}/reconcile",
        headers=ADMIN,
        json={"resolution": "verified"},
    )
    assert resp.status_code == 200, resp.text
    return turn_id


def test_history_hides_actor_in_reason_for_viewer_and_operator(
    client: TestClient,
) -> None:
    _reconciled_lease(client)
    for hdrs in (VIEWER, OPERATOR):
        resp = client.get("/clients/turn-history", headers=hdrs)
        assert resp.status_code == 200
        # The admin email must appear NOWHERE in the body — not in a
        # dropped-key field, not embedded in reason.
        assert "admin@test" not in resp.text
        row = resp.json()["turns"][0]
        assert "owner" not in row
        assert "reconciled_by:[redacted]" in row["reason"]
        # Non-identity parts of the reason survive.
        assert "lease_expired" in row["reason"] or "uncertain" in row["reason"]


def test_control_audit_hides_actor_in_detail_for_viewer_and_operator(
    client: TestClient,
) -> None:
    _reconciled_lease(client)
    for hdrs in (VIEWER, OPERATOR):
        resp = client.get("/clients/control-audit", headers=hdrs)
        assert resp.status_code == 200
        assert "admin@test" not in resp.text
        row = resp.json()["audit"][0]
        assert "actor" not in row
        # The nested JSON detail carried the reconciled_by marker — the
        # marker is redacted, the prior reason stays readable.
        assert "admin@test" not in row["detail"]
        assert "reconciled_by:[redacted]" in row["detail"]


def test_admin_keeps_full_evidence(client: TestClient) -> None:
    _reconciled_lease(client)
    hist = client.get("/clients/turn-history", headers=ADMIN)
    audit = client.get("/clients/control-audit", headers=ADMIN)
    assert "admin@test" in hist.text
    assert "admin@test" in audit.text
    assert audit.json()["audit"][0]["actor"] == "admin@test"
