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
        # Per-version history — append-only audit/snapshot trail (one row per
        # accepted write). It is NEVER read to infer a writer's base: the
        # contract requires the client to declare the version it actually
        # read (expected_version) and the CAS gate enforces it (CONTROL R2).
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


_TRUST_KEYED = {"publishers": "publisherId", "keys": "keyId"}
_TRUST_ID = {"publishers": "publisherId", "keys": "keyId"}


def _merge_trust_doc(current: dict, incoming: dict) -> dict:
    """Sparse delta merge of a trust-store document (BUGHUNT-02 P0-2, R2).

    Every key present in ``incoming`` is a field the writer deliberately
    touched — applied as: dicts merge recursively (omitted sub-fields keep
    their current value), the keyed ``publishers``/``keys`` collections
    upsert per ``publisherId``/``keyId``, other lists union-add, scalars
    take the writer's value (an intentional revert to an older value is a
    legitimate write). ``<field>_remove`` marker keys are ops, not content:
    they are consumed here and never persist into the document — removing
    list entries is possible only through them, at ANY depth (a publisher
    row's ``keyIds_remove`` works the same as a top-level
    ``publishers_remove``). Keys absent from ``incoming`` always preserve
    the current value: an omitted field or collection is never deleted,
    and ``[]`` never clears — INCLUDING inside a keyed row (CONTROL R12:
    nested ``allowedKinds:[]`` must not wipe the stored list).
    """
    merged = dict(current)
    for k, v in incoming.items():
        if k.endswith("_remove"):
            continue
        cv = merged.get(k)
        if k in _TRUST_KEYED and isinstance(v, list) and isinstance(cv, list):
            merged[k] = _merge_keyed(cv, v, _TRUST_KEYED[k])
        elif isinstance(v, dict) and isinstance(cv, dict):
            merged[k] = _merge_trust_doc(cv, v)
        elif isinstance(v, list) and isinstance(cv, list):
            merged[k] = _union_list(cv, v)
        elif v != [] and v != {}:
            merged[k] = v
    # Explicit removals at THIS level: ``<list field>_remove`` drops the
    # named entries. Ids already absent are a no-op (idempotent), never an
    # error — the requested end-state is already reached. Ops fire after
    # the merges above, so a delta that both upserts and removes the same
    # entry resolves deterministically to removed.
    for k, v in incoming.items():
        if not k.endswith("_remove") or not isinstance(v, list) or not v:
            continue
        target = k[: -len("_remove")]
        entries = merged.get(target)
        if not isinstance(entries, list):
            continue
        id_field = _TRUST_ID.get(target)
        if id_field is not None:
            drop = set(v)
            merged[target] = [
                e for e in entries
                if (e.get(id_field) not in drop
                    if isinstance(e, dict) else e not in v)
            ]
        else:
            merged[target] = [e for e in entries if e not in v]
    return merged


def _merge_keyed(current: list, incoming: list, id_field: str) -> list:
    """Upsert ``incoming`` entries into ``current`` keyed by ``id_field`` —
    an existing id merges FIELD-WISE with the same sparse-delta semantics
    as the document (nested lists union-add, ``[]`` preserves, dicts
    recurse, ``<field>_remove`` removes nested entries explicitly); a new
    id appends. Removal is never implicit; only ``*_remove`` ops delete
    entries or nested list items."""
    out = [dict(e) if isinstance(e, dict) else e for e in current]
    idx = {e.get(id_field): i for i, e in enumerate(out) if isinstance(e, dict)}
    for entry in incoming:
        if not isinstance(entry, dict):
            if entry not in out:
                out.append(entry)
            continue
        eid = entry.get(id_field)
        if eid in idx:
            out[idx[eid]] = _merge_trust_doc(out[idx[eid]], entry)
        else:
            idx[eid] = len(out)
            # A brand-new row drops *_remove ops — there is nothing to remove
            # yet — so they are consumed, never persisted into the document.
            out.append({k: v for k, v in entry.items() if not k.endswith("_remove")})
    return out


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
    remove: list[str] | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Validate + CAS-write one setting. Returns the new effective record.

    Keys whose spec declares ``merge != "replace"`` treat ``value`` as a
    DELTA (BUGHUNT-02 P0-1/2, contract CONTROL R2): a CSV union-adds items,
    a JSON doc sparse-merges the fields the writer sent — omitted fields
    preserve, ``[]`` never clears, removal only via explicit ``*_remove``
    ops. ``expected_version`` is the version the writer actually read; the
    strict CAS refuses any stale write with ``ConfigConflictError`` (409)
    before any effect — the client reloads and rebases explicitly. There is
    no base inference anywhere.
    """
    spec = REGISTRY.get(key)
    if spec is None:
        raise UnknownSettingError(key)
    if spec.merge == "json_merge":
        # The payload is a sparse delta, not a full document — full-doc
        # validation runs on the MERGED result below, never on the delta.
        validated = value
    else:
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
            if remove:
                drop = {p.strip() for p in remove}
                merged = ",".join(
                    p for p in merged.split(",") if p and p not in drop
                )
            merged = spec.validate(merged) if merged != validated else merged
        elif remove:
            raise SettingValidationError(
                f"{key}: op-ul `remove` este permis doar pe chei csv_union"
            )
        elif spec.merge == "json_merge":
            incoming_text = validated if isinstance(validated, str) else ""
            if not incoming_text.strip():
                # An empty payload is ambiguous (legacy wipe-shaped write) —
                # refuse it explicitly instead of guessing (CONTROL R2). The
                # stored document is left untouched.
                raise SettingValidationError(
                    f"{key}: document JSON gol refuzat — trimite delta explicit "
                    "(câmpuri prezente = atinse) sau ops *_remove"
                )
            try:
                incoming_doc = json.loads(incoming_text)
            except json.JSONDecodeError as exc:
                raise SettingValidationError(
                    f"{key}: JSON invalid ({exc.msg})"
                ) from exc
            if not isinstance(incoming_doc, dict):
                raise SettingValidationError(
                    f"{key}: document JSON așteptat (obiect), nu {type(incoming_doc).__name__}"
                )
            # The CAS above proves the client's declared version IS the
            # stored one — so the writer's base is the current document
            # itself. No history lookup, no inferred base.
            current_doc = current_raw if isinstance(current_raw, str) and current_raw.strip() else "{}"
            merged_doc = _merge_trust_doc(json.loads(current_doc), incoming_doc)
            merged = validate_tenant_scope(
                tenant, key, spec.validate(json.dumps(merged_doc))
            )
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
