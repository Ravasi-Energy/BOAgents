"""Execution engine (VAL4-01).

Order of operations per step — deliberately fixed:

1. Run flags (pause/cancel) — honored at the step boundary, never
   retroactively on an already-executed effect.
2. Mandate chain re-check — revocation/expiry of the leaf OR any ancestor
   closes access to the effect HERE, not only at creation/approval.
3. Checkpoint write — when required, persistence failure BLOCKS the
   dependent effect (no checkpoint, no step).
4. Ledger intent — stable idempotency identity ``{run}:{step}``; the same
   key with a different payload is a hard conflict.
5. Effect — claim under lease + fence, then the provider call. Receipt →
   SUCCEEDED (CAS on the fence version: a stale worker cannot finalize).
   Lost answer → UNKNOWN for idempotent providers (resume deduplicates),
   RECONCILIATION_REQUIRED for non-idempotent ones (a blind retry would
   double the effect — exactly-once is not promised there).

Execution state (runs + checkpoints) is never the proof of an effect —
the ledger is. Telemetry events are emitted through the durable outbox;
their delivery may lag, the ledger does not.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from openexecutive.bo.execution import store
from openexecutive.bo.execution.mandate import (
    MandateExpiredError,
    MandateRevokedError,
    assert_active,
)
from openexecutive.bo.execution.synth import (
    EffectProvider,
    ProviderError,
    ProviderTimeout,
)

logger = logging.getLogger(__name__)

CheckpointWriter = Callable[..., dict[str, Any]]


class ExecutionDisabledError(Exception):
    """bo.exec.enabled is off — no submission, no work."""


def _setting(tenant: str, key: str, default: Any, db_path: Path | None) -> Any:
    try:
        from openexecutive.bo.settings import store as settings_store

        return settings_store.get_effective_value(tenant, key, db_path=db_path)
    except Exception:  # noqa: BLE001 — settings must not crash the engine
        return default


def enabled(tenant: str, db_path: Path | None = None) -> bool:
    return bool(_setting(tenant, "bo.exec.enabled", False, db_path))


def assert_effect_authority(tenant: str, mandate_id: str, *,
                            step_action: str | None = None,
                            step_resource: str | None = None,
                            db_path: Path | None = None) -> None:
    """Common UI/worker authority, including every delegated constraint."""
    from openexecutive.bo.execution import guardian
    if not enabled(tenant, db_path=db_path):
        raise ExecutionDisabledError("execuția delegată este oprită")
    chain = store.mandate_chain(tenant, mandate_id, db_path=db_path)
    for link in chain:
        assert_active(link)
    for link in chain:
        guardian.assert_effect_authorized(
            tenant, link, step_action=step_action,
            step_resource=step_resource, db_path=db_path,
        )


# --------------------------------------------------------------------------- #
# Submission
# --------------------------------------------------------------------------- #

def submit_execution(
    tenant: str,
    mandate_id: str,
    steps: list[dict[str, Any]],
    *,
    budget_amount: Decimal,
    correlation_id: str | None,
    actor: str,
    parent_run_id: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Create a PENDING run under a mandate. Budget + concurrency are
    reserved atomically inside the same transaction — two concurrent
    submitters cannot overspend the mandate."""
    if not enabled(tenant, db_path=db_path):
        raise ExecutionDisabledError("execuția delegată este oprită (bo.exec.enabled)")
    if budget_amount.is_nan() or budget_amount.is_infinite() or budget_amount <= 0:
        from openexecutive.bo.execution.mandate import MandateValidationError

        raise MandateValidationError("budget_amount: valoare invalidă")
    cap_raw = str(
        _setting(tenant, "bo.exec.budget_cap", "", db_path)
    ).strip()
    if cap_raw:
        cap_amount = Decimal(cap_raw.split()[0])
        if budget_amount > cap_amount:
            from openexecutive.bo.execution.mandate import (
                MandateValidationError,
            )

            raise MandateValidationError(
                f"budget_amount {budget_amount} depășește plafonul "
                f"tenantului ({cap_raw})"
            )
    mandate = store.get_mandate(tenant, mandate_id, db_path=db_path)
    # The whole chain must be active at submission too (not only per step).
    try:
        for link in store.mandate_chain(tenant, mandate_id, db_path=db_path):
            assert_active(link)
    except (MandateRevokedError, MandateExpiredError) as exc:
        raise store.InvalidStateError(str(exc)) from exc
    if len(steps) > int(_setting(tenant, "bo.exec.max_steps", 50, db_path)):
        from openexecutive.bo.execution.mandate import MandateValidationError
        raise MandateValidationError("numărul de pași depășește bo.exec.max_steps")
    slots = min(
        int(_setting(tenant, "bo.exec.default_concurrency", 2, db_path)),
        mandate.concurrency_limit,
    )
    run = store.submit_run(
        tenant, mandate, steps,
        budget_amount=budget_amount, slots=slots,
        correlation_id=correlation_id or f"corr_{uuid.uuid4().hex[:20]}",
        actor=actor, parent_run_id=parent_run_id, db_path=db_path,
    )
    _emit_checkpoint(
        tenant, run, mandate, step=0, state="PENDING", db_path=db_path,
    )
    return run


