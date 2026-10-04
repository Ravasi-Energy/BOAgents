"""SQLite persistence for BoBot definitions, versions, runs and step runs.

Versioning model (BO-BOT-002):
- ``bo_bot_definitions.draft_version`` is the CAS counter for draft edits —
  every PATCH carries ``expected_version`` and a stale writer gets 409.
- ``active_version_no`` points at the published, immutable
  ``bo_bot_versions`` row. Editing a draft never moves the active version;
  publishing creates a new version row and flips the pointer atomically.
- Simulation runs snapshot ``version_hash`` + ``config_version`` so a run is
  reproducible forever — same input + same version ⇒ same ``plan_hash``.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.bo.db import get_conn


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class NotFoundError(KeyError):
    pass


class ConflictError(Exception):
    """CAS / state conflict → HTTP 409."""


class StateError(Exception):
    """Illegal state transition (e.g. editing a published version) → 409."""


def initialize_db(db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS bo_bot_definitions (
                id TEXT PRIMARY KEY,
                tenant TEXT NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                kind TEXT NOT NULL,
                owner TEXT NOT NULL,
                scope TEXT NOT NULL DEFAULT 'tenant',
                status TEXT NOT NULL DEFAULT 'draft',
                draft_version INTEGER NOT NULL DEFAULT 1,
                active_version_no INTEGER,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bo_bot_def_tenant
                ON bo_bot_definitions(tenant, status);

            CREATE TABLE IF NOT EXISTS bo_bot_versions (
                id TEXT PRIMARY KEY,
                definition_id TEXT NOT NULL REFERENCES bo_bot_definitions(id)
                    ON DELETE CASCADE,
                version_no INTEGER NOT NULL,
                schema_version TEXT NOT NULL,
                hash TEXT NOT NULL,
                content_json TEXT NOT NULL,
                status TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(definition_id, version_no)
            );
            CREATE INDEX IF NOT EXISTS idx_bo_bot_ver_def
                ON bo_bot_versions(definition_id);

            CREATE TABLE IF NOT EXISTS bo_bot_runs (
                id TEXT PRIMARY KEY,
                tenant TEXT NOT NULL,
                definition_id TEXT NOT NULL,
                version_id TEXT NOT NULL,
                version_no INTEGER NOT NULL,
                version_hash TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'simulation',
                status TEXT NOT NULL,
                input_json TEXT NOT NULL,
                config_version INTEGER NOT NULL,
                config_snapshot_json TEXT NOT NULL,
                plan_hash TEXT NOT NULL,
                result_json TEXT,
                error TEXT,
                created_by TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bo_bot_runs_def
                ON bo_bot_runs(definition_id, started_at DESC);
            CREATE INDEX IF NOT EXISTS idx_bo_bot_runs_tenant_kind
                ON bo_bot_runs(tenant, kind, started_at);

            CREATE TABLE IF NOT EXISTS bo_bot_step_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES bo_bot_runs(id) ON DELETE CASCADE,
                idx INTEGER NOT NULL,
                step_id TEXT NOT NULL,
                step_type TEXT NOT NULL,
                status TEXT NOT NULL,
                detail_json TEXT,
                receipt_json TEXT,
                ts TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_bo_bot_step_runs_run
                ON bo_bot_step_runs(run_id, idx);

            -- Draft-content snapshots keyed by the definitions' CAS counter.
            -- update_draft's three-way merge reads the snapshot at
            -- expected_version−1 as the writer's base so a stale full
            -- document cannot silently drop a peer's edits (BUGHUNT-02 P0-3).
            CREATE TABLE IF NOT EXISTS bo_bot_draft_history (
                definition_id TEXT NOT NULL,
                draft_version INTEGER NOT NULL,
                content_json TEXT NOT NULL,
                PRIMARY KEY (definition_id, draft_version)
            );
            """
        )


# --------------------------------------------------------------------- #
# Definitions
# --------------------------------------------------------------------- #

