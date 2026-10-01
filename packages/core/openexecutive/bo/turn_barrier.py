"""Durable turn/switch barrier — RA13-A02-01.

The per-client journal (``episodic_memory.db``) is *swapped* on client
activation: ``_save_slot_state`` VACUUMs it into the slot and the restore
replaces it wholesale. A turn admitted under journal A that is still in
flight when journal B goes live loses its evidence — the attempt row,
dedup markers and close record stay in A's parked snapshot while B has
no record the external effect may already have happened. Re-processing
the same provider message under B then produces a SECOND effect.

This module is the durable coordination layer that closes that window.
Its tables live in ``bo_agents.db`` (``BOAGENTS_DB_PATH``) — the BOAgents
slice's own database, which is deliberately *not* part of the per-client
snapshot: ``state.db`` only captures the episodic DB, and neither
``reset_all_state`` nor the fixture loader wipes ``bo_agents.db``. The
barrier therefore survives journal swaps, restores and process restarts.

Protocol (every mutation is a short ``BEGIN IMMEDIATE`` transaction —
SQLite serialises writers on the file lock, so admission and switch
start are atomic across OS processes; no in-memory lock is load
bearing):

- ``admit_turn`` inserts a lease row under the current epoch. It
  refuses (or waits a bounded time) while a switch is in progress.
- ``begin_switch`` refuses while any lease is ``uncertain``, waits a
  bounded deadline for ``active`` leases to close, then sets
  ``switch_in_progress`` and bumps ``epoch`` in the same transaction.
- The worker revalidates (``turn_still_valid``) at the effect
  boundary: same lease row, still ``active``, epoch unchanged.
- A lease expiring (crash, vanished owner) is moved to ``uncertain`` —
  never to "absent". An expired lease is *not* proof the effect didn't
  happen; it blocks switching until an operator reconciles it via
  ``resolve_turn`` (positive journal evidence or explicit attestation).

The safety control cannot be disabled; operational bounds are Settings
values (``bo.turns.lease_seconds``, ``bo.switch.max_wait_seconds``).
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.bo import db as bo_db

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS bo_switch_state (
    id           INTEGER PRIMARY KEY CHECK (id = 1),
    epoch        INTEGER NOT NULL,
    in_progress  INTEGER NOT NULL DEFAULT 0,
    op_id        TEXT,
    op           TEXT,
    owner        TEXT,
    started_at   TEXT,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bo_turn_leases (
    turn_id          TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,
    ref              TEXT,
    tenant           TEXT NOT NULL DEFAULT '',
    client_slug      TEXT NOT NULL DEFAULT '',
    scope_token      TEXT,
    mailbox          TEXT,
    owner            TEXT NOT NULL,
    epoch            INTEGER NOT NULL,
    status           TEXT NOT NULL DEFAULT 'active',
    resolution       TEXT,
    reason           TEXT,
    lease_expires_at TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bo_turn_leases_status
    ON bo_turn_leases (status);
CREATE INDEX IF NOT EXISTS bo_turn_leases_ref
    ON bo_turn_leases (kind, ref);
CREATE TABLE IF NOT EXISTS bo_control_audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action      TEXT NOT NULL,
    turn_id     TEXT,
    actor       TEXT NOT NULL,
    resolution  TEXT,
    reason      TEXT,
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS bo_control_audit_turn
    ON bo_control_audit (turn_id);
"""


class SwitchBusyError(Exception):
    """A client switch is in progress — the turn must not be admitted."""

    def __init__(self, message: str, *, op_id: str | None = None) -> None:
        super().__init__(message)
        self.op_id = op_id


class SwitchRefusedError(Exception):
    """A switch cannot start: open or uncertain turns block it.

    Carries the blocking lease ids + reasons so the API layer can surface
    them to the operator (operation in progress or uncertain, with IDs).
    """

    def __init__(
        self, message: str, *, blockers: list[dict[str, Any]]
    ) -> None:
        super().__init__(message)
        self.blockers = blockers

    def describe(self) -> str:
        """Human-readable refusal: operation state, reason, turn ids —
        what the admin surface (409 detail / turn-blockers list) needs."""
        blk = "; ".join(
            f"{b.get('turn_id')}:{b.get('status')}"
            + (f"({b.get('reason')})" if b.get("reason") else "")
            for b in self.blockers
        )
        return f"{self} Blocked by turn(s): {blk or 'unavailable'}"