# --------------------------------------------------------------------------- #
# Work claiming + execution
# --------------------------------------------------------------------------- #

def work_once(
    tenant: str,
    *,
    provider: EffectProvider,
    worker_id: str | None = None,
    limit: int = 5,
    db_path: Path | None = None,
    checkpoint_writer: CheckpointWriter | None = None,
) -> dict[str, Any]:
    """One bounded work cycle: claim runnable runs and execute them.
    Returns what was claimed and each run's outcome — the caller (API
    route, CLI probe, second process) controls scheduling."""
    worker_id = worker_id or f"wrk_{uuid.uuid4().hex[:12]}"
    if not enabled(tenant, db_path=db_path):
        return {"worker_id": worker_id, "claimed": 0, "outcomes": []}
    lease_s = int(_setting(tenant, "bo.exec.lease_seconds", 60, db_path))
    claimed = store.claim_runs(
        tenant, worker_id=worker_id, limit=limit, lease_s=lease_s,
        db_path=db_path,
    )
    outcomes = []
    for run in claimed:
        outcome = execute_run(
            tenant, run, provider,
            worker_id=worker_id, lease_s=lease_s, db_path=db_path,
            checkpoint_writer=checkpoint_writer,
        )
        outcomes.append(outcome)
    return {"worker_id": worker_id, "claimed": len(claimed), "outcomes": outcomes}


