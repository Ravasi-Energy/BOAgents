"""Serialization for ``bo.execution-control.event.v1`` (VAL4-02 / REM-01).

Wire format owned by A02 — the published common contract lives in
``coordonare/contracte/bo.execution-control.v1/`` (event + mandate
schemas, fixtures, SHA256SUMS). This module emits exactly that shape:

- one envelope = exactly one branch: ``checkpoint`` (execution state —
  NEVER proof of external effect) or ``receipt`` (effect-ledger
  evidence);
- envelope identity (eventId/producerId/installationId/tenantRef) is
  stamped at emission from server config — never from caller input;
- ``observedAt``/``occurredAt`` are explicit-UTC RFC3339 (``Z``);
- evidence carries refs and ``sha256:`` digests only — recursive
  minimization rejects keys outside the contract vocabulary
  (``additionalProperties: false`` semantics) and anything sensitive;
- ``fencingToken`` is monotonically increasing per ``executionRef`` —
  it comes from the run's ``lease_seq``, never synthesized here.

Delivery rides the durable outbox (``kind="execution"``) — persisted
once, resent byte-identically on retry.
"""
from __future__ import annotations

import os
import re
import uuid
from datetime import UTC, datetime
from typing import Any

SCHEMA_VERSION = "bo.execution-control.event.v1"

CHECKPOINT_STATES = frozenset(
    {"PENDING", "CLAIMED", "SUCCEEDED", "FAILED", "UNKNOWN",
     "RECONCILIATION_REQUIRED"}
)
RECEIPT_STATES = frozenset({"ACCEPTED", "CONFIRMED", "FAILED", "UNKNOWN"})

# The contract vocabulary — every key the schema declares, at any depth.
# Anything else on the wire is an additionalProperties violation AND a
# potential leak; rejected recursively below.
_ENVELOPE_KEYS = frozenset({
    "schemaVersion", "eventId", "producerId", "product", "installationId",
    "tenantRef", "observedAt", "correlationId", "eventType",
    "checkpoint", "receipt",
})
_CHECKPOINT_KEYS = frozenset({
    "executionRef", "mandateRef", "parentRef", "step", "state",
    "policyVersion", "lease", "fencingToken", "checkpointDigest", "failureReason",
})
_LEASE_KEYS = frozenset({"ownerRef", "fencingToken", "expiresAt"})
_RECEIPT_KEYS = frozenset({
    "executionRef", "mandateRef", "intentRef", "idempotencyKey",
    "payloadDigest", "provider", "status", "receiptRef", "effectKind",
    "attempt", "occurredAt",
})
_KNOWN_KEYS = (
    _ENVELOPE_KEYS | _CHECKPOINT_KEYS | _LEASE_KEYS | _RECEIPT_KEYS
)

# Sensitive material the receiver scans for — case-folded substrings.
# Schema-declared digest fields (payloadDigest) and fencingToken are in
# _KNOWN_KEYS, so they never reach this check.
_FORBIDDEN_SUBSTRINGS = (
    "prompt", "apikey", "api_key", "secret", "token", "password",
    "credential", "payload", "content", "email", "document",
)

_DIGEST_PREFIX = "sha256:"

# Mirrors definitions/utcTs and checkpoint.failureReason in the schema.
_UTC_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d:[0-5]\d"
    r"(\.\d+)?(Z|\+00:00)$"
)
_FAILURE_REASON_RE = re.compile(r"^[A-Za-z0-9_:. /-]+$")


class ExecutionEventError(ValueError):
    """The event cannot be serialized for the wire."""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _utc_ts(name: str, value: Any) -> None:
    if not isinstance(value, str) or not _UTC_TS_RE.match(value):
        raise ExecutionEventError(f"{name}: timestamp UTC invalid")


def _opaque(name: str, value: Any, *, required: bool = True) -> None:
    if value is None:
        if required:
            raise ExecutionEventError(f"{name}: obligatoriu")
        return
    if not isinstance(value, str) or not (1 <= len(value) <= 128):
        raise ExecutionEventError(f"{name}: ref opac invalid")
    if any(ch.isspace() for ch in value) or "@" in value:
        raise ExecutionEventError(f"{name}: ref opac invalid")


def _digest(name: str, value: Any, *, required: bool = False) -> None:
    if value is None:
        if required:
            raise ExecutionEventError(f"{name}: digest obligatoriu")
        return
    if (
        not isinstance(value, str)
        or not value.startswith(_DIGEST_PREFIX)
        or len(value) != len(_DIGEST_PREFIX) + 64
    ):
        raise ExecutionEventError(f"{name}: format sha256:<64 hex> așteptat")