def _definition_from_row(row: Any) -> dict[str, Any]:
    return {
        "id": row["id"],
        "tenant": row["tenant"],
        "name": row["name"],
        "description": row["description"],
        "kind": row["kind"],
        "owner": row["owner"],
        "scope": row["scope"],
        "status": row["status"],
        "draft_version": row["draft_version"],
        "active_version_no": row["active_version_no"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def create_definition(
    tenant: str, *, name: str, description: str, kind: str,
    owner: str, draft_content_json: str, content_hash: str,
    schema_version: str, db_path: Path | None = None,
) -> dict[str, Any]:
    """Create a definition + its first draft version (v1, status=draft)."""
    def_id = _new_id("bot")
    ver_id = _new_id("bv")
    now = _now()
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT INTO bo_bot_definitions
                (id, tenant, name, description, kind, owner, scope, status,
                 draft_version, active_version_no, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, 'tenant', 'draft', 1, NULL, ?, ?, ?)
            """,
            (def_id, tenant, name, description, kind, owner, owner, now, now),
        )
        conn.execute(
            """
            INSERT INTO bo_bot_versions
                (id, definition_id, version_no, schema_version, hash,
                 content_json, status, created_by, created_at)
            VALUES (?, ?, 1, ?, ?, ?, 'draft', ?, ?)
            """,
            (ver_id, def_id, schema_version, content_hash, draft_content_json,
             owner, now),
        )
        conn.execute(
            "INSERT OR IGNORE INTO bo_bot_draft_history "
            "(definition_id, draft_version, content_json) VALUES (?, ?, ?)",
            (def_id, 1, draft_content_json),
        )
    return get_definition(tenant, def_id, db_path=db_path)


def get_definition(tenant: str, def_id: str, db_path: Path | None = None) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bo_bot_definitions WHERE id = ? AND tenant = ?",
            (def_id, tenant),
        ).fetchone()
    if row is None:
        raise NotFoundError(def_id)
    return _definition_from_row(row)


def list_definitions(tenant: str, db_path: Path | None = None) -> list[dict[str, Any]]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM bo_bot_definitions WHERE tenant = ? "
            "ORDER BY updated_at DESC",
            (tenant,),
        ).fetchall()
    return [_definition_from_row(r) for r in rows]


def update_draft(
    tenant: str, def_id: str, *, expected_version: int, actor: str,
    name: str | None = None, description: str | None = None,
    draft_content_json: str | None = None, content_hash: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Edit the draft under CAS. Never touches the active (published) version."""
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT draft_version FROM bo_bot_definitions WHERE id = ? AND tenant = ?",
            (def_id, tenant),
        ).fetchone()
        if row is None:
            raise NotFoundError(def_id)
        current = int(row["draft_version"])
        if expected_version != current:
            raise ConflictError(
                f"expected_version={expected_version} dar versiunea curentă este {current}"
            )
        now = _now()
        if name is not None or description is not None:
            conn.execute(
                "UPDATE bo_bot_definitions SET "
                "name = COALESCE(?, name), description = COALESCE(?, description) "
                "WHERE id = ? AND tenant = ?",
                (name, description, def_id, tenant),
            )
        if draft_content_json is not None:
            draft_row = conn.execute(
                "SELECT id FROM bo_bot_versions "
                "WHERE definition_id = ? AND status = 'draft' "
                "ORDER BY version_no DESC LIMIT 1",
                (def_id,),
            ).fetchone()
            if draft_row is None:
                raise StateError("nu există ciornă editabilă")
            conn.execute(
                "UPDATE bo_bot_versions SET content_json = ?, hash = ? WHERE id = ?",
                (draft_content_json, content_hash, draft_row["id"]),
            )
        conn.execute(
            "UPDATE bo_bot_definitions SET draft_version = ?, updated_at = ? "
            "WHERE id = ? AND tenant = ?",
            (current + 1, now, def_id, tenant),
        )
        if draft_content_json is not None:
            conn.execute(
                "INSERT OR IGNORE INTO bo_bot_draft_history "
                "(definition_id, draft_version, content_json) VALUES (?, ?, ?)",
                (def_id, current + 1, draft_content_json),
            )
    return get_definition(tenant, def_id, db_path=db_path)


def get_draft_history(
    tenant: str, def_id: str, draft_version: int, db_path: Path | None = None
) -> dict[str, Any] | None:
    """The draft content snapshot recorded at ``draft_version`` — the
    three-way merge's writer-base (``expected_version−1``)."""
    get_definition(tenant, def_id, db_path=db_path)
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT content_json FROM bo_bot_draft_history "
            "WHERE definition_id = ? AND draft_version = ?",
            (def_id, draft_version),
        ).fetchone()
    return None if row is None else json.loads(row["content_json"])