class TurnAdmissionError(Exception):
    """Admission failed for a non-switch reason (coordination store down)."""


class ReconcileRefusedError(Exception):
    """The lease is not reconcilable in its current state (e.g. still
    ``active`` — its owner may yet complete it)."""


@dataclass(frozen=True)
class TurnLease:
    """Handle returned by ``admit_turn`` — proof a turn was registered."""

    turn_id: str
    kind: str
    ref: str | None
    client_slug: str
    epoch: int
    owner: str


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _db_path(db_path: Path | None = None) -> Path:
    return db_path if db_path is not None else bo_db.DB_PATH


@contextmanager
def _conn(db_path: Path | None = None) -> Generator[sqlite3.Connection, None, None]:
    """Autocommit-mode connection for barrier transactions.

    ``isolation_level=None`` is deliberate: the module manages
    transactions explicitly (``BEGIN IMMEDIATE`` … ``COMMIT``). Under
    sqlite3's legacy implicit-transaction mode ``executescript`` — used
    by ``_ensure_schema`` — silently COMMITs a pending transaction, so
    schema setup inside a write txn would drop the RESERVED lock the
    check-and-set protocol relies on. Here the schema runs in autocommit
    and the critical section then holds a *real* IMMEDIATE transaction.
    """
    conn = sqlite3.connect(str(_db_path(db_path)), isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    # Additive migration for databases created before the mailbox column:
    # CREATE TABLE IF NOT EXISTS won't add it to an existing table.
    cols = {
        r[1]
        for r in conn.execute(
            "PRAGMA table_info(bo_turn_leases)"
        ).fetchall()
    }
    if cols and "mailbox" not in cols:
        conn.execute(
            "ALTER TABLE bo_turn_leases ADD COLUMN mailbox TEXT"
        )
    conn.execute(
        "INSERT OR IGNORE INTO bo_switch_state "
        "(id, epoch, in_progress, updated_at) VALUES (1, 0, 0, ?)",
        (_iso(_now()),),
    )


def initialize_db(db_path: Path | None = None) -> None:
    """Additive migration — idempotent, safe on every boot."""
    with _conn(db_path) as conn:
        _ensure_schema(conn)


def _flag_owner_dead(owner: Any) -> bool:
    """True when the recorded switch owner can no longer be alive.

    Owners are ``"<pid>:<nonce>"``. A dead pid can never finish the
    operation its flag fences — the flag is stale and may be swept. An
    unparseable owner is treated as dead: the flag is written only by
    this module, so a malformed value is never a live claim."""
    if not isinstance(owner, str) or ":" not in owner:
        return True
    try:
        pid = int(owner.split(":", 1)[0])
    except ValueError:
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    except OSError:
        return True
    return False


def _sweep_dead_flag(conn: sqlite3.Connection) -> None:
    """Clear an ``in_progress`` flag whose owner process no longer exists.

    Runs inside the caller's transaction. A crash mid-switch leaves the
    flag set with a dead owner; sweeping restores admissions while the
    half-swapped journal state stays fenced by the transition marker.
    A flag held by a *live* owner is never touched."""
    row = conn.execute(
        "SELECT in_progress, owner FROM bo_switch_state WHERE id=1"
    ).fetchone()
    if row and row["in_progress"] and _flag_owner_dead(row["owner"]):
        conn.execute(
            "UPDATE bo_switch_state SET in_progress=0, op_id=NULL, "
            "op=NULL, owner=NULL, started_at=NULL, updated_at=? "
            "WHERE id=1 AND in_progress=1",
            (_iso(_now()),),
        )


def _lease_seconds() -> int:
    return _setting("bo.turns.lease_seconds", 900)


def _switch_wait_seconds() -> int:
    return _setting("bo.switch.max_wait_seconds", 20)


def _setting(key: str, default: int) -> int:
    try:
        from openexecutive.bo.identity import configured_tenant
        from openexecutive.bo.settings import store as settings_store

        return int(settings_store.get_effective_value(configured_tenant(), key))
    except Exception:
        return default


def _active_client_slug() -> str:
    try:
        from openexecutive.clients.slots import get_active_client
        from openexecutive.config import get_settings

        return get_active_client(get_settings()) or ""
    except Exception:
        return ""


def _tenant() -> str:
    try:
        from openexecutive.bo.identity import configured_tenant

        return configured_tenant()
    except Exception:
        return ""


def _sweep_expired(conn: sqlite3.Connection) -> None:
    """Expired active lease → 'uncertain' (crash/vanished owner).

    Never delete, never mark resolved: an expired lease is not evidence
    the effect didn't happen — it must be reconciled, not forgotten.
    """
    conn.execute(
        "UPDATE bo_turn_leases SET status='uncertain', "
        "reason=COALESCE(reason,'lease_expired'), updated_at=? "
        "WHERE status='active' AND lease_expires_at < ?",
        (_iso(_now()), _iso(_now())),
    )


def _open_leases(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    _sweep_expired(conn)
    rows = conn.execute(
        "SELECT turn_id, kind, ref, client_slug, owner, status, reason, "
        "lease_expires_at, created_at FROM bo_turn_leases "
        "WHERE status IN ('active','uncertain')"
    ).fetchall()
    return [dict(r) for r in rows]


def admit_turn(
    kind: str,
    *,
    ref: str | None = None,
    owner: str | None = None,
    scope_token: str | None = None,
    client_slug: str | None = None,
    mailbox: str | None = None,
    wait_s: float | None = None,
) -> TurnLease:
    """Register a turn durably before it may produce effects.

    Refuses while a switch is in progress (the new turn's evidence would
    land in a journal whose identity is mid-flight). ``wait_s`` bounds a
    short wait for the switch to finish; ``None`` uses the configured
    switch wait budget, ``0`` refuses immediately.
    """
    owner = owner or f"{os.getpid()}:{uuid.uuid4().hex[:12]}"
    budget = _switch_wait_seconds() if wait_s is None else wait_s
    deadline = time.monotonic() + budget
    while True:
        turn_id = uuid.uuid4().hex
        try:
            with _conn() as conn:
                _ensure_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                _sweep_dead_flag(conn)
                state = conn.execute(
                    "SELECT epoch, in_progress, op_id FROM bo_switch_state "
                    "WHERE id=1"
                ).fetchone()
                if state["in_progress"]:
                    # Switch holds the barrier — admission deferred.
                    conn.execute("ROLLBACK")
                    if time.monotonic() >= deadline:
                        raise SwitchBusyError(
                            "A client switch is in progress; the turn was "
                            "not admitted.",
                            op_id=state["op_id"],
                        )
                    time.sleep(0.05)
                    continue
                # Resolve the client inside the transaction: a switch
                # completing between admission and the slug read must not
                # attribute the lease to a journal it never ran under.
                slug = (
                    _active_client_slug()
                    if client_slug is None
                    else client_slug
                )
                now = _iso(_now())
                conn.execute(
                    "INSERT INTO bo_turn_leases (turn_id, kind, ref, tenant, "
                    "client_slug, scope_token, mailbox, owner, epoch, "
                    "status, lease_expires_at, created_at, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?,'active',?,?,?)",
                    (
                        turn_id,
                        kind,
                        ref,
                        _tenant(),
                        slug,
                        scope_token,
                        mailbox,
                        owner,
                        state["epoch"],
                        _iso(_now() + timedelta(seconds=_lease_seconds())),
                        now,
                        now,
                    ),
                )
                conn.execute("COMMIT")
                return TurnLease(
                    turn_id=turn_id,
                    kind=kind,
                    ref=ref,
                    client_slug=slug,
                    epoch=state["epoch"],
                    owner=owner,
                )
        except sqlite3.Error as exc:
            if "locked" in str(exc).lower() and time.monotonic() < deadline:
                time.sleep(0.05)
                continue
            raise TurnAdmissionError(
                f"turn coordination store unavailable: {exc}"
            ) from exc


def turn_still_valid(lease: TurnLease) -> bool:
    """Effect-boundary revalidation: the lease must still be ours,
    ``active``, unexpired and under the epoch it was admitted in (a
    completed switch bumps the epoch — a stale lease must refuse its
    effect).

    A missing coordination file reads as *invalid*: a store that was
    deleted or never initialized can hold no evidence either way, and
    absent evidence is never proof of absence."""
    if not _db_path().exists():
        return False
    try:
        with _conn() as conn:
            conn.execute("BEGIN")
            _sweep_dead_flag(conn)
            row = conn.execute(
                "SELECT status, epoch, owner, lease_expires_at "
                "FROM bo_turn_leases WHERE turn_id=?",
                (lease.turn_id,),
            ).fetchone()
            state = conn.execute(
                "SELECT epoch, in_progress FROM bo_switch_state WHERE id=1"
            ).fetchone()
            conn.execute("ROLLBACK")
    except sqlite3.Error:
        return False
    if row is None or state is None:
        return False
    return (
        row["status"] == "active"
        and row["owner"] == lease.owner
        and row["epoch"] == lease.epoch
        and state["epoch"] == lease.epoch
        and not state["in_progress"]
        and row["lease_expires_at"] > _iso(_now())
    )


def _finish(lease: TurnLease, status: str, *, resolution: str | None,
            reason: str | None) -> bool:
    """CAS the lease out of 'active'. Returns False when the row no
    longer matches (stolen/expired-and-swept) — caller reports honestly."""
    try:
        with _conn() as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE bo_turn_leases SET status=?, resolution=?, "
                "reason=?, updated_at=? "
                "WHERE turn_id=? AND owner=? AND status='active'",
                (status, resolution, reason, _iso(_now()),
                 lease.turn_id, lease.owner),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
    except sqlite3.Error:
        logger.exception("turn_barrier: finish failed for %s", lease.turn_id)
        return False


def complete_turn(lease: TurnLease, *, outcome: str) -> bool:
    """Turn finished — effects accounted for. Safe to switch after."""
    return _finish(lease, "closed", resolution=outcome, reason=None)


def fail_turn_uncertain(lease: TurnLease, *, reason: str) -> bool:
    """Effect presence unprovable — the lease stays blocking until an
    operator reconciles it. Never auto-released."""
    return _finish(lease, "uncertain", resolution=None, reason=reason)


def switch_in_progress() -> bool:
    if not _db_path().exists():
        return True
    try:
        with _conn() as conn:
            conn.execute("BEGIN")
            _sweep_dead_flag(conn)
            row = conn.execute(
                "SELECT in_progress FROM bo_switch_state WHERE id=1"
            ).fetchone()
            conn.execute("ROLLBACK")
            return bool(row and row["in_progress"])
    except sqlite3.Error:
        # Unreadable coordination store must not read as "safe to proceed".
        return True


def begin_switch(op: str, *, target: str | None = None) -> str:
    """Start a journal-swapping operation. Returns the operation id.

    - ``uncertain`` lease → refuse immediately (reconcile first).
    - ``active`` lease → wait the bounded switch budget for it to close,
      then refuse with the blocking ids.
    - clear → set ``in_progress`` + bump epoch atomically.

    The caller MUST run the swap in a try/finally that calls
    ``end_switch(op_id)``.
    """
    if target:
        op = f"{op}:{target}"
    owner = f"{os.getpid()}:{uuid.uuid4().hex[:8]}"
    op_id = uuid.uuid4().hex
    deadline = time.monotonic() + _switch_wait_seconds()
    while True:
        try:
            with _conn() as conn:
                _ensure_schema(conn)
                conn.execute("BEGIN IMMEDIATE")
                _sweep_dead_flag(conn)
                held = conn.execute(
                    "SELECT in_progress FROM bo_switch_state WHERE id=1"
                ).fetchone()
                if held["in_progress"]:
                    # Another live switch holds the barrier — wait the
                    # bounded budget for it to release, then refuse.
                    conn.execute("ROLLBACK")
                    if time.monotonic() >= deadline:
                        raise SwitchRefusedError(
                            f"Cannot {op}: another switch operation holds "
                            f"the barrier (op {op_id}).",
                            blockers=[{"reason": "switch_in_progress"}],
                        )
                    time.sleep(0.1)
                    continue
                blockers = _open_leases(conn)
                uncertain = [b for b in blockers if b["status"] == "uncertain"]
                if uncertain:
                    conn.execute("ROLLBACK")
                    raise SwitchRefusedError(
                        f"Cannot {op}: {len(uncertain)} turn(s) are in an "
                        "uncertain state — reconcile them first "
                        f"(op {op_id}).",
                        blockers=blockers,
                    )
                if blockers:
                    conn.execute("ROLLBACK")
                    if time.monotonic() >= deadline:
                        raise SwitchRefusedError(
                            f"Cannot {op}: {len(blockers)} turn(s) still in "
                            f"flight after the bounded wait (op {op_id}).",
                            blockers=blockers,
                        )
                    time.sleep(0.1)
                    continue
                cur = conn.execute(
                    "UPDATE bo_switch_state SET in_progress=1, op_id=?, "
                    "op=?, owner=?, started_at=?, epoch=epoch+1, "
                    "updated_at=? WHERE id=1 AND in_progress=0",
                    (op_id, op, owner, _iso(_now()), _iso(_now())),
                )
                if cur.rowcount != 1:
                    # CAS lost — unreachable under a real IMMEDIATE txn
                    # (writers serialize), kept as a belt: never declare a
                    # switch that did not acquire the flag.
                    conn.execute("ROLLBACK")
                    time.sleep(0.1)
                    continue
                conn.execute("COMMIT")
                return op_id
        except sqlite3.Error as exc:
            if "locked" in str(exc).lower() and time.monotonic() < deadline:
                time.sleep(0.1)
                continue
            raise SwitchRefusedError(
                f"Cannot {op}: turn coordination store unavailable: {exc} "
                f"(op {op_id}).",
                blockers=[{"reason": "coordination_store_unavailable"}],
            ) from exc


def end_switch(op_id: str) -> None:
    """Release the barrier after a switch completes or aborts.

    The epoch stays bumped — leases admitted under the old epoch are
    stale and must refuse their effect boundary. A failed release is
    retried briefly; a flag that still sticks sweeps itself once its
    owner process is gone (``_sweep_dead_flag``), and boot-time
    ``clear_stale_switch`` covers a crashed owner."""
    for attempt in range(3):
        try:
            with _conn() as conn:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "UPDATE bo_switch_state SET in_progress=0, op_id=NULL, "
                    "op=NULL, owner=NULL, started_at=NULL, updated_at=? "
                    "WHERE id=1 AND op_id=?",
                    (_iso(_now()), op_id),
                )
                conn.execute("COMMIT")
                return
        except sqlite3.OperationalError as exc:
            if "locked" in str(exc).lower() and attempt < 2:
                time.sleep(0.1)
                continue
            break
        except sqlite3.Error:
            break
    logger.error(
        "turn_barrier: failed to release switch %s — the barrier "
        "stays up until the flag owner is gone or is swept", op_id,
    )


@contextmanager
def switch_guard(op: str, *, target: str | None = None) -> Generator[str, None, None]:
    """Hold the turn/switch barrier for a journal-mutating operation.

    Raises :class:`SwitchRefusedError` (with ``blockers``) when turns are
    in flight or uncertain; yields the operation id otherwise and always
    releases the flag in ``finally``. A crash mid-operation leaves the
    flag set — fail-closed: new admissions refuse until an operator
    reconciles (the journal state itself is fenced separately by the
    transition marker / restore-blocked machinery)."""
    op_id = begin_switch(op, target=target)
    try:
        yield op_id
    finally:
        end_switch(op_id)


def seed_open_attempts() -> int:
    """Conservative migration: open attempt markers in the live journal
    become ``uncertain`` leases.

    An ``email_attempt@`` marker without its ``email_attempt_result@``
    close row is an interrupted turn — absence of the close is never
    proof the effect didn't happen. Each such marker seeds one uncertain
    lease (keyed deterministically so re-runs are idempotent), which then
    blocks switching until reconciled. Runs at boot after schema init."""
    try:
        from openexecutive.memory.episodic import DB_PATH as _EPISODIC

        if not _EPISODIC.exists():
            return 0
        conn = sqlite3.connect(str(_EPISODIC))
        try:
            exists = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='audit_dedup'"
            ).fetchone()
            if not exists:
                return 0
            keys = [
                r[0]
                for r in conn.execute(
                    "SELECT dedup_key FROM audit_dedup "
                    "WHERE dedup_key LIKE 'email_attempt@%'"
                )
            ]
            closes = {
                r[0]
                for r in conn.execute(
                    "SELECT dedup_key FROM audit_dedup "
                    "WHERE dedup_key LIKE 'email_attempt_result@%'"
                )
            }
        finally:
            conn.close()
    except Exception:
        logger.exception("turn_barrier: open-attempt scan failed")
        return 0
    seeded = 0
    for key in keys:
        # claim:  email_attempt@{scope}:{mid}:{n}
        # close:  email_attempt_result@{scope}:{mid}:{n}
        suffix = key[len("email_attempt@"):]
        if f"email_attempt_result@{suffix}" in closes:
            continue
        scope_tok, _, rest = suffix.partition(":")
        mid = rest.rsplit(":", 1)[0] if rest else suffix
        seeded += 1 if _seed_lease(
            turn_id=f"seed-{uuid.uuid5(uuid.NAMESPACE_URL, key).hex}",
            kind="email",
            ref=mid,
            scope_token=scope_tok or None,
            reason="open_attempt_migrated",
        ) else 0
    if seeded:
        logger.warning(
            "turn_barrier: seeded %d uncertain lease(s) from open "
            "attempt markers — they block switching until reconciled",
            seeded,
        )
    return seeded


