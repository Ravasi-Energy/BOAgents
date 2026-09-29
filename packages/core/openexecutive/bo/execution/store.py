"""SQLite persistence for delegated execution (VAL4-01).

Three tenants of truth, deliberately separate (VAL4 decision document):

* ``bo_exec_mandates`` — the delegation grant. Children are validated
  against the parent's grant at creation (intersection, never union).
* ``bo_exec_runs`` + ``bo_checkpoints`` — execution state. A checkpoint is
  execution memory, NEVER proof of an external effect; resume restores the
  same identity and idempotency keys instead of replacing records.
* ``bo_effect_ledger`` — one row per intent (idempotent identity:
  ``run_id:step``), with payload digest, provider, status, receipt ref,
  lease + fencing. This is NOT the telemetry outbox: telemetry may lag or
  be unavailable while the ledger remains the product's own authority.

The synthetic provider's countable effect (``bo_synth_effects``) lives
here too so crash/restart probes can prove absence of double effects
against the real database file, including from a second process.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from openexecutive.bo.db import get_conn
from openexecutive.bo.execution.mandate import (
    Mandate,
    MandateValidationError,
    check_child_intersection,
    mandate_state,
    new_mandate_id,
    validate_mandate_fields,
)

RUN_PENDING = "PENDING"
RUN_CLAIMED = "CLAIMED"
RUN_RUNNING = "RUNNING"
RUN_PAUSED = "PAUSED"
RUN_SUCCEEDED = "SUCCEEDED"
RUN_FAILED = "FAILED"
RUN_UNKNOWN = "UNKNOWN"
RUN_RECONCILIATION = "RECONCILIATION_REQUIRED"
RUN_CANCELLED = "CANCELLED"

RUN_STATES = frozenset({
    RUN_PENDING, RUN_CLAIMED, RUN_RUNNING, RUN_SUCCEEDED,
    RUN_FAILED, RUN_UNKNOWN, RUN_RECONCILIATION, RUN_CANCELLED,
})

LED_INTENT = "INTENT"
LED_SUBMITTED = "SUBMITTED"
LED_SUCCEEDED = "SUCCEEDED"
LED_FAILED = "FAILED"
LED_UNKNOWN = "UNKNOWN"
LED_RECONCILIATION = "RECONCILIATION_REQUIRED"

LEDGER_STATES = frozenset({
    LED_INTENT, LED_SUBMITTED, LED_SUCCEEDED, LED_FAILED,
    LED_UNKNOWN, LED_RECONCILIATION,
})

RES_RESERVED = "RESERVED"
RES_COMMITTED = "COMMITTED"
RES_RELEASED = "RELEASED"
RES_EXPOSED = "EXPOSED"


class NotFoundError(KeyError):
    pass


class ConflictError(Exception):
    """CAS/idempotency conflict → HTTP 409."""


class BudgetExceededError(Exception):
    """The reservation would exceed the mandate's budget."""


class InvalidStateError(Exception):
    """The requested transition is not valid for the current state."""


