"""BOAgents telemetry adapter — injectable, disabled by default.

Contract (BO-TEL-001): the adapter emits ``bo.telemetry.v1`` envelopes through
a pluggable transport. With ``enabled=false`` (the default) every ``emit`` is
dropped before it is even built — zero I/O, zero network. No transport is
ever created implicitly: operators inject one via env config
(``BO_TELEMETRY_*``) or construct the adapter directly in tests.

Identity is stamped at emission time (``tenantRef`` = the authenticated
tenant, ``product`` = "BOAgents", ``producerId``/``installationId`` from
deployment config) — never trusted from caller-supplied payload.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from openexecutive.bo.settings import store as settings_store
from openexecutive.bo.telemetry import schema

logger = logging.getLogger(__name__)

_PRODUCT = "BOAgents"


def opaque_actor_ref(actor: str) -> str:
    """Server-derived opaque ref for actorRef/ownerRef — never the email.

    Deterministic (same actor -> same ref) so findings can be correlated,
    but not reversible and carries no personal data on the wire.
    """
    digest = hashlib.sha256(actor.encode("utf-8")).hexdigest()[:20]
    return f"actor_{digest}"


class TelemetryDisabledError(RuntimeError):
    """Raised by ``deliver_event`` when the adapter is disabled — the
    envelope stays pending in the outbox for a later flush."""


class CredentialUnavailableError(TelemetryDisabledError):
    """The destination is known but its bound credential reference cannot
    be resolved — the envelope stays pending; no other credential is ever
    substituted."""


#: SecretRefs that are always allowed — the bootstrap credentials themselves.
_BUILTIN_REFS = frozenset({"BO_TELEMETRY_TOKEN", "BO_PILOT_OBSERVATION_TOKEN"})
_REF_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _parse_secret_ref_allowlist(
    raw_list: str,
) -> tuple[set[str], dict[str, set[str]]]:
    """Split ``BO_*_SECRET_REFS`` into (bare names, {name: {tenants}}).

    An entry is either ``NAME`` — usable by any tenant — or ``NAME@tenant``
    — usable only by that tenant (repeatable for several tenants). Scoped
    entries let the operator bind a credential to specific tenants: no
    other tenant's settings can name it."""
    bare: set[str] = set()
    scoped: dict[str, set[str]] = {}
    for raw in raw_list.split(","):
        entry = raw.strip()
        if not entry:
            continue
        name, sep, scope = entry.partition("@")
        if not _REF_NAME_RE.match(name):
            continue
        if sep and scope.strip():
            scoped.setdefault(name, set()).add(scope.strip())
        else:
            bare.add(name)
    return bare, scoped


def provisioned_secret_refs(
    tenant: str | None = None,
) -> frozenset[str]:
    """The env-var names an administered ``bo.telemetry.token_ref`` may
    point at. Provisioning is operator-side only: ``BO_TELEMETRY_SECRET_REFS``
    plus the built-in bootstrap references. An admin can never make an
    arbitrary env var readable by naming it — only names the operator
    provisioned, and a ``NAME@other-tenant`` entry is never usable here."""
    bare, scoped = _parse_secret_ref_allowlist(
        os.environ.get("BO_TELEMETRY_SECRET_REFS", "")
    )
    names = set(_BUILTIN_REFS) | bare
    if tenant is None:
        names.update(scoped)
    else:
        names.update(
            n for n, ts in scoped.items() if tenant in ts
        )
    return frozenset(names)


class Transport(Protocol):
    def send(self, event: dict[str, Any]) -> dict[str, Any] | None: ...


class NullTransport:
    """Accepts and discards — used when telemetry is enabled-but-unrouted."""

    def send(self, event: dict[str, Any]) -> dict[str, Any] | None:  # noqa: ARG002
        return None


class BufferedTransport:
    """Keeps the last ``capacity`` events in memory — tests and UI preview."""

    def __init__(self, capacity: int = 200) -> None:
        self.capacity = capacity
        self.events: list[dict[str, Any]] = []

    def send(self, event: dict[str, Any]) -> dict[str, Any] | None:
        self.events.append(event)
        if len(self.events) > self.capacity:
            del self.events[: len(self.events) - self.capacity]
        return {"status": "RECEIVED", "eventId": event.get("eventId")}