def _seed_lease(
    *, turn_id: str, kind: str, ref: str, scope_token: str | None,
    reason: str,
) -> bool:
    """INSERT OR IGNORE one uncertain lease; True if inserted."""
    try:
        with _conn() as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            epoch = conn.execute(
                "SELECT epoch FROM bo_switch_state WHERE id=1"
            ).fetchone()["epoch"]
            now = _iso(_now())
            cur = conn.execute(
                "INSERT OR IGNORE INTO bo_turn_leases (turn_id, kind, ref, "
                "tenant, client_slug, scope_token, owner, epoch, status, "
                "reason, lease_expires_at, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,'uncertain',?,?,?,?)",
                # client_slug is deliberately '' — after a crash mid-swap
                # the active sentinel may name a different client than the
                # one the attempt ran under; never attribute on a guess.
                (turn_id, kind, ref, _tenant(), "", scope_token,
                 "migration", epoch, reason, now, now, now),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
    except sqlite3.Error:
        logger.exception("turn_barrier: seed lease failed for %s", turn_id)
        return False


def clear_stale_switch() -> bool:
    """Boot-time reconciliation of a barrier flag left by a dead process.

    Only the *flag* is cleared — the row's epoch stays bumped and the
    journal-side transition marker still fences a half-swapped state.
    The flag is cleared only when its recorded owner process is gone:
    a live owner is a real in-flight switch (possibly a sibling process
    sharing this store) and is never swept from under it."""
    try:
        with _conn() as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT in_progress, owner FROM bo_switch_state WHERE id=1"
            ).fetchone()
            if not row or not row["in_progress"]:
                conn.execute("ROLLBACK")
                return False
            if not _flag_owner_dead(row["owner"]):
                conn.execute("ROLLBACK")
                logger.warning(
                    "turn_barrier: switch flag is held by live owner %r — "
                    "not clearing (sibling process mid-switch?)",
                    row["owner"],
                )
                return False
            conn.execute(
                "UPDATE bo_switch_state SET in_progress=0, op_id=NULL, "
                "op=NULL, owner=NULL, started_at=NULL, updated_at=? "
                "WHERE id=1 AND in_progress=1",
                (_iso(_now()),),
            )
            conn.execute("COMMIT")
            logger.warning(
                "turn_barrier: cleared stale switch flag left by a "
                "dead owner (crash mid-switch?)"
            )
            return True
    except sqlite3.Error:
        logger.exception("turn_barrier: stale-flag sweep failed")
        return False