def publish(tenant: str, def_id: str, *, actor: str,
            expected_version: int | None = None,
            db_path: Path | None = None) -> dict[str, Any]:
    """Publish the current draft: version becomes immutable + active.

    ``expected_version`` (BUGHUNT-02 C3) pins the ``draft_version`` the
    caller reviewed — publishing a draft that was edited after review is
    a ConflictError, so a reviewer can't unknowingly publish unreviewed
    content."""
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if expected_version is not None:
            row = conn.execute(
                "SELECT draft_version FROM bo_bot_definitions "
                "WHERE id = ? AND tenant = ?",
                (def_id, tenant),
            ).fetchone()
            if row is None:
                raise NotFoundError(def_id)
            current = int(row["draft_version"])
            if expected_version != current:
                raise ConflictError(
                    f"expected_version={expected_version} dar versiunea curentă este {current}"
                )
        draft_row = conn.execute(
            "SELECT id, version_no FROM bo_bot_versions "
            "WHERE definition_id = ? AND status = 'draft' "
            "ORDER BY version_no DESC LIMIT 1",
            (def_id,),
        ).fetchone()
        if draft_row is None:
            raise StateError("nu există ciornă de publicat")
        conn.execute(
            "UPDATE bo_bot_versions SET status = 'published' WHERE id = ?",
            (draft_row["id"],),
        )
        conn.execute(
            "UPDATE bo_bot_definitions SET status = 'active', "
            "active_version_no = ?, updated_at = ? WHERE id = ? AND tenant = ?",
            (draft_row["version_no"], _now(), def_id, tenant),
        )
        # Open the next draft as a copy of what was just published, so editing
        # never mutates a published row.
        published = conn.execute(
            "SELECT * FROM bo_bot_versions WHERE id = ?", (draft_row["id"],)
        ).fetchone()
        conn.execute(
            """
            INSERT INTO bo_bot_versions
                (id, definition_id, version_no, schema_version, hash,
                 content_json, status, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (_new_id("bv"), def_id, int(published["version_no"]) + 1,
             published["schema_version"], published["hash"],
             published["content_json"], actor, _now()),
        )
    return get_definition(tenant, def_id, db_path=db_path)


def list_versions(tenant: str, def_id: str, db_path: Path | None = None) -> list[dict[str, Any]]:
    get_definition(tenant, def_id, db_path=db_path)  # tenant check
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, version_no, schema_version, hash, status, created_by, created_at "
            "FROM bo_bot_versions WHERE definition_id = ? ORDER BY version_no",
            (def_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_version(
    tenant: str, def_id: str, version_no: int | None = None, *,
    status: str | None = None, db_path: Path | None = None,
) -> dict[str, Any]:
    """Fetch one version row (default: the draft). Raises NotFoundError."""
    get_definition(tenant, def_id, db_path=db_path)
    clauses = ["definition_id = ?"]
    args: list[Any] = [def_id]
    if version_no is not None:
        clauses.append("version_no = ?")
        args.append(version_no)
    if status is not None:
        clauses.append("status = ?")
        args.append(status)
    with get_conn(db_path) as conn:
        row = conn.execute(
            f"SELECT * FROM bo_bot_versions WHERE {' AND '.join(clauses)} "
            "ORDER BY version_no DESC LIMIT 1",
            args,
        ).fetchone()
    if row is None:
        raise NotFoundError(f"{def_id}@{version_no}/{status}")
    out = dict(row)
    out["content"] = json.loads(out.pop("content_json"))
    return out


# --------------------------------------------------------------------- #
# Runs
# --------------------------------------------------------------------- #

def insert_run(
    tenant: str, *, definition_id: str, version_id: str, version_no: int,
    version_hash: str, kind: str, status: str, input_json: str,
    config_version: int, config_snapshot_json: str, plan_hash: str,
    result_json: str | None, error: str | None, created_by: str,
    run_id: str | None = None, db_path: Path | None = None,
) -> str:
    run_id = run_id or _new_id("run")
    now = _now()
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO bo_bot_runs
                (id, tenant, definition_id, version_id, version_no, version_hash,
                 kind, status, input_json, config_version, config_snapshot_json,
                 plan_hash, result_json, error, created_by, started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, tenant, definition_id, version_id, version_no, version_hash,
             kind, status, input_json, config_version, config_snapshot_json,
             plan_hash, result_json, error, created_by, now, now),
        )
    return run_id


def insert_step_runs(
    run_id: str, steps: list[dict[str, Any]], db_path: Path | None = None
) -> None:
    now = _now()
    with get_conn(db_path) as conn:
        for i, step in enumerate(steps):
            conn.execute(
                """
                INSERT INTO bo_bot_step_runs
                    (run_id, idx, step_id, step_type, status, detail_json, receipt_json, ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id, i, step["step_id"], step["step_type"], step["status"],
                    json.dumps(step.get("detail")) if step.get("detail") is not None else None,
                    json.dumps(step.get("receipt")) if step.get("receipt") is not None else None,
                    now,
                ),
            )


