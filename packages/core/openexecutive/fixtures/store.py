"""SQLite store for user-generated company fixtures.

One row = one full fixture bundle, serialized into the same on-disk layout
the curated fixtures use (profile.yaml + people.yaml + departments.yaml +
memory.json + docs/*.md). The serialized artifacts are stored verbatim so a
generated fixture can be **materialized to a temp dir** and loaded through the
exact same ``_apply_state_from_source`` path the curated fixtures use — no
duplicate seeding logic.

Summary fields (industry, stage, arr, …) are denormalized at insert time so
the ``/fixtures`` list endpoint is a cheap SELECT and never has to parse YAML.

Lives in the same ``episodic_memory.db`` as the rest of the app state (the
data volume, at ``/data/episodic_memory.db`` in a deployment). Follows the per-module
``_get_conn()`` + idempotent ``initialize_db()`` convention used across the
codebase (see ``memory/episodic.py``).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DB_PATH = Path(os.environ.get("EPISODIC_DB_PATH", "./episodic_memory.db"))


def _resolve_db_path(db_path: Path | None) -> Path:
    """Resolve the DB path at CALL time so tests can monkeypatch ``DB_PATH``.

    Function defaults bind at definition time, which would freeze ``DB_PATH``
    before a test fixture can redirect it — mirror the people/departments
    stores' ``db_path or DB_PATH`` pattern instead.
    """
    return db_path if db_path is not None else DB_PATH


@contextmanager
def _get_conn(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(_resolve_db_path(db_path)))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_db(db_path: Path | None = None) -> None:
    """Create the ``generated_fixtures`` table. Idempotent."""
    with _get_conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS generated_fixtures (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                scenario_description TEXT NOT NULL DEFAULT '',
                -- serialized bundle artifacts (verbatim, ready to materialize) --
                profile_yaml TEXT NOT NULL DEFAULT '',
                people_yaml TEXT NOT NULL DEFAULT '',
                departments_yaml TEXT NOT NULL DEFAULT '',
                memory_json TEXT NOT NULL DEFAULT '{}',
                docs_json TEXT NOT NULL DEFAULT '{}',
                -- denormalized summary fields for the list endpoint --
                dept_summary_json TEXT NOT NULL DEFAULT '[]',
                people_summary_json TEXT NOT NULL DEFAULT '[]',
                industry TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL DEFAULT '',
                arr TEXT,
                arr_currency TEXT,
                headcount INTEGER,
                founding_year INTEGER,
                mission TEXT NOT NULL DEFAULT '',
                doc_count INTEGER NOT NULL DEFAULT 0,
                scenario_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1))
            );
            CREATE INDEX IF NOT EXISTS idx_generated_fixtures_archived
                ON generated_fixtures(archived);
            """
        )
        # Additive migration for DBs created before arr_currency existed:
        # CREATE TABLE IF NOT EXISTS leaves pre-existing schemas untouched,
        # so the column is added separately. Idempotent — PRAGMA-driven,
        # never drops or rewrites rows; rows keep NULL (currency unknown).
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(generated_fixtures)")
        }
        if "arr_currency" not in cols:
            conn.execute(
                "ALTER TABLE generated_fixtures ADD COLUMN arr_currency TEXT"
            )
        _migrate_arr_text(conn)


def _migrate_arr_text(conn: sqlite3.Connection) -> None:
    """Rebuild ``generated_fixtures.arr`` REAL → TEXT (exact decimal string).

    Money must not round-trip through binary floats (D07/F3); the column now
    stores the canonical decimal text. REAL affinity would coerce a bound
    string back to float, so the fix is a table rebuild. Existing REAL values
    convert via CAST to their shortest-repr text — the honest bound of what a
    float column ever recorded.
    """
    cols = conn.execute("PRAGMA table_info(generated_fixtures)").fetchall()
    arr_col = next((c for c in cols if c["name"] == "arr"), None)
    if arr_col is None or arr_col["type"].upper() == "TEXT":
        return
    logger.info("fixtures: migrating arr column to TEXT")
    if conn.in_transaction:
        conn.commit()
    conn.execute("PRAGMA foreign_keys = OFF")
    try:
        conn.executescript("""
            BEGIN;
            CREATE TABLE generated_fixtures_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                display_name TEXT NOT NULL,
                scenario_description TEXT NOT NULL DEFAULT '',
                profile_yaml TEXT NOT NULL DEFAULT '',
                people_yaml TEXT NOT NULL DEFAULT '',
                departments_yaml TEXT NOT NULL DEFAULT '',
                memory_json TEXT NOT NULL DEFAULT '{}',
                docs_json TEXT NOT NULL DEFAULT '{}',
                dept_summary_json TEXT NOT NULL DEFAULT '[]',
                people_summary_json TEXT NOT NULL DEFAULT '[]',
                industry TEXT NOT NULL DEFAULT '',
                stage TEXT NOT NULL DEFAULT '',
                arr TEXT,
                arr_currency TEXT,
                headcount INTEGER,
                founding_year INTEGER,
                mission TEXT NOT NULL DEFAULT '',
                doc_count INTEGER NOT NULL DEFAULT 0,
                scenario_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0 CHECK(archived IN (0, 1))
            );
            INSERT INTO generated_fixtures_new SELECT
                id, name, display_name, scenario_description,
                profile_yaml, people_yaml, departments_yaml,
                memory_json, docs_json, dept_summary_json,
                people_summary_json, industry, stage,
                CAST(arr AS TEXT), arr_currency, headcount, founding_year,
                mission, doc_count, scenario_count, created_at, updated_at,
                archived
            FROM generated_fixtures;
            DROP TABLE generated_fixtures;
            ALTER TABLE generated_fixtures_new RENAME TO generated_fixtures;
            CREATE INDEX idx_generated_fixtures_archived
                ON generated_fixtures(archived);
            COMMIT;
        """)
    finally:
        conn.execute("PRAGMA foreign_keys = ON")


