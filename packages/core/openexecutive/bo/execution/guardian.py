"""Guardian linkage for delegated execution (VAL4-01 REM-01).

Two faces of the same credential (``bo.exec.guardian_secret_ref`` env var,
never persisted):

* ``assert_effect_authorized`` — the pre-effect boundary. When
  ``bo.exec.guardian_endpoint`` is set, the CURRENT mandate status is
  re-read from ``GET /v1/mandates/{ref}/status`` immediately before each
  external effect. A cached snapshot would let a revoked mandate keep
  producing effects, so every boundary call hits Guardian live:

  - ``REVOKED``/``EXPIRED``/unknown mandate → ``GuardianDeniedError``
    (the run fails or goes to reconciliation — the effect never runs);
  - Guardian unreachable/5xx/timeout → ``GuardianUnavailableError``
    (the run PAUSES — recoverable, resumable by the operator);
  - ``bo.exec.guardian_auth_required=on`` makes missing binding/credential
    deny instead of warn: an unbound mandate cannot bypass the boundary.

* ``post_execution_event`` — the outbox delivery path for
  ``kind="execution"`` envelopes. Posts the persisted envelope
  byte-identically to ``POST /v1/execution-events``:

  - 202 ``RECEIVED`` / 200 ``DUPLICATE`` → delivered;
  - 401/403/409/413/422 → ``GuardianPermanentError`` (dead-letter —
    retrying the same bytes can never succeed; the conflict stays
    visible in the outbox);
  - network/timeout/5xx + receiver-side ``exec_control_disabled`` /
    ``exec_ingest_disabled`` → ``GuardianTransientError`` (stays
    pending for the next cycle).

No caching layer: the window between authorization and effect is already
minimal; a cache would silently widen it. The residual race (revocation
landing between the check and the provider call) is documented — it is
bounded by the provider call latency, not by a TTL.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ACK_OK = {"RECEIVED", "DUPLICATE", "ACCEPTED", "OK"}

# Receiver-side 4xx/5xx details that mean "module temporarily off" —
# the evidence must NOT dead-letter on a receiver toggle.
_RECEIVER_OFF_DETAILS = frozenset(
    {"exec_control_disabled", "exec_ingest_disabled"}
)


class GuardianUnavailableError(Exception):
    """Guardian could not be reached or answered transiently — retryable."""


class GuardianDeniedError(Exception):
    """Guardian refused the effect — permanent until the mandate changes.

    ``kind``: ``revoked`` | ``expired`` | ``not_found`` | ``forbidden`` |
    ``misconfigured`` | ``unbound`` | ``not_active`` |
    ``invalid_response`` | ``policy_revoked`` | ``policy_missing`` |
    ``outside_policy``."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class GuardianTransientError(Exception):
    """Delivery failed retryably — the envelope stays pending."""


class GuardianPermanentError(Exception):
    """Delivery failed permanently — the envelope dead-letters visibly."""


def _setting(tenant: str, key: str, default: Any, db_path: Path | None) -> Any:
    try:
        from openexecutive.bo.settings import store as settings_store

        return settings_store.get_effective_value(
            tenant, key, db_path=db_path
        )
    except Exception:  # noqa: BLE001 — settings hiccup = safe default
        return default


_GUARDIAN_BUILTIN_REFS = frozenset(
    {"BO_GUARDIAN_TOKEN", "BO_GUARDIAN_POLICY_TOKEN", "BO_TELEMETRY_TOKEN"}
)


