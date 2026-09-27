"""SQLite persistence for the observe-mode router (VAL3-01).

Two tenants of truth, deliberately separate:

* ``bo_model_catalog`` — the administered catalog (authority: BOAgents).
  Every write bumps the tenant's ``catalog_version`` counter and carries a
  per-row CAS ``version``, so concurrent editors lose deterministically.
* ``bo_route_observations`` — one durable row per observed model call,
  written BEFORE the telemetry emit so a lost receiver never loses the
  observation; ``delivered``/``delivery_error`` record the outcome and
  ``flush_undelivered`` retries on demand.
"""
from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.bo.db import get_conn
from openexecutive.bo.routing.catalog import (
    CatalogEntry,
    CatalogValidationError,
    Cost,
    Quality,
    validate_fields,
)


class NotFoundError(KeyError):
    pass


class ConflictError(Exception):
    """expected_version did not match the stored version → HTTP 409."""


class DuplicateEntryError(Exception):
    """Same (provider, model_id, model_version) already cataloged → HTTP 409."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def initialize_db(db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_model_catalog (
                entry_id        TEXT NOT NULL,
                tenant          TEXT NOT NULL,
                provider        TEXT NOT NULL,
                model_id        TEXT NOT NULL,
                model_version   TEXT,
                state           TEXT NOT NULL,
                capabilities    TEXT NOT NULL,
                regions         TEXT NOT NULL,
                cost_json       TEXT NOT NULL,
                quality_json    TEXT,
                purpose         TEXT NOT NULL,
                source          TEXT NOT NULL,
                version         INTEGER NOT NULL,
                updated_by      TEXT NOT NULL,
                updated_at      TEXT NOT NULL,
                PRIMARY KEY (tenant, entry_id),
                UNIQUE (tenant, provider, model_id, model_version)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_catalog_meta (
                tenant      TEXT PRIMARY KEY,
                version     INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_route_observations (
                obs_id           TEXT NOT NULL,
                tenant           TEXT NOT NULL,
                occurred_at      TEXT NOT NULL,
                correlation_id   TEXT NOT NULL,
                task_kind        TEXT NOT NULL,
                actor_ref        TEXT NOT NULL,
                policy_version   TEXT NOT NULL,
                catalog_version  TEXT NOT NULL,
                decision         TEXT NOT NULL,
                met_bar          INTEGER NOT NULL,
                reasons_json     TEXT NOT NULL,
                recommendation   TEXT,
                actual_route     TEXT,
                cost_estimate    TEXT,
                measured         TEXT,
                billed           TEXT,
                detail_json      TEXT NOT NULL,
                event_json       TEXT NOT NULL,
                delivered        INTEGER NOT NULL DEFAULT 0,
                delivery_error   TEXT,
                PRIMARY KEY (tenant, obs_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS bo_route_obs_tenant_time "
            "ON bo_route_observations (tenant, occurred_at)"
        )
        # REM-01: durable outbox — the COMPLETE bo.model-observation.v1
        # envelope (eventId + identity + body) is persisted BEFORE the first
        # send attempt and re-sent byte-identically on every retry, so a
        # lost ACK deduplicates on the receiver side by eventId.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_telemetry_outbox (
                event_id     TEXT NOT NULL,
                tenant       TEXT NOT NULL,
                kind         TEXT NOT NULL,
                ref_id       TEXT,
                envelope     TEXT NOT NULL,
                created_at   TEXT NOT NULL,
                attempts     INTEGER NOT NULL DEFAULT 0,
                delivered    INTEGER NOT NULL DEFAULT 0,
                last_error   TEXT,
                lease_owner  TEXT,
                lease_until  TEXT,
                retry_base   INTEGER NOT NULL DEFAULT 0,
                dest_bound   INTEGER,
                dest_endpoint TEXT,
                dest_ref     TEXT,
                PRIMARY KEY (tenant, event_id)
            )
            """
        )
        columns = {r[1] for r in conn.execute("PRAGMA table_info(bo_telemetry_outbox)")}
        if "retry_base" not in columns:
            conn.execute("ALTER TABLE bo_telemetry_outbox ADD COLUMN retry_base INTEGER NOT NULL DEFAULT 0")
        # PILOT-06: persistent destination binding. Rows carry the endpoint +
        # SecretRef resolved at enqueue time; a settings change never
        # re-routes a persisted envelope. dest_bound NULL = pre-migration
        # legacy row — refused at delivery until an explicit audited rebind.
        if "dest_bound" not in columns:
            conn.execute(
                "ALTER TABLE bo_telemetry_outbox ADD COLUMN dest_bound INTEGER")
            conn.execute(
                "ALTER TABLE bo_telemetry_outbox ADD COLUMN dest_endpoint TEXT")
            conn.execute(
                "ALTER TABLE bo_telemetry_outbox ADD COLUMN dest_ref TEXT")
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_outbox_retries (
            tenant TEXT NOT NULL, event_id TEXT NOT NULL, series INTEGER NOT NULL,
            attempts_before INTEGER NOT NULL, last_error TEXT, reason TEXT NOT NULL,
            actor TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (tenant, event_id, series))""")
        # PILOT-07: durable audit intents. The audit event for a mutation
        # (e.g. rebind) is persisted in the SAME transaction as the route
        # change — a crash between the commit and the journal write can no
        # longer lose the audit. Reconciliation replays pending intents to
        # the central journal with intent_id dedup; ``status`` exposes the
        # real state (pending/failed) instead of claiming audited.
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_audit_intents (
            intent_id    TEXT PRIMARY KEY,
            tenant       TEXT NOT NULL,
            event        TEXT NOT NULL,
            actor        TEXT NOT NULL,
            summary      TEXT NOT NULL,
            details_json TEXT NOT NULL,
            status       TEXT NOT NULL DEFAULT 'pending',
            created_at   TEXT NOT NULL,
            delivered_at TEXT,
            attempts     INTEGER NOT NULL DEFAULT 0,
            last_error   TEXT,
            drain_owner  TEXT,
            drain_until  TEXT
        )""")
        # PILOT-08: drain-lease columns for pre-existing DBs — an atomic
        # claim before emit is the only way two concurrent drains can't
        # both emit the same intent (check-then-write is not unique).
        ai_cols = {r[1] for r in conn.execute(
            "PRAGMA table_info(bo_audit_intents)")}
        if "drain_owner" not in ai_cols:
            conn.execute(
                "ALTER TABLE bo_audit_intents ADD COLUMN drain_owner TEXT")
            conn.execute(
                "ALTER TABLE bo_audit_intents ADD COLUMN drain_until TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS bo_outbox_pending "
            "ON bo_telemetry_outbox (tenant, delivered, lease_until)"
        )
        # Catalog sync coalescing: at most one PENDING models-sync per
        # (tenant, ref_id) — a catalog that changes twice before delivery
        # produces one pending event per sync tag, not a queue buildup.
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS bo_outbox_pending_ref "
            "ON bo_telemetry_outbox (tenant, kind, ref_id) WHERE delivered = 0"
        )
        # Migration: observations recorded before REM-01 lack event_id.
        cols = {
            r["name"]
            for r in conn.execute(
                "PRAGMA table_info(bo_route_observations)"
            ).fetchall()
        }
        if "event_id" not in cols:
            conn.execute(
                "ALTER TABLE bo_route_observations "
                "ADD COLUMN event_id TEXT"
            )


