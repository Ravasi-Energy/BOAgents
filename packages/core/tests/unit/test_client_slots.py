"""Client-slot round-trip tests: save → switch → switch back must be lossless.

The slot mechanism's whole contract is "a slot is a faithful save file" —
these tests prove the SQLite state (decisions, scheduled actions, the people
roster), the company artifacts (profile, docs, mcp_servers.json), and the
operator-level tables behave correctly across switches. The vector and
Honcho layers are stubbed: they're side effects of a switch, not part of the
round-trip contract under test.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from openexecutive.clients import slots
from openexecutive.clients.slots import (
    ClientSlotConflictError,
    ClientSlotError,
    ClientSlotNotFoundError,
    activate_client_slot,
    create_client_slot,
    delete_client_slot,
    get_active_client,
    list_client_slots,
    save_active_client,
)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Isolated company dir + episodic DB, with vector/Honcho layers stubbed."""
    company = tmp_path / "company"
    company.mkdir()
    settings = SimpleNamespace(
        company_profile_path=company / "profile.yaml",
        vector_store_path=tmp_path / "chroma",
        mcp_servers_config_path=company / "mcp_servers.json",
        honcho_workspace_id="default-ws",
    )

    db_path = tmp_path / "episodic.db"
    from openexecutive.departments import store as dept_store
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store

    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    monkeypatch.setattr(dept_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    people_store.initialize_db(db_path)
    dept_store.initialize_db(db_path)

    async def _no_vector(_settings: Any, _app_state: Any, *, store: Any = None) -> int:
        return 0

    reseed_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(slots, "_rebuild_vector_state", _no_vector)
    monkeypatch.setattr(slots, "_set_honcho_client_workspace", lambda _slug: None)
    monkeypatch.setattr(
        slots, "_reseed_blank_defaults", lambda **kw: reseed_calls.append(kw)
    )
    return SimpleNamespace(
        settings=settings, db_path=db_path, company=company, reseed_calls=reseed_calls
    )


def _insert_decision(db_path: Path, summary: str) -> None:
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO decisions (timestamp, domain, summary, rationale, outcome, tags, department) "
            "VALUES ('2026-06-01T00:00:00', 'strategy', ?, '', '', '', '')",
            (summary,),
        )
        conn.commit()
    finally:
        conn.close()


