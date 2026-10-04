"""SQLite persistence for the BOAgents settings slice.

One row per ``(tenant, key)`` carrying the tenant override. The *effective*
value is the row's value when present, else the registry default — the API
always reports both the value and its ``origin`` (``tenant`` | ``default``)
so the UI never confuses "configured" with "factory".

Concurrency: every write carries ``expected_version`` (the version the caller
last read, ``0`` when the key was never overridden). The UPDATE is gated on
the stored version inside a ``BEGIN IMMEDIATE`` transaction, so a stale
writer loses deterministically with ``ConfigConflictError`` → HTTP 409.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from openexecutive.bo.db import get_conn
from openexecutive.bo.settings.registry import (
    REGISTRY,
    SettingValidationError,
    validate_tenant_scope,
)


class UnknownSettingError(KeyError):
    pass


class ConfigConflictError(Exception):
    """expected_version did not match the stored version → HTTP 409."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def initialize_db(db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_settings (
                tenant      TEXT NOT NULL,
                key         TEXT NOT NULL,
                value_json  TEXT NOT NULL,
                version     INTEGER NOT NULL,
                updated_by  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (tenant, key)
            )
            """
        )
        # Per-version history — the three-way merge (json_merge keys) needs
        # the value at expected_version−1 as the writer's base to tell
        # "field the writer edited" apart from "field they never saw"
        # (BUGHUNT-02 P0-1/2). Append-only; one row per accepted write.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_settings_history (
                tenant      TEXT NOT NULL,
                key         TEXT NOT NULL,
                version     INTEGER NOT NULL,
                value_json  TEXT NOT NULL,
                updated_by  TEXT NOT NULL,
                updated_at  TEXT NOT NULL,
                PRIMARY KEY (tenant, key, version)
            )
            """
        )


def _audit_change(tenant: str, key: str, *, actor: str, version: int,
                  old: Any, new: Any) -> None:
    """Audit a settings write. Values are logged only for non-sensitive keys;
    sensitive entries (none in Valul 1) would record a redaction marker."""
    from openexecutive.audit import log_event
    from openexecutive.bo.settings.registry import REGISTRY as _REG

    sensitive = _REG[key].sensitivity != "normal"
    log_event(
        "bo_setting_change",
        f"setare {key} → v{version}",
        actor=actor,
        details={
            "tenant": tenant,
            "key": key,
            "version": version,
            "old": "<redacted>" if sensitive else old,
            "new": "<redacted>" if sensitive else new,
        },
    )


def list_effective(tenant: str, db_path: Path | None = None) -> list[dict[str, Any]]:
    """Every registered key with its effective value, origin and version."""
    with get_conn(db_path) as conn:
        rows = {
            r["key"]: r
            for r in conn.execute(
                "SELECT key, value_json, version, updated_by, updated_at "
                "FROM bo_settings WHERE tenant = ?",
                (tenant,),
            )
        }
    out: list[dict[str, Any]] = []
    for key, spec in REGISTRY.items():
        row = rows.get(key)
        if row is None:
            out.append({
                **spec.to_meta(),
                "value": spec.default,
                "origin": "default",
                "version": 0,
                "updated_by": None,
                "updated_at": None,
            })
        else:
            out.append({
                **spec.to_meta(),
                "value": json.loads(row["value_json"]),
                "origin": "tenant",
                "version": row["version"],
                "updated_by": row["updated_by"],
                "updated_at": row["updated_at"],
            })
    return out


def get_effective_value(tenant: str, key: str, db_path: Path | None = None) -> Any:
    """Runtime read: the tenant override if present, else the default."""
    spec = REGISTRY.get(key)
    if spec is None:
        raise UnknownSettingError(key)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT value_json FROM bo_settings WHERE tenant = ? AND key = ?",
            (tenant, key),
        ).fetchone()
    return spec.default if row is None else json.loads(row["value_json"])


def ui_timezone(tenant: str, db_path: Path | None = None) -> ZoneInfo:
    """The tenant's display timezone (``bo.ui.timezone``) as a ``ZoneInfo``.

    Falls back to the registry default when the settings DB is unreadable,
    and to UTC only if even that names an unknown IANA zone. Callers that
    interpret naive wall-clock input (scheduler, cadence) use this so user
    times never silently get pinned to UTC (BUGHUNT-02 C9).
    """
    try:
        name = get_effective_value(tenant, "bo.ui.timezone", db_path=db_path)
    except Exception:
        name = REGISTRY["bo.ui.timezone"].default
    try:
        return ZoneInfo(str(name or "UTC"))
    except ZoneInfoNotFoundError:
        return ZoneInfo("UTC")


def get_stored_value(
    tenant: str, key: str, db_path: Path | None = None
) -> tuple[Any, bool]:
    """The tenant override when one exists — ``(value, True)`` — else
    ``(None, False)``. Callers that must distinguish „administered" from
    „registry default" (bootstrap/env fallbacks) use this instead of
    ``get_effective_value``, which cannot tell them apart."""
    if key not in REGISTRY:
        raise UnknownSettingError(key)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT value_json FROM bo_settings WHERE tenant = ? AND key = ?",
            (tenant, key),
        ).fetchone()
    return (json.loads(row["value_json"]), True) if row is not None else (None, False)