class HttpTransport:
    """POST the event as JSON to a Guardian-compatible ingestion endpoint.

    Exists for contract completeness; it is *never* instantiated unless an
    operator explicitly configures ``BO_TELEMETRY_TRANSPORT=http`` together
    with endpoint + token. Tests use BufferedTransport.

    Returns the receiver's parsed JSON acknowledgement (e.g.
    ``{"status": "RECEIVED"|"DUPLICATE", "eventId": …}``) or ``None`` when
    the receiver answered 2xx with an empty/non-JSON body. HTTP and network
    failures raise — the caller decides retry semantics.
    """

    def __init__(self, endpoint: str, token: str, timeout_s: float = 5.0) -> None:
        self.endpoint = endpoint
        self.token = token
        self.timeout_s = timeout_s

    def send(self, event: dict[str, Any]) -> dict[str, Any] | None:
        import urllib.request

        req = urllib.request.Request(
            self.endpoint,
            data=json.dumps(event).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.token}",
            },
            method="POST",
        )
        raw = urllib.request.urlopen(req, timeout=self.timeout_s).read()  # noqa: S310
        try:
            ack = json.loads(raw)
        except ValueError:
            return None
        return ack if isinstance(ack, dict) else None


@dataclass(frozen=True)
class TelemetryConfig:
    """Effective per-tenant telemetry configuration.

    Precedence per key: the tenant row in ``bo_settings`` wins when the admin
    saved one; otherwise the process bootstrap (``BO_TELEMETRY_*`` env or an
    injected adapter) applies; the registry default is the last resort.
    ``source`` records where each effective value came from so the UI can
    distinguish „salvat" from „activ". ``transport`` is the resolved send
    path — ``None`` when http was selected without endpoint+token (rows stay
    pending, never silently dropped and never marked delivered).

    ``credential_ref`` is the SecretRef *sanctioned for the resolved
    endpoint* — empty when no credential may legitimately reach that
    destination (a tenant-overridden endpoint without an administered,
    provisioned ``token_ref`` never inherits the bootstrap credential).
    ``credential_state``: ``configured`` | ``missing`` | ``unprovisioned`` |
    ``endpoint_without_ref`` | ``none``."""

    enabled: bool
    transport_kind: str
    endpoint: str
    token_ref: str
    token_configured: bool
    transport: Transport | None
    source: dict[str, str]
    credential_ref: str
    credential_state: str


_TENANT_OVERRIDES = (
    ("bo.telemetry.enabled", "enabled"),
    ("bo.telemetry.transport", "transport"),
    ("bo.telemetry.endpoint", "endpoint"),
    ("bo.telemetry.token_ref", "token_ref"),
)


