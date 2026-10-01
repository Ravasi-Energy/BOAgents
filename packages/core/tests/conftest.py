from __future__ import annotations

import os
import tempfile

import pytest

# Required env vars for Settings() — set here so individual test modules
# don't each have to remember. Real values come from .env in dev/prod.
os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")
os.environ.setdefault("EXEC_EMAIL_ADDRESS", "ceo.test@example.com")

# The durable turn/switch barrier (RA13-A02-01) writes to bo_agents.db,
# resolved at import time. Without isolation a crashed test leaves an
# 'uncertain' lease in the shared ./bo_agents.db and later client-slot
# activations refuse spuriously — per-process temp file keeps runs clean.
os.environ.setdefault(
    "BOAGENTS_DB_PATH",
    os.path.join(tempfile.gettempdir(), f"oe_bo_test_{os.getpid()}.db"),
)

# Tests run as a local, non-public process. If this leaks in from the developer's
# shell, create_app() fails closed on the missing BACKEND_SHARED_SECRET and every
# full-app test errors at construction — the same trap BACKEND_SHARED_SECRET sets
# (see CLAUDE.md → Testing). Clear it so the suite matches CI either way.
os.environ.pop("OE_PUBLIC_DEPLOYMENT", None)


@pytest.fixture(autouse=True)
def _isolated_turn_barrier_db(tmp_path, monkeypatch):
    """Per-test bo_agents.db for the turn barrier (RA13-A02-01).

    The module-level default is a per-process file — fine for barrier
    *tables*, but turn leases persist between tests and a 'processed'
    lease from test A would fence the same (mailbox, message_id) in
    test B. The schema is initialized eagerly: barrier *read* paths are
    fail-closed on a missing store file, so tests that only read must
    still see an initialized (empty) store. Tests that pass an explicit
    ``db_path`` are unaffected.
    """
    from openexecutive.bo import db as bo_db
    from openexecutive.bo import turn_barrier

    monkeypatch.setattr(bo_db, "DB_PATH", tmp_path / "bo_agents.db")
    turn_barrier.initialize_db()
    yield


@pytest.fixture(autouse=True)
def reset_active_gateway():
    """Ensure the module-level MCP gateway singleton is cleared between tests."""
    from openexecutive.orchestrator.mcp_gateway import set_active_gateway
    set_active_gateway(None)
    yield
    set_active_gateway(None)


@pytest.fixture
def install_source_feed(monkeypatch: pytest.MonkeyPatch):
    """Point one monitoring source adapter's bounded fetch at a canned body.

    Every feed adapter (``vendor_status``, ``rss``, ``edgar``, …) reaches
    the network through its own module-level ``fetch_bounded`` +
    ``validate_target_url`` pair, so each test module used to carry its own
    near-identical monkeypatch helper. Yields a setter:

        captured = install_source_feed("edgar", b"<feed>…</feed>")
        ...
        assert captured["user_agent"] == ...

    The returned dict records the arguments of the LAST fetch — ``url``,
    ``max_bytes``, and any keyword the adapter passes (edgar sends
    ``user_agent``; keys an adapter doesn't send are absent).
    """
    def _install(module: str, body: bytes | str) -> dict:
        captured: dict = {}
        payload = body.encode() if isinstance(body, str) else body

        async def fake_fetch(url: str, max_bytes: int, **kwargs) -> bytes:
            captured.clear()
            captured.update({"url": url, "max_bytes": max_bytes, **kwargs})
            return payload

        base = f"openexecutive.monitoring.sources.{module}"
        monkeypatch.setattr(f"{base}.fetch_bounded", fake_fetch)
        monkeypatch.setattr(f"{base}.validate_target_url", lambda u: (True, ""))
        return captured

    return _install