def execute_run(
    tenant: str,
    run: dict[str, Any],
    provider: EffectProvider,
    *,
    worker_id: str,
    lease_s: int,
    db_path: Path | None = None,
    checkpoint_writer: CheckpointWriter | None = None,
) -> dict[str, Any]:
    """Drive one claimed run through its steps. Returns the terminal (or
    paused) state. Fencing: every state transition carries the lease_seq
    claimed by THIS worker — if the lease was taken over, the writes are
    refused and we stop without touching anything else."""
    fence = run["lease_seq"]
    write_checkpoint = checkpoint_writer or store.write_checkpoint
    checkpoint_required = bool(
        _setting(tenant, "bo.exec.checkpoint_required", True, db_path)
    )
    run_id = run["run_id"]

    def transition(state: str, **kw: Any) -> None:
        store.transition_run(
            tenant, run_id, state, worker_id=worker_id, lease_s=lease_s,
            fence=fence, db_path=db_path, **kw,
        )

    mandate = store.get_mandate(tenant, run["mandate_id"], db_path=db_path)
    try:
        transition(store.RUN_RUNNING)
    except store.ConflictError:
        return {"run_id": run_id, "state": "lost-claim"}
    # Lease evidence: the fencing token identifies which worker epoch
    # produced each state. Terminal checkpoints carry it too — Guardian
    # orders by (fencing, step, time), so a leaseless terminal event
    # would look older than the CLAIMED that precedes it.
    lease_evidence = {
        "ownerRef": worker_id,
        "fencingToken": fence,
        "expiresAt": (
            datetime.now(UTC) + timedelta(seconds=lease_s)
        ).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
    }
    _emit_checkpoint(
        tenant, run, mandate, step=run["current_step"], state="CLAIMED",
        lease=lease_evidence,
        db_path=db_path,
    )

    chain_ids = run["mandate_id"]
    for step_idx in range(run["current_step"], len(run["steps"])):
        fresh = store.get_run(tenant, run_id, db_path=db_path)
        # 1. Administrative flags at the step boundary.
        try:
            if fresh["cancel_requested"]:
                transition(
                    store.RUN_CANCELLED, clear_lease=True,
                    reservation_state=store.RES_RELEASED,
                )
                _emit_checkpoint(
                    tenant, fresh, mandate, step=step_idx, state="FAILED",
                    lease=lease_evidence,
                    reason="cancelled_by_operator", db_path=db_path,
                )
                return {"run_id": run_id, "state": store.RUN_CANCELLED}
            if fresh["pause_requested"] or not enabled(tenant, db_path=db_path):
                transition(store.RUN_PAUSED, clear_lease=True)
                _emit_checkpoint(
                    tenant, fresh, mandate, step=step_idx, state="PENDING",
                    lease=lease_evidence,
                    reason="pause_requested", db_path=db_path,
                )
                return {"run_id": run_id, "state": store.RUN_PAUSED}
            # 3. Mandatory checkpoint BEFORE the dependent step.
            if checkpoint_required:
                try:
                    write_checkpoint(
                        tenant, run_id, step_idx,
                        {"phase": "pre", "step": step_idx,
                         "action": fresh["steps"][step_idx]["action"],
                         "resource": fresh["steps"][step_idx]["resource"]},
                        db_path=db_path,
                    )
                except Exception as exc:  # noqa: BLE001 — any persistence
                    try:                            # failure blocks the step
                        transition(
                            store.RUN_FAILED, clear_lease=True,
                            block_reason=f"checkpoint_unavailable: {exc}"[:300],
                            reservation_state=store.RES_RELEASED,
                        )
                    except store.ConflictError:
                        return {"run_id": run_id, "state": "lost-claim"}
                    return {
                        "run_id": run_id, "state": store.RUN_FAILED,
                        "block_reason": "checkpoint_unavailable",
                    }
            # 2. Mandate chain re-check at the EFFECT boundary — local
            # chain first, then the Guardian-held status when the
            # mandate is bound to the Guardian authority (REM-01).
            block: str | None = None
            try:
                for link in store.mandate_chain(
                    tenant, chain_ids, db_path=db_path
                ):
                    assert_active(link)
            except (MandateRevokedError, MandateExpiredError) as exc:
                reason = "mandate_revoked" if isinstance(
                    exc, MandateRevokedError
                ) else "mandate_expired"
                block = f"{reason}: {exc}"
            else:
                from openexecutive.bo.execution import guardian
                step_def = fresh["steps"][step_idx]
                try:
                    assert_effect_authority(
                        tenant, mandate.mandate_id,
                        step_action=step_def.get("action"),
                        step_resource=step_def.get("resource"),
                        db_path=db_path,
                    )
                except guardian.GuardianUnavailableError as exc:
                    # Mandatory dependency down — pause, don't fail: the
                    # operator can resume once Guardian is reachable.
                    transition(
                        store.RUN_PAUSED, clear_lease=True,
                        block_reason=f"guardian_unavailable: {exc}"[:300],
                    )
                    _emit_checkpoint(
                        tenant, fresh, mandate, step=step_idx,
                        state="PENDING", lease=lease_evidence,
                        reason="guardian_authorization_unavailable",
                        db_path=db_path,
                    )
                    return {
                        "run_id": run_id, "state": store.RUN_PAUSED,
                        "block_reason": "guardian_unavailable",
                    }
                except guardian.GuardianDeniedError as exc:
                    block = f"guardian_{exc.kind}: {exc}"
                except (MandateRevokedError, MandateExpiredError) as exc:
                    block = f"mandate_inactive: {exc}"
                except ExecutionDisabledError:
                    transition(store.RUN_PAUSED, clear_lease=True)
                    return {"run_id": run_id, "state": store.RUN_PAUSED}
            if block is not None:
                counts = store.ledger_counts(tenant, run_id, db_path=db_path)
                state = (
                    store.RUN_RECONCILIATION
                    if counts.get(store.LED_UNKNOWN)
                    else store.RUN_FAILED
                )
                transition(
                    state, clear_lease=True,
                    block_reason=block[:300],
                    reservation_state=store.RES_RELEASED,
                )
                _emit_checkpoint(
                    tenant, fresh, mandate, step=step_idx,
                    state=state, lease=lease_evidence,
                    reason=block.split(":")[0], db_path=db_path,
                )
                return {
                    "run_id": run_id, "state": state,
                    "block_reason": block.split(":")[0],
                }
        except store.ConflictError:
            return {"run_id": run_id, "state": "lost-claim"}
        # 4 + 5. Ledger intent + effect.
        try:
            outcome = _execute_step(
                tenant, fresh, step_idx, provider,
                worker_id=worker_id, lease_s=lease_s, db_path=db_path,
            )
        except store.ConflictError:
            return {"run_id": run_id, "state": "lost-claim"}
        if outcome["terminal"] == "fenced":
            return {"run_id": run_id, "state": "lost-claim"}
        if outcome["terminal"] is not None:
            try:
                transition(
                    outcome["terminal"], clear_lease=True,
                    reservation_state=store.RES_RELEASED
                    if outcome["terminal"] not in (store.RUN_SUCCEEDED, store.RUN_PAUSED) else None,
                    **({"block_reason": outcome["block_reason"]}
                       if outcome.get("block_reason") else {}),
                )
            except store.ConflictError:
                return {"run_id": run_id, "state": "lost-claim"}
            _emit_checkpoint(
                tenant, fresh, mandate, step=step_idx,
                state=_RUN_TO_EVENT_STATE.get(
                    outcome["terminal"], "FAILED"
                ),
                lease=lease_evidence,
                reason=(outcome.get("block_reason") or "")[:256] or None,
                db_path=db_path,
            )
            return {"run_id": run_id, "state": outcome["terminal"],
                    **({"block_reason": outcome["block_reason"]}
                       if outcome.get("block_reason") else {})}
        # Post-step checkpoint: execution memory — what was done + receipt
        # ref. It is NOT the proof of effect (the ledger is).
        if checkpoint_required:
            try:
                write_checkpoint(
                    tenant, run_id, step_idx,
                    {"phase": "post", "step": step_idx,
                     "ledger_status": outcome["ledger_status"],
                     "receipt_ref": outcome.get("receipt_ref")},
                    db_path=db_path,
                )
            except Exception:  # noqa: BLE001 — post-checkpoint failure
                try:                            # is still blocking
                    transition(
                        store.RUN_FAILED, clear_lease=True,
                        block_reason="checkpoint_unavailable(post)",
                        reservation_state=store.RES_RELEASED,
                    )
                except store.ConflictError:
                    return {"run_id": run_id, "state": "lost-claim"}
                return {"run_id": run_id, "state": store.RUN_FAILED}
        try:
            transition(
                store.RUN_RUNNING, current_step=step_idx + 1,
            )
        except store.ConflictError:
            return {"run_id": run_id, "state": "lost-claim"}
        _emit_checkpoint(
            tenant, fresh, mandate, step=step_idx + 1, state="CLAIMED",
            lease=lease_evidence,
            db_path=db_path,
        )
    transition(
        store.RUN_SUCCEEDED, clear_lease=True,
        reservation_state=store.RES_COMMITTED,
    )
    _emit_checkpoint(
        tenant, fresh, mandate, step=len(run["steps"]),
        state="SUCCEEDED", lease=lease_evidence, db_path=db_path,
    )
    return {"run_id": run_id, "state": store.RUN_SUCCEEDED}