# --------------------------------------------------------------------------- #
# Catalog
# --------------------------------------------------------------------------- #

def _entry_from_row(row: Any) -> CatalogEntry:
    quality_raw = row["quality_json"]
    return CatalogEntry(
        entry_id=row["entry_id"],
        provider=row["provider"],
        model_id=row["model_id"],
        model_version=row["model_version"],
        state=row["state"],
        capabilities=tuple(json.loads(row["capabilities"])),
        regions=tuple(json.loads(row["regions"])),
        cost=Cost.from_dict(json.loads(row["cost_json"])),
        quality=Quality.from_dict(json.loads(quality_raw))
        if quality_raw is not None
        else None,
        purpose=row["purpose"],
        source=row["source"],
        version=int(row["version"]),
        updated_by=row["updated_by"],
        updated_at=row["updated_at"],
    )


def catalog_version(tenant: str, db_path: Path | None = None) -> int:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT version FROM bo_catalog_meta WHERE tenant = ?", (tenant,)
        ).fetchone()
    return 0 if row is None else int(row["version"])


def _bump_catalog(conn: Any, tenant: str) -> int:
    row = conn.execute(
        "SELECT version FROM bo_catalog_meta WHERE tenant = ?", (tenant,)
    ).fetchone()
    new_version = 1 if row is None else int(row["version"]) + 1
    conn.execute(
        "INSERT INTO bo_catalog_meta (tenant, version) VALUES (?, ?) "
        "ON CONFLICT (tenant) DO UPDATE SET version = excluded.version",
        (tenant, new_version),
    )
    return new_version


def list_catalog(tenant: str, db_path: Path | None = None) -> list[CatalogEntry]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM bo_model_catalog WHERE tenant = ? "
            "ORDER BY provider, model_id, model_version",
            (tenant,),
        ).fetchall()
    return [_entry_from_row(r) for r in rows]