def _decision_summaries(db_path: Path) -> list[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return [r[0] for r in conn.execute("SELECT summary FROM decisions").fetchall()]
    finally:
        conn.close()


def _seed_live_company(env: SimpleNamespace, name: str = "Acme Corp") -> None:
    env.settings.company_profile_path.write_text(f"name: {name}\n")
    docs = env.company / "docs"
    docs.mkdir(exist_ok=True)
    (docs / "strategy.md").write_text(f"# {name} strategy")
    env.settings.mcp_servers_config_path.write_text('{"servers": {"crayon": {}}}')
    _insert_decision(env.db_path, f"{name} decision")


async def test_create_from_current_captures_state_and_activates(env: SimpleNamespace) -> None:
    _seed_live_company(env, "Acme Corp")

    result = await create_client_slot(
        env.settings, display_name="Acme Corp", source="current"
    )

    assert result["slug"] == "acme_corp"
    assert result["active"] is True
    assert get_active_client(env.settings) == "acme_corp"

    slot = env.company / "_client_slots" / "acme_corp"
    assert (slot / "profile.yaml").exists()
    assert (slot / "docs" / "strategy.md").exists()
    assert (slot / "mcp_servers.json").exists()
    assert (slot / "state.db").exists()

    listed = list_client_slots(env.settings)
    assert [s["slug"] for s in listed] == ["acme_corp"]
    assert listed[0]["has_state"] is True
    assert listed[0]["has_mcp_config"] is True


async def test_switch_round_trip_is_lossless(env: SimpleNamespace) -> None:
    # Client A: full company with a roster entry and a scheduled action.
    _seed_live_company(env, "Acme Corp")
    from openexecutive.people import store as people_store

    people_store.upsert_person(
        full_name="Dana Acme",
        role="CFO",
        email="dana@acme.example",
        db_path=env.db_path,
    )
    conn = sqlite3.connect(str(env.db_path))
    conn.execute(
        "INSERT INTO scheduled_actions (created_at, run_at, channel, channel_ref, "
        "intent_text, status, attempts, last_error, department, kind) "
        "VALUES ('2026-06-01T00:00:00', '2099-01-01T00:00:00', 'any', '', "
        "'follow up with Acme board', 'pending', 0, '', '', 'ad_hoc')"
    )
    conn.commit()
    conn.close()

    await create_client_slot(env.settings, display_name="Acme Corp", source="current")
    await create_client_slot(env.settings, display_name="Beta Inc", source="blank")

    # Switch to Beta: live state must be Beta's (empty), not Acme's.
    await activate_client_slot(env.settings, "beta_inc")
    assert get_active_client(env.settings) == "beta_inc"
    assert _decision_summaries(env.db_path) == []
    # Acme's roster must not leak into Beta — the other half of the witness.
    assert people_store.find_person_by_email("dana@acme.example", env.db_path) is None
    assert not env.settings.mcp_servers_config_path.exists()
    assert "Beta Inc" in env.settings.company_profile_path.read_text()

    # Do Beta-specific work, then switch back to Acme.
    _insert_decision(env.db_path, "Beta decision")
    (env.company / "docs").mkdir(exist_ok=True)
    (env.company / "docs" / "beta.md").write_text("# Beta")

    await activate_client_slot(env.settings, "acme_corp")
    assert get_active_client(env.settings) == "acme_corp"
    assert _decision_summaries(env.db_path) == ["Acme Corp decision"]
    assert (env.company / "docs" / "strategy.md").exists()
    assert not (env.company / "docs" / "beta.md").exists()
    assert env.settings.mcp_servers_config_path.exists()
    assert people_store.find_person_by_email("dana@acme.example", env.db_path) is not None
    conn = sqlite3.connect(str(env.db_path))
    actions = conn.execute("SELECT intent_text FROM scheduled_actions").fetchall()
    conn.close()
    assert actions == [("follow up with Acme board",)]

    # And Beta's work survived its park.
    await activate_client_slot(env.settings, "beta_inc")
    assert _decision_summaries(env.db_path) == ["Beta decision"]
    assert (env.company / "docs" / "beta.md").exists()
    assert not (env.company / "docs" / "strategy.md").exists()


async def test_generated_fixtures_survive_switches(env: SimpleNamespace) -> None:
    """The fixture library is operator-level — never swapped with the client."""
    from openexecutive.fixtures import store as fixtures_store

    fixtures_store.initialize_db(env.db_path)
    _seed_live_company(env)
    await create_client_slot(env.settings, display_name="Acme", source="current")
    await create_client_slot(env.settings, display_name="Beta", source="blank")

    conn = sqlite3.connect(str(env.db_path))
    conn.execute(
        "INSERT INTO generated_fixtures (name, display_name, created_at, updated_at) "
        "VALUES ('halcyon_test', 'Halcyon Test', '2026-06-01', '2026-06-01')"
    )
    conn.commit()
    conn.close()

    await activate_client_slot(env.settings, "beta")

    conn = sqlite3.connect(str(env.db_path))
    rows = conn.execute("SELECT name FROM generated_fixtures").fetchall()
    conn.close()
    assert rows == [("halcyon_test",)]


async def test_first_activation_snapshots_original_company(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Activating with no active client preserves the user's company first."""
    _seed_live_company(env, "My Real Co")
    snapshots: list[str] = []
    monkeypatch.setattr(
        slots, "snapshot_user_state", lambda s: snapshots.append("taken")
    )

    await create_client_slot(env.settings, display_name="Beta", source="blank")
    await activate_client_slot(env.settings, "beta")

    assert snapshots == ["taken"]
    assert get_active_client(env.settings) == "beta"


async def test_create_from_current_snapshots_original_company(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Entering client mode via create-from-current must leave a _user_backup
    restore point so POST /fixtures/unload remains a working exit path."""
    _seed_live_company(env, "My Real Co")
    snapshots: list[str] = []
    monkeypatch.setattr(
        slots, "snapshot_user_state", lambda s: snapshots.append("taken")
    )

    await create_client_slot(env.settings, display_name="My Real Co", source="current")
    assert snapshots == ["taken"]

    # An existing backup is never overwritten by a later create.
    backup = env.company / "_user_backup"
    backup.mkdir(exist_ok=True)
    (backup / "profile.yaml").write_text("name: Original\n")
    # Exit client mode, then re-enter via a fresh create-from-current.
    (env.company / "_client_slots" / ".active_client").unlink()
    await create_client_slot(env.settings, display_name="Second", source="current")
    assert snapshots == ["taken"]  # no second snapshot


async def test_save_active_client_checkpoints_without_switching(env: SimpleNamespace) -> None:
    _seed_live_company(env)
    await create_client_slot(env.settings, display_name="Acme", source="current")

    _insert_decision(env.db_path, "late decision")
    result = await save_active_client(env.settings)
    assert result["saved"] is True

    # The slot's state.db now contains the late decision.
    slot_db = env.company / "_client_slots" / "acme" / "state.db"
    conn = sqlite3.connect(str(slot_db))
    rows = conn.execute("SELECT summary FROM decisions ORDER BY id").fetchall()
    conn.close()
    assert ("late decision",) in rows


async def test_save_with_no_active_client_raises(env: SimpleNamespace) -> None:
    with pytest.raises(ClientSlotConflictError):
        await save_active_client(env.settings)


async def test_operations_refuse_while_fixture_active(env: SimpleNamespace) -> None:
    backup = env.company / "_user_backup"
    backup.mkdir()
    (backup / ".fixture_active").write_text("halcyon_motors")

    with pytest.raises(ClientSlotConflictError):
        await create_client_slot(env.settings, display_name="Acme", source="current")
    with pytest.raises(ClientSlotConflictError):
        await save_active_client(env.settings)


async def test_create_from_current_refused_when_client_active(env: SimpleNamespace) -> None:
    _seed_live_company(env)
    await create_client_slot(env.settings, display_name="Acme", source="current")
    with pytest.raises(ClientSlotConflictError):
        await create_client_slot(env.settings, display_name="Other", source="current")


async def test_delete_refuses_active_then_deletes_parked(env: SimpleNamespace) -> None:
    _seed_live_company(env)
    await create_client_slot(env.settings, display_name="Acme", source="current")
    await create_client_slot(env.settings, display_name="Beta", source="blank")

    with pytest.raises(ClientSlotConflictError):
        await delete_client_slot(env.settings, "acme")

    result = await delete_client_slot(env.settings, "beta")
    assert result == {"deleted": True, "slug": "beta"}
    assert [s["slug"] for s in list_client_slots(env.settings)] == ["acme"]


async def test_activate_unknown_slug_raises_not_found(env: SimpleNamespace) -> None:
    with pytest.raises(ClientSlotNotFoundError):
        await activate_client_slot(env.settings, "nope")


async def test_activate_already_active_is_a_noop(env: SimpleNamespace) -> None:
    _seed_live_company(env)
    await create_client_slot(env.settings, display_name="Acme", source="current")
    result = await activate_client_slot(env.settings, "acme")
    assert result == {"slug": "acme", "already_active": True}


async def test_sentinel_garbage_is_ignored(env: SimpleNamespace) -> None:
    root = env.company / "_client_slots"
    root.mkdir()
    (root / ".active_client").write_text("../../etc/passwd")
    assert get_active_client(env.settings) is None


# ── Generated (engagement-intake) seed slots ────────────────────────────────


def _intake_bundle() -> dict[str, Any]:
    """A minimal valid engagement bundle (FixtureBundle shape)."""
    return {
        "profile": {
            "name": "Meridian Solar",
            "industry": "Commercial solar",
            "stage": "Private",
            "mission": "Margin-positive installs.",
        },
        "people": [
            {"full_name": "Dana Reyes", "role": "CEO", "is_principal": True},
            {"full_name": "Lee Park", "role": "VP Ops"},
        ],
        "departments": [
            {
                "slug": "operations",
                "title": "Operations",
                "head_person_name": "Lee Park",
            }
        ],
        "memory": {
            "decisions": [
                {
                    "timestamp": "2026-05-01T00:00:00",
                    "domain": "operations",
                    "summary": "Standardized on single-vendor inverters",
                }
            ],
            "initiatives": [],
            "advice_given": [],
            "alerts": [],
        },
        "docs": [
            {"filename": "intake_brief.md", "content": "# Intake brief"},
            {"filename": "open_questions.md", "content": "# Open questions"},
        ],
    }


async def test_generated_seed_slot_create_does_not_touch_live(env: SimpleNamespace) -> None:
    _seed_live_company(env, "My Real Co")

    result = await create_client_slot(
        env.settings,
        display_name="Meridian Solar",
        source="generated",
        bundle=_intake_bundle(),
        intake_description="kickoff call notes",
    )

    assert result["origin"] == "generated"
    assert result["active"] is False
    assert get_active_client(env.settings) is None
    # Live state untouched.
    assert "My Real Co" in env.settings.company_profile_path.read_text()
    assert _decision_summaries(env.db_path) == ["My Real Co decision"]

    slot = env.company / "_client_slots" / "meridian_solar"
    assert (slot / "profile.yaml").exists()
    assert (slot / "people.yaml").exists()
    assert (slot / "departments.yaml").exists()
    assert (slot / "memory.json").exists()
    assert sorted(p.name for p in (slot / "docs").glob("*.md")) == [
        "intake_brief.md",
        "open_questions.md",
    ]
    assert not (slot / "state.db").exists()

    listed = list_client_slots(env.settings)
    assert listed[0]["origin"] == "generated"


# "≈", "→" and "✅" sit outside cp1252, so an unencoded write_text() raises
# UnicodeEncodeError on a default Windows install. Accented Latin-1 characters
# alone would NOT reproduce it — they encode fine in cp1252.
_NON_ASCII_DOC = "Orçamento ≈ R$ 300 → prioridade: presença digital ✅"


async def test_generated_slot_pins_encoding_on_every_text_write(
    env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slot creation must never write or read text at the platform default.

    ``Path.write_text()`` with no ``encoding`` uses the C-level locale
    (cp1252 on a default Windows install), which raised mid-write and
    surfaced as ``ClientSlotError: Failed to write client slot: 'charmap'
    codec can't encode ...`` for any client whose docs were not
    cp1252-representable. Doc bodies are the only artifact written verbatim —
    profile/people/departments YAML and memory.json go through
    ``yaml.safe_dump`` / ``json.dumps``, which escape non-ASCII and are pure
    ASCII on disk — but every site in this path is pinned so the next verbatim
    write is safe by default.

    This asserts the *contract* (each call passes an explicit encoding) rather
    than simulating a locale, because the locale cannot be faked from Python:
    patching ``locale.getencoding`` does not affect ``Path.write_text``, which
    reads the C-level locale. A symptom-based test would therefore be inert on
    the UTF-8 platform CI runs on, and would police nothing.
    """
    real_write, real_read = Path.write_text, Path.read_text
    offenders: list[str] = []

    def guarded_write(self, data, encoding=None, errors=None, newline=None):  # type: ignore[no-untyped-def]
        if encoding is None:
            offenders.append(f"write_text: {self.name}")
        return real_write(self, data, encoding=encoding or "utf-8", errors=errors, newline=newline)

    # No `newline` here: Path.read_text only accepts it on Python 3.13+, and CI
    # runs 3.11 — forwarding it unconditionally would TypeError the moment a
    # read_text call enters the guarded path (exactly the regression this guard
    # exists to catch). **kwargs keeps the forwarding correct on every version.
    def guarded_read(self, encoding=None, **kwargs):  # type: ignore[no-untyped-def]
        if encoding is None:
            offenders.append(f"read_text: {self.name}")
        return real_read(self, encoding=encoding or "utf-8", **kwargs)

    monkeypatch.setattr(Path, "write_text", guarded_write)
    monkeypatch.setattr(Path, "read_text", guarded_read)

    bundle = _intake_bundle()
    bundle["docs"] = [{"filename": "plano.md", "content": _NON_ASCII_DOC}]
    await create_client_slot(
        env.settings,
        display_name="Consultório Ação — Psicologia",
        slug="consultorio_acao",
        source="generated",
        bundle=bundle,
    )
    monkeypatch.undo()

    assert offenders == []

    # And the bytes that landed really are UTF-8, losslessly.
    slot = env.company / "_client_slots" / "consultorio_acao"
    assert (slot / "docs" / "plano.md").read_bytes().decode("utf-8") == _NON_ASCII_DOC


async def test_seed_slot_activation_clears_derived_caches(env: SimpleNamespace) -> None:
    """The outgoing client's cached narrative/insights must not survive a switch.

    /today serves the cached briefing narrative unconditionally and only
    regenerates in a background task (api/routes/today.py::_attach_narrative),
    so a surviving row renders the PREVIOUS client's narrative under the
    incoming one — observed live, one client's briefing appearing verbatim
    under another. Both tables are regenerable caches, so wiping is free.
    """
    from openexecutive.briefing import narrative_cache
    from openexecutive.people import insights_cache

    _seed_live_company(env, "Outgoing Co")
    narrative_cache.put(
        narrative_cache.BriefingNarrative(
            scope="principal",
            input_hash="stale-hash",
            narrative_text="**Outgoing Co is straining** — do not show this elsewhere.",
            generated_at="2026-09-01T00:00:00+00:00",
        )
    )
    insights_cache.put(
        insights_cache.PersonInsight(
            person_id=1,
            input_hash="stale-hash",
            insight_text="Outgoing Co founder is unreachable.",
            generated_at="2026-09-01T00:00:00+00:00",
        )
    )
    # Both caches are warm for the outgoing client.
    assert narrative_cache.get("principal") is not None
    assert insights_cache.get(1) is not None

    await create_client_slot(
        env.settings,
        display_name="Incoming Co",
        source="generated",
        bundle=_intake_bundle(),
    )
    await activate_client_slot(env.settings, "incoming_co")

    assert narrative_cache.get("principal") is None
    assert insights_cache.get(1) is None


async def test_switch_preserves_outgoing_caches_in_its_slot(env: SimpleNamespace) -> None:
    """Wiping the caches on switch must not DESTROY them — only unpublish them.

    The wipe has to happen after the outgoing client's VACUUM INTO save-back, so
    its cached narrative lands in state.db and returns on reactivation. Ordering
    the wipe before the save-back would lose it permanently, and the sibling
    test above cannot catch that: there, no client is active, so the save-back
    branch of activate_client_slot never runs.
    """
    from openexecutive.briefing import narrative_cache
    from openexecutive.people import insights_cache

    _seed_live_company(env, "Client A")
    await create_client_slot(env.settings, display_name="Client A", source="current")
    narrative_cache.put(
        narrative_cache.BriefingNarrative(
            scope="principal",
            input_hash="a-hash",
            narrative_text="**Client A narrative**",
            generated_at="2026-09-01T00:00:00+00:00",
        )
    )
    insights_cache.put(
        insights_cache.PersonInsight(
            person_id=1,
            input_hash="a-hash",
            insight_text="Client A insight",
            generated_at="2026-09-01T00:00:00+00:00",
        )
    )
    await save_active_client(env.settings)

    await create_client_slot(env.settings, display_name="Client B", source="blank")
    await activate_client_slot(env.settings, "client_b")
    assert narrative_cache.get("principal") is None

    await activate_client_slot(env.settings, "client_a")
    restored = narrative_cache.get("principal")
    assert restored is not None
    assert restored.narrative_text == "**Client A narrative**"
    restored_insight = insights_cache.get(1)
    assert restored_insight is not None
    assert restored_insight.insight_text == "Client A insight"


async def test_generated_seed_slot_activation_seeds_org_and_memory(
    env: SimpleNamespace,
) -> None:
    _seed_live_company(env, "My Real Co")
    await create_client_slot(env.settings, display_name="My Real Co", source="current")
    await create_client_slot(
        env.settings,
        display_name="Meridian Solar",
        source="generated",
        bundle=_intake_bundle(),
    )

    await activate_client_slot(env.settings, "meridian_solar")

    # Seeded people, departments, and memory landed in the live DB.
    assert _decision_summaries(env.db_path) == [
        "Standardized on single-vendor inverters"
    ]
    conn = sqlite3.connect(str(env.db_path))
    people = sorted(
        r[0] for r in conn.execute("SELECT full_name FROM people").fetchall()
    )
    depts = [
        r[0] for r in conn.execute("SELECT slug FROM departments").fetchall()
    ]
    conn.close()
    assert people == ["Dana Reyes", "Lee Park"]
    assert depts == ["operations"]
    # Default-department reseed was skipped (the draft supplied an org).
    assert env.reseed_calls[-1] == {"seed_departments": False}
    assert "Meridian Solar" in env.settings.company_profile_path.read_text()
    assert (env.company / "docs" / "intake_brief.md").exists()


async def test_seed_slot_save_back_prefers_state_db(env: SimpleNamespace) -> None:
    """Once a seed slot has been saved back, state.db wins over seed files."""
    _seed_live_company(env, "My Real Co")
    await create_client_slot(env.settings, display_name="My Real Co", source="current")
    await create_client_slot(
        env.settings,
        display_name="Meridian Solar",
        source="generated",
        bundle=_intake_bundle(),
    )
    await activate_client_slot(env.settings, "meridian_solar")
    _insert_decision(env.db_path, "engagement week-1 decision")

    # Park Meridian (save-back writes state.db), then return.
    await activate_client_slot(env.settings, "my_real_co")
    slot = env.company / "_client_slots" / "meridian_solar"
    assert (slot / "state.db").exists()
    assert (slot / "people.yaml").exists()  # birth record kept

    await activate_client_slot(env.settings, "meridian_solar")
    # state.db restore: both the seeded and the new decision survive, and the
    # seeders did NOT run again (people not duplicated).
    assert sorted(_decision_summaries(env.db_path)) == [
        "Standardized on single-vendor inverters",
        "engagement week-1 decision",
    ]
    conn = sqlite3.connect(str(env.db_path))
    n_people = conn.execute("SELECT COUNT(*) FROM people").fetchone()[0]
    conn.close()
    assert n_people == 2


async def test_generated_requires_valid_bundle(env: SimpleNamespace) -> None:
    with pytest.raises(slots.ClientSlotError):
        await create_client_slot(
            env.settings, display_name="X", source="generated", bundle=None
        )
    # Referential failure (department head not in roster) → rejected, no husk.
    bad = _intake_bundle()
    bad["departments"][0]["head_person_name"] = "Ghost"
    with pytest.raises(slots.ClientSlotError):
        await create_client_slot(
            env.settings, display_name="Ghost Co", source="generated", bundle=bad
        )
    assert not (env.company / "_client_slots" / "ghost_co").exists()


async def test_refused_vector_store_leaves_live_state_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PersistedEmbeddingConfigError mid-switch must not strand B's state under A's sentinel.

    Reproduces the SEC-06 defect: _restore_slot_state swapped the live DB,
    profile/docs and MCP config to the target client BEFORE constructing
    ChromaDBStore — a refused store then left live=B while the sentinel
    still named A. The fix validates the store before the first mutation.
    """
    company = tmp_path / "company"
    company.mkdir()
    settings = SimpleNamespace(
        company_profile_path=company / "profile.yaml",
        vector_store_path=tmp_path / "chroma",
        mcp_servers_config_path=company / "mcp_servers.json",
        honcho_workspace_id="default-ws",
    )
    db_path = tmp_path / "episodic.db"
    from openexecutive.departments import store as dept_store
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store

    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    monkeypatch.setattr(dept_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    people_store.initialize_db(db_path)
    dept_store.initialize_db(db_path)
    monkeypatch.setattr(slots, "_set_honcho_client_workspace", lambda _slug: None)
    monkeypatch.setattr(slots, "_reseed_blank_defaults", lambda **kw: None)

    env = SimpleNamespace(settings=settings, db_path=db_path, company=company)
    _seed_live_company(env, "Acme Corp")
    await create_client_slot(env.settings, display_name="Acme Corp", source="current")
    await create_client_slot(env.settings, display_name="Beta Inc", source="blank")

    # Real _rebuild_vector_state, but the store factory refuses — the same
    # failure mode as the persisted-schema guard on a tampered volume.
    import openexecutive.knowledge.store as store_mod
    from openexecutive.knowledge.store import PersistedEmbeddingConfigError

    _real = store_mod.ChromaDBStore

    class _RefusingStore:
        # Lazy imports evaluate collection constants on the patched class —
        # carry the real names so module import survives the refusal.
        COMPANY_COLLECTION = _real.COMPANY_COLLECTION
        RESEARCH_COLLECTION = _real.RESEARCH_COLLECTION
        ATTACHMENT_COLLECTION = _real.ATTACHMENT_COLLECTION
        BUILTIN_COLLECTION = _real.BUILTIN_COLLECTION
        FAILURES_COLLECTION = _real.FAILURES_COLLECTION

        def __init__(self, *_a: Any, **_kw: Any) -> None:
            raise PersistedEmbeddingConfigError(
                "collection 'company_docs': embedding_function config refused"
            )

    monkeypatch.setattr(store_mod, "ChromaDBStore", _RefusingStore)

    with pytest.raises(PersistedEmbeddingConfigError):
        await activate_client_slot(env.settings, "beta_inc")

    # Identity AND live state must both still be Acme — not just the sentinel.
    assert get_active_client(env.settings) == "acme_corp"
    assert _decision_summaries(env.db_path) == ["Acme Corp decision"]
    assert "Acme Corp" in env.settings.company_profile_path.read_text()
    assert (env.company / "docs" / "strategy.md").exists()
    assert env.settings.mcp_servers_config_path.exists()


def _late_refusal_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Like ``env`` but keeps the REAL _rebuild_vector_state so the store
    refusal fires mid-restore, after the live DB/docs swap."""
    company = tmp_path / "company"
    company.mkdir()
    settings = SimpleNamespace(
        company_profile_path=company / "profile.yaml",
        vector_store_path=tmp_path / "chroma",
        mcp_servers_config_path=company / "mcp_servers.json",
        honcho_workspace_id="default-ws",
    )
    db_path = tmp_path / "episodic.db"
    from openexecutive.departments import store as dept_store
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store

    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    monkeypatch.setattr(dept_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    people_store.initialize_db(db_path)
    dept_store.initialize_db(db_path)
    monkeypatch.setattr(slots, "_set_honcho_client_workspace", lambda _slug: None)
    monkeypatch.setattr(slots, "_reseed_blank_defaults", lambda **kw: None)

    # Keep the suite hermetic: doc re-ingest during a recovery restore would
    # run the real ONNX embedding path (a download on a cold CI cache). The
    # vector layer is separately covered by the refusing-store stub; what
    # these tests witness is the FILE/DB swap, not embeddings.
    async def _no_ingest(*args: Any, **kwargs: Any) -> int:
        return 1

    monkeypatch.setattr(
        "openexecutive.knowledge.loader.ingest_file", _no_ingest
    )
    return SimpleNamespace(settings=settings, db_path=db_path, company=company)


def _refusing_store(monkeypatch: pytest.MonkeyPatch, *, once: bool) -> Any:
    """Real ChromaDBStore whose cleanup raises PersistedEmbeddingConfigError —
    the A02 residual window: builds fine at preflight, refuses post-swap."""
    import openexecutive.knowledge.store as store_mod
    from openexecutive.knowledge.store import PersistedEmbeddingConfigError

    _real = store_mod.ChromaDBStore
    armed = {"on": True}

    class _LateRefusingStore(_real):
        def delete_company_docs(self) -> None:
            if armed["on"]:
                if once:
                    armed["on"] = False
                raise PersistedEmbeddingConfigError(
                    "collection 'company_docs': refused (synthetic)"
                )
            return super().delete_company_docs()

    monkeypatch.setattr(store_mod, "ChromaDBStore", _LateRefusingStore)
    return armed


async def test_late_refusal_auto_recovers_previous_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Post-preflight refusal → request fails, live state coherently A again."""
    env = _late_refusal_env(tmp_path, monkeypatch)
    _seed_live_company(env, "Acme Corp")
    await create_client_slot(env.settings, display_name="Acme Corp", source="current")
    await create_client_slot(env.settings, display_name="Beta Inc", source="blank")

    _refusing_store(monkeypatch, once=True)  # refuses once: B's restore only
    from openexecutive.knowledge.store import PersistedEmbeddingConfigError

    with pytest.raises(PersistedEmbeddingConfigError):
        await activate_client_slot(env.settings, "beta_inc")

    # The failed activation recovered A automatically — every layer is Acme.
    assert get_active_client(env.settings) == "acme_corp"
    assert _decision_summaries(env.db_path) == ["Acme Corp decision"]
    assert "Acme Corp" in env.settings.company_profile_path.read_text()
    assert (env.company / "docs" / "strategy.md").exists()
    assert env.settings.mcp_servers_config_path.exists()
    # Not blocked — recovery succeeded, so no marker and ops still work.
    assert slots.get_restore_blocked(env.settings) is None
    await activate_client_slot(env.settings, "beta_inc")
    assert get_active_client(env.settings) == "beta_inc"


async def test_late_refusal_with_persistent_fault_blocks_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Recovery also refused → restore-blocked: no wrong-identity serving,
    no save-back contamination, and only the recorded slug can recover."""
    env = _late_refusal_env(tmp_path, monkeypatch)
    _seed_live_company(env, "Acme Corp")
    await create_client_slot(env.settings, display_name="Acme Corp", source="current")
    await create_client_slot(env.settings, display_name="Beta Inc", source="blank")

    armed = _refusing_store(monkeypatch, once=False)  # volume stays "broken"
    with pytest.raises(slots.ClientSlotError, match="restore-blocked"):
        await activate_client_slot(env.settings, "beta_inc")

    marker = slots.get_restore_blocked(env.settings)
    assert marker is not None and marker["restore_slug"] == "acme_corp"
    # Sentinel quarantined, not deleted; no client attribution under block.
    assert get_active_client(env.settings) is None
    assert not slots._active_client_sentinel(env.settings).exists()
    assert (
        env.company / "_client_slots" / ".active_client.refused"
    ).read_text() == "acme_corp"

    # Mutating ops refuse while blocked — no save-back over good copies.
    with pytest.raises(slots.ClientSlotError):
        await save_active_client(env.settings)
    with pytest.raises(slots.ClientSlotError):
        await create_client_slot(env.settings, display_name="C", source="current")
    with pytest.raises(slots.ClientSlotError):
        await delete_client_slot(env.settings, "beta_inc")
    with pytest.raises(slots.ClientSlotError):
        await activate_client_slot(env.settings, "beta_inc")

    # The recorded recovery slug is allowed through but re-blocks while the
    # volume still refuses — and the marker keeps the ORIGINAL failed_slug
    # forensics, not the recovery attempt's target.
    with pytest.raises(slots.ClientSlotError, match="restore-blocked"):
        await activate_client_slot(env.settings, "acme_corp")
    marker = slots.get_restore_blocked(env.settings)
    assert marker is not None and marker["failed_slug"] == "beta_inc"

    # Acme's slot copy was never touched through any of this.
    assert (
        env.company / "_client_slots" / "acme_corp" / "state.db"
    ).exists()

    # Repair the volume → the recorded restore path completes → unblocked.
    armed["on"] = False
    await activate_client_slot(env.settings, "acme_corp")
    assert slots.get_restore_blocked(env.settings) is None
    assert get_active_client(env.settings) == "acme_corp"
    assert _decision_summaries(env.db_path) == ["Acme Corp decision"]
    assert "Acme Corp" in env.settings.company_profile_path.read_text()
    assert (env.company / "docs" / "strategy.md").exists()


async def test_late_refusal_recovers_user_backup_without_active_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """First activation (no active client): refusal recovers the user's own
    company from _user_backup, save-back-free."""
    env = _late_refusal_env(tmp_path, monkeypatch)
    # User's own company, no client mode yet. ingest_file is stubbed in the
    # env so the backup-restore exercises the real store but no embeddings.
    env.settings.company_profile_path.write_text("name: User Co\n")
    (env.company / "docs").mkdir()
    (env.company / "docs" / "user.md").write_text("# user doc")
    await create_client_slot(env.settings, display_name="Beta Inc", source="blank")

    _refusing_store(monkeypatch, once=True)
    from openexecutive.knowledge.store import PersistedEmbeddingConfigError

    with pytest.raises(PersistedEmbeddingConfigError):
        await activate_client_slot(env.settings, "beta_inc")

    assert get_active_client(env.settings) is None  # still single-company
    assert "User Co" in env.settings.company_profile_path.read_text()
    # _user_backup covers profile/docs/memory.json/people — witnessed here by
    # profile + docs. (In this harness `episodic.list_decisions` binds DB_PATH
    # as a default arg at definition time, so the memory.json dump can't see
    # the patched test DB — a fixture gap, not the defect under test.)
    assert (env.company / "docs" / "user.md").exists()
    assert slots.get_restore_blocked(env.settings) is None


def test_restore_blocked_gate_blocks_everything_but_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HTTP gate: while blocked only /health and recovery endpoints pass."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive import config
    from openexecutive.api.main import _restore_blocked_gate

    company = tmp_path / "company"
    company.mkdir()
    settings = SimpleNamespace(
        company_profile_path=company / "profile.yaml",
        vector_store_path=tmp_path / "chroma",
        mcp_servers_config_path=company / "mcp_servers.json",
        honcho_workspace_id="default-ws",
    )
    monkeypatch.setattr(config, "get_settings", lambda: settings)

    from fastapi.middleware.cors import CORSMiddleware

    app = FastAPI()
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"], allow_methods=["*"]
    )
    app.middleware("http")(_restore_blocked_gate)

    @app.get("/health")
    def _health() -> dict:  # noqa: ANN202
        return {"ok": True}

    @app.get("/today")
    def _today() -> dict:  # noqa: ANN202
        return {"served": True}

    @app.post("/clients/{slug}/activate")
    def _activate(slug: str) -> dict:  # noqa: ANN202
        return {"slug": slug}

    @app.post("/fixtures/unload")
    def _unload() -> dict:  # noqa: ANN202
        return {"unloaded": True}

    c = TestClient(app)
    assert c.get("/today").status_code == 200  # unblocked: normal

    # Simulate the blocked marker.
    (company / "_client_slots").mkdir()
    slots._mark_restore_blocked(
        settings, failed_slug="beta_inc", target_slug="acme_corp"
    )

    assert c.get("/today").status_code == 503
    body = c.get("/today").json()
    assert body["restore_slug"] == "acme_corp"  # operator learns the target
    assert c.get("/health").status_code == 200
    # CORS preflight must survive — the UI's recovery buttons would
    # otherwise be unreachable in a browser while blocked.
    preflight = c.options(
        "/clients/acme_corp/activate",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
        },
    )
    assert preflight.status_code == 200
    assert "access-control-allow-origin" in preflight.headers
    assert c.post("/clients/acme_corp/activate").status_code == 200
    assert c.post("/fixtures/unload").status_code == 200
    assert c.get("/clients/acme_corp/activate").status_code == 503  # GET not a recovery

    # Marker cleared → serving resumes.
    slots._restore_blocked_path(settings).unlink()
    assert c.get("/today").status_code == 200


# ── SEC-09: scheduler/resumer holds on the restore-blocked marker ───────────


def test_scheduler_and_resumer_hold_while_restore_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A blocked instance must not fire due rows or resume runs on behalf of
    a live state that belongs to no client. Both background loops read the
    same marker and fail CLOSED (unlike the rotation pause, which fails open
    so a marker hiccup can't wedge claiming)."""
    from openexecutive.scheduler import runner
    from openexecutive.workflows import resumer

    env = _late_refusal_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: env.settings
    )
    assert runner._restore_blocked_active() is False
    assert resumer._restore_blocked_active() is False

    slots_root = env.company / "_client_slots"
    slots_root.mkdir(parents=True, exist_ok=True)
    (slots_root / ".restore_blocked").write_text(
        json.dumps({"failed_slug": "b", "restore_slug": "a", "kind": "slot"})
    )
    assert runner._restore_blocked_active() is True
    assert resumer._restore_blocked_active() is True

    # Fail-closed: an unreadable/malformed marker still means blocked.
    (slots_root / ".restore_blocked").write_text("not json{")
    assert runner._restore_blocked_active() is True
    assert resumer._restore_blocked_active() is True


def test_restore_blocked_active_fails_closed_on_read_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the settings/marker lookup itself raises, the background gates hold
    rather than let scheduled work run on unverifiable live state."""
    from openexecutive.scheduler import runner
    from openexecutive.workflows import resumer

    def _boom() -> object:
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr("openexecutive.config.get_settings", _boom)
    assert runner._restore_blocked_active() is True
    assert resumer._restore_blocked_active() is True


async def test_marker_write_failure_aborts_before_any_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An I/O fault on the transition marker must abort the activation
    BEFORE the first live mutation: live state is the previous client's,
    untouched, and a restart legitimately finds no block — no transition
    was ever in flight."""
    env = _late_refusal_env(tmp_path, monkeypatch)
    _seed_live_company(env, "Acme Corp")
    await create_client_slot(
        env.settings, display_name="Acme Corp", source="current"
    )
    await create_client_slot(
        env.settings, display_name="Beta Inc", source="blank"
    )

    real_write_text = Path.write_text

    def _fail_marker_write(self: Path, *a: Any, **kw: Any) -> Any:
        if self.name.startswith(".restore_blocked"):
            raise OSError("synthetic marker I/O fault")
        return real_write_text(self, *a, **kw)

    monkeypatch.setattr(Path, "write_text", _fail_marker_write)
    with pytest.raises(slots.ClientSlotError, match="transition marker"):
        await activate_client_slot(env.settings, "beta_inc")

    # Nothing mutated — same live state, same sentinel, no marker, no flag.
    assert get_active_client(env.settings) == "acme_corp"
    assert _decision_summaries(env.db_path) == ["Acme Corp decision"]
    assert not slots._restore_blocked_path(env.settings).exists()
    assert slots._restore_blocked_local is False
    assert slots.get_restore_blocked(env.settings) is None


async def test_post_mutation_marker_refresh_failure_still_fenced_on_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pre-mutation marker is the durable proof; if the post-failure
    refresh write ALSO fails, the marker on disk still fences a restarted
    process — the in-process flag is only the belt on top."""
    env = _late_refusal_env(tmp_path, monkeypatch)
    _seed_live_company(env, "Acme Corp")
    await create_client_slot(
        env.settings, display_name="Acme Corp", source="current"
    )
    await create_client_slot(
        env.settings, display_name="Beta Inc", source="blank"
    )

    real_write_text = Path.write_text
    marker_writes = 0

    def _fail_second_marker_write(self: Path, *a: Any, **kw: Any) -> Any:
        nonlocal marker_writes
        if self.name == ".restore_blocked.tmp":
            marker_writes += 1
            if marker_writes > 1:
                raise OSError("synthetic marker I/O fault")
        return real_write_text(self, *a, **kw)

    _refusing_store(monkeypatch, once=False)  # recovery is refused too
    monkeypatch.setattr(Path, "write_text", _fail_second_marker_write)

    try:
        with pytest.raises(slots.ClientSlotError, match="restore-blocked"):
            await activate_client_slot(env.settings, "beta_inc")

        assert marker_writes == 2  # transition_started + failed refresh
        assert slots._restore_blocked_local is True

        # Simulate restart: drop the in-process fence — the on-disk marker
        # alone must still block everything.
        slots._restore_blocked_local = False
        marker = slots.get_restore_blocked(env.settings)
        assert marker is not None and marker["restore_slug"] == "acme_corp"
        with pytest.raises(slots.ClientSlotError):
            await save_active_client(env.settings)
    finally:
        slots._restore_blocked_local = False


async def test_transition_marker_on_disk_throughout_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pin the ordering invariant directly: the durable marker must exist
    on disk for the WHOLE _restore_slot_state window, carrying the
    transition_started phase and the correct restore target."""
    env = _late_refusal_env(tmp_path, monkeypatch)
    _seed_live_company(env, "Acme Corp")
    await create_client_slot(
        env.settings, display_name="Acme Corp", source="current"
    )
    await create_client_slot(
        env.settings, display_name="Beta Inc", source="blank"
    )

    seen: dict[str, Any] = {}
    real_restore = slots._restore_slot_state

    async def _spy(*a: Any, **kw: Any) -> Any:
        seen["marker"] = slots.get_restore_blocked(env.settings)
        return await real_restore(*a, **kw)

    monkeypatch.setattr(slots, "_restore_slot_state", _spy)
    await activate_client_slot(env.settings, "beta_inc")

    assert seen["marker"] is not None
    assert seen["marker"]["phase"] == "transition_started"
    assert seen["marker"]["failed_slug"] == "beta_inc"
    assert seen["marker"]["restore_slug"] == "acme_corp"
    # Successful activation clears it.
    assert slots.get_restore_blocked(env.settings) is None


def test_restore_blocked_ignores_settings_without_real_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Token-only test doubles (slack/discord/outbound wiring stubs) patch
    get_settings with objects that have no company_profile_path — they have
    no filesystem the marker could live on, so they must not engage the
    production fence. The fail-closed branch is reserved for real settings
    whose lookup fails."""
    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: SimpleNamespace()
    )
    assert slots.is_restore_blocked() is False
    monkeypatch.setattr(
        "openexecutive.config.get_settings",
        lambda: SimpleNamespace(company_profile_path="not-a-path"),
    )
    assert slots.is_restore_blocked() is False


async def test_scheduler_tick_never_claims_while_restore_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Loop-level (not just helper-level): with the marker present a tick
    reaches the restore-blocked hold and never touches claim_due_actions —
    the thing that would fire a wrong company's outbound work."""
    import asyncio

    from openexecutive.scheduler import runner

    env = _late_refusal_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: env.settings
    )
    (env.company / "_client_slots").mkdir(parents=True)
    slots._mark_restore_blocked(
        env.settings, failed_slug="beta", target_slug="acme"
    )

    claimed: list[Any] = []
    monkeypatch.setattr(
        runner, "claim_due_actions", lambda now: claimed.append(now) or []
    )
    monkeypatch.setattr(runner, "_company_profile_active", lambda: True)
    monkeypatch.setattr(runner, "_rotation_pause_active", lambda: False)
    monkeypatch.setattr(runner, "_maybe_sweep_alerts", lambda _n: 0)
    monkeypatch.setattr(runner, "requeue_orphaned_running", lambda: 0)
    monkeypatch.setattr(runner, "seed_principal_briefs", lambda: 0)
    monkeypatch.setattr(
        "openexecutive.clients.rotation.clear_stale_rotation_marker",
        lambda _s: False,
    )
    monkeypatch.setattr(
        "openexecutive.clients.rotation.seed_client_rotation", lambda: None
    )

    task = asyncio.create_task(runner.run_scheduler(poll_interval_seconds=60))
    await asyncio.sleep(0.2)  # one tick
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert claimed == []


async def test_user_backup_recovery_leaves_no_incoming_client_residue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed activation into a slot WITH state.db, while no client is
    active, must not leave the incoming client's DB rows / MCP config live
    under the user's identity after the _user_backup recovery."""
    env = _late_refusal_env(tmp_path, monkeypatch)
    env.settings.company_profile_path.write_text("name: User Co\n")
    (env.company / "docs").mkdir()
    (env.company / "docs" / "user.md").write_text("# user doc")
    env.settings.mcp_servers_config_path.write_text('{"user": true}')
    _insert_decision(env.db_path, "User decision")
    await create_client_slot(env.settings, display_name="Beta", source="blank")

    # First activation succeeds: user state snapshots to _user_backup,
    # live becomes Beta. Give Beta a real row + MCP config, then save back
    # so the slot carries state.db.
    await activate_client_slot(env.settings, "beta")
    _insert_decision(env.db_path, "Beta row")
    env.settings.mcp_servers_config_path.write_text('{"beta": true}')
    await save_active_client(env.settings)

    # Operator quarantined the sentinel mid-incident (the documented
    # recovery step) — a no-active-client activation is now reachable.
    slots._active_client_sentinel(env.settings).unlink()

    _refusing_store(monkeypatch, once=True)
    from openexecutive.knowledge.store import PersistedEmbeddingConfigError

    with pytest.raises(PersistedEmbeddingConfigError):
        await activate_client_slot(env.settings, "beta")

    assert get_active_client(env.settings) is None
    # Beta's rows did not survive the recovery — the backup format has no
    # state.db, so anything DB-resident must be wiped, not preserved.
    assert _decision_summaries(env.db_path) == []
    assert "User Co" in env.settings.company_profile_path.read_text()
    assert (env.company / "docs" / "user.md").exists()
    # The backup captured the user's MCP config (snapshot covers it now);
    # either way, Beta's credentials must not remain live.
    import json as _json

    assert _json.loads(
        env.settings.mcp_servers_config_path.read_text()
    ) == {"user": True}
    assert slots.get_restore_blocked(env.settings) is None


async def test_inbound_resolution_consumed_while_restore_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Socket-mode adapters never cross the HTTP gate: while blocked, an
    inbound reply must NOT resolve a wait_for_human run (its remaining
    steps would fire on the wrong client's data) and must NOT fall through
    to a chat turn. The person is told to resend after recovery."""
    from openexecutive.workflows import inbound_resolver

    env = _late_refusal_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: env.settings
    )
    (env.company / "_client_slots").mkdir(parents=True)
    slots._mark_restore_blocked(
        env.settings, failed_slug="beta", target_slug="acme"
    )

    called: list[Any] = []

    async def _spy_resolve(**kwargs: Any) -> None:
        called.append(kwargs)
        return None

    monkeypatch.setattr(
        inbound_resolver, "resolve_inbound_message", _spy_resolve
    )
    sent: list[str] = []

    async def _send(msg: str) -> None:
        sent.append(msg)

    handled = await inbound_resolver.resolve_and_acknowledge(
        channel="slack",
        channel_ref="U123",
        person_id=7,
        text="approved",
        send=_send,
        message_id="m1",
    )
    assert handled is True
    assert called == []  # resolver never touched the runs table
    assert sent and "maintenance" in sent[0].lower()


async def test_executive_chat_refuses_while_restore_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full chat turns on socket channels must not run on mixed state."""
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    env = _late_refusal_env(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "openexecutive.config.get_settings", lambda: env.settings
    )
    (env.company / "_client_slots").mkdir(parents=True)
    slots._mark_restore_blocked(
        env.settings, failed_slug="beta", target_slug="acme"
    )

    reply = await Executive().chat("hello", Session(session_id="t1"))
    assert "maintenance" in reply.lower()


async def test_unload_clears_block_and_restores_user_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """kind=user_backup + persistent refusal → POST /fixtures/unload is the
    recorded recovery path: it must wipe the incoming client's residue,
    restore the backup, and lift the marker only after the restore landed."""
    from openexecutive.cli import fixture_loader

    env = _late_refusal_env(tmp_path, monkeypatch)
    env.settings.company_profile_path.write_text("name: User Co\n")
    (env.company / "docs").mkdir()
    (env.company / "docs" / "user.md").write_text("# user doc")
    _insert_decision(env.db_path, "User decision")
    await create_client_slot(env.settings, display_name="Beta", source="blank")

    armed = _refusing_store(monkeypatch, once=False)
    with pytest.raises(ClientSlotError, match="restore-blocked"):
        await activate_client_slot(env.settings, "beta")
    assert slots.get_restore_blocked(env.settings) is not None
    assert get_active_client(env.settings) is None

    # Vector store repaired → the unload recovery path must succeed.
    armed["on"] = False
    summary = await fixture_loader.unload_fixture(env.settings)

    assert summary["profile"]["name"] == "User Co"
    assert slots.get_restore_blocked(env.settings) is None
    assert slots.is_restore_blocked() is False
    assert not slots._active_client_sentinel(env.settings).exists()
    assert (env.company / "docs" / "user.md").exists()
    # The backup format has no state.db — live per-client tables must be
    # empty, not carry the half-swapped attempt's rows.
    assert _decision_summaries(env.db_path) == []


async def test_cancelled_activation_waits_for_recovery_under_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling the request mid-recovery must NOT release
    _FIXTURE_OP_LOCK while the shielded restore still mutates live state:
    the task ends cancelled only after the inner recovery completed."""
    env = _late_refusal_env(tmp_path, monkeypatch)
    _seed_live_company(env, "Acme Corp")
    await create_client_slot(env.settings, display_name="Acme Corp", source="current")
    await create_client_slot(env.settings, display_name="Beta Inc", source="blank")
    _refusing_store(monkeypatch, once=True)

    started = asyncio.Event()
    finished = asyncio.Event()
    real_recover = slots._recover_failed_activation

    async def _slow_recover(*args: Any, **kwargs: Any) -> bool:
        started.set()
        await asyncio.sleep(0.3)
        result = await real_recover(*args, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(slots, "_recover_failed_activation", _slow_recover)

    task = asyncio.create_task(
        activate_client_slot(env.settings, "beta_inc")
    )
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The task only surfaced the cancellation after the inner restore
    # finished — the lock was never released mid-write.
    assert finished.is_set()
    # Recovery completed → coherent A, marker cleared despite cancellation.
    assert get_active_client(env.settings) == "acme_corp"
    assert slots.get_restore_blocked(env.settings) is None


async def test_marker_write_failure_fences_process_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the marker file cannot be written (same disk fault that broke the
    restore), the process must still be fenced — otherwise every gate reads
    unblocked on provably-mixed state."""
    env = _late_refusal_env(tmp_path, monkeypatch)
    monkeypatch.setattr("openexecutive.config.get_settings", lambda: env.settings)
    root = env.company / "_client_slots"
    root.mkdir(parents=True)
    root.chmod(0o555)
    try:
        slots._mark_restore_blocked(
            env.settings, failed_slug="beta", target_slug="acme"
        )
        assert not slots._restore_blocked_path(env.settings).exists()
        assert slots._restore_blocked_local is True
        assert slots.get_restore_blocked(env.settings) == {
            "malformed": True,
            "volatile": True,
        }
        assert slots.is_restore_blocked() is True
    finally:
        root.chmod(0o755)
        slots._restore_blocked_local = False
    assert slots.is_restore_blocked() is False


async def test_user_backup_recovery_wipes_db_only_tables(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """dynamic_workflows (and the audit dedup sidecar) live in the episodic
    DB but not in the backup format — they must not survive recovery."""
    env = _late_refusal_env(tmp_path, monkeypatch)
    env.settings.company_profile_path.write_text("name: User Co\n")
    _insert_decision(env.db_path, "User decision")
    await create_client_slot(env.settings, display_name="Beta", source="blank")
    await activate_client_slot(env.settings, "beta")

    # Beta accumulates state that only exists as DB rows.
    _insert_decision(env.db_path, "Beta row")
    conn = sqlite3.connect(str(env.db_path))
    conn.execute(
        "CREATE TABLE IF NOT EXISTS dynamic_workflows "
        "(id INTEGER PRIMARY KEY, name TEXT, definition TEXT, "
        "is_active INTEGER, created_at TEXT, updated_at TEXT)"
    )
    conn.execute(
        "INSERT INTO dynamic_workflows (name, definition, is_active) "
        "VALUES ('beta_wf', '{}', 1)"
    )
    conn.commit()
    conn.close()
    await save_active_client(env.settings)
    slots._active_client_sentinel(env.settings).unlink()

    _refusing_store(monkeypatch, once=True)
    from openexecutive.knowledge.store import PersistedEmbeddingConfigError

    with pytest.raises(PersistedEmbeddingConfigError):
        await activate_client_slot(env.settings, "beta")

    assert slots.get_restore_blocked(env.settings) is None
    conn = sqlite3.connect(str(env.db_path))
    remaining = conn.execute(
        "SELECT COUNT(*) FROM dynamic_workflows"
    ).fetchone()[0]
    conn.close()
    assert remaining == 0


# ---------------------------------------------------------------- B3
# Generic (non-embedding-refusal) cleanup failures on the transition path
# must propagate — a swallowed I/O error leaves the outgoing client's
# research / Notion / attachment / skill rows readable under the incoming
# client (REM-AUDIT-01 B3).


def _io_failing_store(
    monkeypatch: pytest.MonkeyPatch, *, site: str, once: bool
) -> dict[str, bool]:
    """Real ChromaDBStore whose ONE strict cleanup site raises a plain
    OSError — the B3 case: not a persisted-schema refusal, just a failed
    delete (locked volume, transient I/O). ``once`` disarms after the first
    raise so the automatic recovery restore (which re-runs the same layer)
    can succeed; ``once=False`` keeps the volume broken."""
    import openexecutive.knowledge.store as store_mod
    from openexecutive.knowledge.skills_index import SKILLS_COLLECTION
    from openexecutive.knowledge.store import ChromaDBStore

    _real = ChromaDBStore
    armed = {"on": True}

    def _fail() -> None:
        if not armed["on"]:
            return
        if once:
            armed["on"] = False
        raise OSError(f"synthetic I/O fault on {site} cleanup")

    class _IoStore(_real):
        def delete_documents(
            self, collection: str, where: dict[str, Any], *, strict: bool = False
        ) -> None:
            if site == "research" and collection == _real.RESEARCH_COLLECTION or site == "skills" and collection == SKILLS_COLLECTION:
                _fail()
            return super().delete_documents(collection, where, strict=strict)

        def delete_notion_docs(self, *, strict: bool = False) -> None:
            if site == "notion":
                _fail()
            return super().delete_notion_docs(strict=strict)

        def delete_attachment_docs(self, *, strict: bool = False) -> None:
            if site == "attachments":
                _fail()
            return super().delete_attachment_docs(strict=strict)

    monkeypatch.setattr(store_mod, "ChromaDBStore", _IoStore)
    return armed


@pytest.mark.parametrize(
    "site", ["research", "notion", "attachments", "skills"]
)
async def test_generic_cleanup_io_error_aborts_transition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    """A non-EF I/O failure mid-cleanup propagates like the EF refusal:
    the transition aborts, A is auto-recovered, and B is never published
    on top of surviving A rows."""
    env = _late_refusal_env(tmp_path, monkeypatch)
    _seed_live_company(env, "Acme Corp")
    await create_client_slot(env.settings, display_name="Acme Corp", source="current")
    await create_client_slot(env.settings, display_name="Beta Inc", source="blank")

    _io_failing_store(monkeypatch, site=site, once=True)
    with pytest.raises(OSError, match="synthetic I/O fault"):
        await activate_client_slot(env.settings, "beta_inc")

    # Recovery restored A on every layer — the failed B was not published.
    assert get_active_client(env.settings) == "acme_corp"
    assert _decision_summaries(env.db_path) == ["Acme Corp decision"]
    assert slots.get_restore_blocked(env.settings) is None


@pytest.mark.parametrize(
    "site", ["research", "notion", "attachments", "skills"]
)
async def test_generic_cleanup_io_error_persistent_fault_blocks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, site: str
) -> None:
    """Cleanup fails during BOTH the transition and the recovery →
    restore-blocked with the durable marker, never a silent publish."""
    env = _late_refusal_env(tmp_path, monkeypatch)
    _seed_live_company(env, "Acme Corp")
    await create_client_slot(env.settings, display_name="Acme Corp", source="current")
    await create_client_slot(env.settings, display_name="Beta Inc", source="blank")

    _io_failing_store(monkeypatch, site=site, once=False)
    with pytest.raises(ClientSlotError, match="restore-blocked"):
        await activate_client_slot(env.settings, "beta_inc")

    marker = slots.get_restore_blocked(env.settings)
    assert marker is not None and marker["restore_slug"] == "acme_corp"
    assert get_active_client(env.settings) is None


def test_delete_documents_strict_flag_controls_propagation() -> None:
    """Store-level contract: default stays best-effort (runtime callers),
    strict propagates ANY failure (transition callers), and the persisted
    schema refusal propagates in BOTH modes."""
    from openexecutive.knowledge.store import (
        ChromaDBStore,
        PersistedEmbeddingConfigError,
    )

    class _IoFailing(ChromaDBStore):
        def __init__(self) -> None:  # skip the chroma client entirely
            pass

        def _get_or_create_collection(self, name: str) -> Any:
            raise OSError("synthetic I/O fault")

    store = _IoFailing()
    store.delete_documents("c", {"k": "v"})  # tolerated + logged
    with pytest.raises(OSError, match="synthetic I/O fault"):
        store.delete_documents("c", {"k": "v"}, strict=True)

    class _Refusing(_IoFailing):
        def _get_or_create_collection(self, name: str) -> Any:
            raise PersistedEmbeddingConfigError("refused")

    with pytest.raises(PersistedEmbeddingConfigError):
        _Refusing().delete_documents("c", {"k": "v"})  # even non-strict
