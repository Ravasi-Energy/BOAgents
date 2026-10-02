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

import asyncio
import concurrent.futures
import contextlib
import contextvars
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
import weakref
from collections.abc import AsyncGenerator, Generator
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


def heartbeat_seconds() -> float:
    """Cadence for owner check-ins: well inside the lease TTL."""
    return max(5.0, _lease_seconds() / 3.0)


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
            reason: str | None, epoch: int | None = None) -> bool:
    """CAS the lease out of 'active'. Returns False when the row no
    longer matches (stolen/expired-and-swept) — caller reports honestly.
    ``epoch`` additionally pins the admission epoch when given."""
    predicate = "AND epoch=?" if epoch is not None else ""
    args = (status, resolution, reason, _iso(_now()),
            lease.turn_id, lease.owner, *(() if epoch is None else (epoch,)))
    try:
        with _conn() as conn:
            _ensure_schema(conn)
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE bo_turn_leases SET status=?, resolution=?, "
                "reason=?, updated_at=? "
                "WHERE turn_id=? AND owner=? AND status='active' "
                + predicate,
                args,
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
    except sqlite3.Error:
        logger.exception("turn_barrier: finish failed for %s", lease.turn_id)
        return False


# Tasks/futures that already have a late-outcome audit callback
# attached — a child can surface at both the dispatcher's cancel path
# and ``_audit_orphaned_work``; the audit row must be written once.
_OUTCOME_TRACKED: weakref.WeakSet = weakref.WeakSet()


def _mark_outcome_tracked(obj: Any) -> bool:
    """True the first time an object is marked for outcome audit."""
    with _TRACK_LOCK:
        if obj in _OUTCOME_TRACKED:
            return False
        _OUTCOME_TRACKED.add(obj)
        return True


def _audit_orphaned_work(turn_id: str) -> None:
    """Attach outcome audit to child work still capable of effects at
    the moment its turn could not close — RA15-BO01-09. Tasks get the
    ``late_child_outcome`` callback; executor futures get the same
    durable row written when the worker thread itself reports done
    (the callback fires in the completing thread, which is fine — the
    write uses its own connection on the unswapped store)."""
    for task in list(_LIVE_CHILDREN.get(turn_id, ())):
        if task.done() or not _mark_outcome_tracked(task):
            continue
        coro = task.get_coro()
        label = getattr(coro, "__qualname__", "child-task")
        task.add_done_callback(
            lambda t, tid=turn_id, lbl=label:
            late_child_outcome(tid, lbl, t)
        )
    with _TRACK_LOCK:
        pending = list(_PENDING_CHILD_WORK.get(turn_id, ()))
    for fut in pending:
        if fut.done() or not _mark_outcome_tracked(fut):
            continue

        def _rec(f: Any, tid: str = turn_id) -> None:
            try:
                if f.cancelled():
                    outcome = "cancelled"
                elif f.exception() is not None:
                    outcome = f"error:{type(f.exception()).__name__}"
                else:
                    outcome = "completed"
                record_child_outcome(tid, "executor-work", outcome)
            except Exception:
                logger.exception(
                    "turn_barrier: orphaned-work audit failed for "
                    "turn %s", tid)

        fut.add_done_callback(_rec)


def complete_turn(lease: TurnLease, *, outcome: str) -> bool:
    """Turn finished — effects accounted for. Safe to switch after.

    The CAS carries the admission epoch: a close racing a switch that
    somehow ran must not land as 'completed' under a stale epoch.

    A turn that ends while dispatched child work — or executor work a
    child submitted — is still capable of producing effects cannot
    honestly close (RA15-BO01-06 internal-timeout residual): the lease
    degrades to ``uncertain`` instead, stays a switch blocker, and a
    reconcile is refused until the work demonstrably ends (or dies
    with the process). The orphaned work gets durable outcome audit.
    """
    if children_alive(lease.turn_id):
        _audit_orphaned_work(lease.turn_id)
        return _finish(
            lease, "uncertain", resolution=None,
            reason="child work unproven at turn end",
            epoch=lease.epoch)
    return _finish(lease, "closed", resolution=outcome, reason=None,
                   epoch=lease.epoch)


def fail_turn_uncertain(lease: TurnLease, *, reason: str) -> bool:
    """Effect presence unprovable — the lease stays blocking until an
    operator reconciles it. Never auto-released."""
    return _finish(lease, "uncertain", resolution=None, reason=reason)


# ---------------------------------------------------------------------------
# RA15-BO01-03 — effect-boundary fencing + owner liveness
# ---------------------------------------------------------------------------