class PayloadConflictError(ConflictError):
    """Same idempotency key with a different payload digest."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def initialize_db(db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_exec_mandates (
                tenant              TEXT NOT NULL,
                mandate_id          TEXT NOT NULL,
                parent_mandate_id   TEXT,
                principal_ref       TEXT NOT NULL,
                depth               INTEGER NOT NULL,
                allowed_resources   TEXT NOT NULL,
                allowed_actions     TEXT NOT NULL,
                budget_limit        TEXT NOT NULL,
                concurrency_limit   INTEGER NOT NULL,
                max_steps           INTEGER NOT NULL,
                max_depth           INTEGER NOT NULL,
                expires_at          TEXT NOT NULL,
                policy_version      INTEGER NOT NULL,
                revoked_at          TEXT,
                revoked_reason      TEXT,
                guardian_ref        TEXT,
                created_by          TEXT NOT NULL,
                created_at          TEXT NOT NULL,
                PRIMARY KEY (tenant, mandate_id)
            )
            """
        )
        if "guardian_ref" not in {
            r[1] for r in conn.execute(
                "PRAGMA table_info(bo_exec_mandates)"
            ).fetchall()
        }:
            conn.execute(
                "ALTER TABLE bo_exec_mandates "
                "ADD COLUMN guardian_ref TEXT"
            )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_exec_runs (
                tenant            TEXT NOT NULL,
                run_id            TEXT NOT NULL,
                mandate_id        TEXT NOT NULL,
                parent_run_id     TEXT,
                state             TEXT NOT NULL,
                steps_json        TEXT NOT NULL,
                current_step      INTEGER NOT NULL DEFAULT 0,
                budget_reserved   TEXT NOT NULL,
                concurrency_slots INTEGER NOT NULL,
                policy_version    INTEGER NOT NULL,
                correlation_id    TEXT NOT NULL,
                lease_owner       TEXT,
                lease_until       TEXT,
                lease_seq         INTEGER NOT NULL DEFAULT 0,
                pause_requested   INTEGER NOT NULL DEFAULT 0,
                cancel_requested  INTEGER NOT NULL DEFAULT 0,
                block_reason      TEXT,
                created_by        TEXT NOT NULL,
                created_at        TEXT NOT NULL,
                updated_at        TEXT NOT NULL,
                finished_at       TEXT,
                PRIMARY KEY (tenant, run_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS bo_exec_runs_claimable "
            "ON bo_exec_runs (tenant, state, lease_until)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_checkpoints (
                tenant             TEXT NOT NULL,
                run_id             TEXT NOT NULL,
                step               INTEGER NOT NULL,
                checkpoint_version INTEGER NOT NULL,
                state_json         TEXT NOT NULL,
                payload_digest     TEXT NOT NULL,
                created_at         TEXT NOT NULL,
                PRIMARY KEY (tenant, run_id, step, checkpoint_version)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_effect_ledger (
                tenant          TEXT NOT NULL,
                entry_id        TEXT NOT NULL,
                run_id          TEXT NOT NULL,
                step            INTEGER NOT NULL,
                intent_ref      TEXT NOT NULL,
                idempotency_key TEXT NOT NULL,
                payload_digest  TEXT NOT NULL,
                provider        TEXT NOT NULL,
                action          TEXT NOT NULL,
                resource        TEXT NOT NULL,
                status          TEXT NOT NULL,
                receipt_ref     TEXT,
                receipt_json    TEXT,
                fence_version   INTEGER NOT NULL DEFAULT 0,
                lease_owner     TEXT,
                lease_until     TEXT,
                attempts        INTEGER NOT NULL DEFAULT 0,
                policy_version  INTEGER NOT NULL,
                correlation_id  TEXT NOT NULL,
                submitted_at    TEXT,
                finalized_at    TEXT,
                created_at      TEXT NOT NULL,
                PRIMARY KEY (tenant, entry_id),
                UNIQUE (tenant, idempotency_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS bo_effect_ledger_run "
            "ON bo_effect_ledger (tenant, run_id, step)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_budget_reservations (
                tenant      TEXT NOT NULL,
                reservation_id TEXT NOT NULL,
                run_id      TEXT NOT NULL,
                mandate_id  TEXT NOT NULL,
                amount      TEXT NOT NULL,
                slots       INTEGER NOT NULL,
                state       TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (tenant, reservation_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS bo_budget_res_mandate "
            "ON bo_budget_reservations (tenant, mandate_id, state)"
        )
        # Upgrade pre-remediation accounting without deleting evidence.
        # Older workers released even ambiguous or partially executed runs.
        for target, statuses in (
            (RES_EXPOSED, (LED_SUBMITTED, LED_UNKNOWN, LED_RECONCILIATION)),
            (RES_COMMITTED, (LED_SUCCEEDED,)),
        ):
            marks = ",".join("?" for _ in statuses)
            conn.execute(
                "UPDATE bo_budget_reservations AS r SET state = ? WHERE state = ? "
                "AND EXISTS (SELECT 1 FROM bo_effect_ledger e WHERE e.tenant = r.tenant "
                f"AND e.run_id = r.run_id AND e.status IN ({marks}))",
                (target, RES_RELEASED, *statuses),
            )
        # Synthetic provider state — the countable, persistent effect used
        # by probes. It is the provider's OWN truth, deliberately kept in
        # the same database file so restart/fencing tests exercise real
        # persistence, but writes go only through the provider module.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_synth_effects (
                tenant      TEXT NOT NULL,
                effect_key  TEXT NOT NULL,
                digest      TEXT NOT NULL,
                amount      INTEGER NOT NULL,
                receipt_ref TEXT NOT NULL,
                provider    TEXT NOT NULL,
                created_at  TEXT NOT NULL,
                PRIMARY KEY (tenant, effect_key)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_synth_counter (
                tenant  TEXT PRIMARY KEY,
                total   INTEGER NOT NULL
            )
            """
        )


# --------------------------------------------------------------------------- #
# Mandates
# --------------------------------------------------------------------------- #

def _mandate_from_row(row: Any) -> Mandate:
    return Mandate(
        tenant=row["tenant"],
        mandate_id=row["mandate_id"],
        parent_mandate_id=row["parent_mandate_id"],
        principal_ref=row["principal_ref"],
        depth=int(row["depth"]),
        allowed_resources=frozenset(json.loads(row["allowed_resources"])),
        allowed_actions=frozenset(json.loads(row["allowed_actions"])),
        budget_limit=Decimal(row["budget_limit"]),
        concurrency_limit=int(row["concurrency_limit"]),
        max_steps=int(row["max_steps"]),
        max_depth=int(row["max_depth"]),
        expires_at=row["expires_at"],
        policy_version=int(row["policy_version"]),
        revoked_at=row["revoked_at"],
        revoked_reason=row["revoked_reason"],
        created_by=row["created_by"],
        created_at=row["created_at"],
        guardian_ref=row["guardian_ref"],
    )


def create_mandate(
    tenant: str,
    fields: dict[str, Any],
    *,
    parent: Mandate | None,
    principal_ref: str,
    policy_version: int,
    actor: str,
    max_depth_cap: int,
    guardian_ref: str | None = None,
    db_path: Path | None = None,
) -> Mandate:
    """Validate + persist a mandate. ``parent=None`` creates a root.

    A child is checked against BOTH the parent's grant (intersection) and
    the tenant's administered ``max_depth_cap``. The principal ref is the
    creator's server-derived identity — a child cannot grant to another
    principal either (no identity widening through delegation).
    """
    v = validate_mandate_fields(**fields)
    if guardian_ref is not None and (
        not isinstance(guardian_ref, str)
        or not (1 <= len(guardian_ref) <= 128)
        or any(ch.isspace() for ch in guardian_ref)
        or "@" in guardian_ref
    ):
        raise MandateValidationError(
            "guardian_ref: ref opac invalid (1..128, fără spații/@)"
        )
    if parent is not None:
        state = mandate_state(parent)
        if state != "active":
            raise MandateValidationError(
                f"părintele {parent.mandate_id} este {state}"
            )
        check_child_intersection(parent, v)
        depth = parent.depth + 1
        parent_id = parent.mandate_id
        guardian_ref = guardian_ref or parent.guardian_ref
    else:
        depth = 0
        parent_id = None
    if depth > max_depth_cap or v["max_depth"] > max_depth_cap:
        raise MandateValidationError(
            f"adâncimea depășește limita tenantului ({max_depth_cap})"
        )
    mandate = Mandate(
        tenant=tenant,
        mandate_id=new_mandate_id(),
        parent_mandate_id=parent_id,
        principal_ref=principal_ref,
        depth=depth,
        allowed_resources=v["allowed_resources"],
        allowed_actions=v["allowed_actions"],
        budget_limit=v["budget_limit"],
        concurrency_limit=v["concurrency_limit"],
        max_steps=v["max_steps"],
        max_depth=v["max_depth"],
        expires_at=v["expires_at"],
        policy_version=policy_version,
        revoked_at=None,
        revoked_reason=None,
        created_by=actor,
        created_at=_now(),
        guardian_ref=guardian_ref,
    )
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bo_exec_mandates (
                tenant, mandate_id, parent_mandate_id, principal_ref, depth,
                allowed_resources, allowed_actions, budget_limit,
                concurrency_limit, max_steps, max_depth, expires_at,
                policy_version, guardian_ref, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant, mandate.mandate_id, mandate.parent_mandate_id,
                mandate.principal_ref, mandate.depth,
                json.dumps(sorted(mandate.allowed_resources)),
                json.dumps(sorted(mandate.allowed_actions)),
                str(mandate.budget_limit), mandate.concurrency_limit,
                mandate.max_steps, mandate.max_depth, mandate.expires_at,
                mandate.policy_version, guardian_ref, actor,
                mandate.created_at,
            ),
        )
    _audit(tenant, "bo_mandate_create", {
        "mandate_id": mandate.mandate_id,
        "parent": mandate.parent_mandate_id,
        "depth": mandate.depth,
        "budget_limit": str(mandate.budget_limit),
        "policy_version": mandate.policy_version,
    }, actor=actor)
    return mandate


def get_mandate(
    tenant: str, mandate_id: str, db_path: Path | None = None
) -> Mandate:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bo_exec_mandates WHERE tenant = ? AND mandate_id = ?",
            (tenant, mandate_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(mandate_id)
    return _mandate_from_row(row)


def mandate_chain(
    tenant: str, mandate_id: str, db_path: Path | None = None
) -> list[Mandate]:
    """Root-first chain; re-checking the whole chain catches revocation or
    expiry of ANY ancestor at the effect boundary, not just the leaf."""
    chain: list[Mandate] = []
    current = get_mandate(tenant, mandate_id, db_path=db_path)
    while True:
        chain.append(current)
        if current.parent_mandate_id is None:
            break
        current = get_mandate(
            tenant, current.parent_mandate_id, db_path=db_path
        )
    return list(reversed(chain))


def list_mandates(tenant: str, db_path: Path | None = None) -> list[Mandate]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM bo_exec_mandates WHERE tenant = ? "
            "ORDER BY created_at, mandate_id",
            (tenant,),
        ).fetchall()
    return [_mandate_from_row(r) for r in rows]


def revoke_mandate(
    tenant: str,
    mandate_id: str,
    *,
    reason: str,
    actor: str,
    db_path: Path | None = None,
) -> Mandate:
    """Revoke a mandate — takes effect at the NEXT effect boundary check,
    even mid-run. Children are revoked transitively (a revoked parent must
    never leave active descendants)."""
    now = _now()
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT mandate_id FROM bo_exec_mandates "
            "WHERE tenant = ? AND mandate_id = ?",
            (tenant, mandate_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(mandate_id)
        # Transitive revocation of the whole subtree — one recursive walk.
        descendants = conn.execute(
            """
            WITH RECURSIVE subtree(mid) AS (
                SELECT mandate_id FROM bo_exec_mandates
                WHERE tenant = ? AND mandate_id = ?
                UNION ALL
                SELECT m.mandate_id FROM bo_exec_mandates m
                JOIN subtree s ON m.parent_mandate_id = s.mid
                WHERE m.tenant = ?
            )
            SELECT mid FROM subtree
            """,
            (tenant, mandate_id, tenant),
        ).fetchall()
        for d in descendants:
            conn.execute(
                "UPDATE bo_exec_mandates SET revoked_at = ?, "
                "revoked_reason = ? WHERE tenant = ? AND mandate_id = ? "
                "AND revoked_at IS NULL",
                (now, reason, tenant, d["mid"]),
            )
    _audit(tenant, "bo_mandate_revoke", {
        "mandate_id": mandate_id, "reason": reason,
        "descendants": len(descendants),
    }, actor=actor)
    return get_mandate(tenant, mandate_id, db_path=db_path)


# --------------------------------------------------------------------------- #
# Runs — submission, claims, state transitions
# --------------------------------------------------------------------------- #

def _run_from_row(row: Any) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "mandate_id": row["mandate_id"],
        "parent_run_id": row["parent_run_id"],
        "state": row["state"],
        "steps": json.loads(row["steps_json"]),
        "current_step": int(row["current_step"]),
        "budget_reserved": row["budget_reserved"],
        "concurrency_slots": int(row["concurrency_slots"]),
        "policy_version": int(row["policy_version"]),
        "correlation_id": row["correlation_id"],
        "lease_owner": row["lease_owner"],
        "lease_until": row["lease_until"],
        "lease_seq": int(row["lease_seq"]),
        "pause_requested": bool(row["pause_requested"]),
        "cancel_requested": bool(row["cancel_requested"]),
        "block_reason": row["block_reason"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "finished_at": row["finished_at"],
    }


def submit_run(
    tenant: str,
    mandate: Mandate,
    steps: list[dict[str, Any]],
    *,
    budget_amount: Decimal,
    slots: int,
    correlation_id: str,
    actor: str,
    parent_run_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Create a PENDING run AND its budget/concurrency reservation in one
    ``BEGIN IMMEDIATE`` — atomic: concurrent submissions can't overspend
    the mandate's budget or its concurrency limit."""
    if mandate_state(mandate) != "active":
        raise InvalidStateError(
            f"mandatul {mandate.mandate_id} nu este activ "
            f"({mandate_state(mandate)})"
        )
    if not steps:
        raise MandateValidationError("execuția trebuie să aibă cel puțin un pas")
    if len(steps) > mandate.max_steps:
        raise MandateValidationError(
            f"{len(steps)} pași depășesc max_steps={mandate.max_steps}"
        )
    for i, step in enumerate(steps):
        action = step.get("action")
        resource = step.get("resource")
        if action not in mandate.allowed_actions:
            raise MandateValidationError(
                f"pasul {i}: acțiunea {action!r} nu e în mandat"
            )
        if not _resource_allowed(resource, mandate.allowed_resources):
            raise MandateValidationError(
                f"pasul {i}: resursa {resource!r} nu e în mandat"
            )
    run_id = f"run_{uuid.uuid4().hex[:20]}"
    now = _now()
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        _reserve_budget(
            conn, tenant, mandate, run_id, budget_amount, slots, now
        )
        conn.execute(
            """
            INSERT INTO bo_exec_runs (
                tenant, run_id, mandate_id, parent_run_id, state, steps_json,
                current_step, budget_reserved, concurrency_slots,
                policy_version, correlation_id, created_by, created_at,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant, run_id, mandate.mandate_id, parent_run_id,
                RUN_PENDING, json.dumps(steps), str(budget_amount), slots,
                mandate.policy_version, correlation_id, actor, now, now,
            ),
        )
    _audit(tenant, "bo_run_submit", {
        "run_id": run_id, "mandate_id": mandate.mandate_id,
        "steps": len(steps), "budget": str(budget_amount),
    }, actor=actor)
    return get_run(tenant, run_id, db_path=db_path)


def _resource_allowed(resource: Any, allowed: frozenset[str]) -> bool:
    """Exact match or ``prefix.*`` wildcard inside the mandate's allow-list."""
    if not isinstance(resource, str):
        return False
    for pattern in allowed:
        if pattern.endswith(".*"):
            if resource == pattern[:-2] or resource.startswith(pattern[:-1]):
                return True
        elif resource == pattern:
            return True
    return False


def _reserve_budget(
    conn: Any,
    tenant: str,
    mandate: Mandate,
    run_id: str,
    amount: Decimal,
    slots: int,
    now: str,
) -> None:
    """Atomic reservation inside the caller's BEGIN IMMEDIATE transaction.

    Budget includes reserved, exposed and committed amounts. Concurrency
    includes reserved slots only. Every ancestor bounds its whole subtree.
    """
    # Check each ancestor against reservations in its entire subtree. This
    # also covers pre-upgrade reservations without duplicating ledger rows.
    link = mandate
    while True:
        rows = conn.execute(
            "WITH RECURSIVE descendants(id) AS (SELECT ? UNION ALL "
            "SELECT m.mandate_id FROM bo_exec_mandates m JOIN descendants d "
            "ON m.parent_mandate_id = d.id WHERE m.tenant = ?) "
            "SELECT amount, slots, state FROM bo_budget_reservations "
            "WHERE tenant = ? AND mandate_id IN (SELECT id FROM descendants) "
            "AND run_id != ? AND state != ?",
            (link.mandate_id, tenant, tenant, run_id, RES_RELEASED),
        ).fetchall()
        used = sum((Decimal(r["amount"]) for r in rows), Decimal(0))
        occupied = sum(r["slots"] for r in rows if r["state"] == RES_RESERVED)
        if used + amount > link.budget_limit:
            raise BudgetExceededError("bugetul agregat al lanțului este epuizat")
        if occupied + slots > link.concurrency_limit:
            raise BudgetExceededError("concurența agregată a lanțului este epuizată")
        if link.parent_mandate_id is None:
            break
        link = _mandate_from_row(conn.execute(
            "SELECT * FROM bo_exec_mandates WHERE tenant = ? AND mandate_id = ?",
            (tenant, link.parent_mandate_id),
        ).fetchone())
    existing = conn.execute(
        "SELECT reservation_id FROM bo_budget_reservations WHERE tenant = ? AND run_id = ?",
        (tenant, run_id),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE bo_budget_reservations SET state = ?, slots = ?, updated_at = ? "
            "WHERE tenant = ? AND run_id = ?",
            (RES_RESERVED, slots, now, tenant, run_id),
        )
        return
    conn.execute(
        """
        INSERT INTO bo_budget_reservations (
            tenant, reservation_id, run_id, mandate_id, amount, slots,
            state, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            tenant, f"rsv_{uuid.uuid4().hex[:16]}", run_id,
            mandate.mandate_id, str(amount), slots, RES_RESERVED, now, now,
        ),
    )


def settle_reservation(
    conn: Any, tenant: str, run_id: str, state: str, now: str
) -> None:
    if state == RES_RELEASED:
        evidence = conn.execute(
            "SELECT status FROM bo_effect_ledger WHERE tenant = ? AND run_id = ?",
            (tenant, run_id),
        ).fetchall()
        if any(r["status"] in (LED_SUBMITTED, LED_UNKNOWN, LED_RECONCILIATION)
               for r in evidence):
            state = RES_EXPOSED  # budget retained, execution slot released
        elif any(r["status"] == LED_SUCCEEDED for r in evidence):
            state = RES_COMMITTED
    conn.execute(
        "UPDATE bo_budget_reservations SET state = ?, updated_at = ? "
        "WHERE tenant = ? AND run_id = ? AND state = ?",
        (state, now, tenant, run_id, RES_RESERVED),
    )


def resume_reserved_run(tenant: str, run_id: str, *, db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM bo_exec_runs WHERE tenant = ? AND run_id = ?",
                           (tenant, run_id)).fetchone()
        if row is None:
            raise NotFoundError(run_id)
        if row["cancel_requested"] or not (
            row["state"] in (RUN_PAUSED, RUN_UNKNOWN, RUN_RECONCILIATION)
            or (row["state"] in (RUN_CLAIMED, RUN_RUNNING)
                and (row["lease_until"] is None or row["lease_until"] < _now()))
        ):
            raise InvalidStateError("rularea nu poate fi reluată")
        if conn.execute("SELECT 1 FROM bo_effect_ledger WHERE tenant = ? AND run_id = ? AND status = ?",
                        (tenant, run_id, LED_RECONCILIATION)).fetchone():
            raise InvalidStateError("reconciliere necesară")
        mandate = _mandate_from_row(conn.execute(
            "SELECT * FROM bo_exec_mandates WHERE tenant = ? AND mandate_id = ?",
            (tenant, row["mandate_id"]),
        ).fetchone())
        _reserve_budget(conn, tenant, mandate, run_id, Decimal(row["budget_reserved"]),
                        row["concurrency_slots"], _now())
        conn.execute(
            "UPDATE bo_exec_runs SET state = ?, pause_requested = 0, lease_owner = NULL, "
            "lease_until = NULL, lease_seq = lease_seq + 1, updated_at = ? WHERE tenant = ? AND run_id = ?",
            (RUN_PENDING, _now(), tenant, run_id),
        )


def get_run(tenant: str, run_id: str, db_path: Path | None = None) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bo_exec_runs WHERE tenant = ? AND run_id = ?",
            (tenant, run_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(run_id)
    return _run_from_row(row)


def list_runs(
    tenant: str,
    *,
    state: str | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM bo_exec_runs WHERE tenant = ?"
    params: list[Any] = [tenant]
    if state:
        sql += " AND state = ?"
        params.append(state)
    sql += " ORDER BY created_at DESC, run_id"
    with get_conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_run_from_row(r) for r in rows]


def claim_runs(
    tenant: str,
    *,
    worker_id: str,
    limit: int,
    lease_s: int,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Lease up to ``limit`` runnable runs for this worker.

    Claimable = PENDING, or RUNNING/PAUSED whose lease expired (crashed
    worker). ``lease_seq`` is the fencing counter: a stale worker that
    comes back can't finalize a run another worker owns.
    """
    now = _now()
    until = (datetime.now(UTC) + timedelta(seconds=lease_s)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            """
            SELECT * FROM bo_exec_runs
            WHERE tenant = ?
              AND (
                state = ?
                OR (state IN (?, ?)
                    AND (lease_until IS NULL OR lease_until < ?))
              )
              AND cancel_requested = 0 AND pause_requested = 0
            ORDER BY created_at LIMIT ?
            """,
            (tenant, RUN_PENDING, RUN_CLAIMED, RUN_RUNNING,
             now, limit),
        ).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE bo_exec_runs SET state = ?, lease_owner = ?, "
                "lease_until = ?, lease_seq = lease_seq + 1, updated_at = ? "
                "WHERE tenant = ? AND run_id = ?",
                (RUN_CLAIMED, worker_id, until, now, tenant, row["run_id"]),
            )
    return [get_run(tenant, r["run_id"], db_path=db_path) for r in rows]


def transition_run(
    tenant: str,
    run_id: str,
    state: str,
    *,
    worker_id: str | None = None,
    lease_s: int | None = None,
    fence: int | None = None,
    current_step: int | None = None,
    block_reason: str | None = None,
    clear_lease: bool = False,
    clear_flags: bool = False,
    reservation_state: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Move a run to ``state``. When ``fence`` is given the write only
    lands if the worker still owns the claim — a stale worker's transition
    is silently dropped and detected by the caller via rowcount."""
    now = _now()
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        sets = ["state = ?", "updated_at = ?"]
        params: list[Any] = [state, now]
        if lease_s is not None and worker_id is not None:
            sets.append("lease_until = ?")
            params.append(
                (datetime.now(UTC) + timedelta(seconds=lease_s))
                .isoformat(timespec="milliseconds").replace("+00:00", "Z")
            )
        if clear_lease:
            sets.append("lease_owner = NULL")
            sets.append("lease_until = NULL")
        if clear_flags:
            # Only the pause flag: a pending cancel must still be honored
            # at the next boundary after resume.
            sets.append("pause_requested = 0")
        if current_step is not None:
            sets.append("current_step = ?")
            params.append(current_step)
        if block_reason is not None:
            sets.append("block_reason = ?")
            params.append(block_reason[:300])
        if state in (RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED):
            sets.append("finished_at = ?")
            params.append(now)
        where = "WHERE tenant = ? AND run_id = ?"
        params.extend([tenant, run_id])
        if fence is not None:
            where += " AND lease_seq = ? AND lease_owner = ?"
            params.extend([fence, worker_id])
        cur = conn.execute(
            f"UPDATE bo_exec_runs SET {', '.join(sets)} {where}", params
        )
        if fence is not None and cur.rowcount == 0:
            raise ConflictError(
                f"workerul nu mai deține rularea {run_id} (fencing)"
            )
        if reservation_state is not None:
            settle_reservation(conn, tenant, run_id, reservation_state, now)


def request_flag(
    tenant: str,
    run_id: str,
    flag: str,
    *,
    actor: str,
    reason: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Set pause/cancel — honored at the next step boundary, never
    retroactively on an already-executed effect. The operator's reason
    is captured in the audit trail (consequential operation)."""
    if flag not in ("pause_requested", "cancel_requested"):
        raise InvalidStateError(flag)
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT state, lease_until FROM bo_exec_runs WHERE tenant = ? AND run_id = ?",
            (tenant, run_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(run_id)
        if row["state"] in (RUN_SUCCEEDED, RUN_FAILED, RUN_CANCELLED):
            raise InvalidStateError(
                f"rularea este deja {row['state']}"
            )
        conn.execute(
            f"UPDATE bo_exec_runs SET {flag} = 1, updated_at = ? "
            "WHERE tenant = ? AND run_id = ?",
            (_now(), tenant, run_id),
        )
        if row["state"] in (RUN_PENDING, RUN_PAUSED) or (
            row["state"] in (RUN_CLAIMED, RUN_RUNNING)
            and (row["lease_until"] is None or row["lease_until"] < _now())
        ):
            state = RUN_CANCELLED if flag == "cancel_requested" else RUN_PAUSED
            conn.execute(
                "UPDATE bo_exec_runs SET state = ?, lease_owner = NULL, "
                "lease_until = NULL, lease_seq = lease_seq + 1 WHERE tenant = ? AND run_id = ?",
                (state, tenant, run_id),
            )
            if state == RUN_CANCELLED:
                settle_reservation(conn, tenant, run_id, RES_RELEASED, _now())
    _audit(tenant, f"bo_run_{flag.removesuffix('_requested')}", {
        "run_id": run_id,
        "reason": (reason or "")[:300] or None,
    }, actor=actor)
    return get_run(tenant, run_id, db_path=db_path)


# --------------------------------------------------------------------------- #
# Checkpoints — append-only execution memory
# --------------------------------------------------------------------------- #

def write_checkpoint(
    tenant: str,
    run_id: str,
    step: int,
    state_data: dict[str, Any],
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Append a versioned checkpoint. The row is INSERT-only (history is
    preserved on resume — nothing is replaced). A failure propagates to
    the engine, which must NOT proceed to the dependent effect."""
    import hashlib

    digest = hashlib.sha256(
        json.dumps(state_data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT COALESCE(MAX(checkpoint_version), 0) AS v "
            "FROM bo_checkpoints WHERE tenant = ? AND run_id = ? AND step = ?",
            (tenant, run_id, step),
        ).fetchone()
        version = int(row["v"]) + 1
        conn.execute(
            """
            INSERT INTO bo_checkpoints (
                tenant, run_id, step, checkpoint_version, state_json,
                payload_digest, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant, run_id, step, version, json.dumps(state_data),
                digest, _now(),
            ),
        )
    return {
        "run_id": run_id, "step": step, "checkpoint_version": version,
        "payload_digest": digest,
    }


def list_checkpoints(
    tenant: str, run_id: str, db_path: Path | None = None
) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM bo_checkpoints WHERE tenant = ? AND run_id = ? "
            "ORDER BY step, checkpoint_version",
            (tenant, run_id),
        ).fetchall()
    return [
        {
            "step": int(r["step"]),
            "checkpoint_version": int(r["checkpoint_version"]),
            "state": json.loads(r["state_json"]),
            "payload_digest": r["payload_digest"],
            "created_at": r["created_at"],
        }
        for r in rows
    ]


def latest_checkpoint(
    tenant: str, run_id: str, step: int, db_path: Path | None = None
) -> dict[str, Any] | None:
    checkpoints = [
        c for c in list_checkpoints(tenant, run_id, db_path=db_path)
        if c["step"] == step
    ]
    return checkpoints[-1] if checkpoints else None


# --------------------------------------------------------------------------- #
# Effect ledger — intents, claims (lease+fence), finalization
# --------------------------------------------------------------------------- #

def _entry_from_row(row: Any) -> dict[str, Any]:
    return {
        "entry_id": row["entry_id"],
        "run_id": row["run_id"],
        "step": int(row["step"]),
        "intent_ref": row["intent_ref"],
        "idempotency_key": row["idempotency_key"],
        "payload_digest": row["payload_digest"],
        "provider": row["provider"],
        "action": row["action"],
        "resource": row["resource"],
        "status": row["status"],
        "receipt_ref": row["receipt_ref"],
        "receipt": json.loads(row["receipt_json"])
        if row["receipt_json"] is not None
        else None,
        "fence_version": int(row["fence_version"]),
        "lease_owner": row["lease_owner"],
        "lease_until": row["lease_until"],
        "attempts": int(row["attempts"]),
        "policy_version": int(row["policy_version"]),
        "correlation_id": row["correlation_id"],
        "submitted_at": row["submitted_at"],
        "finalized_at": row["finalized_at"],
        "created_at": row["created_at"],
    }


def get_or_create_intent(
    tenant: str,
    run: dict[str, Any],
    step: int,
    *,
    provider: str,
    payload: dict[str, Any],
    db_path: Path | None = None,
) -> dict[str, Any]:
    """The ledger row for (run, step). Idempotency identity is stable
    across resume: ``{run_id}:{step}`` — the same row is returned on
    retry, never duplicated. The same key with a DIFFERENT payload digest
    is a hard conflict."""
    import hashlib

    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    step_desc = run["steps"][step]
    key = f"{run['run_id']}:{step}"
    intent_ref = f"intent:{run['run_id']}:{step}"
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT * FROM bo_effect_ledger "
            "WHERE tenant = ? AND idempotency_key = ?",
            (tenant, key),
        ).fetchone()
        if row is not None:
            if row["payload_digest"] != digest:
                raise PayloadConflictError(
                    f"cheia de idempotență {key} are alt payload "
                    f"(digest diferit)"
                )
            return _entry_from_row(row)
        entry_id = f"eff_{uuid.uuid4().hex[:20]}"
        conn.execute(
            """
            INSERT INTO bo_effect_ledger (
                tenant, entry_id, run_id, step, intent_ref, idempotency_key,
                payload_digest, provider, action, resource, status,
                policy_version, correlation_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tenant, entry_id, run["run_id"], step, intent_ref, key,
                digest, provider, step_desc["action"], step_desc["resource"],
                LED_INTENT, run["policy_version"], run["correlation_id"],
                _now(),
            ),
        )
    return get_ledger_entry(tenant, entry_id, db_path=db_path)


def get_ledger_entry(
    tenant: str, entry_id: str, db_path: Path | None = None
) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bo_effect_ledger "
            "WHERE tenant = ? AND entry_id = ?",
            (tenant, entry_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(entry_id)
    return _entry_from_row(row)


def ledger_by_key(
    tenant: str, idempotency_key: str, db_path: Path | None = None
) -> dict[str, Any] | None:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bo_effect_ledger "
            "WHERE tenant = ? AND idempotency_key = ?",
            (tenant, idempotency_key),
        ).fetchone()
    return None if row is None else _entry_from_row(row)


def list_ledger(
    tenant: str, run_id: str, db_path: Path | None = None
) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM bo_effect_ledger WHERE tenant = ? AND run_id = ? "
            "ORDER BY step, created_at",
            (tenant, run_id),
        ).fetchall()
    return [_entry_from_row(r) for r in rows]


def claim_ledger_entry(
    tenant: str,
    entry_id: str,
    *,
    worker_id: str,
    lease_s: int,
    retry_backoff_s: int = 0,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Claim a ledger entry for execution: bumps ``fence_version`` and
    sets the lease. Claimable = INTENT, or SUBMITTED/UNKNOWN whose lease
    expired (crashed worker — the reclaimer must then do a receipt lookup
    before re-submitting; that decision belongs to the engine). A
    SUCCEEDED/FAILED/RECONCILIATION entry is never re-executed.

    ``retry_backoff_s`` adds a floor after lease expiry: a stale
    SUBMITTED/UNKNOWN entry is claimable only once
    ``lease_until + backoff`` has passed — retries cannot spin tighter
    than the configured backoff."""
    now_dt = datetime.now(UTC)
    until = (now_dt + timedelta(seconds=lease_s)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, lease_until FROM bo_effect_ledger "
            "WHERE tenant = ? AND entry_id = ?",
            (tenant, entry_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(entry_id)
        claimable = row["status"] == LED_INTENT
        if not claimable and row["status"] in (LED_SUBMITTED, LED_UNKNOWN):
            if row["lease_until"] is None:
                claimable = True
            else:
                try:
                    lease_end = datetime.fromisoformat(
                        str(row["lease_until"]).replace("Z", "+00:00")
                    )
                except ValueError:
                    lease_end = now_dt  # corrupt lease → treat as expired
                claimable = now_dt >= lease_end + timedelta(
                    seconds=max(0, retry_backoff_s)
                )
        if not claimable:
            raise InvalidStateError(
                f"intrarea {entry_id} este {row['status']}, nu poate fi revendicată"
            )
        conn.execute(
            "UPDATE bo_effect_ledger SET fence_version = fence_version + 1, "
            "lease_owner = ?, lease_until = ?, attempts = attempts + 1, "
            "status = ? WHERE tenant = ? AND entry_id = ?",
            (worker_id, until, LED_SUBMITTED, tenant, entry_id),
        )
    return get_ledger_entry(tenant, entry_id, db_path=db_path)


def finalize_ledger_entry(
    tenant: str,
    entry_id: str,
    *,
    status: str,
    fence_version: int,
    receipt_ref: str | None = None,
    receipt: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> bool:
    """CAS finalization on ``fence_version`` — a stale worker that lost
    the claim (lease expired, another worker took over) CANNOT finalize.
    Returns False when the write was fenced out."""
    now = _now()
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE bo_effect_ledger SET status = ?, receipt_ref = ?, "
            "receipt_json = ?, lease_owner = NULL, lease_until = NULL, "
            "finalized_at = ? WHERE tenant = ? AND entry_id = ? "
            "AND fence_version = ? AND status = 'SUBMITTED' AND lease_owner IS NOT NULL",
            (
                status, receipt_ref,
                json.dumps(receipt) if receipt is not None else None,
                now, tenant, entry_id, fence_version,
            ),
        )
    return cur.rowcount == 1


def mark_ledger_status(
    tenant: str,
    entry_id: str,
    status: str,
    *,
    expected_fence: int | None = None,
    receipt_ref: str | None = None,
    receipt: dict[str, Any] | None = None,
    db_path: Path | None = None,
) -> None:
    """CAS reconciliation of ambiguous evidence; invalidate the old worker.

    A receipt can resolve an active claim, but declaring it unexecuted
    requires its lease to expire. Terminal evidence cannot be overwritten.
    """
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT status, fence_version, lease_until, finalized_at FROM bo_effect_ledger WHERE tenant = ? AND entry_id = ?",
            (tenant, entry_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(entry_id)
        if row["status"] not in (LED_UNKNOWN, LED_SUBMITTED, LED_RECONCILIATION):
            raise ConflictError("efectul nu mai este reconciliabil")
        if (status == LED_INTENT and row["status"] == LED_SUBMITTED
                and row["lease_until"] and row["lease_until"] > _now()):
            raise ConflictError("un worker activ nu poate fi declarat neexecutat")
        if expected_fence is not None and row["fence_version"] != expected_fence:
            raise ConflictError("reconciliere depășită (fencing)")
        # Guardian orders verdicts of the same attempt by occurredAt. A
        # recovered receipt is a new verdict, even within one clock tick.
        finalized = datetime.fromisoformat(_now().replace("Z", "+00:00"))
        if row["finalized_at"]:
            previous = datetime.fromisoformat(row["finalized_at"].replace("Z", "+00:00"))
            finalized = max(finalized, previous + timedelta(milliseconds=1))
        finalized_at = finalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        conn.execute(
            "UPDATE bo_effect_ledger SET fence_version = fence_version + 1, status = ?, receipt_ref = "
            "COALESCE(?, receipt_ref), receipt_json = "
            "COALESCE(?, receipt_json), lease_owner = NULL, "
            "lease_until = NULL, finalized_at = ? "
            "WHERE tenant = ? AND entry_id = ?",
            (
                status, receipt_ref,
                json.dumps(receipt) if receipt is not None else None,
                finalized_at, tenant, entry_id,
            ),
        )


def ledger_counts(tenant: str, run_id: str, db_path: Path | None = None) -> dict[str, int]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM bo_effect_ledger "
            "WHERE tenant = ? AND run_id = ? GROUP BY status",
            (tenant, run_id),
        ).fetchall()
    return {r["status"]: int(r["n"]) for r in rows}


def reservation_for(
    tenant: str, run_id: str, db_path: Path | None = None
) -> dict[str, Any] | None:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bo_budget_reservations "
            "WHERE tenant = ? AND run_id = ?",
            (tenant, run_id),
        ).fetchone()
    return None if row is None else dict(row)


def _audit(tenant: str, event: str, details: dict[str, Any], *, actor: str) -> None:
    from openexecutive.audit import log_event

    log_event(
        event, f"{event}: {details.get('run_id') or details.get('mandate_id')}",
        actor=actor, details={"tenant": tenant, **details},
    )
