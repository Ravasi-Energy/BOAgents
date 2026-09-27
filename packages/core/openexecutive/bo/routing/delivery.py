"""Bounded, administrable delivery for bo.model-observation.v1 (REM-01).

The observe hook NEVER touches the network: it persists the observation and
its complete envelope (stable ``eventId`` + identity) in ``bo_telemetry_outbox``
and returns. Everything past that point lives here:

* ``deliver_pending`` — claims a bounded batch under a SQLite lease
  (``BEGIN IMMEDIATE``), re-sends each persisted envelope unchanged through
  the telemetry adapter, and records RECEIVED/DUPLICATE as delivered. A lost
  ACK is just a failed send: the same envelope retries, and the receiver
  deduplicates by ``eventId``.
* ``ensure_worker`` — one optional daemon thread per process. Each tenant
  with pending envelopes runs on its OWN ``bo.router.delivery_interval_s``
  schedule, re-read on every wake (``0`` excludes the tenant from automatic
  delivery — manual ``/bo/routing/flush`` always works — and a slower
  tenant's interval never delays a faster one). Bounded by
  ``delivery_batch_size``; envelopes past ``delivery_max_attempts`` are marked
  dead — visible, never silently dropped.

There is no in-flight fire-and-forget: either the row is leased to a worker
that will resolve it, or it stays pending for the next cycle/flush.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from openexecutive.bo.routing import store

logger = logging.getLogger(__name__)

#: Receiver acknowledgements that mean "the receiver holds this event".
_ACK_OK = {"RECEIVED", "DUPLICATE", "ACCEPTED", "OK"}


def _setting(tenant: str, key: str, default: int, db_path: Path | None) -> int:
    try:
        from openexecutive.bo.settings import store as settings_store

        return int(
            settings_store.get_effective_value(tenant, key, db_path=db_path)
        )
    except Exception:  # noqa: BLE001 — delivery must survive settings hiccups
        return default


def deliver_pending(
    tenant: str,
    *,
    db_path: Path | None = None,
    worker_id: str | None = None,
    adapter: Any | None = None,
    kinds: set[str] | None = None,
) -> dict[str, int]:
    """One bounded delivery cycle for a tenant.

    Claims up to ``delivery_batch_size`` envelopes under a lease, sends each
    persisted envelope byte-identically, and resolves each outcome. Envelopes
    that reached ``delivery_max_attempts`` are marked dead (delivered=2) —
    surfaced in status/UI, never retried forever.
    """
    from openexecutive.bo.telemetry.adapter import (
        CredentialUnavailableError,
        TelemetryDisabledError,
        get_adapter,
    )

    adapter = adapter or get_adapter()
    worker_id = worker_id or f"wrk_{uuid.uuid4().hex[:12]}"
    batch_size = _setting(
        tenant, "bo.router.delivery_batch_size", 50, db_path
    )
    max_attempts = _setting(
        tenant, "bo.router.delivery_max_attempts", 25, db_path
    )
    # The lease outlives the worst-case send (transport timeout) so a slow
    # receiver can't produce a double-send from a second worker mid-flight.
    lease_s = max(60, _setting(tenant, "bo.router.delivery_interval_s", 30,
                               db_path) * 4)

    sent = failed = dead = 0
    claimed = store.claim_outbox(
        tenant, worker_id=worker_id, limit=batch_size, lease_s=lease_s,
        kinds=kinds, db_path=db_path,
    )
    for row in claimed:
        if not row["dest_bound"]:
            # Legacy row without a recorded destination association: never
            # guess — the envelope stays pending until an explicit, audited
            # rebind (/execution/outbox/rebind) assigns one. The claim
            # filter lets an unbound row be claimed exactly once, so this
            # refusal is recorded without burning the attempt cap.
            store.resolve_outbox(
                tenant, row["event_id"], kind=row["kind"],
                ref_id=row["ref_id"],
                error="plic legacy fără destinație asociată — "
                      "reautorizare prin rebind",
                db_path=db_path,
            )
            failed += 1
            continue
        if row["series_attempts"] > max_attempts:
            store.resolve_outbox(
                tenant, row["event_id"], kind=row["kind"],
                ref_id=row["ref_id"], error="attempt cap reached",
                dead=True, db_path=db_path,
            )
            dead += 1
            continue
        # Bound rows always carry their recorded (endpoint, secret_ref).
        # A recorded endpoint sends strictly to that destination; the
        # ("", "") sink binding follows the current transport only while
        # it is still a sink — a later http destination refuses until an
        # explicit rebind.
        destination = (
            (row["dest_endpoint"] or "", row["dest_ref"] or "")
        )
        try:
            ack: dict[str, Any] | None
            if row["kind"] == "execution":
                ack = _deliver_execution(tenant, row, db_path, destination)
            elif row["kind"] in ("service", "pilot-telemetry"):
                from openexecutive.bo.pilot.delivery import deliver
                ack = deliver(tenant, row["envelope"], db_path,
                              destination=destination)
            else:
                ack = adapter.deliver_event(
                    row["envelope"], tenant=tenant, db_path=db_path,
                    destination=destination,
                )
        except CredentialUnavailableError as exc:
            # Bound destination whose recorded credential ref cannot be
            # resolved — the refusal reason stays visible, envelope pending.
            store.resolve_outbox(
                tenant, row["event_id"], kind=row["kind"],
                ref_id=row["ref_id"], error=str(exc)[:200],
                db_path=db_path,
            )
            failed += 1
        except TelemetryDisabledError:
            store.resolve_outbox(
                tenant, row["event_id"], kind=row["kind"],
                ref_id=row["ref_id"], error="telemetry disabled",
                db_path=db_path,
            )
            failed += 1
        except _ExecutionDeadLetter as exc:
            # Permanent refusal — retrying identical bytes can never
            # succeed; the conflict stays inspectable in the outbox.
            store.resolve_outbox(
                tenant, row["event_id"], kind=row["kind"],
                ref_id=row["ref_id"], error=str(exc)[:200],
                dead=True, db_path=db_path,
            )
            dead += 1
        except Exception as exc:  # noqa: BLE001 — receiver down / ACK lost
            store.resolve_outbox(
                tenant, row["event_id"], kind=row["kind"],
                ref_id=row["ref_id"], error=str(exc)[:200],
                db_path=db_path,
            )
            failed += 1
        else:
            status = (ack or {}).get("status")
            if status is not None and status not in _ACK_OK:
                store.resolve_outbox(
                    tenant, row["event_id"], kind=row["kind"],
                    ref_id=row["ref_id"],
                    error=f"ack neașteptat: {status}"[:200],
                    db_path=db_path,
                )
                failed += 1
                continue
            store.resolve_outbox(
                tenant, row["event_id"], kind=row["kind"],
                ref_id=row["ref_id"], error=None, db_path=db_path,
            )
            sent += 1
    return {
        "sent": sent, "failed": failed, "dead": dead,
        "claimed": len(claimed),
    }


class _ExecutionDeadLetter(Exception):
    """Internal: an execution envelope that must dead-letter now."""


def _deliver_execution(
    tenant: str, row: dict[str, Any], db_path: Path | None,
    destination: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Route one ``kind="execution"`` envelope to Guardian's real
    ``/v1/execution-events`` receiver — by schema, not by kind alone.

    * Provisional ``bo.execution-control.v1`` envelopes (pre-REM-01) can
      never be accepted by the contract receiver → dead-letter visibly,
      with the reason pointing at re-emission from the ledger.
    * ``GuardianPermanentError`` → dead-letter (401/403/409/413/422 —
      authorization/schema/conflict: identical retries always fail).
    * ``GuardianTransientError`` propagates → stays pending.
    """
    from openexecutive.bo.execution import guardian

    envelope = row["envelope"]
    if envelope.get("schemaVersion") != "bo.execution-control.event.v1":
        raise _ExecutionDeadLetter(
            f"plic provizoriu "
            f"{envelope.get('schemaVersion') or 'necunoscut'} — "
            f"re-emis din ledger, nu retransmis"
        )
    try:
        return guardian.post_execution_event(
            tenant, envelope, db_path=db_path, destination=destination
        )
    except guardian.GuardianPermanentError as exc:
        raise _ExecutionDeadLetter(str(exc)) from exc