# In-process registry of the asyncio tasks that own admitted leases.
# `os.kill(pid, 0)` alone cannot distinguish "turn task still running"
# from "same long-lived process, task finished" — a reconcile gated on
# pid liveness alone would deadlock on daemon owners (poller, API).
_LIVE_TURNS: dict[str, Any] = {}

# Dispatched child work (skill/MCP/specialist tasks) bound to the owning
# lease — RA15-BO01-06. A cancelled parent task does not prove the
# children stopped: ``asyncio.gather`` delivers cancellation but does not
# wait for children that swallow it or still await an executor thread.
# Children are registered here at dispatch and the dispatcher shields
# them, so ``task.done()`` is a real completion signal (the coroutine —
# and any thread it was awaiting — actually finished). Reconcile refuses
# while any registered child is still running. The registry is
# in-process by design: after a restart the children are dead with the
# process, which IS proof they can no longer produce effects.
_LIVE_CHILDREN: dict[str, set] = {}

# Executor work bound to the owning lease — RA15-BO01-06 residual.
# A dispatched child can await ``asyncio.to_thread`` /
# ``run_in_executor`` inside ``asyncio.wait_for``: on timeout the
# child's *task* ends honestly while the executor thread keeps
# running — ``task.done()`` cannot see it. The loop's default executor
# is instrumented once (``ensure_thread_tracking``); every Future
# submitted from inside a tracked child coroutine (``_CURRENT_TURN``
# context var) is registered here, and a
# ``concurrent.futures.Future.done()`` is set by the worker thread
# itself, so it stays honest about real thread termination. Reconcile
# refuses while a tracked Future is still running, exactly like a live
# task. Process death remains proof — executor threads die with it.
_CURRENT_TURN: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "bo_turn", default=None
)
_PENDING_CHILD_WORK: dict[str, set[concurrent.futures.Future]] = {}
# Future done-callbacks fire in the completing worker THREAD while
# submit() runs on the loop thread — mutations of the registry must be
# serialized, or a pop racing a setdefault could hide a fresh Future.
_TRACK_LOCK = threading.Lock()

_TRACK_EXECUTOR_FLAG = "_bo_turn_tracking"


class _TrackingExecutor(concurrent.futures.ThreadPoolExecutor):
    """ThreadPoolExecutor that attributes submitted work to the turn
    whose tracked child coroutine submitted it (``_CURRENT_TURN``).
    ``set_default_executor`` requires a real ThreadPoolExecutor, so the
    wrapper subclasses it: when the loop already had a default executor
    the submissions delegate to it (its pool sizing is preserved and
    our own inherited pool never spawns a thread); otherwise this
    instance IS the pool. Submissions outside a tracked child pass
    through untouched."""

    def __init__(
        self, inner: concurrent.futures.ThreadPoolExecutor | None = None
    ) -> None:
        super().__init__()
        self._inner = inner

    def submit(self, fn: Any, /, *args: Any, **kwargs: Any) -> Any:
        if self._inner is not None:
            fut = self._inner.submit(fn, *args, **kwargs)
        else:
            fut = super().submit(fn, *args, **kwargs)
        turn_id = _CURRENT_TURN.get()
        if turn_id is not None:
            with _TRACK_LOCK:
                _PENDING_CHILD_WORK.setdefault(turn_id, set()).add(fut)

            def _done(f: Any, tid: str = turn_id) -> None:
                _child_work_done(tid, f)

            fut.add_done_callback(_done)
        return fut

    def shutdown(self, wait: bool = True, *,
                 cancel_futures: bool = False) -> None:
        if self._inner is not None:
            self._inner.shutdown(wait, cancel_futures=cancel_futures)
        super().shutdown(wait, cancel_futures=cancel_futures)


def _child_work_done(turn_id: str, fut: Any) -> None:
    with _TRACK_LOCK:
        pending = _PENDING_CHILD_WORK.get(turn_id)
        if pending is not None:
            pending.discard(fut)
            if not pending:
                _PENDING_CHILD_WORK.pop(turn_id, None)


