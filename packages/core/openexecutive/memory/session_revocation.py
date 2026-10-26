"""Per-session revocation (C10 logout-replay).

The UI session is a JWT — deleting the cookie cannot un-issue it. This
table is the server-side authority: ``POST /sessions/revoke`` (called by
the NextAuth ``signOut`` event with the service credential) writes the
session's ``jti`` here, and ``resolve_identity`` refuses any delegated
identity presenting a revoked ``x-caller-session-jti`` — so a token copied
before logout is dead afterwards, across processes and restarts (the row
lives in the durable episodic DB), while OTHER sessions of the same user
stay valid (per-session, not per-roster).
"""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from openexecutive.memory.episodic import DB_PATH, _get_conn


def initialize_db(db_path: Path | None = None) -> None:
    db_path = db_path or DB_PATH
    with _get_conn(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS bo_session_revocations (
                jti TEXT PRIMARY KEY,
                actor TEXT NOT NULL,
                revoked_at TEXT NOT NULL
            )
            """
        )


def revoke(jti: str, actor: str, db_path: Path | None = None) -> None:
    """Mark a session id revoked. Idempotent — a repeated revoke for the
    same jti converges to the same row."""
    jti = (jti or "").strip()
    if not jti:
        return
    db_path = db_path or DB_PATH
    initialize_db(db_path)
    with _get_conn(db_path) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO bo_session_revocations "
            "(jti, actor, revoked_at) VALUES (?, ?, ?)",
            (jti, actor, datetime.now(UTC).isoformat()),
        )
        conn.commit()


def is_revoked(jti: str | None, db_path: Path | None = None) -> bool:
    jti = (jti or "").strip()
    if not jti:
        return False
    db_path = db_path or DB_PATH
    initialize_db(db_path)
    with _get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM bo_session_revocations WHERE jti = ?",
            (jti,),
        ).fetchone()
    return row is not None