# --------------------------------------------------------------------------- #
# Background worker — optional, bounded, one per process
# --------------------------------------------------------------------------- #

_worker_lock = threading.Lock()
_worker_stop = threading.Event()
_worker_thread: threading.Thread | None = None

# Upper bound for one scheduling wake — a tenant's live setting change is
# picked up at the next wake even when nothing was due sooner.
_RECHECK_S = 30.0


def _adapter_enabled(adapter: Any, tenant: str, db_path: Path | None) -> bool:
    """Effective enabled flag for a tenant: the administered setting wins
    when a resolver exists, else the adapter's bootstrap value. A resolver
    failure keeps the bootstrap value — delivery survives settings hiccups."""
    try:
        resolve = getattr(adapter, "resolve", None)
        if resolve is not None:
            return bool(resolve(tenant, db_path=db_path).enabled)
        return bool(adapter.enabled)
    except Exception:  # noqa: BLE001 — never kill the worker over a settings read
        return bool(getattr(adapter, "enabled", False))


def _worker_cycle(
    now: float,
    due_at: dict[str, float],
    adapter: Any,
    db_path: Path | None,
) -> float:
    """One scheduling pass over tenants that hold pending envelopes.

    ``bo.router.delivery_interval_s`` is re-read per tenant on every pass:
    ``0`` excludes the tenant from automatic delivery entirely (manual flush
    stays the only drain for it), a shortened interval reschedules the tenant
    sooner, and a longer one applies after the current due time — each
    tenant's cadence is its own, never a shared maximum. Returns the seconds
    until the soonest due tenant (bounded by ``_RECHECK_S``)."""
    wait = _RECHECK_S
    tenants = store.outbox_tenants(db_path=db_path)
    for stale in set(due_at) - set(tenants):
        due_at.pop(stale)  # drained tenants leave no schedule behind
    for tenant in tenants:
        interval = _setting(
            tenant, "bo.router.delivery_interval_s", 30, db_path
        )
        if interval <= 0:
            # Manual-only tenant: drop any pending schedule so a later
            # re-enable starts a fresh countdown instead of firing at once.
            due_at.pop(tenant, None)
            continue
        if _adapter_enabled(adapter, tenant, db_path):
            kinds: set[str] | None = None
        elif store.has_pending_kind(tenant, "execution", db_path):
            # Telemetry administered off must not stall the Guardian
            # authority channel — drain only the execution envelopes.
            kinds = {"execution"}
        else:
            due_at.pop(tenant, None)
            continue
        target = min(due_at.get(tenant, now + interval), now + interval)
        if target <= now:
            deliver_pending(
                tenant, db_path=db_path, adapter=adapter, kinds=kinds
            )
            due_at[tenant] = time.monotonic() + interval
        else:
            due_at[tenant] = target
        # The tenant's next due time bounds the wait whether it was just
        # rescheduled after a send or still pending — a fast tenant's
        # cadence is never stretched to the recheck bound. The remaining
        # time subtracts a FRESH clock so batch processing time doesn't
        # silently stretch the interval.
        wait = min(wait, max(0.0, due_at[tenant] - time.monotonic()))
    return wait