def ensure_thread_tracking() -> None:
    """Install the turn-tracking default executor on the running loop
    (idempotent, per loop). Without it, executor work spawned inside a
    dispatched child is invisible to reconcile once the child's task
    has ended — the internal-timeout hole of RA15-BO01-06."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    if getattr(loop, _TRACK_EXECUTOR_FLAG, False):
        return
    try:
        inner = getattr(loop, "_default_executor", None)
        loop.set_default_executor(_TrackingExecutor(inner))
    except Exception:
        logger.exception(
            "turn_barrier: cannot install the tracking executor; "
            "executor work inside dispatched children will not be "
            "visible to reconcile")
        return
    setattr(loop, _TRACK_EXECUTOR_FLAG, True)


def wrap_child_context(turn_id: str, coro: Any) -> Any:
    """Return a coroutine that runs ``coro`` with this turn's context
    var set, so executor submissions made anywhere inside the child —
    including through ``asyncio.wait_for`` — are attributed to it."""

    async def _wrapped() -> Any:
        token = _CURRENT_TURN.set(turn_id)
        try:
            return await coro
        finally:
            _CURRENT_TURN.reset(token)

    return _wrapped()


# Acquiring the fence's BEGIN IMMEDIATE may legitimately wait behind
# another turn's effect section (skill/MCP dispatch can run tens of
# seconds). Failing after the default 5s would fence healthy concurrent
# turns on pure contention, so holds use a wider budget; a wedged holder
# still yields an honest refusal, never a hang.
_HOLD_BUSY_TIMEOUT_MS = 30_000


def register_turn_task(turn_id: str, task: Any | None = None) -> None:
    """Bind the lease's owner task (called from the owning coroutine)."""
    if task is None:
        try:
            task = asyncio.current_task()
        except RuntimeError:
            task = None
    if task is not None:
        _LIVE_TURNS[turn_id] = task


def unregister_turn_task(turn_id: str) -> None:
    _LIVE_TURNS.pop(turn_id, None)


def register_child_task(turn_id: str, task: Any) -> None:
    """Bind a dispatched child task to the lease so reconcile can see it."""
    bucket = _LIVE_CHILDREN.setdefault(turn_id, set())
    bucket.add(task)
    task.add_done_callback(lambda t, tid=turn_id: _child_done(tid, t))


def _child_done(turn_id: str, task: Any) -> None:
    bucket = _LIVE_CHILDREN.get(turn_id)
    if bucket is not None:
        bucket.discard(task)
        if not bucket:
            _LIVE_CHILDREN.pop(turn_id, None)


def children_alive(turn_id: str) -> bool:
    """True while any registered child task — or executor work it
    submitted — is unfinished. A child coroutine that ends while an
    executor thread it spawned is still running
    (``wait_for(to_thread)`` timeout) must keep the lease blocked: the
    thread can still produce effects."""
    if any(not t.done() for t in _LIVE_CHILDREN.get(turn_id, ())):
        return True
    with _TRACK_LOCK:
        pending = _PENDING_CHILD_WORK.get(turn_id)
        if not pending:
            return False
        snapshot = list(pending)
    return any(not f.done() for f in snapshot)


def record_child_outcome(
    turn_id: str,
    label: str,
    outcome: str,
    detail: dict[str, Any] | None = None,
) -> None:
    """Durable audit for a dispatched child whose result arrived after
    the parent turn unwound — RA15-BO01-09. The row lands in the
    unswapped coordination store (``bo_control_audit``), correlated by
    ``turn_id``, so a later journal swap cannot hide it and it cannot
    be lost by a reconcile/switch the way a per-client journal row
    could. ``outcome`` is the observed terminal state
    (completed/error:<type>/cancelled/unknown), never a fabricated
    success."""
    payload: dict[str, Any] = {"child": label, "outcome": outcome}
    if detail:
        payload.update(detail)
    with _conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "INSERT INTO bo_control_audit (action, turn_id, actor, "
                "resolution, reason, detail, created_at) "
                "VALUES ('turn_child_outcome', ?, 'barrier', "
                "NULL, NULL, ?, ?)",
                (turn_id, json.dumps(payload), _iso(_now())),
            )
            conn.execute("COMMIT")
        except sqlite3.Error:
            with contextlib.suppress(sqlite3.Error):
                conn.execute("ROLLBACK")
            raise
    # Best-effort forward into the episodic audit trail; the durable
    # evidence is the bo_control_audit row above.
    try:
        from openexecutive.audit import log_event

        log_event(
            "bo_turn_child_outcome",
            f"turn {turn_id} child {label} finished late ({outcome})",
            details={"turn_id": turn_id, **payload},
        )
    except Exception:
        logger.exception(
            "turn_barrier: child-outcome audit forward failed "
            "(durable row is in bo_control_audit)")


def late_child_outcome(turn_id: str, label: str, task: Any) -> None:
    """``Task.add_done_callback`` target: audit a still-pending child
    once it truly ends. Never raises into the event loop."""
    try:
        if task.cancelled():
            outcome = "cancelled"
        elif task.exception() is not None:
            outcome = f"error:{type(task.exception()).__name__}"
        else:
            outcome = "completed"
    except Exception:
        outcome = "unknown"
    try:
        record_child_outcome(turn_id, label, outcome)
    except Exception:
        logger.exception(
            "turn_barrier: durable audit of late child outcome failed "
            "for turn %s child %s", turn_id, label)