def get_entry(
    tenant: str, entry_id: str, db_path: Path | None = None
) -> CatalogEntry:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bo_model_catalog WHERE tenant = ? AND entry_id = ?",
            (tenant, entry_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(entry_id)
    return _entry_from_row(row)


def _audit_catalog(
    tenant: str, action: str, entry: CatalogEntry, *, actor: str
) -> None:
    from openexecutive.audit import log_event

    log_event(
        "bo_catalog_change",
        f"catalog {action}: {entry.provider}/{entry.model_id} v{entry.version}",
        actor=actor,
        details={
            "tenant": tenant,
            "action": action,
            "entry_id": entry.entry_id,
            "provider": entry.provider,
            "model_id": entry.model_id,
            "model_version": entry.model_version,
            "state": entry.state,
            "version": entry.version,
        },
    )


def create_entry(
    tenant: str,
    fields: dict[str, Any],
    *,
    actor: str,
    db_path: Path | None = None,
) -> CatalogEntry:
    v = validate_fields(**fields)
    entry = CatalogEntry(
        entry_id=f"cat_{uuid.uuid4().hex[:16]}",
        version=1,
        updated_by=actor,
        updated_at=_now(),
        **v,
    )
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        clash = conn.execute(
            "SELECT entry_id FROM bo_model_catalog WHERE tenant = ? "
            "AND provider = ? AND model_id = ? AND model_version IS ?",
            (tenant, entry.provider, entry.model_id, entry.model_version),
        ).fetchone()
        if clash is not None:
            raise DuplicateEntryError(
                f"{entry.provider}/{entry.model_id} "
                f"(versiune {entry.model_version or 'necunoscută'}) există deja"
            )
        _bump_catalog(conn, tenant)
        conn.execute(
            """
            INSERT INTO bo_model_catalog (
                entry_id, tenant, provider, model_id, model_version, state,
                capabilities, regions, cost_json, quality_json, purpose,
                source, version, updated_by, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry.entry_id, tenant, entry.provider, entry.model_id,
                entry.model_version, entry.state,
                json.dumps(list(entry.capabilities)),
                json.dumps(list(entry.regions)),
                json.dumps(entry.cost.to_dict()),
                json.dumps(entry.quality.to_dict())
                if entry.quality is not None
                else None,
                entry.purpose, entry.source,
                entry.version, actor, entry.updated_at,
            ),
        )
    _audit_catalog(tenant, "create", entry, actor=actor)
    return entry


def update_entry(
    tenant: str,
    entry_id: str,
    fields: dict[str, Any],
    *,
    expected_version: int,
    actor: str,
    db_path: Path | None = None,
) -> CatalogEntry:
    v = validate_fields(**fields)
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT version FROM bo_model_catalog WHERE tenant = ? AND entry_id = ?",
            (tenant, entry_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(entry_id)
        current_version = int(row["version"])
        if expected_version != current_version:
            raise ConflictError(
                f"expected_version={expected_version} dar versiunea curentă "
                f"este {current_version}"
            )
        clash = conn.execute(
            "SELECT entry_id FROM bo_model_catalog WHERE tenant = ? "
            "AND provider = ? AND model_id = ? AND model_version IS ? "
            "AND entry_id <> ?",
            (tenant, v["provider"], v["model_id"], v["model_version"], entry_id),
        ).fetchone()
        if clash is not None:
            raise DuplicateEntryError(
                f"{v['provider']}/{v['model_id']} există deja pe altă intrare"
            )
        _bump_catalog(conn, tenant)
        conn.execute(
            """
            UPDATE bo_model_catalog SET
                provider = ?, model_id = ?, model_version = ?, state = ?,
                capabilities = ?, regions = ?, cost_json = ?, quality_json = ?,
                purpose = ?, source = ?, version = ?, updated_by = ?,
                updated_at = ?
            WHERE tenant = ? AND entry_id = ? AND version = ?
            """,
            (
                v["provider"], v["model_id"], v["model_version"], v["state"],
                json.dumps(list(v["capabilities"])),
                json.dumps(list(v["regions"])),
                json.dumps(v["cost"].to_dict()),
                json.dumps(v["quality"].to_dict())
                if v["quality"] is not None
                else None,
                v["purpose"], v["source"], current_version + 1,
                actor, _now(), tenant, entry_id, current_version,
            ),
        )
    entry = get_entry(tenant, entry_id, db_path=db_path)
    _audit_catalog(tenant, "update", entry, actor=actor)
    return entry


# --------------------------------------------------------------------------- #
# Observations
# --------------------------------------------------------------------------- #

def _dest_binding(tenant: str, kind: str, db_path: Path | None) -> tuple[str, str]:
    """The (endpoint, secret_ref) an envelope of ``kind`` is bound to.

    Recorded on the row at enqueue time: delivery can only ever reach that
    destination, with a credential resolved strictly from that reference —
    a later settings change does not re-route a persisted envelope, and no
    other credential is ever substituted.

    ``kind="execution"`` binds the Guardian authority channel
    (``bo.exec.*``); every other kind binds the telemetry channel
    (``bo.telemetry.*`` effective resolution). ``("", "")`` means "no HTTP
    destination recorded at enqueue" (disabled/sink transport) — delivery
    then follows the current effective config at send time. Never raises:
    a resolution hiccup binds ``("", "")`` rather than losing the envelope."""
    try:
        if kind == "execution":
            from openexecutive.bo.execution import guardian

            return guardian.binding_for(tenant, db_path)
        from openexecutive.bo.telemetry.adapter import get_adapter

        cfg = get_adapter().resolve(tenant, db_path)
        if cfg.transport_kind == "http" and cfg.endpoint:
            return cfg.endpoint, cfg.credential_ref
        return "", ""
    except Exception:  # noqa: BLE001 — enqueue must survive config hiccups
        return "", ""


def record_observation(
    tenant: str,
    obs: dict[str, Any],
    envelope: dict[str, Any],
    *,
    db_path: Path | None = None,
) -> str:
    """Persist one observation row AND its complete telemetry envelope
    atomically — BEFORE any send attempt (REM-01). The persisted envelope,
    including its ``eventId``, is what every retry re-sends unchanged."""
    obs_id = f"obs_{uuid.uuid4().hex[:20]}"
    dest_endpoint, dest_ref = _dest_binding(tenant, "routing", db_path)
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO bo_route_observations (
                obs_id, tenant, occurred_at, correlation_id, task_kind,
                actor_ref, policy_version, catalog_version, decision,
                met_bar, reasons_json, recommendation, actual_route,
                cost_estimate, measured, billed, detail_json, event_json,
                event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                obs_id, tenant, obs["occurred_at"], obs["correlation_id"],
                obs["task_kind"], obs["actor_ref"], obs["policy_version"],
                obs["catalog_version"], obs["decision"], int(obs["met_bar"]),
                json.dumps(obs["reasons"]),
                json.dumps(obs["recommendation"])
                if obs.get("recommendation") is not None
                else None,
                json.dumps(obs["actual_route"])
                if obs.get("actual_route") is not None
                else None,
                json.dumps(obs["cost_estimate"])
                if obs.get("cost_estimate") is not None
                else None,
                json.dumps(obs["measured"])
                if obs.get("measured") is not None
                else None,
                json.dumps(obs["billed"])
                if obs.get("billed") is not None
                else None,
                json.dumps(obs["detail"]),
                json.dumps(obs["event"]),
                envelope["eventId"],
            ),
        )
        conn.execute(
            """
            INSERT INTO bo_telemetry_outbox (
                event_id, tenant, kind, ref_id, envelope, created_at,
                dest_endpoint, dest_ref, dest_bound
            ) VALUES (?, ?, 'routing', ?, ?, ?, ?, ?, 1)
            """,
            (
                envelope["eventId"], tenant, obs_id,
                json.dumps(envelope), obs["occurred_at"],
                dest_endpoint, dest_ref,
            ),
        )
    return obs_id


# --------------------------------------------------------------------------- #
# Telemetry outbox — durable, leased, retryable delivery queue
# --------------------------------------------------------------------------- #

def enqueue_outbox(
    tenant: str,
    kind: str,
    ref_id: str | None,
    envelope: dict[str, Any],
    *,
    db_path: Path | None = None,
) -> bool:
    """Queue an envelope for delivery — bound to the destination and
    SecretRef effective at enqueue time. Returns False when an identical
    pending (tenant, kind, ref_id) row already exists — catalog syncs for
    the same catalog version coalesce instead of queueing up."""
    dest_endpoint, dest_ref = _dest_binding(tenant, kind, db_path)
    try:
        with get_conn(db_path) as conn:
            conn.execute(
                """
                INSERT INTO bo_telemetry_outbox (
                    event_id, tenant, kind, ref_id, envelope, created_at,
                    dest_endpoint, dest_ref, dest_bound
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    envelope["eventId"], tenant, kind, ref_id,
                    json.dumps(envelope), _now(), dest_endpoint, dest_ref,
                ),
            )
    except sqlite3.IntegrityError:
        return False
    return True


def claim_outbox(
    tenant: str,
    *,
    worker_id: str,
    limit: int,
    lease_s: int,
    kinds: set[str] | None = None,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Atomically lease up to ``limit`` pending envelopes for this worker.

    ``BEGIN IMMEDIATE`` makes the claim race-safe across threads AND
    processes: a second flush sees either rows already leased (skipped) or
    rows whose lease expired (crash between receive and confirm).

    Legacy rows without a destination binding (``dest_bound`` NULL) are
    claimed exactly once — long enough to record the refusal reason —
    then left alone: a permanently-undeliverable row must not burn the
    attempt cap nor starve bound rows ahead of it in the queue."""
    now = _now()
    until = (datetime.now(UTC) + timedelta(seconds=lease_s)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    query = (
        "SELECT event_id, kind, ref_id, envelope, attempts, retry_base,"
        "       dest_endpoint, dest_ref, dest_bound"
        " FROM bo_telemetry_outbox"
        " WHERE tenant = ? AND delivered = 0"
        "   AND (lease_until IS NULL OR lease_until < ?)"
        "   AND (COALESCE(dest_bound, 0) = 1 OR attempts = 0)"
    )
    params: list[Any] = [tenant, now]
    if kinds is not None:
        placeholders = ",".join("?" for _ in kinds)
        query += f" AND kind IN ({placeholders})"
        params += sorted(kinds)
    query += " ORDER BY created_at LIMIT ?"
    params.append(limit)
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(query, params).fetchall()
        for row in rows:
            conn.execute(
                "UPDATE bo_telemetry_outbox SET lease_owner = ?, "
                "lease_until = ?, attempts = attempts + 1 "
                "WHERE tenant = ? AND event_id = ?",
                (worker_id, until, tenant, row["event_id"]),
            )
    return [
        {
            "event_id": r["event_id"],
            "kind": r["kind"],
            "ref_id": r["ref_id"],
            "envelope": json.loads(r["envelope"]),
            "attempts": int(r["attempts"]) + 1,
            "series_attempts": int(r["attempts"]) + 1 - int(r["retry_base"]),
            "dest_endpoint": r["dest_endpoint"],
            "dest_ref": r["dest_ref"],
            "dest_bound": bool(r["dest_bound"]),
        }
        for r in rows
    ]


def resolve_outbox(
    tenant: str,
    event_id: str,
    *,
    kind: str | None = None,
    ref_id: str | None = None,
    error: str | None,
    dead: bool = False,
    db_path: Path | None = None,
) -> None:
    """Record the delivery outcome and release the lease. ``dead`` marks a
    permanently failed envelope (attempt cap reached) — visible, never
    silently dropped. Routing observations mirror the outcome."""
    delivered = 2 if dead else (0 if error else 1)
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE bo_telemetry_outbox SET delivered = ?, last_error = ?, "
            "lease_owner = NULL, lease_until = NULL "
            "WHERE tenant = ? AND event_id = ?",
            (delivered, error, tenant, event_id),
        )
        if kind == "routing" and ref_id is not None:
            conn.execute(
                "UPDATE bo_route_observations SET delivered = ?, "
                "delivery_error = ? WHERE tenant = ? AND obs_id = ?",
                (delivered, error, tenant, ref_id),
            )


def list_outbox(
    tenant: str,
    *,
    delivered: int | None = None,
    kind: str | None = None,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Inspectable outbox view for operators: pending + dead-lettered
    envelopes with their error, attempts and lease. The persisted
    envelope bytes are returned too — the UI shows the exact payload
    that would be re-sent (conflict/debugging surface)."""
    query = (
        "SELECT event_id, kind, ref_id, envelope, created_at, attempts, retry_base, "
        "delivered, last_error, lease_owner, lease_until, "
        "dest_endpoint, dest_ref, dest_bound "
        "FROM bo_telemetry_outbox WHERE tenant = ?"
    )
    params: list[Any] = [tenant]
    if delivered is not None:
        query += " AND delivered = ?"
        params.append(delivered)
    if kind is not None:
        query += " AND kind = ?"
        params.append(kind)
    query += " ORDER BY created_at DESC LIMIT ?"
    params.append(limit)
    with get_conn(db_path) as conn:
        rows = conn.execute(query, params).fetchall()
        histories = {}
        for row in rows:
            histories[row["event_id"]] = [dict(h) for h in conn.execute(
                "SELECT series, attempts_before, last_error, reason, actor, created_at "
                "FROM bo_outbox_retries WHERE tenant = ? AND event_id = ? ORDER BY series",
                (tenant, row["event_id"]),
            ).fetchall()]
    out = []
    for r in rows:
        try:
            envelope = json.loads(r["envelope"])
        except ValueError:
            envelope = {"_unparseable": True}
        out.append({
            "event_id": r["event_id"],
            "kind": r["kind"],
            "ref_id": r["ref_id"],
            "event_type": envelope.get("eventType"),
            "schema_version": envelope.get("schemaVersion"),
            "envelope": envelope,
            "created_at": r["created_at"],
            "attempts": int(r["attempts"]),
            "series_attempts": int(r["attempts"]) - int(r["retry_base"]),
            "retry_history": histories[r["event_id"]],
            "delivered": int(r["delivered"]),
            "last_error": r["last_error"],
            "lease_owner": r["lease_owner"],
            "lease_until": r["lease_until"],
            # Destination binding recorded at enqueue — the SecretRef NAME,
            # never the credential value.
            "dest_endpoint": r["dest_endpoint"],
            "dest_ref": r["dest_ref"],
            "dest_bound": bool(r["dest_bound"]),
        })
    return out


def retry_outbox_entry(
    tenant: str,
    event_id: str,
    *,
    reason: str,
    actor: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Operator-authorized requeue of a DEAD-lettered envelope — the only
    retryable state: pending entries are in-flight, delivered ones are
    done. Byte-identical requeue: the persisted envelope is re-sent
    as-is, so a receiver-side dedup makes a second RECEIVED impossible
    to double-apply. Audited, reason mandatory."""
    if not reason.strip():
        raise ConflictError("motivul reluării este obligatoriu")
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT delivered, attempts, last_error FROM bo_telemetry_outbox "
            "WHERE tenant = ? AND event_id = ?",
            (tenant, event_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(event_id)
        if int(row["delivered"]) != 2:
            raise ConflictError(
                "doar înregistrările din dead-letter pot fi reluate — "
                "cele în așteptare/livrate nu se ating"
            )
        conn.execute(
            "INSERT INTO bo_outbox_retries SELECT ?, ?, COALESCE(MAX(series), 0) + 1, ?, ?, ?, ?, ? "
            "FROM bo_outbox_retries WHERE tenant = ? AND event_id = ?",
            (tenant, event_id, row["attempts"], row["last_error"], reason[:300], actor, _now(), tenant, event_id),
        )
        conn.execute(
            "UPDATE bo_telemetry_outbox SET delivered = 0, retry_base = attempts, "
            "lease_owner = NULL, lease_until = NULL "
            "WHERE tenant = ? AND event_id = ?",
            (tenant, event_id),
        )
    from openexecutive.bo.execution.store import _audit as audit

    audit(tenant, "bo_outbox_retry", {
        "event_id": event_id, "reason": reason[:300],
    }, actor=actor)
    return {"event_id": event_id, "requeued": True}


def outbox_state(
    tenant: str,
    event_ids: list[str],
    *,
    db_path: Path | None = None,
) -> dict[str, int]:
    """delivered flag per event_id for envelopes already persisted —
    a read-only lookup that detects missing rows without touching
    pending, delivered or dead-lettered history."""
    if not event_ids:
        return {}
    placeholders = ",".join("?" for _ in event_ids)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT event_id, delivered FROM bo_telemetry_outbox "
            f"WHERE tenant = ? AND event_id IN ({placeholders})",
            (tenant, *event_ids),
        ).fetchall()
    return {r["event_id"]: int(r["delivered"]) for r in rows}


def rebind_outbox(
    tenant: str,
    *,
    event_ids: list[str] | None = None,
    actor: str,
    reason: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Explicit, audited re-association of undelivered envelopes with the
    tenant's CURRENT effective destination for their kind — the only
    authorized way to move backlog or legacy rows that carry no binding.

    Targets pending and dead-lettered rows (``delivered`` 0/2); already
    delivered envelopes are history and never move — naming one is a
    conflict. Rows held by an active lease (a send may be in flight to
    the recorded binding) are skipped, not touched. Envelope bytes and
    identities are untouched — only ``dest_endpoint``/``dest_ref``/
    ``dest_bound`` are rewritten. ``event_ids`` scopes the operation;
    omitted means every undelivered row of the tenant."""
    if not reason.strip():
        raise ConflictError("motivul reasocierii este obligatoriu")
    now = _now()
    skipped = 0
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if event_ids is None:
            all_rows = conn.execute(
                "SELECT event_id, kind, lease_until FROM "
                "bo_telemetry_outbox WHERE tenant = ? AND delivered <> 1",
                (tenant,),
            ).fetchall()
            rows = []
            for r in all_rows:
                if r["lease_until"] is not None and r["lease_until"] > now:
                    skipped += 1
                else:
                    rows.append({"event_id": r["event_id"],
                                 "kind": r["kind"]})
        else:
            rows = []
            if event_ids:
                placeholders = ",".join("?" for _ in event_ids)
                found_rows = conn.execute(
                    "SELECT event_id, kind, delivered, lease_until "
                    "FROM bo_telemetry_outbox "
                    f"WHERE tenant = ? AND event_id IN ({placeholders})",
                    (tenant, *event_ids),
                ).fetchall()
                found = {r["event_id"] for r in found_rows}
                missing = [e for e in event_ids if e not in found]
                if missing:
                    raise NotFoundError(missing[0])
                if any(int(r["delivered"]) == 1 for r in found_rows):
                    raise ConflictError(
                        "plic deja livrat — istoricul nu se reasociază"
                    )
                for r in found_rows:
                    if r["lease_until"] is not None and r["lease_until"] > now:
                        skipped += 1
                    else:
                        rows.append({"event_id": r["event_id"],
                                     "kind": r["kind"]})
        destinations: dict[str, set[str]] = {}
        for row in rows:
            endpoint, ref = _dest_binding(tenant, row["kind"], db_path)
            conn.execute(
                "UPDATE bo_telemetry_outbox SET dest_endpoint = ?, "
                "dest_ref = ?, dest_bound = 1, lease_owner = NULL, "
                "lease_until = NULL WHERE tenant = ? AND event_id = ?",
                (endpoint, ref, tenant, row["event_id"]),
            )
            destinations.setdefault(row["kind"], set()).add(
                f"{endpoint or 'sink'}|{ref or 'n/a'}")
        # The audit event is an INTENT row in this same transaction — the
        # mutation and its audit commit or roll back together. Delivery to
        # the central journal happens after commit via drain_audit_intents;
        # a crash in between leaves a pending intent, not a silent loss.
        intent_id = _insert_audit_intent(
            conn, tenant, "bo_outbox_rebind", actor,
            f"bo_outbox_rebind: {len(rows)} rebound",
            {
                "rebound": len(rows),
                "skipped_leased": skipped,
                "reason": reason[:300],
                # SecretRef names + endpoints — never credential material.
                "destinations":
                    {k: sorted(v) for k, v in destinations.items()},
                "event_ids":
                    list(event_ids) if event_ids is not None else None,
            },
        )
    drain_audit_intents(db_path=db_path)
    intent_state = next(
        (
            i["status"]
            for i in pending_audit_intents(db_path=db_path)
            if i["intent_id"] == intent_id
        ),
        "delivered",
    )
    return {
        "rebound": len(rows),
        "skipped_leased": skipped,
        "audit": intent_state,
    }


# --------------------------------------------------------------------------- #
# Durable audit intents (PILOT-07) — journal reconcile
# --------------------------------------------------------------------------- #


def _insert_audit_intent(
    conn: sqlite3.Connection,
    tenant: str,
    event: str,
    actor: str,
    summary: str,
    details: dict[str, Any],
) -> str:
    """Persist one audit intent inside the caller's open transaction —
    the mutation and its audit commit atomically on this connection."""
    intent_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO bo_audit_intents (intent_id, tenant, event, actor, "
        "summary, details_json, status, created_at) "
        "VALUES (?,?,?,?,?,?,'pending',?)",
        (intent_id, tenant, event, actor, summary[:300],
         json.dumps(details, default=str), _now()),
    )
    return intent_id


def pending_audit_intents(
    db_path: Path | None = None, *, tenant: str | None = None
) -> list[dict[str, Any]]:
    """Intents not yet confirmed in the central journal."""
    where = "status <> 'delivered'"
    params: list[Any] = []
    if tenant is not None:
        where += " AND tenant = ?"
        params.append(tenant)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT intent_id, tenant, event, actor, summary, "
            "details_json, status, created_at, attempts, last_error, "
            "drain_owner, drain_until "
            f"FROM bo_audit_intents WHERE {where} ORDER BY created_at",
            params,
        ).fetchall()
    return [dict(r) for r in rows]


def audit_intents_status(
    tenant: str, db_path: Path | None = None
) -> dict[str, int]:
    """{pending, failed, claimed, stale_claim} — the real audit-drain state
    for API/UI. ``claimed`` counts pending intents under a live drain lease;
    ``stale_claim`` counts pending intents whose lease expired without a
    result — the visible trace of a crashed/hung emitter."""
    now = _now()
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT status, COUNT(*) AS n FROM bo_audit_intents "
            "WHERE tenant = ? AND status <> 'delivered' GROUP BY status",
            (tenant,),
        ).fetchall()
        claimed = conn.execute(
            "SELECT COUNT(*) AS n FROM bo_audit_intents "
            "WHERE tenant = ? AND status = 'pending' "
            "AND drain_until IS NOT NULL AND drain_until >= ?",
            (tenant, now),
        ).fetchone()["n"]
        stale = conn.execute(
            "SELECT COUNT(*) AS n FROM bo_audit_intents "
            "WHERE tenant = ? AND status = 'pending' "
            "AND drain_until IS NOT NULL AND drain_until < ?",
            (tenant, now),
        ).fetchone()["n"]
    out = {"pending": 0, "failed": 0, "claimed": int(claimed),
           "stale_claim": int(stale)}
    for r in rows:
        out[str(r["status"])] = int(r["n"])
    return out


_DRAIN_LEASE_S = 60  # fallback only — administered via bo.router.audit_drain_lease_s


def _drain_lease_s(tenant: str, db_path: Path | None) -> int:
    """Operator-tunable claim lease for audit drain — bounds how long a
    crashed emitter blocks reconciliation (a stale claim is re-claimable
    after expiry; the journal dedup keeps that safe)."""
    try:
        from openexecutive.bo.settings import store as settings_store

        return int(settings_store.get_effective_value(
            tenant, "bo.router.audit_drain_lease_s", db_path=db_path))
    except Exception:
        return _DRAIN_LEASE_S


def _drain_max_attempts(tenant: str, db_path: Path | None) -> int:
    """Operator-tunable emit-attempt budget before an intent is parked as
    ``failed`` (durable evidence, excluded from auto-drain)."""
    try:
        from openexecutive.bo.settings import store as settings_store

        return int(settings_store.get_effective_value(
            tenant, "bo.router.audit_drain_attempts", db_path=db_path))
    except Exception:
        return 3


def _claim_audit_intent(
    conn: sqlite3.Connection, intent_id: str, owner: str, now: str,
    lease_s: int,
) -> str | None:
    """Atomically lease the intent for this drainer — the UPDATE succeeds
    only if nobody else holds a live claim. Returns the lease generation
    (the ``drain_until`` token this claim wrote) or ``None`` when the claim
    was lost. The generation fences every later write: a stale owner whose
    claim was re-taken can no longer touch the row."""
    until = (datetime.now(UTC) + timedelta(seconds=lease_s)
             ).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    cur = conn.execute(
        "UPDATE bo_audit_intents SET drain_owner = ?, drain_until = ? "
        "WHERE intent_id = ? AND status = 'pending' "
        "AND (drain_until IS NULL OR drain_until < ?)",
        (owner, until, intent_id, now),
    )
    return until if cur.rowcount == 1 else None


def drain_audit_intents(
    db_path: Path | None = None, *, tenant: str | None = None,
    owner: str | None = None,
) -> dict[str, Any]:
    """Replay persisted audit intents into the central journal.

    Exactly-once has two independent layers, and NEITHER alone is enough:
    the atomic claim lease (``drain_owner``/``drain_until``) makes exactly
    one process the emitter inside any live-claim window, while the
    journal-side ``dedup_key`` (``audit_dedup`` marker committed in the same
    transaction as the ``audit_log`` row) is the hard guarantee when a claim
    expired mid-emit and a new owner raced the old one. Every status write
    is fenced on the claim generation, so a stale owner that wakes late
    cannot overwrite a newer owner's state.

    A crash after emit leaves a stale claim; once it expires a later drain
    re-claims, the dedup marker suppresses re-emit, and only the mark
    ``delivered`` lands — no second journal row. A journal read error is not
    proof of absence: on read failure the emit is still attempted and the
    dedup constraint, not the failed read, decides uniqueness.

    ``failed`` (attempts ≥ ``bo.router.audit_drain_attempts``) is parked —
    durable evidence, excluded from auto-drain so a dead journal never
    loops; ``requeue_failed_audit_intents`` is the explicit operator path.
    Lease length comes from ``bo.router.audit_drain_lease_s``.
    """
    from openexecutive.audit import log_event
    from openexecutive.audit.logger import get_audit_logger

    me = owner or f"drain-{uuid.uuid4()}"
    now = _now()
    where = "status = 'pending'"
    params: list[Any] = []
    if tenant is not None:
        where += " AND tenant = ?"
        params.append(tenant)
    with get_conn(db_path) as conn:
        candidates = [
            dict(r) for r in conn.execute(
                "SELECT intent_id, tenant, event, actor, summary, "
                "details_json FROM bo_audit_intents "
                f"WHERE {where} ORDER BY created_at", params)
        ]
    delivered = failed = skipped = preempted = 0
    for intent in candidates:
        iid = intent["intent_id"]
        lease_s = _drain_lease_s(intent["tenant"], db_path)
        with get_conn(db_path) as conn:
            gen = _claim_audit_intent(conn, iid, me, now, lease_s)
        if gen is None:
            skipped += 1
            continue
        try:
            # The logger resolves/opens its DB lazily — an unreachable
            # journal must fail the intent, not the whole drain.
            journal = get_audit_logger()
            # A failing read (fresh file without schema, corrupt) must not
            # skip the emit — a failed read is not proof of absence; the
            # dedup constraint inside log() is what keeps it unique.
            try:
                emitted = journal.has_detail("intent_id", iid)
            except Exception:  # noqa: BLE001
                emitted = False
            if not emitted:
                # The journal may have been restored as an empty/new file —
                # ``log`` only INSERTs, so the drain must ensure the schema.
                journal.initialize_db()
                details = json.loads(intent["details_json"])
                # Emit through the module seam (tests patch log_event) with
                # the intent_id as the journal-level dedup key — then
                # CONFIRM by reading the journal back; log_event swallows
                # its own failures, so the row's presence is the truth.
                log_event(
                    intent["event"], intent["summary"],
                    actor=intent["actor"],
                    details={**details, "intent_id": iid},
                    dedup_key=iid,
                )
                emitted = journal.has_detail("intent_id", iid)
        except Exception:  # noqa: BLE001 — journal unreachable/corrupt
            emitted = False
        budget = _drain_max_attempts(intent["tenant"], db_path)
        with get_conn(db_path) as conn:
            if emitted:
                # Fenced on (owner, generation): a stale owner whose claim
                # was re-taken must not touch the new owner's state.
                cur = conn.execute(
                    "UPDATE bo_audit_intents SET status='delivered', "
                    "delivered_at=?, drain_owner=NULL, drain_until=NULL "
                    "WHERE intent_id=? AND drain_owner=? AND drain_until=?",
                    (_now(), iid, me, gen),
                )
                if cur.rowcount == 1:
                    delivered += 1
                else:
                    preempted += 1
            else:
                # Release the claim immediately on failure — the next drain
                # may retry right away; the attempt budget parks it. The
                # same fence applies: only the live owner may bump attempts.
                cur = conn.execute(
                    "UPDATE bo_audit_intents SET attempts=attempts+1, "
                    "last_error='journal unreachable', "
                    "drain_owner=NULL, drain_until=NULL, "
                    "status = CASE WHEN attempts+1 >= ? THEN 'failed' "
                    "ELSE 'pending' END WHERE intent_id=? "
                    "AND drain_owner=? AND drain_until=?",
                    (budget, iid, me, gen),
                )
                if cur.rowcount == 1:
                    failed += 1
                else:
                    preempted += 1
    return {"delivered": delivered, "failed": failed,
            "skipped": skipped, "preempted": preempted}


def requeue_failed_audit_intents(
    tenant: str, *, actor: str, db_path: Path | None = None
) -> dict[str, Any]:
    """Explicit operator recovery: parked ``failed`` intents go back to
    ``pending`` so the next drain retries them against a restored journal.
    Audited like the mutation it is — the intent is recorded in the same
    transaction, then drained."""
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "UPDATE bo_audit_intents SET status='pending', "
            "drain_owner=NULL, drain_until=NULL "
            "WHERE tenant = ? AND status = 'failed'",
            (tenant,),
        )
        count = cur.rowcount
        if count:
            _insert_audit_intent(
                conn, tenant, "bo_audit_intents_requeue", actor,
                f"bo_audit_intents_requeue: {count} relansate",
                {"requeued": count},
            )
    drain_audit_intents(db_path=db_path, tenant=tenant)
    return {"requeued": count}