def get_run(tenant: str, run_id: str, db_path: Path | None = None) -> dict[str, Any]:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM bo_bot_runs WHERE id = ? AND tenant = ?",
            (run_id, tenant),
        ).fetchone()
        if row is None:
            raise NotFoundError(run_id)
        steps = conn.execute(
            "SELECT idx, step_id, step_type, status, detail_json, receipt_json, ts "
            "FROM bo_bot_step_runs WHERE run_id = ? ORDER BY idx",
            (run_id,),
        ).fetchall()
    out = dict(row)
    for k in ("input_json", "config_snapshot_json", "result_json"):
        out[k.replace("_json", "")] = (
            json.loads(out[k]) if out.get(k) else None
        )
        out.pop(k, None)
    out["timeline"] = [
        {
            **{k: s[k] for k in ("idx", "step_id", "step_type", "status", "ts")},
            "detail": json.loads(s["detail_json"]) if s["detail_json"] else None,
            "receipt": json.loads(s["receipt_json"]) if s["receipt_json"] else None,
        }
        for s in steps
    ]
    return out


def list_runs(tenant: str, def_id: str, db_path: Path | None = None) -> list[dict[str, Any]]:
    get_definition(tenant, def_id, db_path=db_path)
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT id, version_no, version_hash, kind, status, plan_hash, "
            "config_version, created_by, started_at, finished_at, error "
            "FROM bo_bot_runs WHERE tenant = ? AND definition_id = ? "
            "ORDER BY started_at DESC LIMIT 200",
            (tenant, def_id),
        ).fetchall()
    return [dict(r) for r in rows]


def sweep_simulation_runs(tenant: str, retention_days: int,
                          db_path: Path | None = None) -> int:
    """Delete simulation runs (and their step rows) older than the retention.

    Only ``kind='simulation'`` rows are touched — the audit log lives in a
    different database and is never in scope for this sweep (BO-SET-001).
    """
    cutoff = (datetime.now(UTC) - timedelta(days=retention_days)).isoformat()
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "DELETE FROM bo_bot_runs WHERE tenant = ? AND kind = 'simulation' "
            "AND started_at < ?",
            (tenant, cutoff),
        )
        return cur.rowcount if cur.rowcount is not None else 0
