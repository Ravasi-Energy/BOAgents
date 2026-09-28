"""Serializer for ``bo.model-observation.v1`` (the shared A02 contract).

Builds the wire document from internal observation/catalog state. The
envelope identity fields (``eventId``, ``producerId``, ``product``,
``installationId``, ``tenantRef``, ``observedAt``) are stamped by the
telemetry adapter at emission time — never taken from caller input — and
body-level ``tenantRef``-like conflicts are refused by the receiver, so the
serializer only fills body fields.

Reason codes are restricted to the contract pattern ``^[A-Z0-9_:-]+$``.
"""
from __future__ import annotations

from typing import Any

from openexecutive.bo.routing.catalog import CatalogEntry

SCHEMA_VERSION = "bo.model-observation.v1"


def route_choice(ref: dict[str, Any] | None) -> dict[str, Any] | None:
    """``routeChoice`` shape: provider + modelId required; version explicit
    (``None`` = unknown version, per contract — never guessed)."""
    if ref is None:
        return None
    return {
        "provider": ref["provider"],
        "modelId": ref["modelId"],
        "modelVersion": ref.get("modelVersion"),
    }


def routing_body(
    *,
    correlation_id: str,
    policy_version: str,
    catalog_version: str,
    task_kind: str,
    recommendation: dict[str, Any] | None,
    actual_route: dict[str, Any] | None,
    met_bar: bool,
    reasons: list[str],
    cost_estimate: dict[str, Any] | None,
    measured: dict[str, Any] | None,
    billed: dict[str, Any] | None,
) -> dict[str, Any]:
    """The ``routing`` member of a bo.model-observation.v1 document."""
    routing: dict[str, Any] = {
        "correlationId": correlation_id,
        "policyVersion": policy_version,
        "catalogVersion": catalog_version,
        "taskKind": task_kind,
        "metBar": met_bar,
        "reasons": list(reasons)[:16],
    }
    if recommendation is not None:
        routing["recommendation"] = route_choice(recommendation)
    if actual_route is not None:
        routing["actualRoute"] = route_choice(actual_route)
    if cost_estimate is not None:
        routing["costEstimate"] = dict(cost_estimate)
    if measured is not None:
        routing["measuredUsage"] = dict(measured)
    if billed is not None:
        routing["billedCost"] = dict(billed)
    return {"routing": routing}


def model_observation(entry: CatalogEntry, *, owner_ref: str, last_seen: str) -> dict[str, Any]:
    """One ``models[]`` element — the inventory face of a catalog entry."""
    doc: dict[str, Any] = {
        "provider": entry.provider,
        "modelId": entry.model_id,
        "modelVersion": entry.model_version,
        "ownerRef": owner_ref,
        "purpose": entry.purpose,
        "source": entry.source,
        "state": entry.state,
        "lastSeen": last_seen,
    }
    if entry.quality is not None:
        doc["quality"] = entry.quality.to_wire()
    return doc


def models_body(
    entries: list[CatalogEntry], *, owner_ref: str, last_seen: str,
    sync_id: str, complete: bool,
) -> dict[str, Any]:
    """The ``models`` member — a catalog snapshot/delta for Guardian."""
    return {
        "sync": {"syncId": sync_id, "complete": complete},
        "models": [
            model_observation(e, owner_ref=owner_ref, last_seen=last_seen)
            for e in entries[:64]
        ],
    }


__all__ = ["SCHEMA_VERSION", "models_body", "routing_body"]