def outbox_stats(tenant: str, db_path: Path | None = None) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN delivered = 0 THEN 1 ELSE 0 END) AS pending, "
            "SUM(CASE WHEN delivered = 2 THEN 1 ELSE 0 END) AS dead, "
            "SUM(CASE WHEN delivered <> 1 AND "
            "         COALESCE(dest_bound, 0) = 0 THEN 1 ELSE 0 END)"
            " AS unbound, "
            "SUM(attempts) AS attempts "
            "FROM bo_telemetry_outbox WHERE tenant = ?",
            (tenant,),
        ).fetchone()
        last = conn.execute(
            "SELECT last_error FROM bo_telemetry_outbox WHERE tenant = ? "
            "AND last_error IS NOT NULL ORDER BY created_at DESC LIMIT 1",
            (tenant,),
        ).fetchone()
    audit = audit_intents_status(tenant, db_path)
    return {
        "outbox_total": int(row["total"] or 0),
        "outbox_pending": int(row["pending"] or 0),
        "outbox_dead": int(row["dead"] or 0),
        "outbox_unbound": int(row["unbound"] or 0),
        "outbox_attempts": int(row["attempts"] or 0),
        "outbox_last_error": last["last_error"] if last else None,
        "audit_pending": audit["pending"],
        "audit_failed": audit["failed"],
        "audit_claimed": audit["claimed"],
        "audit_stale_claim": audit["stale_claim"],
    }