def _owner_still_apt(owner: Any, turn_id: str) -> bool:
    """True when the lease owner may still be able to produce effects.

    - no owner / unparseable → seeds and legacy rows carry no live owner;
    - dead pid → cannot produce anything;
    - foreign live pid → may still run → treat as apt (single-host limit:
      we cannot introspect another process's tasks);
    - our own pid → consult the task registry: a live task is apt, a
      finished/unknown one is not.
    """
    if not isinstance(owner, str) or ":" not in owner:
        return False
    try:
        pid = int(owner.split(":", 1)[0])
    except ValueError:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    if pid != os.getpid():
        return True
    task = _LIVE_TURNS.get(turn_id)
    return task is not None and not task.done()


def renew_lease(lease: TurnLease) -> bool:
    """Heartbeat: extend the expiry while the turn is demonstrably alive.

    A turn that keeps checking in can never be swept to 'uncertain' — so
    expiry reliably means "the owner stopped producing check-ins" (crash,
    wedge, cancel), which is the honest precondition for reconcile."""
    try:
        with _conn() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cur = conn.execute(
                "UPDATE bo_turn_leases SET lease_expires_at=?, updated_at=? "
                "WHERE turn_id=? AND owner=? AND status='active' AND epoch=?",
                (
                    _iso(_now() + timedelta(seconds=_lease_seconds())),
                    _iso(_now()),
                    lease.turn_id,
                    lease.owner,
                    lease.epoch,
                ),
            )
            conn.execute("COMMIT")
            return cur.rowcount == 1
    except sqlite3.Error:
        return False


def _lease_valid_in_tx(conn: sqlite3.Connection, lease: TurnLease) -> bool:
    """The fence predicate, evaluated inside the caller's transaction:
    lease still ours, active, unexpired, admission epoch, no switch."""
    row = conn.execute(
        "SELECT status, epoch, owner, lease_expires_at "
        "FROM bo_turn_leases WHERE turn_id=?",
        (lease.turn_id,),
    ).fetchone()
    state = conn.execute(
        "SELECT epoch, in_progress FROM bo_switch_state WHERE id=1"
    ).fetchone()
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


