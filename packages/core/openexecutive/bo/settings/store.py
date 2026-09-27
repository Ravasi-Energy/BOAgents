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


def set_value(
    tenant: str,
    key: str,
    value: Any,
    *,
    expected_version: int,
    actor: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Validate + CAS-write one setting. Returns the new effective record."""
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
            (tenant, key, json.dumps(validated), new_version, actor, _now()),
        )
    old_value = spec.default if row is None else json.loads(row["value_json"])
    _audit_change(tenant, key, actor=actor, version=new_version,
                  old=old_value, new=validated)
    return {
        **spec.to_meta(),
        "value": validated,
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