def provisioned_secret_refs() -> set[str]:
    """Env-var names an administered Guardian secret ref may point at:
    the built-in references plus the operator allow-list
    ``BO_GUARDIAN_SECRET_REFS`` (comma-separated env names). Administering
    a SecretRef can never reach an arbitrary environment variable."""
    refs = set(_GUARDIAN_BUILTIN_REFS)
    for raw in os.environ.get("BO_GUARDIAN_SECRET_REFS", "").split(","):
        name = raw.strip()
        if name and re.match(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$", name):
            refs.add(name)
    return refs


def binding_for(tenant: str, db_path: Path | None) -> tuple[str, str]:
    """(endpoint, secret_ref) — the Guardian authority destination for
    outbox destination-binding. Same resolution as ``_link_config`` minus
    the token material: the SecretRef NAME is what gets persisted on the
    row, never the secret.

    The recorded ref is the one that would actually supply the token:
    the configured ref when provisioned; on the bootstrap channel (env
    endpoint, no administered override) the ``BO_TELEMETRY_TOKEN``
    fallback ``_link_config`` honors — a bound row never dead-letters on
    a ref that was never going to be used. A tenant-administered endpoint
    NEVER inherits the bootstrap token."""
    try:
        from openexecutive.bo.settings import store as settings_store

        stored_endpoint, _stored = settings_store.get_stored_value(
            tenant, "bo.exec.guardian_endpoint", db_path=db_path
        )
    except Exception:  # noqa: BLE001 — settings hiccup = safe default
        stored_endpoint = None
    endpoint = str(stored_endpoint or "").rstrip("/")
    administered = bool(endpoint)
    env_endpoint = os.environ.get("BO_TELEMETRY_ENDPOINT", "").rstrip("/")
    if not endpoint and env_endpoint:
        endpoint = env_endpoint.split("/v1/")[0]
    secret_ref = str(
        _setting(
            tenant, "bo.exec.guardian_secret_ref",
            "BO_GUARDIAN_TOKEN", db_path,
        )
    )
    if (
        not administered
        and not os.environ.get(secret_ref)
        and os.environ.get("BO_TELEMETRY_TOKEN")
    ):
        secret_ref = "BO_TELEMETRY_TOKEN"
    return endpoint, secret_ref


def _link_config(
    tenant: str, db_path: Path | None
) -> tuple[str, str | None, float, bool]:
    """(endpoint, token-or-None, timeout_s, auth_required).

    ``BO_TELEMETRY_ENDPOINT``/``BO_TELEMETRY_TOKEN`` are honored as the
    deployment-level transport contract (the gate sets them directly);
    the persisted endpoint takes precedence and the environment is its
    fallback. On the bootstrap channel the token falls back to
    BO_TELEMETRY_TOKEN; a tenant-administered endpoint only ever
    receives its explicitly administered ref — never the bootstrap
    credential.
    ``BO_TELEMETRY_ENDPOINT`` is a full URL — the base is
    recovered by stripping ``/v1/...`` for the mandate-status calls."""
    endpoint, secret_ref = binding_for(tenant, db_path)
    # binding_for already reports the effective ref — including the
    # BO_TELEMETRY_TOKEN fallback on the bootstrap channel. A bound or
    # administered endpoint never substitutes another credential.
    token = os.environ.get(secret_ref) or None
    timeout_s = float(
        _setting(tenant, "bo.exec.guardian_timeout_s", 5, db_path)
    )
    required = bool(
        _setting(
            tenant, "bo.exec.guardian_auth_required", False, db_path
        )
    )
    return endpoint, token, timeout_s, required


def _policy_token(tenant: str, db_path: Path | None) -> str | None:
    """Optional credential with ``execpolicy:read`` for the
    effective-rights layer (current Guardian policy). A producer normally
    holds only ``execobs:write`` — the policy credential is provisioned
    separately; without it the layer is skipped (documented limit)."""
    ref = str(
        _setting(
            tenant, "bo.exec.guardian_policy_secret_ref",
            "BO_GUARDIAN_POLICY_TOKEN", db_path,
        )
    )
    return os.environ.get(ref) or None


def _request(
    method: str, url: str, token: str, timeout_s: float,
    body: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    """One HTTP round-trip. Returns (status, parsed-json-or-{}).
    ``urllib.error.HTTPError`` carries the status; everything else
    network-side raises ``GuardianUnavailableError``/``Transient`` via
    the callers' classification."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8") if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method=method,
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
        raw = resp.read()
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = {}
    return resp.status, parsed if isinstance(parsed, dict) else {}


def _error_detail(exc: urllib.error.HTTPError, token: str | None = None) -> str:
    """Receiver-supplied error detail — redacted before it can persist.

    A hostile receiver can reflect the Bearer credential back in the
    error body; the detail that reaches ``last_error``/API/UI never
    carries it."""
    try:
        body = json.loads(exc.read())
        detail = str(body.get("detail") or body)[:200]
    except Exception:  # noqa: BLE001 — non-JSON error body
        return f"HTTP {exc.code}"
    if token and token in detail:
        detail = detail.replace(token, "[redat]")
    return detail


# --------------------------------------------------------------------------- #
# Pre-effect authorization — GET /v1/mandates/{ref}/status
# --------------------------------------------------------------------------- #

def assert_effect_authorized(
    tenant: str, mandate: Any, *,
    step_action: str | None = None,
    step_resource: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Re-check the Guardian-held mandate status NOW, at the effect
    boundary. Returns silently only for standalone (unbound) mandates
    with the link not configured; raises ``GuardianDeniedError``/
    ``GuardianUnavailableError`` otherwise.

    A BOUND mandate (``guardian_ref`` set) can never silently downgrade
    to local-only control: without an endpoint or credential the
    verification is impossible → ``GuardianUnavailableError`` (the run
    PAUSES — recoverable once the operator restores the link). A setting
    change cannot turn a bound execution's authorization optional.

    When a policy credential (``bo.exec.guardian_policy_secret_ref``,
    ``execpolicy:read`` scope) is provisioned, a second layer verifies
    the EFFECTIVE rights against the CURRENT Guardian policy — not just
    the ACTIVE label: a revoked policy denies, and the step's
    action/resource must still sit inside ``allowedActions``/
    ``allowedResources`` (rights reduced after approval are caught).

    Guardian holds the distributed revocation — the local mandate row
    only proves the chain BOAgents issued; a mandate revoked centrally
    must stop effects even while the local row still looks active.
    """
    endpoint, token, timeout_s, required = _link_config(tenant, db_path)
    guardian_ref = getattr(mandate, "guardian_ref", None)
    if not endpoint:
        if guardian_ref or required:
            raise GuardianUnavailableError(
                "endpoint Guardian neconfigurat — mandatul legat nu "
                "poate fi verificat"
            )
        return  # standalone — local chain only (documented mode)
    if not guardian_ref:
        if required:
            raise GuardianDeniedError(
                "unbound",
                "mandatul nu are guardian_ref — autorizarea Guardian e "
                "obligatorie",
            )
        return
    if not token:
        # Bound mandate, missing credential — the check cannot run, so
        # the effect must NOT run. Pause (recoverable), never skip.
        raise GuardianUnavailableError(
            "credențialul Guardian lipsește — verificarea la frontieră "
            "e obligatorie pentru mandate legate"
        )
    installation = os.environ.get("BO_INSTALLATION_ID", "local-installation")
    query = {"product": "BOAgents", "installation": installation}
    if step_action is not None:
        query["action"] = step_action
    if step_resource is not None:
        query["resource"] = step_resource
    url = (f"{endpoint}/v1/mandates/{urllib.parse.quote(guardian_ref, safe='')}/status?"
           + urllib.parse.urlencode(query))
    try:
        status, body = _request("GET", url, token, timeout_s)
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc, token)
        if exc.code >= 500 or detail in _RECEIVER_OFF_DETAILS:
            raise GuardianUnavailableError(
                f"HTTP {exc.code}: {detail}"
            ) from exc
        if exc.code in (401, 403):
            raise GuardianDeniedError("forbidden", detail) from exc
        if exc.code == 404:
            raise GuardianDeniedError(
                "not_found",
                f"mandatul {guardian_ref} nu există în Guardian",
            ) from exc
        raise GuardianDeniedError(
            "forbidden", f"HTTP {exc.code}: {detail}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — DNS/connect/timeout/TLS
        raise GuardianUnavailableError(str(exc)[:200]) from exc
    if "status" not in body:
        raise GuardianDeniedError(
            "invalid_response",
            f"răspuns invalid de la autoritate pentru "
            f"{guardian_ref} (câmpul status lipsește)",
        )
    mandate_status = str(body.get("status") or "")
    if mandate_status == "REVOKED":
        raise GuardianDeniedError(
            "revoked", f"mandatul {guardian_ref} este REVOKED în Guardian"
        )
    expires_raw = body.get("expiresAt")
    expired = mandate_status == "EXPIRED"
    if not expired and expires_raw:
        try:
            expired = datetime.fromisoformat(
                str(expires_raw).replace("Z", "+00:00")
            ) <= datetime.now(UTC)
        except ValueError:
            expired = False  # unparseable timestamp — status field governs
    if expired:
        raise GuardianDeniedError(
            "expired", f"mandatul {guardian_ref} a expirat în Guardian"
        )
    if mandate_status != "ACTIVE":
        raise GuardianDeniedError(
            "not_active",
            f"mandatul {guardian_ref} are starea "
            f"{mandate_status or 'lipsă'} în Guardian",
        )
    for key, expected in (("tenantRef", tenant), ("mandateId", guardian_ref),
                          ("product", "BOAgents"), ("installationId", installation)):
        if body.get(key) != expected:
            raise GuardianDeniedError("authority_mismatch", f"autoritate necorespunzătoare: {key}")
    allowed = body.get("allowed")
    if not isinstance(allowed, dict) or any(
        not isinstance(allowed.get(key), list) or
        any(not isinstance(value, str) for value in allowed[key])
        for key in ("actions", "resources")
    ):
        raise GuardianDeniedError("invalid_response", "drepturile mandatului lipsesc sau sunt invalide")
    if ((step_action is not None and step_action not in allowed["actions"])
            or (step_resource is not None and step_resource not in allowed["resources"])):
        raise GuardianDeniedError("outside_mandate", "efectul nu este autorizat de mandatul Guardian")
    _assert_policy_rights(
        tenant, endpoint, timeout_s,
        step_action=step_action, step_resource=step_resource,
        db_path=db_path,
    )


def _assert_policy_rights(
    tenant: str, endpoint: str, timeout_s: float, *,
    step_action: str | None, step_resource: str | None,
    db_path: Path | None,
) -> None:
    """Second boundary layer: the CURRENT Guardian policy, when an
    ``execpolicy:read`` credential is provisioned. A policy revoked or
    narrowed after the mandate was issued must still stop the effect —
    the ACTIVE label on a mandate does not prove the policy still
    allows it.

    Credential missing → optional direct policy read skipped; the status
    endpoint still checks the effective current policy. Credential refused → the
    rights cannot be verified → pause (unavailable), not proceed."""
    token = _policy_token(tenant, db_path)
    if not token:
        return
    url = f"{endpoint}/v1/exec-policies?tenant={tenant}"
    try:
        status, body = _request("GET", url, token, timeout_s)
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc, token)
        if exc.code >= 500 or detail in _RECEIVER_OFF_DETAILS:
            raise GuardianUnavailableError(
                f"HTTP {exc.code}: {detail}"
            ) from exc
        # Credential provisioned but refused — rights unverifiable.
        raise GuardianUnavailableError(
            f"credențialul de politică a fost refuzat "
            f"(HTTP {exc.code}: {detail}) — drepturile efective nu pot "
            f"fi verificate"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — network/timeout/TLS
        raise GuardianUnavailableError(str(exc)[:200]) from exc
    if not isinstance(body, dict) or "exists" not in body:
        raise GuardianDeniedError(
            "invalid_response",
            "răspuns invalid de la /v1/exec-policies — drepturile "
            "efective nu pot fi stabilite",
        )
    if body.get("revoked"):
        raise GuardianDeniedError(
            "policy_revoked",
            "politica Guardian curentă este revocată — mandatul ACTIVE "
            "nu mai autorizează efecte",
        )
    if not body.get("exists"):
        raise GuardianDeniedError(
            "policy_missing",
            "nu există o politică Guardian curentă pentru tenant",
        )
    policy = body.get("policy") or {}
    allowed_actions = set(policy.get("allowedActions") or [])
    allowed_resources = set(policy.get("allowedResources") or [])
    if step_action is not None and step_action not in allowed_actions:
        raise GuardianDeniedError(
            "outside_policy",
            f"acțiunea {step_action!r} nu e în politica Guardian curentă",
        )
    if step_resource is not None and not any(
        _resource_covered(step_resource, r) for r in allowed_resources
    ):
        raise GuardianDeniedError(
            "outside_policy",
            f"resursa {step_resource!r} nu e în politica Guardian curentă",
        )


def _resource_covered(resource: str, allowed: str) -> bool:
    """Exact match or ``prefix.*`` wildcard — the same convention as the
    local mandate check (`store._resource_allowed`)."""
    if allowed.endswith(".*"):
        return resource == allowed[:-2] or resource.startswith(
            allowed[:-1])
    return resource == allowed


# --------------------------------------------------------------------------- #
# Event delivery — POST /v1/execution-events
# --------------------------------------------------------------------------- #

def post_execution_event(
    tenant: str, envelope: dict[str, Any], *, db_path: Path | None = None,
    destination: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Deliver one persisted execution envelope to Guardian.

    ``destination=(endpoint, secret_ref)`` delivers through the binding
    recorded on the outbox row at enqueue time — the envelope can only go
    to its recorded authority endpoint with the recorded credential
    reference; a missing env value is transient (provisioning may return),
    never substituted.

    The caller (outbox delivery) maps outcomes:
    ``GuardianTransientError`` → stays pending; ``GuardianPermanentError``
    → dead-letter with the receiver's detail; any ack dict → delivered.
    """
    if destination is not None:
        endpoint, secret_ref = destination
        token = os.environ.get(secret_ref) if secret_ref else None
        timeout_s = float(
            _setting(tenant, "bo.exec.guardian_timeout_s", 5, db_path)
        )
        if not endpoint:
            raise GuardianTransientError(
                "destinația asociată plicului lipsește"
            )
        if not token:
            raise GuardianTransientError(
                "credentialul asociat plicului nu este provisionat"
            )
    else:
        endpoint, token, timeout_s, _required = _link_config(tenant, db_path)
        if not endpoint:
            raise GuardianTransientError(
                "bo.exec.guardian_endpoint neconfigurat"
            )
        if not token:
            raise GuardianPermanentError(
                "secretul Guardian nu este setat în mediul procesului"
            )
    # Use the same authority base as status checks. The generic telemetry
    # endpoint may address observations, not the execution-event contract.
    url = f"{endpoint}/v1/execution-events"
    try:
        status, body = _request(
            "POST", url, token, timeout_s, body=envelope,
        )
    except urllib.error.HTTPError as exc:
        detail = _error_detail(exc, token)
        if exc.code >= 500 or exc.code == 429 or (
            exc.code == 404 and detail in _RECEIVER_OFF_DETAILS
        ):
            raise GuardianTransientError(
                f"HTTP {exc.code}: {detail}"
            ) from exc
        raise GuardianPermanentError(
            f"HTTP {exc.code}: {detail}"
        ) from exc
    except Exception as exc:  # noqa: BLE001 — network/timeout/TLS
        raise GuardianTransientError(str(exc)[:200]) from exc
    if status >= 500:
        raise GuardianTransientError(f"HTTP {status}")
    ack_status = str(body.get("status") or "")
    if token and token in ack_status:
        # A hostile receiver can reflect the Bearer it just received —
        # the persisted error never carries it back.
        ack_status = ack_status.replace(token, "[redat]")
    if ack_status not in _ACK_OK:
        raise GuardianPermanentError(
            f"ack neașteptat: {ack_status or f'HTTP {status}'}"
        )
    return body