class TelemetryAdapter:
    """Builds + validates + routes ``bo.telemetry.v1`` events."""

    def _bootstrap_kind(self) -> str:
        if isinstance(self.transport, BufferedTransport):
            return "buffered"
        if isinstance(self.transport, HttpTransport):
            return "http"
        return "null"

    def _stored(self, tenant: str, key: str, db_path: Path | None) -> Any:
        """Tenant override if one exists; (value, True) / (None, False).
        Settings-store failures degrade to „no override" — telemetry must
        survive a settings hiccup, not gate on it."""
        try:
            return settings_store.get_stored_value(tenant, key, db_path=db_path)
        except Exception:  # noqa: BLE001
            return None, False

    def resolve(
        self, tenant: str | None = None, db_path: Path | None = None
    ) -> TelemetryConfig:
        """The effective configuration for ``tenant`` at call time —
        re-resolved on every emit/delivery, so an administered change applies
        at the next envelope without a process restart."""
        enabled = self.enabled
        kind = self._bootstrap_kind()
        endpoint = getattr(self.transport, "endpoint", "") or os.environ.get(
            "BO_TELEMETRY_ENDPOINT", ""
        ).rstrip("/")
        token_ref = "BO_TELEMETRY_TOKEN"
        source = {
            "enabled": "bootstrap",
            "transport": "bootstrap",
            "endpoint": "bootstrap" if endpoint else "default",
            "token_ref": "default",
        }
        if tenant is not None:
            for key, attr in _TENANT_OVERRIDES:
                value, present = self._stored(tenant, key, db_path)
                if not present:
                    continue
                source[attr] = "tenant"
                if attr == "enabled":
                    enabled = bool(value)
                elif attr == "transport":
                    kind = str(value)
                elif attr == "endpoint":
                    endpoint = str(value)
                elif attr == "token_ref":
                    token_ref = str(value)
        # Credential is bound to the destination AND the tenant: a
        # tenant-administered endpoint requires an administered, provisioned
        # token_ref — the bootstrap credential never follows the envelope to
        # a destination it was not explicitly bound to.
        credential_ref = ""
        credential_state = "none"
        token = ""
        if kind == "http":
            endpoint_administered = source["endpoint"] == "tenant"
            ref_administered = source["token_ref"] == "tenant"
            if endpoint_administered and not ref_administered:
                credential_state = "endpoint_without_ref"
            elif ref_administered and (
                token_ref not in provisioned_secret_refs(tenant)
            ):
                credential_state = "unprovisioned"
            else:
                candidate = token_ref
                token = os.environ.get(candidate, "")
                if not token and not ref_administered:
                    # Bootstrap path keeps its documented fallback only.
                    token = os.environ.get("BO_TELEMETRY_TOKEN", "")
                credential_ref = candidate
                credential_state = "configured" if token else "missing"
        transport: Transport | None
        if kind == "http":
            current = self.transport if isinstance(self.transport, HttpTransport) else None
            if (
                current is not None
                and current.endpoint == endpoint
                and source["token_ref"] == "default"
                and source["endpoint"] != "tenant"
            ):
                transport = current
            else:
                transport = (
                    HttpTransport(endpoint, token)
                    if endpoint and token and credential_state == "configured"
                    else None
                )
        elif kind == "buffered":
            transport = (
                self.transport
                if isinstance(self.transport, BufferedTransport)
                else BufferedTransport()
            )
        else:
            # An opaque/injected transport the admin never overrode stays as
            # the bootstrap behavior.
            transport = self.transport
        return TelemetryConfig(
            enabled=enabled,
            transport_kind=kind,
            endpoint=endpoint,
            token_ref=token_ref,
            token_configured=bool(token),
            transport=transport,
            source=source,
            credential_ref=credential_ref,
            credential_state=credential_state,
        )

    def __init__(
        self,
        *,
        enabled: bool = False,
        transport: Transport | None = None,
        producer_id: str = "boagents",
        installation_id: str = "local-installation",
    ) -> None:
        self.enabled = enabled
        self.transport = transport or NullTransport()
        self.producer_id = producer_id
        self.installation_id = installation_id
        self.emitted = 0
        self.dropped = 0
        self.rejected = 0

    def emit(
        self,
        *,
        tenant: str,
        kind: str,
        data: dict[str, Any],
        agent_ref: str | None = None,
        run_ref: str | None = None,
        correlation_id: str | None = None,
        db_path: Path | None = None,
    ) -> dict[str, Any] | None:
        cfg = self.resolve(tenant, db_path)
        if not cfg.enabled:
            self.dropped += 1
            return None
        try:
            config_version = settings_store.config_version(tenant, db_path=db_path)
        except Exception:  # noqa: BLE001 — telemetry must survive a settings
            config_version = 0   # store hiccup; it never gates the product
        # Contract: configVersion is an opaque non-empty string on the wire;
        # the internal numeric version is serialized explicitly here.
        config_version_str = str(config_version)
        event = {
            "schemaVersion": schema.SCHEMA_VERSION,
            "eventId": f"evt_{uuid.uuid4().hex[:24]}",
            "producerId": self.producer_id,
            "installationId": self.installation_id,
            "tenantRef": tenant,
            "product": _PRODUCT,
            "kind": kind,
            "occurredAt": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "correlationId": correlation_id or f"corr_{uuid.uuid4().hex[:24]}",
            "agentRef": agent_ref,
            "runRef": run_ref,
            "configVersion": config_version_str,
            "data": data,
        }
        try:
            schema.validate_event(event)
        except schema.TelemetrySchemaError:
            self.rejected += 1
            raise
        if cfg.transport is None:
            # http selected without endpoint/token: controlled drop, visible
            # in counters and /telemetry/status — never silent, never throws
            # into the caller's business path.
            self.dropped += 1
            return None
        cfg.transport.send(event)
        self.emitted += 1
        return event

    def validate_incoming(self, event: Any) -> dict[str, Any]:
        """Strictly validate an externally-supplied event (fixture endpoint)."""
        return schema.validate_event(event)

    def build_model_observation(
        self,
        *,
        tenant: str,
        body: dict[str, Any],
        occurred_at: str | None = None,
        event_id: str | None = None,
    ) -> dict[str, Any]:
        """Assemble a complete ``bo.model-observation.v1`` envelope WITHOUT
        sending it (REM-01): the caller persists the returned document and
        hands the exact same bytes to every retry, so a lost ACK retries the
        same ``eventId`` — which is what lets Guardian deduplicate.

        ``event_id`` is only set by the delivery path re-stamping nothing —
        pass ``None`` for a genuinely new emission (new id, new envelope).
        Envelope identity is server-derived; never trusted from ``body``.
        """
        return {
            "schemaVersion": "bo.model-observation.v1",
            "eventId": event_id or f"evt_{uuid.uuid4().hex[:24]}",
            "producerId": self.producer_id,
            "product": _PRODUCT,
            "installationId": self.installation_id,
            "tenantRef": tenant,
            "observedAt": occurred_at
            or datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            **body,
        }

    def deliver_event(
        self,
        event: dict[str, Any],
        *,
        tenant: str | None = None,
        db_path: Path | None = None,
        destination: tuple[str, str] | None = None,
    ) -> dict[str, Any] | None:
        """Send an already-built envelope through the tenant's effective
        transport (administered ``bo.telemetry.*`` rows win over bootstrap).

        ``destination=(endpoint, secret_ref)`` delivers through the binding
        persisted on the outbox row at enqueue time: the envelope goes only
        to its recorded endpoint, and only the recorded env reference may
        provide the credential — missing env is a controlled refusal
        (``CredentialUnavailableError``), never a substitution.

        Returns the receiver ack (``RECEIVED``/``DUPLICATE`` both mean the
        receiver holds the event). Raises ``TelemetryDisabledError`` when the
        adapter is disabled — the caller must keep the envelope pending.
        """
        cfg = self.resolve(tenant or event.get("tenantRef"), db_path)
        if not cfg.enabled:
            self.dropped += 1
            raise TelemetryDisabledError("telemetria este dezactivată")
        transport: Transport | None
        if destination is not None:
            endpoint, ref = destination
            if endpoint:
                # The bound ref must still be provisioned FOR THIS TENANT —
                # a NAME@other scope or a removed allow-list entry revokes
                # the credential even though the env var still exists.
                # ``or ""`` — a missing tenant must never satisfy a scoped
                # entry (scoped names resolve only for their own tenant).
                token = (
                    os.environ.get(ref, "")
                    if ref and ref in provisioned_secret_refs(
                        tenant or event.get("tenantRef") or ""
                    )
                    else ""
                )
                if not token:
                    self.dropped += 1
                    raise CredentialUnavailableError(
                        "credentialul asociat plicului nu este provisionat "
                        "în mediul procesului"
                    )
                transport = HttpTransport(endpoint, token)
            elif cfg.transport_kind == "http" and cfg.endpoint:
                # Bound to "no HTTP destination" at enqueue — a newly
                # configured endpoint must never receive this envelope;
                # only an audited rebind may re-associate it.
                self.dropped += 1
                raise CredentialUnavailableError(
                    "plic legat fără destinație http la persistare — "
                    "reasociere explicită necesară (rebind)"
                )
            else:
                # Bound to the sink at enqueue AND still a sink — deliver
                # through the current buffered/null transport.
                transport = cfg.transport
        else:
            transport = cfg.transport
        if transport is None:
            self.dropped += 1
            raise TelemetryDisabledError(
                "transportul http este incomplet configurat (endpoint/token)"
            )
        ack = transport.send(event)
        self.emitted += 1
        return ack

    def emit_model_observation(
        self,
        *,
        tenant: str,
        body: dict[str, Any],
        occurred_at: str | None = None,
        db_path: Path | None = None,
    ) -> dict[str, Any] | None:
        """Emit a ``bo.model-observation.v1`` document (VAL3-01, A02 contract).

        Same transport + identity stamping as ``emit``: the caller supplies
        only the body members (``models`` and/or ``routing``); envelope fields
        are server-derived. Disabled adapter → dropped before the document is
        even assembled for the wire (``self.dropped`` counts it).

        One-shot convenience for fixture/probe drivers — the product's durable
        path is ``build_model_observation`` + outbox + ``deliver_event``.
        """
        cfg = self.resolve(tenant, db_path)
        if not cfg.enabled:
            self.dropped += 1
            return None
        event = self.build_model_observation(
            tenant=tenant, body=body, occurred_at=occurred_at
        )
        if cfg.transport is None:
            self.dropped += 1
            return None
        cfg.transport.send(event)
        self.emitted += 1
        return event