def turn_history(kind: str, ref: str) -> list[dict[str, Any]]:
    """All leases ever recorded for ``(kind, ref)`` — the unswapped record
    of whether a turn ran for this message, regardless of which client
    journal is live. A missing or unreadable store is "history may
    exist", never empty."""
    if not _db_path().exists():
        return [{"turn_id": None, "status": "uncertain", "mailbox": None,
                 "reason": "coordination_store_unavailable"}]
    try:
        with _conn() as conn:
            conn.execute("BEGIN")
            rows = conn.execute(
                "SELECT turn_id, status, resolution, reason, client_slug, "
                "mailbox, scope_token, created_at FROM bo_turn_leases "
                "WHERE kind=? AND ref=?",
                (kind, ref),
            ).fetchall()
            conn.execute("ROLLBACK")
            return [dict(r) for r in rows]
    except sqlite3.Error:
        return [{"turn_id": None, "status": "uncertain", "mailbox": None,
                 "reason": "coordination_store_unavailable"}]


# Lease outcomes where the Executive never ran — safe to re-admit.
_NEVER_RAN = frozenset(
    {"lost_claim", "claim_not_durable", "refused_pre_run"}
)


def ref_has_effect_history(
    kind: str, ref: str, *, mailbox: str | None = None
) -> bool:
    """True when a prior lease for this ref ran the turn — fence, don't
    re-execute.

    ``mailbox`` distinguishes provider message ids, which are only unique
    per mailbox: ``mid`` seen under mailbox B with a completed lease under
    mailbox A is a legitimately different inbound (RA11-B01) and must NOT
    be fenced. A lease for the *same* mailbox in a different scope —
    journal swapped or account rebound — means this exact message may
    already have produced an effect: fence. ``uncertain`` always fences.
    """
    for row in turn_history(kind, ref):
        same_mailbox = (
            mailbox is None
            or row.get("mailbox") in (None, "")
            or row["mailbox"] == mailbox
        )
        if row["status"] == "uncertain":
            return True
        if not same_mailbox:
            continue
        if row["status"] == "closed" and row.get("resolution") not in (
            None, *_NEVER_RAN
        ):
            return True
        if row["status"] == "active":
            # In flight right now — a second admission under another
            # context must wait for reconciliation, not double-run.
            return True
    return False