def _execute_step(
    tenant: str,
    run: dict[str, Any],
    step: int,
    provider: EffectProvider,
    *,
    worker_id: str,
    lease_s: int,
    db_path: Path | None,
) -> dict[str, Any]:
    """Ledger + one provider call for step ``step``. Returns
    ``{"terminal": None, "ledger_status": ..., "receipt_ref": ...}`` to
    continue, or a terminal run state to stop."""
    fresh = store.get_run(tenant, run["run_id"], db_path=db_path)
    if fresh["lease_owner"] != worker_id or fresh["lease_seq"] != run["lease_seq"]:
        return {"terminal": "fenced"}
    if fresh["cancel_requested"]:
        return {"terminal": store.RUN_CANCELLED}
    if fresh["pause_requested"] or not enabled(tenant, db_path=db_path):
        return {"terminal": store.RUN_PAUSED, "block_reason": "execution_disabled_or_paused"}
    step_desc = run["steps"][step]
    if step_desc.get("resource") == "synth.erp":
        from openexecutive.bo.pilot.provider import PilotProvider
        if len(run["steps"]) != 1 or step_desc.get("action") != "diagnose":
            return {"terminal": store.RUN_FAILED, "block_reason": "plan diagnostic invalid"}
        provider = PilotProvider(tenant, run, db_path)
    payload = step_desc.get("payload", {})
    try:
        entry = store.get_or_create_intent(
            tenant, run, step, provider=provider.name, payload=payload,
            db_path=db_path,
        )
    except store.PayloadConflictError as exc:
        return {"terminal": store.RUN_FAILED, "block_reason": str(exc)}

    if entry["status"] == store.LED_SUCCEEDED:
        return {
            "terminal": None, "ledger_status": store.LED_SUCCEEDED,
            "receipt_ref": entry["receipt_ref"],
        }
    if entry["status"] in (store.LED_FAILED, store.LED_RECONCILIATION):
        return {
            "terminal": store.RUN_FAILED
            if entry["status"] == store.LED_FAILED
            else store.RUN_RECONCILIATION,
            "block_reason": f"step {step}: effect {entry['status']}",
        }

    # SUBMITTED/UNKNOWN after a crash — receipt lookup BEFORE any retry.
    if entry["status"] in (store.LED_SUBMITTED, store.LED_UNKNOWN):
        receipt = provider.receipt_for(
            tenant=tenant, idempotency_key=entry["idempotency_key"],
        )
        if receipt is not None:
            # The effect provably happened — finalize, no re-execution.
            store.mark_ledger_status(
                tenant, entry["entry_id"], store.LED_SUCCEEDED,
                expected_fence=entry["fence_version"],
                receipt_ref=receipt["receipt_ref"], receipt=receipt,
                db_path=db_path,
            )
            _emit_receipt(
                tenant, run, entry, status="CONFIRMED",
                receipt_ref=receipt["receipt_ref"], db_path=db_path,
            )
            return {
                "terminal": None, "ledger_status": store.LED_SUCCEEDED,
                "receipt_ref": receipt["receipt_ref"],
            }
        if not provider.idempotent or not provider.retry_unknown:
            # Ambiguous external state + no dedup guarantee: a retry could
            # double the effect. Surface it, don't guess.
            store.mark_ledger_status(
                tenant, entry["entry_id"], store.LED_RECONCILIATION,
                expected_fence=entry["fence_version"],
                db_path=db_path,
            )
            return {
                "terminal": store.RUN_RECONCILIATION,
                "block_reason": f"step {step}: stare externă ambiguă, "
                f"provider fără idempotență — reconciliere necesară",
            }
        # Idempotent provider: safe to re-claim and retry — the provider
        # deduplicates by key. The attempt cap still applies: past it the
        # entry stops being auto-reclaimed and needs reconciliation.
        max_attempts = int(
            _setting(tenant, "bo.exec.max_effect_attempts", 3, db_path)
        )
        if int(entry["attempts"]) >= max_attempts:
            store.mark_ledger_status(
                tenant, entry["entry_id"], store.LED_RECONCILIATION,
                expected_fence=entry["fence_version"],
                db_path=db_path,
            )
            return {
                "terminal": store.RUN_RECONCILIATION,
                "block_reason": f"step {step}: pragul de tentative "
                f"({max_attempts}) a fost atins — reconciliere necesară",
            }

    backoff_s = int(
        _setting(tenant, "bo.exec.retry_backoff_s", 0, db_path)
    )
    try:
        entry = store.claim_ledger_entry(
            tenant, entry["entry_id"], worker_id=worker_id,
            lease_s=lease_s, retry_backoff_s=backoff_s, db_path=db_path,
        )
    except store.InvalidStateError:
        # Someone else owns it (active lease) — stop without touching.
        return {"terminal": "fenced"}

    try:
        receipt = provider.submit(
            tenant=tenant,
            idempotency_key=entry["idempotency_key"],
            payload_digest=entry["payload_digest"],
            amount=int(payload.get("amount", 1)),
        )
    except ProviderTimeout:
        # Answer lost AFTER submission — ambiguous. Fence-CAS to UNKNOWN:
        # idempotent provider → resumable; non-idempotent → reconciliation.
        status = (
            store.LED_UNKNOWN if provider.idempotent
            else store.LED_RECONCILIATION
        )
        if not store.finalize_ledger_entry(
            tenant, entry["entry_id"], status=status,
            fence_version=entry["fence_version"], db_path=db_path,
        ):
            return {"terminal": "fenced"}
        _emit_receipt(
            tenant, run, entry, status="UNKNOWN", db_path=db_path,
        )
        return {
            "terminal": store.RUN_UNKNOWN if provider.idempotent
            else store.RUN_RECONCILIATION,
            "block_reason": f"step {step}: răspuns pierdut după trimitere "
            f"(efect ambiguu)",
        }
    except ProviderError as exc:
        if not store.finalize_ledger_entry(
            tenant, entry["entry_id"], status=store.LED_FAILED,
            fence_version=entry["fence_version"], db_path=db_path,
        ):
            return {"terminal": "fenced"}
        _emit_receipt(
            tenant, run, entry, status="FAILED", db_path=db_path,
        )
        return {"terminal": store.RUN_FAILED, "block_reason": str(exc)}
    except Exception:  # noqa: BLE001 — infrastructure failure, not a
        if not store.finalize_ledger_entry(  # definitive provider answer
            tenant, entry["entry_id"], status=store.LED_UNKNOWN,
            fence_version=entry["fence_version"], db_path=db_path,
        ):
            return {"terminal": "fenced"}
        _emit_receipt(
            tenant, run, entry, status="UNKNOWN", db_path=db_path,
        )
        return {"terminal": store.RUN_UNKNOWN}

    if not store.finalize_ledger_entry(
        tenant, entry["entry_id"], status=store.LED_SUCCEEDED,
        fence_version=entry["fence_version"],
        receipt_ref=receipt["receipt_ref"], receipt=receipt,
        db_path=db_path,
    ):
        return {"terminal": "fenced"}
    _emit_receipt(
        tenant, run, entry, status="CONFIRMED",
        receipt_ref=receipt["receipt_ref"], db_path=db_path,
    )
    return {
        "terminal": None, "ledger_status": store.LED_SUCCEEDED,
        "receipt_ref": receipt["receipt_ref"],
    }