def fixture_name_exists(name: str, db_path: Path | None = None) -> bool:
    """True if ``name`` occupies the UNIQUE slot — INCLUDING archived rows.

    A soft-deleted (archived) row still holds its ``name`` in the UNIQUE
    index, so inserting the same slug would raise. Slug derivation must treat
    an archived name as taken; otherwise recreating a deleted fixture's name
    would route back to the same slug and 409 forever. This is intentionally
    broader than :func:`get_fixture` / :func:`list_fixtures`, which exclude
    archived rows because they answer "is this fixture live?".
    """
    if not _resolve_db_path(db_path).exists():
        return False
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM generated_fixtures WHERE name = ?",
            (name,),
        ).fetchone()
    return row is not None


def insert_fixture(
    *,
    name: str,
    display_name: str,
    scenario_description: str,
    profile_yaml: str,
    people_yaml: str,
    departments_yaml: str,
    memory_json: str,
    docs_json: str,
    dept_summary_json: str = "[]",
    people_summary_json: str = "[]",
    industry: str = "",
    stage: str = "",
    arr: Decimal | None = None,
    arr_currency: str | None = None,
    headcount: int | None = None,
    founding_year: int | None = None,
    mission: str = "",
    doc_count: int = 0,
    scenario_count: int = 0,
    db_path: Path | None = None,
) -> int:
    """Insert one generated fixture. Returns the new row id.

    Raises ``sqlite3.IntegrityError`` if ``name`` collides with an existing
    (including archived) row — the caller picks a unique slug first via
    :func:`fixture_name_exists`.
    """
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            """
            INSERT INTO generated_fixtures (
                name, display_name, scenario_description,
                profile_yaml, people_yaml, departments_yaml,
                memory_json, docs_json, dept_summary_json, people_summary_json,
                industry, stage, arr, arr_currency, headcount, founding_year,
                mission, doc_count, scenario_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                display_name,
                scenario_description,
                profile_yaml,
                people_yaml,
                departments_yaml,
                memory_json,
                docs_json,
                dept_summary_json,
                people_summary_json,
                industry,
                stage,
                str(arr) if arr is not None else None,
                arr_currency,
                headcount,
                founding_year,
                mission,
                doc_count,
                scenario_count,
                now,
                now,
            ),
        )
        return int(cur.lastrowid or 0)


def get_fixture(name: str, db_path: Path | None = None) -> dict[str, Any] | None:
    """Return the full row (including serialized artifacts) or None."""
    if not _resolve_db_path(db_path).exists():
        return None
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM generated_fixtures WHERE name = ? AND archived = 0",
            (name,),
        ).fetchone()
    return dict(row) if row else None


def list_fixtures(db_path: Path | None = None) -> list[dict[str, Any]]:
    """Return list-endpoint summaries for all non-archived generated fixtures.

    Shape mirrors ``cli.fixture_loader.list_fixtures`` so the route can merge
    curated + generated rows uniformly; each row carries ``source:
    "generated"``.
    """
    if not _resolve_db_path(db_path).exists():
        return []
    with _get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT name, display_name, industry, stage, arr, arr_currency,
                   headcount, founding_year, mission, doc_count, scenario_count,
                   dept_summary_json, people_summary_json
            FROM generated_fixtures
            WHERE archived = 0
            ORDER BY updated_at DESC
            """
        ).fetchall()

    out: list[dict[str, Any]] = []
    for row in rows:
        d = dict(row)
        # Parse the denormalized org summaries into the same shape the curated
        # list emits (departments: [{title, head}], people: [{name, role,
        # is_principal}]) so the demo cards render uniformly.
        d["departments"] = json.loads(d.pop("dept_summary_json") or "[]")
        d["people"] = json.loads(d.pop("people_summary_json") or "[]")
        d["source"] = "generated"
        out.append(d)
    return out


def delete_fixture(name: str, db_path: Path | None = None) -> bool:
    """Soft-delete a generated fixture. Returns True if a row was archived.

    The row's ``name`` stays in the UNIQUE index (see
    :func:`fixture_name_exists`) so a re-created same-named company gets a
    bumped slug rather than colliding.
    """
    if not _resolve_db_path(db_path).exists():
        return False
    now = datetime.now(UTC).isoformat()
    with _get_conn(db_path) as conn:
        cur = conn.execute(
            "UPDATE generated_fixtures SET archived = 1, updated_at = ? "
            "WHERE name = ? AND archived = 0",
            (now, name),
        )
        return cur.rowcount > 0