def blockers() -> list[dict[str, Any]]:
    """Open/uncertain leases for the admin surface. IMMEDIATE so the
    expiry sweep persists — a deferred read would roll the UPDATE back
    and report 'uncertain' for rows the DB still calls 'active'."""
    if not _db_path().exists():
        return [{"turn_id": None, "status": "unknown",
                 "reason": "coordination_store_unavailable"}]
    try:
        with _conn() as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            out = _open_leases(conn)
            conn.execute("COMMIT")
            return out
    except sqlite3.Error:
        return [{"turn_id": None, "status": "unknown",
                 "reason": "coordination_store_unavailable"}]


def reconcile_turn(
    turn_id: str, *, resolution: str, actor: str
) -> dict[str, Any]:
    """Operator reconciliation of an uncertain lease.

    ``resolution='verified'`` — the caller attests the journal/provider
    evidence for the turn's outcome was checked. ``'attested'`` — the
    operator accepts responsibility for deciding the effect did not (or
    did) happen. The lease transitions to ``closed`` with the resolution
    recorded; the row is never deleted.

    Only ``uncertain`` leases reconcile: an ``active`` lease may still be
    running under a live owner — closing it would reopen the mid-switch
    window the lease exists to close. An expired ``active`` lease sweeps
    to ``uncertain`` here first, so an operator never waits on a sweep.
    """
    if resolution not in ("verified", "attested"):
        raise ValueError("resolution must be 'verified' or 'attested'")
    with _conn() as conn:
        _ensure_schema(conn)
        conn.execute("BEGIN IMMEDIATE")
        _sweep_expired(conn)
        row = conn.execute(
            "SELECT status, reason FROM bo_turn_leases WHERE turn_id=?",
            (turn_id,),
        ).fetchone()
        if row is None:
            conn.execute("ROLLBACK")
            raise KeyError(f"unknown turn {turn_id}")
        if row["status"] == "active":
            conn.execute("ROLLBACK")
            raise ReconcileRefusedError(
                f"turn {turn_id} is still active — its owner may yet "
                "complete it. Wait for the lease to expire (it then "
                "reads 'uncertain') or stop the owner first."
            )
        if row["status"] != "uncertain":
            conn.execute("ROLLBACK")
            return {"turn_id": turn_id, "status": row["status"]}
        prior = row["reason"]
        reconciled_reason = (
            f"{prior}|reconciled_by:{actor}" if prior
            else f"reconciled_by:{actor}"
        )
        conn.execute(
            "UPDATE bo_turn_leases SET status='closed', resolution=?, "
            "reason=?, updated_at=? "
            "WHERE turn_id=? AND status='uncertain'",
            (resolution, reconciled_reason, _iso(_now()), turn_id),
        )
        # Durable audit evidence lives in THIS transaction, in the
        # unswapped coordination store — the episodic audit log the
        # forward below targets is journal-swapped per client, so a
        # reconcile recorded only there could be parked out of sight.
        # If this COMMIT fails nothing lands: no unevidenced unblock.
        conn.execute(
            "INSERT INTO bo_control_audit (action, turn_id, actor, "
            "resolution, reason, detail, created_at) "
            "VALUES ('turn_reconcile', ?, ?, ?, ?, ?, ?)",
            (
                turn_id, actor, resolution, prior,
                json.dumps({"reason": reconciled_reason}),
                _iso(_now()),
            ),
        )
        conn.execute("COMMIT")
    # Best-effort forward into the main audit trail; the atomic evidence
    # already landed in bo_control_audit, so an I/O error here is logged
    # but cannot silently lose the reconciliation record.
    try:
        from openexecutive.audit import log_event

        log_event(
            "bo_turn_reconcile",
            f"turn {turn_id} reconciled ({resolution})",
            actor=actor,
            details={"turn_id": turn_id, "resolution": resolution},
        )
    except Exception:
        logger.exception(
            "turn_barrier: reconcile audit forward failed "
            "(durable row is in bo_control_audit)")
    return {"turn_id": turn_id, "status": "closed", "resolution": resolution}


def control_audit(turn_id: str | None = None) -> list[dict[str, Any]]:
    """Durable control-plane audit rows (unswapped coordination store).

    These are the authoritative evidence for operator actions such as
    ``turn_reconcile`` — written in the same transaction as the state
    change, so an action never lands without its record. Returns [] when
    the store is missing (fail-closed readers treat that as uncertain).
    """
    if not _db_path().exists():
        return []
    try:
        with _conn() as conn:
            _ensure_schema(conn)  # adds the table to pre-existing stores
            if turn_id is None:
                rows = conn.execute(
                    "SELECT * FROM bo_control_audit ORDER BY id"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM bo_control_audit WHERE turn_id=? "
                    "ORDER BY id",
                    (turn_id,),
                ).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error:
        return []