# --------------------------------------------------------------------------- #
# Resume + reconcile
# --------------------------------------------------------------------------- #

def resume_run(
    tenant: str, run_id: str, *, actor: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Make a stopped run claimable again — SAME run id, SAME idempotency
    keys, history preserved (checkpoints are append-only). Resume never
    re-creates intents and never claims to reverse an external effect."""
    run = store.get_run(tenant, run_id, db_path=db_path)
    now = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    resumable = run["state"] in (
        store.RUN_PAUSED, store.RUN_UNKNOWN, store.RUN_RECONCILIATION,
    ) or (
        run["state"] in (store.RUN_CLAIMED, store.RUN_RUNNING)
        and (run["lease_until"] is None or run["lease_until"] < now)
    )
    if not resumable:
        raise store.InvalidStateError(
            f"rularea {run_id} este {run['state']} — nu poate fi reluată"
        )
    unresolved = [
        e for e in store.list_ledger(tenant, run_id, db_path=db_path)
        if e["status"] == store.LED_RECONCILIATION
    ]
    if unresolved:
        raise store.InvalidStateError(
            f"{len(unresolved)} efecte cer reconciliere explicită "
            f"înainte de reluare"
        )
    store.resume_reserved_run(tenant, run_id, db_path=db_path)
    _audit(tenant, "bo_run_resume", {"run_id": run_id}, actor=actor)
    return store.get_run(tenant, run_id, db_path=db_path)


def reconcile_run(
    tenant: str,
    run_id: str,
    provider: EffectProvider,
    *,
    resolution: str,
    actor: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Resolve UNKNOWN/SUBMITTED/RECONCILIATION entries.

    ``resolution="receipt"``: receipt lookup against the provider — a
    found receipt finalizes SUCCEEDED without re-executing; nothing found
    keeps the entry RECONCILIATION_REQUIRED (still not re-executed).
    ``resolution="mark_failed"``: the operator asserts the effect did not
    happen (audited). Only then is resume meaningful — the intent may be
    retried by the engine because the operator took responsibility for
    the external state.
    """
    entries = [
        e for e in store.list_ledger(tenant, run_id, db_path=db_path)
        if e["status"] in (
            store.LED_UNKNOWN, store.LED_SUBMITTED, store.LED_RECONCILIATION,
        )
    ]
    resolved = pending = 0
    for entry in entries:
        if entry["provider"] == "synth.erp":
            from openexecutive.bo.pilot.provider import PilotProvider
            provider = PilotProvider(tenant, store.get_run(tenant, run_id, db_path=db_path), db_path)
            if resolution != "receipt":
                raise store.InvalidStateError("Pilot UNKNOWN cere readback corelat; fără retrimitere oarbă")
        if resolution == "receipt":
            receipt = provider.receipt_for(
                tenant=tenant, idempotency_key=entry["idempotency_key"],
            )
            if receipt is not None:
                store.mark_ledger_status(
                    tenant, entry["entry_id"], store.LED_SUCCEEDED,
                    expected_fence=entry["fence_version"],
                    receipt_ref=receipt["receipt_ref"], receipt=receipt,
                    db_path=db_path,
                )
                _emit_receipt(
                    tenant, store.get_run(tenant, run_id, db_path=db_path),
                    entry, status="CONFIRMED", receipt_ref=receipt["receipt_ref"],
                    db_path=db_path,
                )
                resolved += 1
            else:
                store.mark_ledger_status(
                    tenant, entry["entry_id"], store.LED_RECONCILIATION,
                    expected_fence=entry["fence_version"],
                    db_path=db_path,
                )
                pending += 1
        elif resolution == "mark_failed":
            store.mark_ledger_status(
                tenant, entry["entry_id"], store.LED_INTENT,
                expected_fence=entry["fence_version"], db_path=db_path,
            )
            resolved += 1
        else:
            raise store.InvalidStateError(f"rezoluție necunoscută: {resolution}")
    _audit(tenant, "bo_run_reconcile", {
        "run_id": run_id, "resolution": resolution,
        "resolved": resolved, "pending": pending,
    }, actor=actor)
    # When every ambiguity is resolved and steps remain, the run may be
    # resumed; when nothing remains ambiguous and nothing remains to do,
    # close it.
    counts = store.ledger_counts(tenant, run_id, db_path=db_path)
    ambiguous = counts.get(store.LED_UNKNOWN, 0) + counts.get(
        store.LED_RECONCILIATION, 0
    ) + counts.get(store.LED_SUBMITTED, 0)
    fresh = store.get_run(tenant, run_id, db_path=db_path)
    if ambiguous == 0 and fresh["state"] in (
        store.RUN_UNKNOWN, store.RUN_RECONCILIATION,
    ):
        state = (
            store.RUN_SUCCEEDED
            if fresh["current_step"] >= len(fresh["steps"])
            else store.RUN_PAUSED
        )
        store.transition_run(
            tenant, run_id, state, clear_lease=True,
            reservation_state=store.RES_COMMITTED
            if state == store.RUN_SUCCEEDED else None,
            db_path=db_path,
        )
    return {
        "run": store.get_run(tenant, run_id, db_path=db_path),
        "resolved": resolved, "pending": pending,
    }


# --------------------------------------------------------------------------- #
# Evidence events → durable outbox (telemetry, not the ledger)
# --------------------------------------------------------------------------- #

def _audit(tenant: str, event: str, details: dict[str, Any], *, actor: str) -> None:
    from openexecutive.audit import log_event

    log_event(
        event, f"{event}: {details.get('run_id') or details.get('mandate_id')}",
        actor=actor, details={"tenant": tenant, **details},
    )


# Internal run state -> checkpoint state on the wire. PAUSED maps to
# PENDING (the run is claimable again); CANCELLED to FAILED — the
# contract has no cancellation state and a cancelled run is definitively
# stopped, with the reason in ``failureReason``.
_RUN_TO_EVENT_STATE = {
    store.RUN_PENDING: "PENDING",
    store.RUN_CLAIMED: "CLAIMED",
    store.RUN_RUNNING: "CLAIMED",
    store.RUN_PAUSED: "PENDING",
    store.RUN_SUCCEEDED: "SUCCEEDED",
    store.RUN_FAILED: "FAILED",
    store.RUN_CANCELLED: "FAILED",
    store.RUN_UNKNOWN: "UNKNOWN",
    store.RUN_RECONCILIATION: "RECONCILIATION_REQUIRED",
}


def _enqueue(
    tenant: str, ref_id: str, envelope: dict[str, Any],
    db_path: Path | None,
) -> None:
    """Persist the sealed envelope in the durable outbox — delivery
    failure never erases local execution evidence."""
    from openexecutive.bo.routing import store as routing_store

    try:
        routing_store.enqueue_outbox(
            tenant, "execution", ref_id, envelope, db_path=db_path,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never gate execution
        # Only the exception class reaches the log — the message can carry
        # arbitrary internals (paths, payload echoes) that must not be
        # persisted alongside delivery history.
        logger.warning("evenimentul de execuție nu a putut fi pus în outbox (%s)",
                       type(exc).__name__)


_REASON_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_:. /-"
)


def _safe_reason(reason: str | None) -> str | None:
    """The contract's ``failureReason`` charset is ASCII-only — internal
    Romanian messages carry diacritics and punctuation the wire pattern
    rejects. Map anything else to ``_``; drop if nothing survives."""
    if not reason:
        return None
    cleaned = "".join(ch if ch in _REASON_ALLOWED else "_" for ch in reason)
    cleaned = cleaned.strip()[:256]
    return cleaned or None


def _emit_checkpoint(
    tenant: str,
    run: dict[str, Any],
    mandate: Any,
    *,
    step: int,
    state: str,
    lease: dict[str, Any] | None = None,
    digest: str | None = None,
    reason: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Emit a ``checkpoint`` event — execution state, never effect
    evidence. ``mandateRef`` is the Guardian-bound ref when the mandate
    carries one, else the local mandate id."""
    from openexecutive.bo.execution import serialize

    try:
        mandate_ref = getattr(mandate, "guardian_ref", None) or (
            mandate.mandate_id if mandate else run["mandate_id"]
        )
        parent_ref = (
            getattr(mandate, "parent_mandate_id", None)
            if mandate else None
        )
        env = serialize.build_checkpoint_event(
            tenant,
            execution_ref=run["run_id"],
            mandate_ref=mandate_ref,
            parent_ref=parent_ref,
            step=step,
            state=state,
            policy_version=str(run["policy_version"]),
            lease=lease,
            fencing_token=int(run["lease_seq"]),
            checkpoint_digest=(
                f"sha256:{digest}" if digest and
                not digest.startswith("sha256:") else digest
            ),
            failure_reason=_safe_reason(reason),
            correlation_id=run["correlation_id"],
        )
        _enqueue(tenant, f"{run['run_id']}:{step}:{state}:{uuid.uuid4().hex[:8]}",
                 env, db_path)
    except Exception as exc:  # noqa: BLE001 — telemetry must never gate execution
        logger.warning("checkpointul de execuție nu a putut fi emis (%s)",
                       type(exc).__name__)


def _emit_receipt(
    tenant: str,
    run: dict[str, Any],
    entry: dict[str, Any],
    *,
    status: str,
    receipt_ref: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Emit a ``receipt`` event — the effect-ledger evidence: intentRef,
    idempotencyKey, payloadDigest, provider status, receiptRef."""
    from openexecutive.bo.execution import serialize

    try:
        # Callers may hold the pre-finalization snapshot. Emit the persisted
        # verdict/time, and never publish an old verdict after reconciliation.
        entry = store.get_ledger_entry(tenant, entry["entry_id"], db_path=db_path)
        expected_states = {
            "CONFIRMED": (store.LED_SUCCEEDED,),
            "FAILED": (store.LED_FAILED,),
            "UNKNOWN": (store.LED_UNKNOWN, store.LED_RECONCILIATION),
        }
        if entry["status"] not in expected_states.get(status, ()):
            return
        mandate = store.get_mandate(
            tenant, run["mandate_id"], db_path=db_path
        )
        env = serialize.build_receipt_event(
            tenant,
            execution_ref=run["run_id"],
            mandate_ref=getattr(mandate, "guardian_ref", None)
            or run["mandate_id"],
            intent_ref=entry["intent_ref"],
            idempotency_key=entry["idempotency_key"],
            payload_digest=(
                f"sha256:{entry['payload_digest']}"
                if not entry["payload_digest"].startswith("sha256:")
                else entry["payload_digest"]
            ),
            provider=entry["provider"],
            status=status,
            receipt_ref=receipt_ref,
            effect_kind=f"{entry['action']}",
            attempt=max(1, int(entry["attempts"])),
            occurred_at=(
                entry.get("finalized_at") or entry.get("submitted_at")
                or datetime.now(UTC).isoformat(
                    timespec="milliseconds"
                ).replace("+00:00", "Z")
            ),
            correlation_id=run["correlation_id"],
        )
        _enqueue(tenant, f"{entry['entry_id']}:receipt:{uuid.uuid4().hex[:8]}",
                 env, db_path)
    except Exception as exc:  # noqa: BLE001 — telemetry must never gate execution
        logger.warning("receiptul de execuție nu a putut fi emis (%s)",
                       type(exc).__name__)