def config_version(tenant: str, db_path: Path | None = None) -> int:
    """Max stored version for the tenant — the configVersion snapshot tag."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(version), 0) AS v FROM bo_settings WHERE tenant = ?",
            (tenant,),
        ).fetchone()
    return int(row["v"])


_MISSING = object()


def _merge_doc(base: Any, current: Any, incoming: Any) -> Any:
    """Three-way merge ``incoming`` onto ``current`` using ``base`` as the
    version the writer's document was built from (BUGHUNT-02 P0-2).

    For every key in ``incoming``: when it equals ``base`` the writer did not
    touch it → the current value survives, even if another writer changed it
    concurrently. When it differs, it is a deliberate writer edit: dicts
    merge recursively, lists union-add (removal is never implicit — an
    explicit field is required by contract), scalars take the writer's value.
    Keys absent from ``incoming`` are untouched and keep ``current``.
    """
    if isinstance(incoming, dict) and isinstance(current, dict):
        base_map = base if isinstance(base, dict) else {}
        merged = dict(current)
        for k, v in incoming.items():
            bv = base_map.get(k, _MISSING)
            if bv is not _MISSING and v == bv:
                continue
            cv = merged.get(k, _MISSING)
            if isinstance(v, dict) and isinstance(cv, dict):
                merged[k] = _merge_doc(bv if bv is not _MISSING else {}, cv, v)
            elif isinstance(v, list) and isinstance(cv, list):
                merged[k] = _union_list(cv, v)
            else:
                merged[k] = v
        return merged
    if isinstance(incoming, list) and isinstance(current, list):
        return _union_list(current, incoming)
    return incoming


def _union_list(current: list, incoming: list) -> list:
    """current items first, then incoming items not already present —
    order-preserving and deduplicating by equality (dicts included)."""
    out = list(current)
    for item in incoming:
        if item not in out:
            out.append(item)
    return out


def _csv_union(current: str, incoming: str) -> str:
    items = [p for p in current.split(",") if p]
    for part in incoming.split(","):
        p = part.strip()
        if p and p not in items:
            items.append(p)
    return ",".join(items)


def set_value(
    tenant: str,
    key: str,
    value: Any,
    *,
    expected_version: int,
    actor: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Validate + CAS-write one setting. Returns the new effective record.

    Keys whose spec declares ``merge != "replace"`` treat ``value`` as a
    DELTA against the stored document rather than the whole document
    (BUGHUNT-02 P0-1/2): a CSV union-adds items, a JSON doc three-way
    merges fields against the value at ``expected_version−1`` — so a stale
    client writing a full old snapshot at the right version cannot silently
    drop a concurrent field edit. The CAS check itself is unchanged.
    """
    spec = REGISTRY.get(key)
    if spec is None:
        raise UnknownSettingError(key)
    validated = spec.validate(value)  # raises SettingValidationError
    validated = validate_tenant_scope(tenant, key, validated)

    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT value_json, version FROM bo_settings WHERE tenant = ? AND key = ?",
            (tenant, key),
        ).fetchone()
        current_version = 0 if row is None else int(row["version"])
        if expected_version != current_version:
            raise ConfigConflictError(
                f"expected_version={expected_version} dar versiunea curentă este {current_version}"
            )
        current_raw: Any = (
            spec.default if row is None else json.loads(row["value_json"])
        )

        if spec.merge == "csv_union" and isinstance(validated, str):
            merged = _csv_union(
                current_raw if isinstance(current_raw, str) else "", validated
            )
            merged = spec.validate(merged) if merged != validated else merged
        elif spec.merge == "json_merge":
            incoming_text = validated if isinstance(validated, str) else ""
            if not incoming_text.strip():
                # Empty payload is a no-op, never a wipe (P0-2: "" must not
                # erase a populated trust store).
                merged = current_raw
            else:
                incoming_doc = json.loads(incoming_text)
                base_row = conn.execute(
                    "SELECT value_json FROM bo_settings_history "
                    "WHERE tenant = ? AND key = ? AND version = ?",
                    (tenant, key, expected_version - 1),
                ).fetchone()
                base_raw: Any = (
                    None if base_row is None else json.loads(base_row["value_json"])
                )
                # The stored value is itself a JSON *string* holding the doc,
                # so it needs a second parse to become a dict.
                base_doc: Any = (
                    json.loads(base_raw)
                    if isinstance(base_raw, str) and base_raw.strip()
                    else {}
                )
                current_doc = current_raw if isinstance(current_raw, str) and current_raw.strip() else "{}"
                merged_doc = _merge_doc(base_doc, json.loads(current_doc), incoming_doc)
                merged = spec.validate(json.dumps(merged_doc))
        else:
            merged = validated

        new_version = current_version + 1
        conn.execute(
            """
            INSERT INTO bo_settings (tenant, key, value_json, version, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (tenant, key) DO UPDATE SET
                value_json = excluded.value_json,
                version    = excluded.version,
                updated_by = excluded.updated_by,
                updated_at = excluded.updated_at
            """,
            (tenant, key, json.dumps(merged), new_version, actor, _now()),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO bo_settings_history
                (tenant, key, version, value_json, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (tenant, key, new_version, json.dumps(merged), actor, _now()),
        )
    old_value = current_raw
    _audit_change(tenant, key, actor=actor, version=new_version,
                  old=old_value, new=merged)
    return {
        **spec.to_meta(),
        "value": merged,
        "origin": "tenant",
        "version": new_version,
        "updated_by": actor,
    }


__all__ = [
    "ConfigConflictError",
    "SettingValidationError",
    "UnknownSettingError",
    "config_version",
    "get_effective_value",
    "get_stored_value",
    "initialize_db",
    "list_effective",
    "set_value",
]