_adapter: TelemetryAdapter | None = None


def _build_from_env() -> TelemetryAdapter:
    enabled = os.environ.get("BO_TELEMETRY_ENABLED", "").lower() in (
        "1", "true", "yes",
    )
    transport: Transport = NullTransport()
    if enabled:
        kind = os.environ.get("BO_TELEMETRY_TRANSPORT", "buffered")
        if kind == "http":
            endpoint = os.environ.get("BO_TELEMETRY_ENDPOINT", "")
            token = os.environ.get("BO_TELEMETRY_TOKEN", "")
            if not endpoint or not token:
                logger.warning(
                    "BO_TELEMETRY_TRANSPORT=http fără endpoint/token — "
                    "telemetria rămâne dezactivată"
                )
                return TelemetryAdapter(enabled=False)
            transport = HttpTransport(endpoint, token)
        elif kind == "buffered":
            transport = BufferedTransport()
    return TelemetryAdapter(
        enabled=enabled,
        transport=transport,
        producer_id=os.environ.get("BO_TELEMETRY_PRODUCER_ID", "boagents"),
        installation_id=os.environ.get("BO_INSTALLATION_ID", "local-installation"),
    )


def get_adapter() -> TelemetryAdapter:
    global _adapter
    if _adapter is None:
        _adapter = _build_from_env()
    return _adapter


def set_adapter(adapter: TelemetryAdapter | None) -> None:
    """Replace the process adapter — tests inject BufferedTransport here."""
    global _adapter
    _adapter = adapter
