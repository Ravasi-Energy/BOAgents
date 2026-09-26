"""Shared SQLite plumbing for the BOAgents slice.

The product keeps its own database file (``bo_agents.db`` by default,
``BOAGENTS_DB_PATH`` to override) rather than borrowing
``episodic_memory.db``: the services are meant to be separable, and the
simulation-retention sweep must never touch the host app's audit history.

Mirrors the ``people.store`` pattern: a module-level ``DB_PATH`` resolved at
call time so tests can monkeypatch it.
"""
from __future__ import annotations

import os
import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(os.environ.get("BOAGENTS_DB_PATH", "./bo_agents.db"))


def _resolve_db_path(db_path: Path | None) -> Path:
    return db_path if db_path is not None else DB_PATH


@contextmanager
def get_conn(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    conn = sqlite3.connect(str(_resolve_db_path(db_path)))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize_db(db_path: Path | None = None) -> None:
    """Create every BOAgents table idempotently. Called from the lifespan."""
    from openexecutive.bo.bots import store as bots_store
    from openexecutive.bo.execution import store as execution_store
    from openexecutive.bo.packages import store as packages_store
    from openexecutive.bo.routing import store as routing_store
    from openexecutive.bo.settings import store as settings_store

    settings_store.initialize_db(db_path)
    bots_store.initialize_db(db_path)
    packages_store.initialize_db(db_path)
    routing_store.initialize_db(db_path)
    execution_store.initialize_db(db_path)
    from openexecutive.bo.pilot.service import initialize_db as initialize_pilot
    initialize_pilot(db_path)
