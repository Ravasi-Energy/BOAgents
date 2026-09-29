"""Observation-only delivery through the existing durable outbox."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.request import ProxyHandler, Request, build_opener

from openexecutive.bo.pilot.provider import NoRedirect
from openexecutive.bo.telemetry.adapter import (
    BufferedTransport,
    CredentialUnavailableError,
    NullTransport,
    TelemetryDisabledError,
    get_adapter,
    provisioned_secret_refs,
)


def _kind_url(endpoint: str, service: bool) -> str:
    """Map the administered telemetry endpoint to the canonical route for
    the envelope's schema — same receiver, contract path per kind:
    /v1/observations for bo.service-observation.v1, /v1/telemetry for
    bo.telemetry.v1."""
    base = endpoint.split("/v1/")[0].rstrip("/")
    return base + ("/v1/observations" if service else "/v1/telemetry")


def _timeout_s(tenant: str, db_path: Path | None) -> float:
    try:
        from openexecutive.bo.settings import store as settings_store

        return float(settings_store.get_effective_value(
            tenant, "bo.exec.guardian_timeout_s", db_path=db_path))
    except Exception:
        return 5.0


def deliver(
    tenant: str,
    envelope: dict[str, Any],
    db_path: Path | None = None,
    *,
    destination: tuple[str, str] | None = None,
) -> dict[str, Any]:
    """Deliver one persisted pilot envelope through the EFFECTIVE telemetry
    configuration — the same source the general adapter resolves:
    ``bo.telemetry.endpoint``/``token_ref`` administered per tenant win over
    the process bootstrap. ``bo.exec.*`` remains the authority channel for
    ``kind="execution"`` envelopes only.

    ``destination=(endpoint, secret_ref)`` sends strictly to the binding
    recorded on the outbox row at enqueue time — retry/DLQ/replay can never
    re-route an envelope or substitute another credential. An empty
    endpoint records "bound to the sink at enqueue": it delivers only if
    the effective transport is still a sink; a newly configured HTTP
    destination refuses until an audited rebind.
    """
    adapter = get_adapter()
    try:
        # Per-tenant administered setting wins over the process bootstrap;
        # a resolver failure keeps the bootstrap flag (delivery must survive
        # a settings hiccup).
        resolve = getattr(adapter, "resolve", None)
        cfg = resolve(tenant, db_path=db_path) if resolve is not None else None
        enabled = cfg.enabled if cfg is not None else adapter.enabled
    except Exception:
        cfg = None
        enabled = getattr(adapter, "enabled", False)
    if not enabled:
        raise TelemetryDisabledError("telemetria este dezactivată")
    service = envelope["schemaVersion"] == "bo.service-observation.v1"
    if destination is not None:
        endpoint, ref = destination
        if endpoint:
            # Same rule as the telemetry adapter bound path: the persisted
            # ref is re-checked against the CURRENT tenant scope — a
            # re-scoped/removed NAME@tenant revokes the credential even
            # though the env var still exists. ``or ""`` — a missing tenant
            # never satisfies a scoped entry.
            token = (
                os.environ.get(ref, "")
                if ref and ref in provisioned_secret_refs(
                    tenant or envelope.get("tenantRef") or ""
                )
                else ""
            )
            if not token:
                raise CredentialUnavailableError(
                    "credentialul asociat plicului nu este provisionat "
                    "în mediul procesului")
            url = _kind_url(endpoint, service)
        else:
            # Bound to "no HTTP destination" at enqueue.
            if (cfg is not None
                    and cfg.transport_kind == "http" and cfg.endpoint):
                raise CredentialUnavailableError(
                    "plic legat fără destinație http la persistare — "
                    "reasociere explicită necesară (rebind)")
            if cfg is None or not isinstance(
                    cfg.transport, BufferedTransport):
                raise CredentialUnavailableError(
                    "destinația asociată plicului nu poate fi rezolvată")
            ack = cfg.transport.send(envelope)
            return ack if isinstance(ack, dict) else {"status": "RECEIVED"}
    else:
        if cfg is None:
            raise CredentialUnavailableError(
                "Configurația telemetriei nu a putut fi rezolvată")
        if isinstance(cfg.transport, BufferedTransport):
            # Bound to a sink at enqueue (or sink is the current config):
            # delivers into the in-process buffer — no wire, no credential.
            ack = cfg.transport.send(envelope)
            return ack if isinstance(ack, dict) else {"status": "RECEIVED"}
        if (cfg.transport_kind != "http" or not cfg.endpoint
                or cfg.transport is None
                or isinstance(cfg.transport, NullTransport)):
            raise CredentialUnavailableError(
                "Endpoint telemetrie neconfigurat")
        if not cfg.credential_ref:
            raise CredentialUnavailableError(
                "Credential telemetrie neasociat destinației administrate")
        token = os.environ.get(cfg.credential_ref, "")
        if not token:
            raise CredentialUnavailableError("Credential telemetrie neprovisionat")
        url = _kind_url(cfg.endpoint, service)
    request = Request(url, data=json.dumps(envelope).encode(),
                      headers={"Content-Type": "application/json",
                               "Authorization": "Bearer " + token})
    try:
        with build_opener(ProxyHandler({}), NoRedirect()).open(
                request, timeout=_timeout_s(tenant, db_path)) as response:
            raw = response.read(65537)
            if len(raw) > 65536:
                raise ValueError
            return json.loads(raw)
    except Exception:
        # Never persist transport exception text that might echo credentials.
        raise ValueError("Livrarea observației sintetice nu a fost confirmată") from None
