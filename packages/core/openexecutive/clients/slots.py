"""Client-slot machinery: save/restore the full company context by name.

A slot directory (``company/_client_slots/<slug>/``) is a faithful save file
of one client company:

- ``state.db`` — transactionally-consistent copy of the shared SQLite DB
  (``VACUUM INTO`` on save, online backup API on restore). This carries chat
  history, scheduled actions, departments, people, watchlist — everything the
  stores keep.
- ``profile.yaml`` / ``docs/`` / ``skills/`` — the company directory artifacts.
- ``mcp_servers.json`` — per-client external MCP tools (e.g. one client's
  Crayon credentials). The MCP gateway reads this at process startup, so a
  changed config takes effect on the next restart.
- ``meta.json`` — display name + timestamps.

Slots deliberately reuse the fixture switcher's primitives and its
destructive-op lock (``_FIXTURE_OP_LOCK``): fixture loads, snapshots, resets,
and slot switches all mutate the same live state, so they must serialize
against each other.

Invariants:

- At most one slot is *active*; the ``.active_client`` sentinel records it.
- Only the active client is "live" — its scheduled actions fire, its docs are
  indexed. Parked clients sleep in their slot dirs.
- A fixture and a client slot are never active simultaneously: slot operations
  refuse while a demo fixture is loaded, and loading a fixture saves the
  active client back to its slot first (see ``cli/fixture_loader.py``).
- The ``generated_fixtures`` table is operator-level (the demo/fixture
  library), not client data — it is preserved verbatim across slot switches.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.cli.fixture_loader import (
    _FIXTURE_OP_LOCK,
    _SAFE_NAME_RE,
    get_fixture_status,
    snapshot_user_state,
)
from openexecutive.cli.fixture_loader import (
    PER_CLIENT_CACHE_TABLES as _PER_CLIENT_CACHE_TABLES,
)

logger = logging.getLogger(__name__)

# Honcho workspaces for client slots are stable (no uuid suffix) so a client's
# peer memory survives parking/reactivating. The prefix lets the fixture
# loader recognise them and never tear them down the way it does the
# sacrificial per-fixture demo workspaces.
CLIENT_WORKSPACE_PREFIX = "openexec-client-"

# Tables preserved verbatim across slot restores — operator-level state that
# does not belong to any one client company.
_GLOBAL_TABLES = ("generated_fixtures",)

# Engagement metadata lives in meta.json — deliberately OUTSIDE the swapped
# client state, so the practice layer (cockpit, renewal awareness) can see
# every engagement regardless of which client is active. Renewal reminders
# must never be modeled as scheduled_actions: those swap with the client and
# would vanish the moment the client is parked.
ENGAGEMENT_META_FIELDS: frozenset[str] = frozenset(
    {
        "role",              # what you are for this client (e.g. "Fractional CFO")
        "status",            # engagement stage — see ENGAGEMENT_STATUSES
        "engagement_start",  # ISO date
        "renewal_date",      # ISO date — next renewal/review checkpoint
        "retainer",          # free text, display only (billing stays external)
        "hours_per_week",    # number, display only
        "primary_contact",   # name of the client-side contact
        "notes",             # free-form engagement notes
    }
)

ENGAGEMENT_STATUSES = ("active", "paused", "winding_down", "completed")

# Per-client tables wiped when activating a *blank* slot (no state.db yet).
# Ordered children-before-parents for PRAGMA foreign_keys=ON. Existence-guarded
# at delete time, so stores that haven't initialized on this box are skipped.
_BLANK_WIPE_TABLES = (
    # Derived caches first — see PER_CLIENT_CACHE_TABLES for why they must be
    # wiped by every company-swapping path, not just this one. Neither declares
    # a foreign key, so head position is free.
    #
    # Do not assume person_insights' input_hash makes it self-guarding. The hash
    # (people.insights.build_insight_input_hash) covers role, is_principal,
    # status, awaiting_count, awaiting_reply_count, overdue, on_leave_until,
    # reachable_now, authority_scope, department_slugs, the hour-bucketed
    # soonest_sla_at / oldest_awaiting_reply_at / next_window_at /
    # last_contact_at, and the UTC day — every one of them a per-person signal,
    # and NOT the person's name, the person's id, or the company. The cache key
    # is person_id, a reused autoincrement PK. So an outgoing principal and a
    # freshly seeded incoming principal hash identically whenever those signals
    # coincide: both principals, same role string, no awaiting work, no leave,
    # same default departments, same UTC day. That is an ordinary steady state
    # for a seeded slot, not a collision.
    *_PER_CLIENT_CACHE_TABLES,
    "chat_messages",
    "sessions",
    "decisions",
    "initiatives",
    "advice_given",
    "scheduled_actions",
    "voice_personas",
    "alerts",
    "mute_topics",
    "user_preferences",
    "workflow_runs",
    "audit_log",
    "eval_runs",
    "external_signals",
    "watchlist",
    "watchlist_declines",
    "watchlist_policy_outcomes",
    "page_watch_state",
    "outbound_context",
    "audit_dedup",
    # User-authored workflow definitions live in the same DB (not in
    # workflow_runs) — without this a failed/incoming client's custom
    # workflows stay active under the next identity.
    "dynamic_workflows",
    # Legacy talent / staff-onboarding tables. Both features are gone and
    # nothing writes these any more, but the rows may still exist on upgraded
    # installs and they carry candidate PII (names, employers, screening
    # summaries, offer comp) — keep wiping them so a blank slot never inherits
    # a previous client's pipeline. Existence-guarded, so a no-op on fresh
    # installs where the tables were never created.
    "onboarding_tasks",
    "onboarding_plans",
    "onboarding_templates",
    "offers",
    "candidates",
    "engagements",
    "decision_instances",
    "decision_class_state",
    "agent_override_history",
    "agent_overrides",
    "review_annotations",
    "review_items",
    "person_authority_scope",
    "person_availability",
    "people",
    "department_goals",
    "departments",
    "departments_meta",
)


class ClientSlotError(ValueError):
    """Base error for slot operations (maps to HTTP 400)."""


class ClientSlotNotFoundError(ClientSlotError):
    """The named slot does not exist (maps to HTTP 404)."""


class ClientSlotConflictError(ClientSlotError):
    """The operation conflicts with current state (maps to HTTP 409)."""


def _clients_root(settings: Any) -> Path:
    """Slot storage lives inside the gitignored company dir, as a sibling of
    ``_user_backup`` — client data must never be committable."""
    return settings.company_profile_path.parent / "_client_slots"


def _active_client_sentinel(settings: Any) -> Path:
    return _clients_root(settings) / ".active_client"


def _slot_dir(settings: Any, slug: str) -> Path:
    return _clients_root(settings) / slug


def _episodic_db_path() -> Path:
    # Resolved lazily so tests can monkeypatch ``memory.episodic.DB_PATH``.
    from openexecutive.memory.episodic import DB_PATH

    return Path(str(DB_PATH))


_RESTORE_BLOCKED = ".restore_blocked"

# Process-local fallback for when the marker file itself cannot be written
# (the same disk/volume fault that broke the activation usually breaks this
# write too). Without it the instance would claim "restore-blocked" in its
# error while every gate read unblocked. Fail-closed for the process
# lifetime; cleared wherever the marker is cleared after a coherent restore.
_restore_blocked_local = False


def _restore_blocked_path(settings: Any) -> Path:
    return _clients_root(settings) / _RESTORE_BLOCKED


def _clear_restore_blocked(settings: Any) -> None:
    """Single choke point for lifting the block: marker file + local flag."""
    global _restore_blocked_local
    _restore_blocked_path(settings).unlink(missing_ok=True)
    _restore_blocked_local = False


def get_restore_blocked(settings: Any) -> dict[str, Any] | None:
    """The restore-blocked marker, or None when client switching is healthy.

    Written BEFORE the first live mutation of a client transition (phase
    ``transition_started``) as the durable proof that keeps a crashed or
    killed restore fenced across restarts, and refreshed when the
    automatic save-back-free recovery fails (phase ``recovery_failed``).
    While it exists the slot ops that read or overwrite state (save,
    create-from-current, park, delete, activate) refuse until the
    recorded restore target succeeds — and the API gate 503s normal
    traffic entirely. Fail closed — an unreadable marker still means
    blocked.
    """
    marker = _restore_blocked_path(settings)
    try:
        raw = marker.read_text(encoding="utf-8")
    except FileNotFoundError:
        if _restore_blocked_local:
            # The file could never be written, but this process knows a
            # recovery failed — fence anyway.
            return {"malformed": True, "volatile": True}
        return None
    except Exception:
        # Fail closed: an unreadable marker (perms, ELOOP) means "cannot
        # prove live state is coherent" — same as a malformed one.
        return {"malformed": True}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {"malformed": True}


def is_restore_blocked() -> bool:
    """Shared fail-CLOSED check for background gates (scheduler tick,
    resumer, API middleware). Resolves settings lazily; any error —
    including a settings/read failure — means "cannot prove live state is
    coherent", which is itself a block condition. A settings object that
    cannot even produce a real Path (test doubles) has no filesystem the
    marker could live on — outside the production contract, so it does not
    engage the fence."""
    try:
        from openexecutive.config import get_settings

        settings = get_settings()
        profile_path = getattr(settings, "company_profile_path", None)
        if not isinstance(profile_path, Path):
            return False
        return get_restore_blocked(settings) is not None
    except Exception:
        return True


def _require_not_restore_blocked(settings: Any, *, allow_slug: str | None = None) -> None:
    marker = get_restore_blocked(settings)
    if marker is None:
        return
    restore_slug = marker.get("restore_slug")
    if allow_slug is not None and restore_slug and allow_slug == restore_slug:
        if _slot_dir(settings, restore_slug).is_dir():
            return
        raise ClientSlotError(
            f"Client switching is restore-blocked and the recorded recovery "
            f"slot {restore_slug!r} no longer exists — restore the user "
            "backup via POST /fixtures/unload, or repair manually."
        )
    if restore_slug:
        raise ClientSlotError(
            "Client switching is restore-blocked: a failed activation left "
            "live state inconsistent and automatic recovery failed. Repair "
            "the vector store, then re-activate "
            f"{restore_slug!r} (or restore the user backup via "
            "POST /fixtures/unload)."
        )
    raise ClientSlotError(
        "Client switching is restore-blocked: restore the user backup via "
        "POST /fixtures/unload. If no user backup exists either, inspect "
        "live state manually and, only after verifying nothing worth "
        "keeping is live, delete "
        f"{_restore_blocked_path(settings)} to unblock (or, when the marker "
        "never made it to disk, restart the process to clear the "
        "in-process fence)."
    )


def _write_restore_marker_payload(
    *, failed_slug: str, target_slug: str | None, phase: str
) -> str:
    return json.dumps(
        {
            "failed_slug": failed_slug,
            "restore_slug": target_slug,
            "kind": "slot" if target_slug else "user_backup",
            "phase": phase,
            "at": datetime.now(UTC).isoformat(),
        }
    )


def _write_marker_file(marker: Path, payload: str) -> None:
    """Write the marker via a same-directory tmp file + rename — a torn
    write must never leave a half-written marker that loses
    ``restore_slug`` (a malformed marker still blocks, but degrades
    recovery to the user-backup path)."""
    tmp = marker.with_name(marker.name + ".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(marker)


def _write_transition_marker(
    settings: Any, *, failed_slug: str, target_slug: str | None
) -> None:
    """Durable proof of an in-progress client transition, written BEFORE
    the first live-state mutation. If the process dies mid-restore the
    marker survives and a restarted process fences the instance until the
    recorded target is restored. Refuses the transition outright when the
    marker cannot be persisted — no durable proof, no mutation."""
    marker = _restore_blocked_path(settings)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        _write_marker_file(
            marker,
            _write_restore_marker_payload(
                failed_slug=failed_slug,
                target_slug=target_slug,
                phase="transition_started",
            ),
        )
    except Exception as exc:
        raise ClientSlotError(
            "Cannot persist the client-transition marker — refusing to "
            "start the switch without durable proof of an incomplete "
            "transition (a write fault here leaves live state untouched; "
            "retry once the filesystem is healthy)."
        ) from exc


def _mark_restore_blocked(
    settings: Any, *, failed_slug: str, target_slug: str | None
) -> None:
    global _restore_blocked_local
    marker = _restore_blocked_path(settings)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        _write_marker_file(
            marker,
            _write_restore_marker_payload(
                failed_slug=failed_slug,
                target_slug=target_slug,
                phase="recovery_failed",
            ),
        )
        _restore_blocked_local = False
    except Exception:
        # The marker file is the whole containment — if it can't be written
        # the instance still must not serve mixed state. Fence this process
        # in memory and escalate loudly; the block then survives until a
        # verified recovery clears it via _clear_restore_blocked.
        _restore_blocked_local = True
        logger.critical(
            "client-slots: could not write restore-blocked marker %s — "
            "instance fenced in-process only; a restart loses the fence — "
            "take it down manually",
            marker,
        )


def get_active_client(settings: Any) -> str | None:
    """The active slot slug, or None when running single-company / fixture mode."""
    if get_restore_blocked(settings) is not None:
        # Under a restore block nothing may attribute live state to a client.
        return None
    sentinel = _active_client_sentinel(settings)
    if not sentinel.exists():
        return None
    try:
        content = sentinel.read_text(encoding="utf-8").strip()
    except Exception:
        return None
    # Same defence-in-depth as the fixture sentinel: garbage never reaches the UI.
    if content and _SAFE_NAME_RE.match(content):
        return content
    return None


def list_client_slots(settings: Any) -> list[dict[str, Any]]:
    """Summaries of all slots, newest-saved first."""
    root = _clients_root(settings)
    if not root.exists():
        return []

    out: list[dict[str, Any]] = []
    for slot in sorted(root.iterdir()):
        if not slot.is_dir() or not _SAFE_NAME_RE.match(slot.name):
            continue
        meta = _read_meta(slot)
        summary: dict[str, Any] = {
            "slug": slot.name,
            "display_name": meta.get("display_name") or slot.name,
            "created_at": meta.get("created_at"),
            "saved_at": meta.get("saved_at"),
            "origin": meta.get("origin"),
            **{field: meta.get(field) for field in ENGAGEMENT_META_FIELDS},
            "has_state": (slot / "state.db").exists(),
            "has_mcp_config": (slot / "mcp_servers.json").exists(),
            "doc_count": len(list((slot / "docs").glob("*")))
            if (slot / "docs").exists()
            else 0,
        }
        profile_path = slot / "profile.yaml"
        if profile_path.exists():
            try:
                from openexecutive.memory.company_profile import CompanyProfile

                profile = CompanyProfile.load_from_yaml(profile_path)
                summary["industry"] = profile.industry
                summary["stage"] = profile.stage
            except Exception:
                pass
        out.append(summary)

    out.sort(key=lambda s: s.get("saved_at") or "", reverse=True)
    return out


def _read_meta(slot: Path) -> dict[str, Any]:
    meta_path = slot / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_meta(slot: Path, **updates: Any) -> None:
    meta = _read_meta(slot)
    meta.update(updates)
    (slot / "meta.json").write_text(json.dumps(meta, indent=2, default=str), encoding="utf-8")


async def update_client_meta(
    settings: Any, slug: str, patch: dict[str, Any]
) -> dict[str, Any]:
    """Update a slot's engagement metadata (allowlisted fields only).

    Metadata edits are valid for any slot — active or parked — because
    meta.json is practice-level state, never swapped with the client. Takes
    the shared lock only to serialize the read-modify-write against a
    concurrent save-back touching the same meta.json.
    """
    unknown = set(patch) - ENGAGEMENT_META_FIELDS
    if unknown:
        raise ClientSlotError(
            f"Unknown metadata fields: {', '.join(sorted(unknown))}. "
            f"Allowed: {', '.join(sorted(ENGAGEMENT_META_FIELDS))}"
        )
    status = patch.get("status")
    if status is not None and status not in ENGAGEMENT_STATUSES:
        raise ClientSlotError(
            f"status must be one of: {', '.join(ENGAGEMENT_STATUSES)}"
        )
    if "display_name" in patch:  # defence in depth — not in the allowlist
        raise ClientSlotError("display_name cannot be changed here")

    async with _FIXTURE_OP_LOCK:
        slot = _require_slot(settings, slug)
        _write_meta(slot, **patch)
        meta = _read_meta(slot)
        return {
            "slug": slug,
            **{field: meta.get(field) for field in ENGAGEMENT_META_FIELDS},
        }


def derive_client_slug(display_name: str, settings: Any) -> str:
    """Filesystem-safe unique slug for a new slot (reuses the fixture slugifier)."""
    from openexecutive.fixtures.generator import derive_slug

    return derive_slug(display_name, lambda s: _slot_dir(settings, s).exists())


# ── Save: live state → slot dir ─────────────────────────────────────────────


def _save_slot_state(settings: Any, slot: Path) -> dict[str, Any]:
    """Write the full live company context into ``slot``. Caller holds the lock."""
    slot.mkdir(parents=True, exist_ok=True)
    company_dir: Path = settings.company_profile_path.parent

    # 1. profile.yaml — always write one so the slot stays loadable.
    from openexecutive.memory.company_profile import CompanyProfile

    if settings.company_profile_path.exists():
        shutil.copy2(settings.company_profile_path, slot / "profile.yaml")
    else:
        CompanyProfile().save_to_yaml(slot / "profile.yaml")

    # 2. docs/ + skills/ — full directory copies (all file types, unlike the
    #    fixture snapshot's *.md filter: slots are save files, not demo data).
    docs_copied = _replace_dir_copy(company_dir / "docs", slot / "docs")
    _replace_dir_copy(company_dir / "skills", slot / "skills")

    # 3. mcp_servers.json — per-client external tool config.
    mcp_src = Path(settings.mcp_servers_config_path)
    mcp_dst = slot / "mcp_servers.json"
    if mcp_src.exists():
        shutil.copy2(mcp_src, mcp_dst)
    else:
        mcp_dst.unlink(missing_ok=True)

    # 4. state.db — VACUUM INTO produces a transactionally-consistent copy
    #    even while other connections hold the live DB open.
    db_path = _episodic_db_path()
    state_dst = slot / "state.db"
    state_dst.unlink(missing_ok=True)
    db_saved = False
    if db_path.exists():
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute("VACUUM INTO ?", (str(state_dst),))
            db_saved = True
        finally:
            conn.close()

    _write_meta(slot, saved_at=datetime.now(UTC).isoformat())
    return {"docs_saved": docs_copied, "db_saved": db_saved}


def _replace_dir_copy(src: Path, dst: Path) -> int:
    """Replace ``dst`` with a copy of ``src``. Returns files copied (0 if no src)."""
    if dst.exists():
        shutil.rmtree(dst)
    if not src.exists():
        return 0
    shutil.copytree(src, dst)
    return sum(1 for p in dst.rglob("*") if p.is_file())


# ── Restore: slot dir → live state ───────────────────────────────────────────


async def _restore_slot_state(
    settings: Any,
    slot: Path,
    *,
    app_state: Any | None = None,
    store: Any | None = None,
) -> dict[str, Any]:
    """Make ``slot`` the live company context. Caller holds the lock.

    ``store`` may carry a preflighted ChromaDBStore — see below; passing one
    lets the caller distinguish a clean refusal (before any mutation) from
    a mid-restore failure needing recovery.
    """
    company_dir: Path = settings.company_profile_path.parent
    # "Blank" here means "no state.db yet" — true for both empty blank slots
    # and generated seed slots (which carry YAML/JSON seed files instead).
    # Once any slot has been saved back, state.db exists and wins.
    is_blank = not (slot / "state.db").exists()

    # 0. Preflight the vector store BEFORE the first live mutation. Building
    #    ChromaDBStore runs the persisted-schema guard; if the volume is
    #    refused (tampered schema, incompatible chromadb) raising here leaves
    #    the previous client's live state fully intact — DB, profile/docs,
    #    MCP config and the sentinel all still agree. Without this, the swaps
    #    below would already have run and the refusal would strand live
    #    state on the incoming client while get_active_client() still named
    #    the previous one. Callers that already preflighted (activation does
    #    its own, so it can tell "refused before touching anything" apart
    #    from "died mid-restore") pass the validated instance via ``store``.
    if store is None:
        from openexecutive.knowledge.store import ChromaDBStore

        store = ChromaDBStore(persist_directory=settings.vector_store_path)

    # 1. SQLite state — whole-DB restore (or factory wipe for blank slots),
    #    with operator-level tables carried across.
    preserved = _dump_global_tables()
    if is_blank:
        _wipe_per_client_tables()
    else:
        _restore_db_from_file(slot / "state.db")
    _ensure_schemas()
    _restore_global_tables(preserved)
    seeded: dict[str, Any] = {}
    if is_blank:
        # Generated (seed) slots carry people.yaml / departments.yaml /
        # memory.json from the intake draft — apply them with the fixture
        # loader's seeders (people first so memory + department heads can
        # resolve person ids by name), then layer the boot-time defaults on
        # top, skipping the default org when the draft supplied one.
        seeded = _seed_from_slot_files(settings, slot)
        _reseed_blank_defaults(
            seed_departments=not bool(seeded.get("departments_seeded"))
        )

    # 2. Company directory artifacts.
    from openexecutive.memory.company_profile import CompanyProfile

    profile_path = slot / "profile.yaml"
    profile = (
        CompanyProfile.load_from_yaml(profile_path)
        if profile_path.exists()
        else CompanyProfile()
    )
    profile.save_to_yaml(settings.company_profile_path)
    _replace_dir_copy(slot / "docs", company_dir / "docs")
    (company_dir / "docs").mkdir(parents=True, exist_ok=True)
    _replace_dir_copy(slot / "skills", company_dir / "skills")

    mcp_src = slot / "mcp_servers.json"
    mcp_live = Path(settings.mcp_servers_config_path)
    mcp_changed = mcp_src.exists() or mcp_live.exists()
    if mcp_src.exists():
        mcp_live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(mcp_src, mcp_live)
    else:
        mcp_live.unlink(missing_ok=True)

    # 3. Vector state — rebuild the company collections from the restored dirs.
    docs_indexed = await _rebuild_vector_state(settings, app_state, store=store)

    return {
        "display_name": profile.name,
        "profile": profile.model_dump(),
        "docs_indexed": docs_indexed,
        "blank": is_blank,
        # The gateway reads mcp_servers.json at process startup; flag so the
        # caller/UI can tell the operator a restart applies the new config.
        "mcp_config_changed": mcp_changed,
        **seeded,
    }


def _seed_from_slot_files(settings: Any, slot: Path) -> dict[str, Any]:
    """Apply a seed slot's people/memory/departments files to the live DB.

    A slot created from an engagement-intake bundle has no ``state.db`` yet —
    its org and history live in the same YAML/JSON artifacts curated fixtures
    use, so the fixture loader's seeders apply them verbatim. Plain blank
    slots have none of these files and this is a no-op. The files remain in
    the slot afterward as the engagement's birth record; once the first
    save-back writes ``state.db`` they are no longer consulted.
    """
    from openexecutive.cli.fixture_loader import (
        _seed_departments,
        _seed_episodic_memory,
        _seed_people,
    )

    out: dict[str, Any] = {}
    if (slot / "people.yaml").exists():
        out["people_seeded"] = _seed_people(slot / "people.yaml")
    if (slot / "memory.json").exists():
        out["memory_seeded"] = _seed_episodic_memory(slot / "memory.json", settings)
    if (slot / "departments.yaml").exists():
        out["departments_seeded"] = _seed_departments(slot / "departments.yaml")
    return out


def _restore_db_from_file(state_src: Path) -> None:
    """Replace the live DB's content with the slot copy via the backup API.

    The backup API writes through a destination *connection*, so the live
    file's inode never changes — connections other code holds stay valid
    (they just see the new content), unlike a file swap which would strand
    them on an orphaned inode.
    """
    db_path = _episodic_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(str(state_src))
    dst = sqlite3.connect(str(db_path), timeout=30.0)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


def _wipe_per_client_tables() -> None:
    """Factory-wipe per-client tables (blank-slot activation). Existence-guarded."""
    db_path = _episodic_db_path()
    if not db_path.exists():
        return
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        existing = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        if "people" in existing:
            # reports_to_person_id is a SELF-FK: a bulk DELETE can hit a
            # manager row while its subordinate still references it.
            cols = {
                row[1]
                for row in conn.execute("PRAGMA table_info(people)")
            }
            if "reports_to_person_id" in cols:
                conn.execute(
                    "UPDATE people SET reports_to_person_id = NULL"
                )
        for table in _BLANK_WIPE_TABLES:
            if table in existing:
                conn.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed allowlist
        conn.commit()
    finally:
        conn.close()


def _dump_global_tables() -> dict[str, tuple[list[str], list[tuple[Any, ...]]]]:
    """Read operator-level table rows from the live DB before a restore."""
    db_path = _episodic_db_path()
    out: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}
    if not db_path.exists():
        return out
    conn = sqlite3.connect(str(db_path))
    try:
        for table in _GLOBAL_TABLES:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            cursor = conn.execute(f"SELECT * FROM {table}")  # noqa: S608 — fixed allowlist
            cols = [d[0] for d in cursor.description]
            out[table] = (cols, cursor.fetchall())
    finally:
        conn.close()
    return out


def _restore_global_tables(
    preserved: dict[str, tuple[list[str], list[tuple[Any, ...]]]],
) -> None:
    """Write the operator-level rows back after the restore replaced the DB."""
    if not preserved:
        return
    db_path = _episodic_db_path()
    conn = sqlite3.connect(str(db_path))
    try:
        for table, (cols, rows) in preserved.items():
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            if not exists:
                continue
            conn.execute(f"DELETE FROM {table}")  # noqa: S608 — fixed allowlist
            if rows:
                placeholders = ",".join("?" for _ in cols)
                col_list = ",".join(cols)
                conn.executemany(
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})",  # noqa: S608
                    rows,
                )
        conn.commit()
    finally:
        conn.close()


def _ensure_schemas() -> None:
    """Re-run idempotent schema inits after a restore.

    A slot saved before a schema-adding deploy lacks the new tables; the
    lifespan inits only run at boot, so re-run them here (same set, same
    order — people before departments for FK ordering).
    """
    from openexecutive.agents.overrides import initialize_overrides_db
    from openexecutive.alerts.store import initialize_db as init_alerts
    from openexecutive.departments.store import initialize_db as init_departments
    from openexecutive.fixtures.store import initialize_db as init_fixtures
    from openexecutive.knowledge.review_store import ReviewStore
    from openexecutive.memory.episodic import cancel_orphaned_talent_reminders
    from openexecutive.memory.episodic import initialize_db as init_episodic
    from openexecutive.monitoring.store import initialize_db as init_monitoring
    from openexecutive.people.store import initialize_db as init_people

    # Pass the path explicitly everywhere: some initializers bind their
    # DB_PATH default at import time, which would ignore a runtime override.
    db_path = _episodic_db_path()
    init_episodic(db_path)
    # A parked slot's state.db may predate the talent removal; give it the
    # same one-shot reminder sweep the live DB gets at boot.
    try:
        swept = cancel_orphaned_talent_reminders(db_path)
    except Exception:
        logger.exception("client-slots: orphaned-reminder sweep failed")
    else:
        if swept:
            logger.info("client-slots: cancelled %d orphaned talent reminder(s)", swept)
    init_alerts(db_path)
    init_fixtures(db_path)
    initialize_overrides_db(db_path)
    init_people(db_path)
    init_departments(db_path)
    init_monitoring(db_path)
    ReviewStore.initialize_db(db_path)
    # Register shipped knowledge as trusted defaults too — without this a
    # freshly activated slot has an empty review_items table until the next
    # process restart, so its knowledge page and review stats read as empty.
    ReviewStore.sync_builtin_registrations(db_path)
    # External sources as well, mirroring the lifespan. A slot captured from a
    # pre-existing database carries `external:*` rows; the backfill below only
    # promotes rows a sync has flagged as shipped, so skipping this would leave
    # them `pending` — and pending is now withheld from retrieval.
    try:
        from openexecutive.knowledge.external_sources import load_manifest

        ingested = [
            {"id": src.id, "domains": src.domains}
            for src in load_manifest()
            if src.cache_dir.exists() and any(src.cache_dir.iterdir())
        ]
        if ingested:
            ReviewStore.sync_external_registrations(ingested, db_path)
    except Exception:
        logger.exception("slot schema init: sync_external_registrations failed")
    # Must follow both syncs: it only promotes rows they have flagged.
    ReviewStore.backfill_trusted_defaults(db_path)


def _reseed_blank_defaults(*, seed_departments: bool = True) -> None:
    """Blank slot = factory state: default org + the boot-time scheduled rows.

    Mirrors ``reset_all_state`` step 5/5a — without these the new client's
    Today page stays blank until the next process restart. Every call is
    idempotent and individually guarded. ``seed_departments=False`` skips the
    default 8-department org (used when a generated seed slot supplied its
    own departments — the cadence/brief bootstraps still run and pick those
    up from the table).
    """
    from openexecutive.config import get_settings
    from openexecutive.departments.store import seed_default_departments

    if seed_departments:
        try:
            seed_default_departments()
        except Exception:
            logger.exception("client-slots: seed_default_departments failed")
    try:
        from openexecutive.scheduler.runner import seed_principal_briefs

        seed_principal_briefs()
    except Exception:
        logger.exception("client-slots: seed_principal_briefs failed")
    try:
        from openexecutive.departments.cadence import bootstrap_cadences

        bootstrap_cadences()
    except Exception:
        logger.exception("client-slots: bootstrap_cadences failed")
    settings = get_settings()
    if settings.nudge_scan_enabled:
        try:
            from openexecutive.scheduler.nudge_engine import bootstrap_nudge_scan

            bootstrap_nudge_scan()
        except Exception:
            logger.exception("client-slots: bootstrap_nudge_scan failed")
    if settings.external_monitor_enabled:
        try:
            from openexecutive.monitoring.pipeline import (
                bootstrap_external_monitor_scan,
            )

            bootstrap_external_monitor_scan()
        except Exception:
            logger.exception("client-slots: bootstrap_external_monitor_scan failed")
    if settings.watchlist_research_enabled:
        try:
            from openexecutive.monitoring.research.scheduler import (
                bootstrap_watchlist_research_scan,
            )

            bootstrap_watchlist_research_scan()
        except Exception:
            logger.exception(
                "client-slots: bootstrap_watchlist_research_scan failed"
            )


async def _rebuild_vector_state(
    settings: Any, app_state: Any | None, *, store: Any | None = None
) -> int:
    """Rebuild ChromaDB company collections from the restored live dirs.

    ``store`` may be supplied by a caller that already constructed (and
    therefore guard-validated) one; a fresh instance is built otherwise.

    Returns the number of company-doc chunks indexed. Isolated here so tests
    can stub the vector layer without touching the file/DB round-trip logic.
    """
    from openexecutive.knowledge.loader import ingest_file
    from openexecutive.knowledge.skills_index import SKILLS_COLLECTION, index_skill
    from openexecutive.knowledge.skills_repo import list_skills
    from openexecutive.knowledge.store import ChromaDBStore

    if store is None:
        store = ChromaDBStore(persist_directory=settings.vector_store_path)
    store.delete_company_docs()
    # Per-company research artifacts never carry across companies.
    store.delete_documents(
        collection=ChromaDBStore.RESEARCH_COLLECTION,
        where={"type": "recent_research"},
    )
    store.delete_notion_docs()
    # Inbound attachments are per-company too, and no longer swept by
    # delete_company_docs above now that they live in their own collection.
    store.delete_attachment_docs()
    from openexecutive.knowledge.notion_sync import reset_local_state

    reset_local_state(profile_path=settings.company_profile_path)

    company_docs_dir: Path = settings.company_profile_path.parent / "docs"
    docs_indexed = 0
    for doc in sorted(company_docs_dir.glob("*.md")):
        # Never let one unreadable doc abort the restore. This runs AFTER the DB
        # and company dir have been swapped but BEFORE the active-client
        # sentinel is written, so raising here strands live state belonging to
        # the incoming client while get_active_client() still reports None —
        # which makes park_active_client a no-op and lets a later
        # create(source="current") capture that client's data as the operator's
        # own. A doc that fails to index is a degraded search index; losing the
        # sentinel is data attribution loss. Mirrors the skills loop below.
        try:
            docs_indexed += await ingest_file(
                path=doc, store=store, collection=ChromaDBStore.COMPANY_COLLECTION
            )
        except Exception:
            logger.exception("client-slots: reindex company doc failed: %s", doc.name)

    # Company-authored skills: drop the old client's rows, index the restored set.
    store.delete_documents(collection=SKILLS_COLLECTION, where={"source": "company"})
    for skill in list_skills():
        if skill.source == "company":
            try:
                index_skill(skill, store)
            except Exception:
                logger.exception("client-slots: reindex skill failed")

    if app_state is not None and hasattr(app_state, "store"):
        app_state.store = store
    return docs_indexed


def _set_honcho_client_workspace(slug: str) -> str | None:
    """Point Honcho at the client's stable workspace. Best-effort.

    Stable (no uuid) so the client's peer memory survives parking. The fixture
    loader's teardown paths skip ``CLIENT_WORKSPACE_PREFIX`` workspaces, so
    this memory is durable until the slot is deleted.
    """
    try:
        from openexecutive.memory.honcho_client import set_active_workspace_id

        workspace = f"{CLIENT_WORKSPACE_PREFIX}{slug}"
        set_active_workspace_id(workspace)
        return workspace
    except Exception:
        logger.exception("client-slots: honcho workspace switch failed")
        return None


def _write_generated_slot(
    slot: Path, bundle: Any, intake_description: str
) -> None:
    """Materialize a validated intake bundle into ``slot`` as a seed slot.

    profile.yaml + docs/ are read by the normal restore path;
    people.yaml / departments.yaml / memory.json are applied by
    ``_seed_from_slot_files`` on first activation (no ``state.db`` yet).
    """
    from openexecutive.fixtures.generator import (
        bundle_to_serialized,
        materialize_to_dir,
    )

    serialized = bundle_to_serialized(bundle, intake_description)
    materialize_to_dir(serialized, slot)
    _write_meta(
        slot,
        origin="generated",
        intake_description=intake_description,
        doc_count=serialized.get("doc_count", 0),
    )


def park_active_client(settings: Any) -> str | None:
    """Save the active client to its slot and leave client mode.

    Used by the fixture switcher before it replaces live state (load and
    unload both call this) so demo flows can never destroy client work.
    Returns the parked slug, or None when no client was active. The caller
    MUST already hold ``_FIXTURE_OP_LOCK`` — this helper deliberately takes
    no lock so it can run inside the fixture loader's critical sections.
    """
    active = get_active_client(settings)
    if active is None:
        return None
    slot = _slot_dir(settings, active)
    if slot.is_dir():
        _save_slot_state(settings, slot)
    _active_client_sentinel(settings).unlink(missing_ok=True)
    logger.info("client-slots: parked active client %r to its slot", active)
    return active


# ── Public operations (each holds the shared destructive-op lock) ───────────


async def _recover_failed_activation(
    settings: Any, target_slug: str | None, *, app_state: Any | None = None
) -> bool:
    """Save-back-free restore of the coherent previous state after a failed
    activation. Returns True only when live state provably matches
    ``target_slug`` (or the user backup when ``target_slug`` is None).

    This is the product-side equivalent of rotation's ``_force_restore``:
    the caller must already hold ``_FIXTURE_OP_LOCK``. Every failure —
    including a still-refused vector store — returns False so the caller
    can drop into the restore-blocked state instead of leaving partial
    data servable under a wrong identity.
    """
    try:
        sentinel = _active_client_sentinel(settings)
        if target_slug is not None:
            target_slot = _slot_dir(settings, target_slug)
            if not target_slot.is_dir():
                raise ClientSlotNotFoundError(
                    f"recovery target slot {target_slug!r} is gone"
                )
            await _restore_slot_state(settings, target_slot, app_state=app_state)
            sentinel.parent.mkdir(parents=True, exist_ok=True)
            sentinel.write_text(target_slug, encoding="utf-8")
            _set_honcho_client_workspace(target_slug)
        else:
            # No previous client — the user's own company lives in
            # _user_backup (the same restore point POST /fixtures/unload
            # uses). That format never carried the DB-resident per-client
            # state, MCP config or skills — wipe them first, or the failed
            # client's chat/workflow/audit rows and MCP credentials stay
            # live under the user's identity.
            from openexecutive.cli.fixture_loader import (
                _apply_state_from_source,
                _discard_state_not_in_backup,
            )

            backup = settings.company_profile_path.parent / "_user_backup"
            if not (backup / "profile.yaml").exists():
                raise ClientSlotError(
                    "no user backup exists to recover the live state"
                )
            _discard_state_not_in_backup()
            await _apply_state_from_source(
                backup, settings, strict_per_company=True
            )
            sentinel.unlink(missing_ok=True)
            try:
                # Mirror unload_fixture: no client is active after a backup
                # restore, so drop any client-scoped Honcho workspace
                # override — otherwise post-recovery user turns keep
                # syncing into the failed client's workspace.
                from openexecutive.memory.honcho_client import (
                    clear_active_workspace_id,
                )

                clear_active_workspace_id()
            except Exception:
                logger.exception(
                    "client-slots: post-recovery honcho workspace clear failed"
                )
        logger.info(
            "client-slots: recovered previous state after failed activation "
            "(target=%r)",
            target_slug,
        )
        return True
    except Exception:
        logger.exception(
            "client-slots: automatic recovery after failed activation failed "
            "(target=%r)",
            target_slug,
        )
        return False


def _require_no_fixture(settings: Any) -> None:
    active_fixture = get_fixture_status(settings).get("active_fixture")
    if active_fixture:
        raise ClientSlotConflictError(
            f"Demo fixture {active_fixture!r} is active — live state is fixture "
            "data, not client data. Unload it (POST /fixtures/unload) first."
        )


def _require_slot(settings: Any, slug: str) -> Path:
    if not _SAFE_NAME_RE.match(slug):
        raise ClientSlotError("Invalid client slug")
    slot = _slot_dir(settings, slug)
    if not slot.is_dir() or not (slot / "meta.json").exists():
        raise ClientSlotNotFoundError(f"Client {slug!r} not found")
    return slot


async def create_client_slot(
    settings: Any,
    *,
    display_name: str,
    slug: str | None = None,
    source: str = "current",
    bundle: dict[str, Any] | None = None,
    intake_description: str = "",
) -> dict[str, Any]:
    """Create a slot from the live state, empty, or an intake-generated bundle.

    ``source="current"`` captures the live company into the new slot and marks
    it active — this is how a single-company install enters client mode, and
    it guarantees every later switch has a save-back target. It is refused
    while another client is active (the live state already belongs to that
    slot; use blank + activate instead).

    ``source="blank"`` creates an empty-but-loadable slot to onboard fresh.

    ``source="generated"`` writes a *seed slot* from an engagement-intake
    ``bundle`` (the ``FixtureBundle`` shape from ``/clients/generate``):
    profile + docs land as slot artifacts, and people/departments/memory land
    as the same YAML/JSON seed files curated fixtures use — applied to the
    live DB on first activation. Live state is untouched and the slot is NOT
    activated.
    """
    display_name = (display_name or "").strip()
    if not display_name:
        raise ClientSlotError("display_name is required")
    if source not in ("current", "blank", "generated"):
        raise ClientSlotError("source must be 'current', 'blank', or 'generated'")
    if source == "generated" and not bundle:
        raise ClientSlotError("source='generated' requires a bundle")

    async with _FIXTURE_OP_LOCK:
        # Inside the lock: a block marker can land between an outside
        # check and lock acquisition, and source="current" would then
        # capture half-swapped live state into a fresh slot.
        _require_not_restore_blocked(settings)
        _require_no_fixture(settings)

        if slug is not None and not _SAFE_NAME_RE.match(slug):
            raise ClientSlotError("Invalid client slug")
        slug = slug or derive_client_slug(display_name, settings)
        slot = _slot_dir(settings, slug)
        if slot.exists():
            raise ClientSlotConflictError(f"Client {slug!r} already exists")

        if source == "current" and get_active_client(settings) is not None:
            raise ClientSlotConflictError(
                "A client is already active — its live state belongs to that "
                "slot. Create a blank client and activate it instead."
            )

        # Parse + validate a generated bundle BEFORE creating the slot dir so
        # a rejected draft never leaves an empty husk behind.
        parsed_bundle = None
        if source == "generated":
            from openexecutive.fixtures.generator import (
                FixtureBundle,
                validate_bundle,
            )

            try:
                parsed_bundle = FixtureBundle.model_validate(bundle)
            except Exception as exc:
                raise ClientSlotError(f"Invalid bundle: {exc}") from exc
            errors = validate_bundle(parsed_bundle)
            if errors:
                raise ClientSlotError("Invalid bundle: " + "; ".join(errors))

        slot.mkdir(parents=True, exist_ok=True)
        _write_meta(
            slot,
            slug=slug,
            display_name=display_name,
            created_at=datetime.now(UTC).isoformat(),
            saved_at=None,
        )

        if source == "current":
            # Preserve the user's original company in _user_backup before
            # entering client mode, so POST /fixtures/unload remains a working
            # "exit client mode" path (the slot holds the same data, but
            # unload restores from _user_backup specifically).
            if not (
                settings.company_profile_path.parent / "_user_backup" / "profile.yaml"
            ).exists():
                try:
                    snapshot_user_state(settings)
                except Exception:
                    logger.exception(
                        "client-slots: pre-create user snapshot failed"
                    )
            saved = _save_slot_state(settings, slot)
            _active_client_sentinel(settings).write_text(slug, encoding="utf-8")
            _set_honcho_client_workspace(slug)
            return {"slug": slug, "display_name": display_name, "active": True, **saved}

        if source == "generated":
            assert parsed_bundle is not None  # guaranteed by the gate above
            try:
                _write_generated_slot(slot, parsed_bundle, intake_description)
            except Exception as exc:
                # Never leave a half-written husk: the no-husk guarantee must
                # also hold for failures AFTER mkdir (serialization, disk).
                shutil.rmtree(slot, ignore_errors=True)
                raise ClientSlotError(
                    f"Failed to write client slot: {exc}"
                ) from exc
            return {
                "slug": slug,
                "display_name": display_name,
                "active": False,
                "origin": "generated",
                "people": len(parsed_bundle.people),
                "departments": len(parsed_bundle.departments),
                "docs": len(parsed_bundle.docs),
            }

        # Blank: an empty-but-loadable bundle. Live state is untouched.
        from openexecutive.memory.company_profile import CompanyProfile

        CompanyProfile(name=display_name).save_to_yaml(slot / "profile.yaml")
        (slot / "docs").mkdir(exist_ok=True)
        return {"slug": slug, "display_name": display_name, "active": False}


async def save_active_client(settings: Any) -> dict[str, Any]:
    """Checkpoint the live state into the active slot without switching."""
    async with _FIXTURE_OP_LOCK:
        _require_no_fixture(settings)
        _require_not_restore_blocked(settings)
        active = get_active_client(settings)
        if active is None:
            raise ClientSlotConflictError(
                "No client is active — nothing to save. Create one with "
                "source='current' to capture the live company."
            )
        slot = _require_slot(settings, active)
        saved = _save_slot_state(settings, slot)
        return {"slug": active, "saved": True, **saved}


async def activate_client_slot(
    settings: Any, slug: str, *, app_state: Any | None = None
) -> dict[str, Any]:
    """Switch the live company context to ``slug``, saving the current one back.

    With an active client: save it to its slot, then restore the target. With
    no active client (first activation from single-company mode): the live
    state is the user's original company — auto-snapshot it to ``_user_backup``
    (same restore point the fixture switcher uses; ``POST /fixtures/unload``
    brings it back) before it is replaced.
    """
    async with _FIXTURE_OP_LOCK:
        _require_no_fixture(settings)

        # Under a restore block only the recorded recovery target may be
        # activated — anything else could strand this instance deeper. Check
        # BEFORE _require_slot so a deleted recovery slot surfaces the
        # blocked-remediation error, not a misleading 404.
        blocked = get_restore_blocked(settings)
        if blocked is not None:
            _require_not_restore_blocked(settings, allow_slug=slug)
        slot = _require_slot(settings, slug)

        active = get_active_client(settings)
        if active == slug:
            return {"slug": slug, "already_active": True}
        # When blocked, the sentinel was quarantined and `active` reads None;
        # the coherent recovery target comes from the marker, not the lock
        # state, so a second failure must still aim at the same slot.
        recovery_target = active if blocked is None else blocked.get("restore_slug")

        if active is not None:
            try:
                previous_slot = _require_slot(settings, active)
            except ClientSlotNotFoundError:
                # Sentinel points at a deleted dir — nothing to save into.
                logger.warning(
                    "client-slots: active sentinel %r has no slot dir; skipping save-back",
                    active,
                )
            else:
                _save_slot_state(settings, previous_slot)
        elif blocked is None and not (
            settings.company_profile_path.parent / "_user_backup" / "profile.yaml"
        ).exists():
            # Mirror the fixture switcher's first-load behavior: preserve the
            # user's original company before replacing it. Best-effort — an
            # empty environment has nothing worth snapshotting.
            try:
                snapshot_user_state(settings)
            except Exception:
                logger.exception(
                    "client-slots: pre-activation user snapshot failed"
                )

        # Preflight BEFORE _restore_slot_state: a refusal at this point means
        # zero live mutations — plain propagation, nothing to recover.
        from openexecutive.knowledge.store import ChromaDBStore

        store = ChromaDBStore(persist_directory=settings.vector_store_path)
        if blocked is None:
            # Durable proof BEFORE the first live mutation: a crash or
            # killed process mid-restore leaves this marker on disk and a
            # restarted instance stays fenced until the recorded target is
            # restored. If the marker cannot be persisted the activation
            # aborts here — live state untouched. A recovery activation
            # (blocked is not None) already has its durable marker.
            _write_transition_marker(
                settings, failed_slug=slug, target_slug=recovery_target
            )
        try:
            summary = await _restore_slot_state(
                settings, slot, app_state=app_state, store=store
            )
        except BaseException as exc:
            # A late refusal has already swapped DB/profile/docs while the
            # sentinel still names the previous client — the request must not
            # end serving B's data under A's identity. The marker is written
            # BEFORE recovery runs: the restore takes seconds and without the
            # fence up the whole window would serve mixed state. BaseException
            # (not Exception): a task cancellation mid-restore must not skip
            # containment either.
            _mark_restore_blocked(
                settings,
                failed_slug=(blocked or {}).get("failed_slug") or slug,
                target_slug=recovery_target,
            )
            # Keep a handle on the recovery task and await it to completion
            # UNDER THE LOCK even if this task gets cancelled while waiting.
            # Releasing the lock early would let a retry/restore run a
            # second concurrent writer against the still-mutating live
            # state — the incoherence this block exists to fence.
            inner = asyncio.ensure_future(
                _recover_failed_activation(
                    settings, recovery_target, app_state=app_state
                )
            )
            cancelled = False
            while True:
                try:
                    recovered = await asyncio.shield(inner)
                    break
                except asyncio.CancelledError:
                    # Cancellation interrupted the WAIT, not the work —
                    # the shielded inner task is still restoring. Keep
                    # waiting until it finishes before judging the result.
                    cancelled = True
                    if inner.done():
                        try:
                            recovered = inner.result()
                        except BaseException:
                            recovered = False
                        break
            if recovered:
                # The marker fenced the recovery window; recovery verified
                # coherent live state, so the fence comes down again.
                _clear_restore_blocked(settings)
                if cancelled:
                    raise asyncio.CancelledError() from exc
                raise
            sentinel = _active_client_sentinel(settings)
            if sentinel.exists():
                # Quarantine, never delete: the file is incident
                # forensics. Nothing reads it again — successful
                # recovery rewrites .active_client — so it can be
                # archived or removed with the incident record.
                sentinel.rename(
                    sentinel.with_name(".active_client.refused")
                )
            if cancelled:
                # Containment is now durable; deliver the cancellation the
                # caller asked for rather than swallowing it.
                raise asyncio.CancelledError() from exc
            if isinstance(
                exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)
            ):
                raise
            raise ClientSlotError(
                f"Activation of {slug!r} failed and automatic recovery "
                "failed — the instance is restore-blocked until the "
                "vector store is repaired and the recorded client is "
                "re-activated (or the user backup is restored via "
                "POST /fixtures/unload)."
            ) from exc
        # Sentinel BEFORE the marker unlink: a crash between them otherwise
        # leaves the instance unblocked with restored-B live state and no
        # active_client at all. Remove any stale quarantined sentinel too.
        _active_client_sentinel(settings).parent.mkdir(parents=True, exist_ok=True)
        _active_client_sentinel(settings).write_text(slug, encoding="utf-8")
        _active_client_sentinel(settings).with_name(
            ".active_client.refused"
        ).unlink(missing_ok=True)
        _clear_restore_blocked(settings)
        workspace = _set_honcho_client_workspace(slug)
        if workspace:
            summary["honcho_workspace"] = workspace

        # Scheduled rows swap with the client, so the nightly rotation row
        # must be re-seeded into whichever DB just went live (idempotent;
        # no-op unless CLIENT_ROTATION_ENABLED).
        try:
            from openexecutive.clients.rotation import seed_client_rotation

            seed_client_rotation()
        except Exception:
            logger.exception("client-slots: rotation re-seed failed")

        summary["slug"] = slug
        summary["previous"] = active
        return summary


async def delete_client_slot(settings: Any, slug: str) -> dict[str, Any]:
    """Delete a parked slot. Refuses the active one (switch away first)."""
    async with _FIXTURE_OP_LOCK:
        _require_not_restore_blocked(settings)
        from openexecutive.clients.rotation import rotation_in_progress

        if rotation_in_progress(settings):
            # Mid-rotation, "parked" is a moving target — and deleting the
            # rotation's original client would strand its restore.
            raise ClientSlotConflictError(
                "Overnight rotation is in progress — try again when it "
                "finishes (a minute or two)."
            )
        slot = _require_slot(settings, slug)
        if get_active_client(settings) == slug:
            raise ClientSlotConflictError(
                "This client is currently active — activate another client "
                "(or restore your company via POST /fixtures/unload) before deleting."
            )
        shutil.rmtree(slot)
        return {"deleted": True, "slug": slug}