def _worker_loop(db_path: Path | None) -> None:
    from openexecutive.bo.telemetry.adapter import get_adapter

    due_at: dict[str, float] = {}
    while not _worker_stop.is_set():
        wait = _RECHECK_S
        try:
            wait = _worker_cycle(
                time.monotonic(), due_at, get_adapter(), db_path
            )
        except Exception as exc:  # noqa: BLE001 — a broken cycle never kills the worker
            logger.warning("ciclul de livrare a eșuat (%s)", type(exc).__name__)
        _worker_stop.wait(wait)


def ensure_worker(db_path: Path | None = None) -> bool:
    """Start the singleton delivery thread when at least one tenant with
    pending envelopes opted in via ``bo.router.delivery_interval_s``
    (``0`` = manual flush only, for that tenant only). Cheap and I/O-free —
    safe to call from the observe hook."""
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return True
        pending = store.outbox_tenants(db_path=db_path)
        if not pending:
            return False
        # The worker exists while ANY pending tenant wants automatic
        # delivery; tenants at interval 0 are excluded inside the cycle, not
        # by refusing to start the thread for everyone.
        if not any(
            _setting(t, "bo.router.delivery_interval_s", 30, db_path) > 0
            for t in pending
        ):
            return False
        _worker_stop.clear()
        _worker_thread = threading.Thread(
            target=_worker_loop,
            args=(db_path,),
            name="bo-telemetry-delivery",
            daemon=True,
        )
        _worker_thread.start()
        return True


def stop_worker() -> None:
    """Stop the background worker (tests, shutdown)."""
    global _worker_thread
    with _worker_lock:
        _worker_stop.set()
        thread = _worker_thread
        _worker_thread = None
    if thread is not None:
        thread.join(timeout=5)


__all__ = ["deliver_pending", "ensure_worker", "stop_worker"]