def _assert_minimized(node: Any, path: str = "") -> None:
    """Recursive minimization — walk every key at every depth. Keys must
    belong to the contract vocabulary; unknown keys are rejected, and an
    unknown key that also looks sensitive is named in the error (never
    its value)."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key not in _KNOWN_KEYS:
                lowered = str(key).lower()
                if any(bad in lowered for bad in _FORBIDDEN_SUBSTRINGS):
                    raise ExecutionEventError(
                        f"câmp sensibil refuzat pe fir: {path}{key}"
                    )
                raise ExecutionEventError(
                    f"câmp în afara contractului pe fir: {path}{key}"
                )
            _assert_minimized(value, f"{path}{key}.")
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _assert_minimized(item, f"{path}{i}.")


def _envelope(
    *,
    tenant: str,
    correlation_id: str,
    producer_id: str,
    installation_id: str,
    observed_at: str | None,
) -> dict[str, Any]:
    _opaque("tenantRef", tenant)
    _opaque("correlationId", correlation_id)
    _opaque("producerId", producer_id)
    _opaque("installationId", installation_id)
    observed = observed_at or _utc_now()
    _utc_ts("observedAt", observed)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "eventId": f"evt_{uuid.uuid4().hex[:24]}",
        "producerId": producer_id,
        "product": "BOAgents",
        "installationId": installation_id,
        "tenantRef": tenant,
        "observedAt": observed,
        "correlationId": correlation_id,
    }


def _identity() -> tuple[str, str]:
    """Producer/installation from server config — same env contract as
    the telemetry adapter; the receiver binds them to the credential."""
    return (
        os.environ.get("BO_TELEMETRY_PRODUCER_ID", "boagents"),
        os.environ.get("BO_INSTALLATION_ID", "local-installation"),
    )


def build_checkpoint_event(
    tenant: str,
    *,
    execution_ref: str,
    mandate_ref: str,
    parent_ref: str | None,
    step: int,
    state: str,
    policy_version: str,
    lease: dict[str, Any] | None = None,
    fencing_token: int | None = None,
    checkpoint_digest: str | None = None,
    failure_reason: str | None = None,
    correlation_id: str,
    observed_at: str | None = None,
    producer_id: str | None = None,
    installation_id: str | None = None,
) -> dict[str, Any]:
    """Checkpoint = execution state, NOT external-effect evidence.
    ``state=CLAIMED`` requires a lease with a monotonically increasing
    ``fencingToken`` (the run's ``lease_seq``)."""
    if state not in CHECKPOINT_STATES:
        raise ExecutionEventError(f"stare checkpoint necunoscută: {state}")
    if state == "CLAIMED" and not lease:
        raise ExecutionEventError("CLAIMED cere lease (fencingToken)")
    _opaque("executionRef", execution_ref)
    _opaque("mandateRef", mandate_ref)
    _opaque("parentRef", parent_ref, required=False)
    _opaque("policyVersion", policy_version)
    if not isinstance(step, int) or not (0 <= step <= 100000):
        raise ExecutionEventError("step: întreg 0..100000")
    _digest("checkpointDigest", checkpoint_digest)
    if failure_reason is not None and not (
        isinstance(failure_reason, str)
        and 1 <= len(failure_reason) <= 256
        and _FAILURE_REASON_RE.match(failure_reason)
    ):
        raise ExecutionEventError("failureReason: 1..256 caractere, set restrâns")
    body: dict[str, Any] = {
        "executionRef": execution_ref,
        "mandateRef": mandate_ref,
        "parentRef": parent_ref,
        "step": step,
        "state": state,
        "policyVersion": policy_version,
    }
    if lease is not None:
        _opaque("lease.ownerRef", lease.get("ownerRef"))
        token = lease.get("fencingToken")
        if not isinstance(token, int) or token < 0:
            raise ExecutionEventError("lease.fencingToken: întreg ≥ 0")
        _utc_ts("lease.expiresAt", lease.get("expiresAt"))
        body["lease"] = {
            "ownerRef": lease["ownerRef"],
            "fencingToken": token,
            "expiresAt": lease["expiresAt"],
        }
    if fencing_token is None and lease is not None:
        fencing_token = lease["fencingToken"]
    if fencing_token is not None:
        if type(fencing_token) is not int or fencing_token < 0:
            raise ExecutionEventError("fencingToken: întreg ≥ 0")
        if lease is not None and fencing_token != lease["fencingToken"]:
            raise ExecutionEventError("epoca checkpointului diferă de lease")
        body["fencingToken"] = fencing_token
    if checkpoint_digest is not None:
        body["checkpointDigest"] = checkpoint_digest
    if failure_reason is not None:
        body["failureReason"] = failure_reason
    pid, iid = _identity()
    env = _envelope(
        tenant=tenant, correlation_id=correlation_id,
        producer_id=producer_id or pid,
        installation_id=installation_id or iid,
        observed_at=observed_at,
    )
    env["eventType"] = "checkpoint"
    env["checkpoint"] = body
    _assert_minimized(env)
    return env


def build_receipt_event(
    tenant: str,
    *,
    execution_ref: str,
    mandate_ref: str | None,
    intent_ref: str,
    idempotency_key: str | None,
    payload_digest: str,
    provider: str,
    status: str,
    occurred_at: str,
    receipt_ref: str | None = None,
    effect_kind: str | None = None,
    attempt: int = 1,
    correlation_id: str,
    observed_at: str | None = None,
    producer_id: str | None = None,
    installation_id: str | None = None,
) -> dict[str, Any]:
    """Receipt = the effect-ledger entry. ``CONFIRMED`` only with a
    provider ``receiptRef``; ``idempotencyKey=None`` signals a provider
    without dedup — exactly-once is NOT promised."""
    if status not in RECEIPT_STATES:
        raise ExecutionEventError(f"stare receipt necunoscută: {status}")
    if status == "CONFIRMED" and not receipt_ref:
        raise ExecutionEventError("CONFIRMED cere receiptRef al providerului")
    _opaque("executionRef", execution_ref)
    _opaque("mandateRef", mandate_ref, required=False)
    _opaque("intentRef", intent_ref)
    _opaque("idempotencyKey", idempotency_key, required=False)
    _opaque("provider", provider)
    _opaque("receiptRef", receipt_ref, required=False)
    _opaque("effectKind", effect_kind, required=False)
    _digest("payloadDigest", payload_digest, required=True)
    _utc_ts("occurredAt", occurred_at)
    if not isinstance(attempt, int) or not (1 <= attempt <= 1000):
        raise ExecutionEventError("attempt: întreg 1..1000")
    body: dict[str, Any] = {
        "executionRef": execution_ref,
        "mandateRef": mandate_ref,
        "intentRef": intent_ref,
        "idempotencyKey": idempotency_key,
        "payloadDigest": payload_digest,
        "provider": provider,
        "status": status,
        "occurredAt": occurred_at,
    }
    if receipt_ref is not None:
        body["receiptRef"] = receipt_ref
    if effect_kind is not None:
        body["effectKind"] = effect_kind
    body["attempt"] = attempt
    pid, iid = _identity()
    env = _envelope(
        tenant=tenant, correlation_id=correlation_id,
        producer_id=producer_id or pid,
        installation_id=installation_id or iid,
        observed_at=observed_at,
    )
    env["eventType"] = "receipt"
    env["receipt"] = body
    _assert_minimized(env)
    return env


def validate_execution_event(event: Any) -> dict[str, Any]:
    """Structural check mirroring the published schema — the full
    JSON-Schema validation against the contract artifact runs in tests
    (jsonschema is dev-only). Rejects non-contract keys recursively."""
    if not isinstance(event, dict):
        raise ExecutionEventError("evenimentul trebuie să fie un obiect")
    if event.get("schemaVersion") != SCHEMA_VERSION:
        raise ExecutionEventError("schemaVersion invalid")
    for key in (
        "eventId", "producerId", "product", "installationId",
        "tenantRef", "observedAt", "correlationId", "eventType",
    ):
        if event.get(key) is None:
            raise ExecutionEventError(f"câmp obligatoriu lipsă: {key}")
    if event.get("product") != "BOAgents":
        raise ExecutionEventError("product invalid")
    has_cp = "checkpoint" in event
    has_rc = "receipt" in event
    if has_cp and has_rc:
        raise ExecutionEventError("un eveniment poartă o singură ramură")
    et = event["eventType"]
    if et == "checkpoint" and not has_cp:
        raise ExecutionEventError("eventType=checkpoint fără ramură")
    if et == "receipt" and not has_rc:
        raise ExecutionEventError("eventType=receipt fără ramură")
    if et not in ("checkpoint", "receipt"):
        raise ExecutionEventError(f"eventType necunoscut: {et}")
    _assert_minimized(event)
    return event