def outbox_tenants(db_path: Path | None = None) -> list[str]:
    """Tenants with pending outbox rows — what the delivery worker drains."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT tenant FROM bo_telemetry_outbox "
            "WHERE delivered = 0"
        ).fetchall()
    return [r["tenant"] for r in rows]


def has_pending_kind(
    tenant: str, kind: str, db_path: Path | None = None
) -> bool:
    """True when the tenant holds at least one pending row of ``kind`` —
    lets the worker drain Guardian-authority envelopes even while the
    telemetry channel is administered off."""
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM bo_telemetry_outbox "
            "WHERE tenant = ? AND kind = ? AND delivered = 0 LIMIT 1",
            (tenant, kind),
        ).fetchone()
    return row is not None


def _obs_from_row(row: Any) -> dict[str, Any]:
    return {
        "obs_id": row["obs_id"],
        "event_id": row["event_id"],
        "occurred_at": row["occurred_at"],
        "correlation_id": row["correlation_id"],
        "task_kind": row["task_kind"],
        "actor_ref": row["actor_ref"],
        "policy_version": row["policy_version"],
        "catalog_version": row["catalog_version"],
        "decision": row["decision"],
        "met_bar": bool(row["met_bar"]),
        "reasons": json.loads(row["reasons_json"]),
        "recommendation": json.loads(row["recommendation"])
        if row["recommendation"] is not None
        else None,
        "actual_route": json.loads(row["actual_route"])
        if row["actual_route"] is not None
        else None,
        "cost_estimate": json.loads(row["cost_estimate"])
        if row["cost_estimate"] is not None
        else None,
        "measured": json.loads(row["measured"])
        if row["measured"] is not None
        else None,
        "billed": json.loads(row["billed"]) if row["billed"] is not None else None,
        "detail": json.loads(row["detail_json"]),
        "event": json.loads(row["event_json"]),
        # 0 = în așteptare, 1 = livrat, 2 = eșuat definitiv (cap de tentative)
        "delivered": int(row["delivered"]),
        "delivery_error": row["delivery_error"],
    }


def list_observations(
    tenant: str,
    *,
    decision: str | None = None,
    task_kind: str | None = None,
    met_bar: bool | None = None,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    sql = "SELECT * FROM bo_route_observations WHERE tenant = ?"
    params: list[Any] = [tenant]
    if decision is not None:
        sql += " AND decision = ?"
        params.append(decision)
    if task_kind is not None:
        sql += " AND task_kind = ?"
        params.append(task_kind)
    if met_bar is not None:
        sql += " AND met_bar = ?"
        params.append(int(met_bar))
    sql += " ORDER BY occurred_at DESC, obs_id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    with get_conn(db_path) as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_obs_from_row(r) for r in rows]


def get_observation(
    tenant: str, obs_id: str, db_path: Path | None = None
) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bo_route_observations WHERE tenant = ? AND obs_id = ?",
            (tenant, obs_id),
        ).fetchone()
    if row is None:
        raise NotFoundError(obs_id)
    return _obs_from_row(row)


def sweep_observations(
    tenant: str, retention_days: int, db_path: Path | None = None
) -> int:
    """Delete observations older than the retention window. Never touches
    audit or other tables."""
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    with get_conn(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM bo_route_observations WHERE tenant = ? "
            "AND occurred_at < ?",
            (tenant, cutoff),
        )
        return cur.rowcount


def observation_stats(
    tenant: str, db_path: Path | None = None
) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS total, "
            "SUM(CASE WHEN delivered = 0 THEN 1 ELSE 0 END) AS pending, "
            "SUM(CASE WHEN delivered = 2 THEN 1 ELSE 0 END) AS dead, "
            "SUM(CASE WHEN met_bar = 1 THEN 1 ELSE 0 END) AS met, "
            "MAX(occurred_at) AS last_at "
            "FROM bo_route_observations WHERE tenant = ?",
            (tenant,),
        ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "pending_delivery": int(row["pending"] or 0),
        "dead_delivery": int(row["dead"] or 0),
        "met_bar": int(row["met"] or 0),
        "last_at": row["last_at"],
    }


__all__ = [
    "CatalogValidationError",
    "ConflictError",
    "DuplicateEntryError",
    "NotFoundError",
    "catalog_version",
    "create_entry",
    "get_entry",
    "get_observation",
    "initialize_db",
    "claim_outbox",
    "enqueue_outbox",
    "list_catalog",
    "list_observations",
    "observation_stats",
    "list_outbox",
    "outbox_state",
    "outbox_stats",
    "outbox_tenants",
    "retry_outbox_entry",
    "rebind_outbox",
    "record_observation",
    "resolve_outbox",
    "sweep_observations",
    "update_entry",
]