class EffectFence:
    """Lease-scoped fence for real effect boundaries (RA15-BO01-03).

    ``check()`` — cheap read + heartbeat; used per observable boundary
    (each yielded item). ``run_atomic(fn)`` — holds BEGIN IMMEDIATE on
    the coordination store across validate+effect, so a switch's epoch
    bump can never interleave between the check and the commit: the
    effect either lands under the epoch it was validated in, or not at
    all. ``ahold()`` is the async variant for effect sections that await.
    """

    def __init__(self, lease: TurnLease):
        self.lease = lease

    def check(self) -> bool:
        """Validate the lease and renew it.

        Validation is a pure read (autocommit SELECTs): readers are never
        blocked by another turn's BEGIN IMMEDIATE hold, so a concurrent
        long effect section cannot fence this turn by lock contention —
        only by real invalidation. Renewal is a separate best-effort write:
        a momentarily busy writer just defers the heartbeat to the next
        check (the TTL has slack)."""
        if not _db_path().exists():
            return False
        try:
            with _conn() as conn:
                ok = _lease_valid_in_tx(conn, self.lease)
            if ok:
                renew_lease(self.lease)
            return ok
        except sqlite3.Error:
            return False

    def run_atomic(self, fn: Any, *args: Any, **kwargs: Any) -> tuple[bool, Any]:
        """Validate inside BEGIN IMMEDIATE, then run ``fn`` while the
        write lock is still held, then COMMIT — the epoch cannot move
        between validation and the effect commit. Returns
        ``(executed, result)``; ``(False, None)`` when fenced."""
        try:
            with _conn() as conn:
                conn.execute(
                    f"PRAGMA busy_timeout={_HOLD_BUSY_TIMEOUT_MS}"
                )
                conn.execute("BEGIN IMMEDIATE")
                _sweep_dead_flag(conn)
                if not _lease_valid_in_tx(conn, self.lease):
                    conn.execute("ROLLBACK")
                    return False, None
                # Renew inside the same write txn: a live owner working at
                # its boundary keeps the lease; expiry then only ever means
                # "owner stopped checking in".
                conn.execute(
                    "UPDATE bo_turn_leases SET lease_expires_at=?, "
                    "updated_at=? WHERE turn_id=? AND owner=? "
                    "AND status='active'",
                    (
                        _iso(_now() + timedelta(seconds=_lease_seconds())),
                        _iso(_now()),
                        self.lease.turn_id,
                        self.lease.owner,
                    ),
                )
                try:
                    result = fn(*args, **kwargs)
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                return True, result
        except sqlite3.Error:
            # Store unreadable at the boundary — the effect did not run
            # (fn only runs after validation). Honest failure: fenced.
            return False, None

    @contextlib.asynccontextmanager
    async def ahold(self) -> AsyncGenerator[bool, None]:
        """Hold BEGIN IMMEDIATE across an ``await`` section.

        A dedicated single-thread executor keeps the sqlite connection
        thread-affine while the event loop runs the effect body. Yields
        False when the lease is already invalid — the body must then be
        skipped."""
        import concurrent.futures

        loop = asyncio.get_running_loop()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        conn: sqlite3.Connection | None = None
        ok = False
        try:

            def _begin() -> bool:
                nonlocal conn
                conn = sqlite3.connect(
                    str(_db_path()), isolation_level=None
                )
                conn.row_factory = sqlite3.Row
                conn.execute(
                    f"PRAGMA busy_timeout={_HOLD_BUSY_TIMEOUT_MS}"
                )
                conn.execute("BEGIN IMMEDIATE")
                _sweep_dead_flag(conn)
                ok = _lease_valid_in_tx(conn, self.lease)
                if ok:
                    conn.execute(
                        "UPDATE bo_turn_leases SET lease_expires_at=?, "
                        "updated_at=? WHERE turn_id=? AND owner=? "
                        "AND status='active'",
                        (
                            _iso(
                                _now() + timedelta(seconds=_lease_seconds())
                            ),
                            _iso(_now()),
                            self.lease.turn_id,
                            self.lease.owner,
                        ),
                    )
                return ok

            try:
                ok = await loop.run_in_executor(pool, _begin)
            except sqlite3.Error:
                # Store unreadable at the boundary — same fail-closed
                # convention as check()/run_atomic: body skipped.
                ok = False
            yield ok
        finally:
            if conn is not None:
                await loop.run_in_executor(
                    pool, lambda: conn.execute("COMMIT" if ok else "ROLLBACK")
                )
                await loop.run_in_executor(pool, conn.close)
            pool.shutdown(wait=False)


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


def recent_turns(limit: int = 50) -> list[dict[str, Any]]:
    """Closed/reconciled leases, newest first (F-2, REM-AUDIT-18).

    The durable trail the operator inspects after a refusal: because the
    table lives in the unswapped coordination store it survives client
    switches, fixture resets and process restarts. ``resolution``
    distinguishes provider-verified evidence from human attestation.
    """
    if not _db_path().exists():
        return []
    try:
        with _conn() as conn:
            _ensure_schema(conn)
            rows = conn.execute(
                "SELECT turn_id, kind, ref, client_slug, mailbox, owner, "
                "epoch, status, resolution, reason, lease_expires_at, "
                "created_at, updated_at FROM bo_turn_leases "
                "WHERE status != 'active' ORDER BY updated_at DESC "
                "LIMIT ?",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


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
            "SELECT status, reason, owner FROM bo_turn_leases "
            "WHERE turn_id=?",
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
        # RA15-BO01-03: an uncertain lease whose owner can still produce
        # effects must not be released — the reconcile would unblock the
        # switch while the tardy worker is still runnable. Expiry, a dead
        # PID or a human declaration are not proof of absence; only a
        # demonstrably inert owner (dead process or finished task) lets
        # the operator close it here.
        if _owner_still_apt(row["owner"], turn_id):
            conn.execute("ROLLBACK")
            raise ReconcileRefusedError(
                f"turn {turn_id} is uncertain but its owner "
                f"({row['owner']}) is still running — stop the owner "
                "process/task first, then reconcile. While it lives, "
                "closing the lease cannot be shown safe."
            )
        # RA15-BO01-06: a finished/cancelled parent task does not prove
        # its dispatched children stopped — shielded skill/MCP/specialist
        # work outlives the parent. Closing while a child is runnable
        # would release the turn's authority while an effect can still
        # land. Admin attestation is not a substitute for demonstrated
        # child termination.
        if children_alive(turn_id):
            conn.execute("ROLLBACK")
            raise ReconcileRefusedError(
                f"turn {turn_id} is uncertain and still has live "
                "dispatched children (skill/MCP/specialist work) — "
                "closing the lease while a child can still produce "
                "effects is not safe. Wait for the children to finish "
                "or stop the process, then reconcile."
            )
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
