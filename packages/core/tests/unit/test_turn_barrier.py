"""Unit tests for the durable turn/switch barrier (RA13-A02-01).

The barrier lives in ``bo_agents.db`` — the unswapped coordination store —
so a client slot's journal can be saved/restored without losing turn
evidence. Every property here maps to a defect scenario: duplicate
provider effects after a journal swap mid-turn.
"""
from __future__ import annotations

import threading

import pytest

from openexecutive.bo import turn_barrier as tb


@pytest.fixture
def barrier_db(tmp_path, monkeypatch):
    from openexecutive.bo import db as bo_db

    db = tmp_path / "bo_agents.db"
    monkeypatch.setattr(bo_db, "DB_PATH", db)
    monkeypatch.setenv("BO_TENANT_ID", "t-test")
    # Bounded switch wait is exercised by the integration probe; unit
    # tests pin it to 0 so refusals are immediate.
    monkeypatch.setattr(tb, "_switch_wait_seconds", lambda: 0)
    tb.initialize_db()
    return db


def _expire(db, lease) -> None:
    """Push the lease's expiry into the past (crash/owner-vanished model)."""
    from openexecutive.bo import db as bo_db

    with bo_db.get_conn(db) as conn:
        conn.execute(
            "UPDATE bo_turn_leases SET lease_expires_at='2000-01-01' "
            "WHERE turn_id=?",
            (lease.turn_id,),
        )


def test_admit_then_switch_refused_then_allowed(barrier_db):
    lease = tb.admit_turn("email", ref="m1", wait_s=0)
    with pytest.raises(tb.SwitchRefusedError) as ei:
        tb.begin_switch("activate:x")
    assert lease.turn_id in [b["turn_id"] for b in ei.value.blockers]
    tb.complete_turn(lease, outcome="processed")
    op = tb.begin_switch("activate:x")
    tb.end_switch(op)


def test_admission_refused_while_switch_in_progress(barrier_db):
    op = tb.begin_switch("activate:x")
    with pytest.raises(tb.SwitchBusyError):
        tb.admit_turn("email", ref="m2", wait_s=0)
    tb.end_switch(op)
    tb.admit_turn("email", ref="m2", wait_s=0)


def test_epoch_bumps_on_switch_and_stale_lease_refuses(barrier_db):
    """Operator reconciles an expired (uncertain) lease; the next switch
    bumps the epoch and the stale worker's effect-boundary revalidation
    refuses to write terminal evidence into the new context."""
    lease = tb.admit_turn("email", ref="m3", wait_s=0)
    assert tb.turn_still_valid(lease)
    _expire(barrier_db, lease)
    tb.reconcile_turn(lease.turn_id, resolution="verified", actor="op")
    op = tb.begin_switch("activate:x")
    tb.end_switch(op)
    # Same lease, epoch moved + status closed → invalid at the boundary.
    assert not tb.turn_still_valid(lease)


def test_reconcile_refuses_active_lease(barrier_db):
    """An active lease's owner may still complete the turn — reconciling
    it would reopen the mid-switch window the lease exists to close."""
    lease = tb.admit_turn("email", ref="m3b", wait_s=0)
    with pytest.raises(tb.ReconcileRefusedError):
        tb.reconcile_turn(
            lease.turn_id, resolution="attested", actor="op"
        )
    _expire(barrier_db, lease)  # → uncertain on the next sweep
    out = tb.reconcile_turn(
        lease.turn_id, resolution="attested", actor="op"
    )
    assert out["status"] == "closed"
    op = tb.begin_switch("activate:x")
    tb.end_switch(op)


def test_reconcile_audit_is_atomic_with_state(barrier_db, monkeypatch):
    """RA15-BO01-02: a reconcile must never close a lease without durable
    audit evidence. The evidence row is written in the SAME transaction
    in the unswapped coordination store, so an I/O error in the main
    (journal-swapped) audit log cannot silently lose the record."""
    import openexecutive.audit as audit

    lease = tb.admit_turn("email", ref="m-audit", wait_s=0)
    _expire(barrier_db, lease)

    def _boom(*a, **k):
        raise OSError("audit db gone")

    monkeypatch.setattr(audit, "log_event", _boom)
    out = tb.reconcile_turn(
        lease.turn_id, resolution="verified", actor="op-7"
    )
    assert out["status"] == "closed"
    rows = tb.control_audit(lease.turn_id)
    assert len(rows) == 1
    assert rows[0]["action"] == "turn_reconcile"
    assert rows[0]["actor"] == "op-7"
    assert rows[0]["resolution"] == "verified"


def test_reconcile_refusal_writes_no_audit(barrier_db):
    lease = tb.admit_turn("email", ref="m-noaudit", wait_s=0)
    with pytest.raises(tb.ReconcileRefusedError):
        tb.reconcile_turn(
            lease.turn_id, resolution="attested", actor="op"
        )
    assert tb.control_audit(lease.turn_id) == []


def test_second_switch_cannot_acquire(barrier_db):
    """CAS on in_progress: a second begin_switch can never hold the
    barrier while a live one does (cross-process exclusion)."""
    op1 = tb.begin_switch("activate:x")
    with pytest.raises(tb.SwitchRefusedError) as ei:
        tb.begin_switch("activate:y")
    assert ei.value.blockers[0]["reason"] == "switch_in_progress"
    tb.end_switch(op1)
    op2 = tb.begin_switch("activate:y")
    tb.end_switch(op2)


def test_dead_owner_flag_is_swept(barrier_db):
    """A flag whose owner pid is gone no longer fences admissions."""
    import os

    from openexecutive.bo import db as bo_db

    with bo_db.get_conn() as conn:
        conn.execute(
            "UPDATE bo_switch_state SET in_progress=1, owner='999999999:x'"
        )
    assert not tb.switch_in_progress()  # dead pid → swept
    tb.admit_turn("email", ref="m20", wait_s=0)

    # A live owner (this process) still fences.
    with bo_db.get_conn() as conn:
        conn.execute(
            "UPDATE bo_switch_state SET in_progress=1, "
            "owner=?", (f"{os.getpid()}:x",),
        )
    assert tb.switch_in_progress()
    with pytest.raises(tb.SwitchBusyError):
        tb.admit_turn("email", ref="m21", wait_s=0)
    with bo_db.get_conn() as conn:
        conn.execute("UPDATE bo_switch_state SET in_progress=0")


def test_missing_store_reads_fail_closed(barrier_db, monkeypatch):
    """A deleted/never-created coordination store is 'unavailable',
    never 'empty': reads fence instead of reporting no history."""
    from openexecutive.bo import db as bo_db

    lease = tb.TurnLease(
        turn_id="gone", kind="email", ref="m22",
        client_slug="", epoch=0, owner="x",
    )
    monkeypatch.setattr(
        bo_db, "DB_PATH", barrier_db.parent / "never-created" / "b.db"
    )
    assert not tb.turn_still_valid(lease)
    assert tb.switch_in_progress()
    assert tb.blockers()[0]["reason"] == "coordination_store_unavailable"
    assert tb.ref_has_effect_history("email", "m22", mailbox="a@x")


def test_seeded_lease_fences_same_message(barrier_db, monkeypatch):
    """A migrated open attempt (crash pre-close) seeds an uncertain lease
    whose ref matches the bare message id — the cross-context fence must
    catch it after the swap that stranded the attempt's journal."""
    import sqlite3

    import openexecutive.memory.episodic as episodic

    epi = barrier_db.parent / "episodic.db"
    conn = sqlite3.connect(str(epi))
    conn.execute(
        "CREATE TABLE audit_dedup (dedup_key TEXT PRIMARY KEY)"
    )
    conn.execute(
        "INSERT INTO audit_dedup VALUES ('email_attempt@s1.abc:m23:0')"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(episodic, "DB_PATH", epi)
    assert tb.seed_open_attempts() == 1
    assert tb.ref_has_effect_history("email", "m23", mailbox="any@x")
    with pytest.raises(tb.SwitchRefusedError):
        tb.begin_switch("activate:x")


def test_expired_lease_becomes_uncertain_and_blocks(barrier_db):
    lease = tb.admit_turn("email", ref="m4", wait_s=0)
    _expire(barrier_db, lease)
    # No auto-unlock: the expired lease sweeps to 'uncertain' and the
    # switch refuses until reconciled — timeout is never proof of absence.
    with pytest.raises(tb.SwitchRefusedError):
        tb.begin_switch("activate:x")
    st = [b for b in tb.blockers() if b["turn_id"] == lease.turn_id]
    assert st and st[0]["status"] == "uncertain"
    # The sweep is durable — a second read still reports 'uncertain'.
    st = [b for b in tb.blockers() if b["turn_id"] == lease.turn_id]
    assert st and st[0]["status"] == "uncertain"


def test_reconcile_unblocks_switch(barrier_db):
    lease = tb.admit_turn("email", ref="m5", wait_s=0)
    _expire(barrier_db, lease)
    tb.reconcile_turn(lease.turn_id, resolution="verified", actor="test")
    op = tb.begin_switch("activate:x")
    tb.end_switch(op)


def test_effect_history_fences_same_mailbox_cross_context(barrier_db):
    """A lease that ran for (mailbox, mid) fences that exact pair under a
    different scope — the swap13 defect. A different mailbox with the same
    mid is a legitimately different inbound (RA11-B01) — not fenced."""
    lease = tb.admit_turn("email", ref="m6", mailbox="a@x", wait_s=0)
    tb.complete_turn(lease, outcome="processed")
    assert tb.ref_has_effect_history("email", "m6", mailbox="a@x")
    assert not tb.ref_has_effect_history("email", "m6", mailbox="b@y")
    assert not tb.ref_has_effect_history("email", "m-other", mailbox="a@x")


def test_never_ran_outcomes_do_not_fence(barrier_db):
    for outcome in ("lost_claim", "claim_not_durable", "refused_pre_run"):
        lease = tb.admit_turn("email", ref="m7", mailbox="a@x", wait_s=0)
        tb.complete_turn(lease, outcome=outcome)
        assert not tb.ref_has_effect_history("email", "m7", mailbox="a@x")


def test_uncertain_lease_fences_even_across_mailboxes(barrier_db):
    lease = tb.admit_turn("email", ref="m8", mailbox="a@x", wait_s=0)
    tb.fail_turn_uncertain(lease, reason="executive_failed_unproven")
    assert tb.ref_has_effect_history("email", "m8", mailbox="b@y")


def test_switch_guard_releases_flag_on_error(barrier_db):
    with pytest.raises(RuntimeError), tb.switch_guard("activate:x"):
        raise RuntimeError("boom")
    assert not tb.switch_in_progress()
    tb.admit_turn("email", ref="m9", wait_s=0)


def test_concurrent_admission_and_switch_serialize(barrier_db):
    """Two threads race: the turn is admitted XOR the switch proceeds —
    never both. If admission won, the racing switch must have refused;
    if the switch won, admission must have deferred (SwitchBusyError)."""
    def _turn(outcome, i):
        try:
            outcome["turn"] = tb.admit_turn("email", ref=f"m10-{i}", wait_s=0)
        except tb.SwitchBusyError:
            outcome["turn"] = "busy"

    def _switch(outcome):
        try:
            op = tb.begin_switch("activate:x")
            tb.end_switch(op)
            outcome["switch"] = "went"
        except tb.SwitchRefusedError:
            outcome["switch"] = "refused"

    for i in range(8):
        outcome: dict[str, object] = {}
        t1 = threading.Thread(target=_turn, args=(outcome, i))
        t2 = threading.Thread(target=_switch, args=(outcome,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        turn_ok = isinstance(outcome["turn"], tb.TurnLease)
        # Causality of refusals: a refused switch implies a live turn
        # blocked it; a busy admission implies the flag was held. Both
        # proceeding means the ordering serialized legitimately (admit
        # after end_switch) — that's fine, the barrier is about the
        # critical-section decision, not wall-clock overlap.
        if outcome["switch"] == "refused":
            assert turn_ok, f"race {i}: switch refused without a turn"
        if outcome["turn"] == "busy":
            assert outcome["switch"] == "went"
        if turn_ok:
            tb.complete_turn(outcome["turn"], outcome="processed")


def test_corrupt_switch_state_recovers(barrier_db):
    """A deleted switch_state row re-creates itself (INSERT OR IGNORE) —
    corruption degrades to a fresh epoch, not a wedge."""
    from openexecutive.bo import db as bo_db

    with bo_db.get_conn() as conn:
        conn.execute("DELETE FROM bo_switch_state")
    tb.admit_turn("email", ref="m11", wait_s=0)


def test_store_unavailable_refuses_admission(barrier_db, monkeypatch):
    from openexecutive.bo import db as bo_db

    monkeypatch.setattr(
        bo_db, "DB_PATH", barrier_db / "no-such-dir" / "bo.db"
    )
    with pytest.raises(tb.TurnAdmissionError):
        tb.admit_turn("email", ref="m12", wait_s=0)


# ---------------------------------------------------------------------------
# RA15-BO01-03 — effect fencing + owner liveness
# ---------------------------------------------------------------------------


class _FakeTask:
    """Stand-in for an asyncio.Task in the liveness registry."""

    def __init__(self, done: bool = False):
        self._done = done

    def done(self) -> bool:
        return self._done


def test_reconcile_refuses_while_owner_task_alive(barrier_db):
    """The BO01 window: a suspended-but-runnable owner must not be released
    by reconcile — the close would unblock the switch while the worker can
    still complete. Only a demonstrably inert owner reconciles."""
    lease = tb.admit_turn("email", ref="m30", wait_s=0)
    _expire(barrier_db, lease)
    tb.register_turn_task(lease.turn_id, _FakeTask(done=False))
    with pytest.raises(tb.ReconcileRefusedError, match="still running"):
        tb.reconcile_turn(
            lease.turn_id, resolution="verified", actor="op"
        )
    # Once the owner task is demonstrably finished, reconcile proceeds.
    tb._LIVE_TURNS[lease.turn_id] = _FakeTask(done=True)
    out = tb.reconcile_turn(
        lease.turn_id, resolution="verified", actor="op"
    )
    assert out["status"] == "closed"
    tb.unregister_turn_task(lease.turn_id)


def test_reconcile_refuses_live_foreign_owner(barrier_db):
    """A live owner pid in ANOTHER process cannot be introspected —
    treated as apt; the operator must stop that process first."""
    import os

    lease = tb.admit_turn(
        "email", ref="m31", owner="1:foreign", wait_s=0
    )
    _expire(barrier_db, lease)
    with pytest.raises(tb.ReconcileRefusedError):
        tb.reconcile_turn(
            lease.turn_id, resolution="attested", actor="op"
        )
    # Dead pid → inert → reconcile allowed.
    lease2 = tb.admit_turn(
        "email", ref="m31b", owner="999999999:gone", wait_s=0
    )
    _expire(barrier_db, lease2)
    out = tb.reconcile_turn(
        lease2.turn_id, resolution="attested", actor="op"
    )
    assert out["status"] == "closed"
    assert os.getpid() != 1  # sanity: our pid really is ours


def test_fence_check_renews_lease(barrier_db):
    """A live owner checking in keeps its lease — expiry then only ever
    means 'stopped producing check-ins'. An already-expired lease is
    fenced, never resurrected."""
    import time as _t

    from openexecutive.bo import db as bo_db

    lease = tb.admit_turn("email", ref="m32", wait_s=0)
    with bo_db.get_conn() as conn:
        admission_exp = conn.execute(
            "SELECT lease_expires_at FROM bo_turn_leases "
            "WHERE turn_id=?", (lease.turn_id,),
        ).fetchone()[0]
    fence = tb.EffectFence(lease)
    _t.sleep(0.02)  # ensure the renewed timestamp strictly advances
    assert fence.check()
    with bo_db.get_conn() as conn:
        exp = conn.execute(
            "SELECT lease_expires_at, status FROM bo_turn_leases "
            "WHERE turn_id=?", (lease.turn_id,),
        ).fetchone()
    assert exp[1] == "active"
    assert exp[0] > admission_exp
    # Expired → fenced (no resurrection).
    _expire(barrier_db, lease)
    assert not fence.check()


def test_fence_refuses_after_invalidation(barrier_db):
    """The residual path the liveness gate cannot cover alone: a lease
    closed+epoch bumped while a detached worker object still exists."""
    lease = tb.admit_turn("email", ref="m33", wait_s=0)
    fence = tb.EffectFence(lease)
    assert fence.check()
    _expire(barrier_db, lease)
    tb.reconcile_turn(lease.turn_id, resolution="verified", actor="op")
    op = tb.begin_switch("activate:x")
    tb.end_switch(op)
    assert not fence.check()
    # complete_turn's epoch-pinned CAS refuses to close a stale lease.
    assert not tb.complete_turn(lease, outcome="completed")


def test_run_atomic_holds_validation_across_effect(barrier_db):
    lease = tb.admit_turn("email", ref="m34", wait_s=0)
    fence = tb.EffectFence(lease)
    calls: list[str] = []
    executed, result = fence.run_atomic(lambda: calls.append("fx") or "ok")
    assert executed and result == "ok" and calls == ["fx"]
    # After invalidate+reconcile+switch the effect body never runs.
    _expire(barrier_db, lease)
    tb.reconcile_turn(lease.turn_id, resolution="verified", actor="op")
    op = tb.begin_switch("activate:x")
    tb.end_switch(op)
    executed, result = fence.run_atomic(
        lambda: calls.append("late") or "bad"
    )
    assert not executed and result is None and calls == ["fx"]


def test_ahold_fences_stale_turn(barrier_db):
    import asyncio

    async def _drive() -> tuple[list[bool], bool]:
        lease = tb.admit_turn("email", ref="m35", wait_s=0)
        fence = tb.EffectFence(lease)
        seen: list[bool] = []
        async with fence.ahold() as ok:
            seen.append(ok)
        _expire(barrier_db, lease)
        tb.reconcile_turn(
            lease.turn_id, resolution="verified", actor="op"
        )
        op = tb.begin_switch("activate:x")
        tb.end_switch(op)
        async with fence.ahold() as ok2:
            seen.append(ok2)
        return seen, True

    seen, _ = asyncio.run(_drive())
    assert seen == [True, False]


def test_decorated_generator_fenced_mid_stream(barrier_db):
    """BO01's exact chain at the real boundary: a live Executive generator
    whose lease is invalidated and whose epoch moved cannot emit further
    items — and its durable tail never runs."""
    import asyncio

    from openexecutive.orchestrator.executive import _barrier_turn

    tail_writes: list[str] = []

    class _FakeExec:
        @_barrier_turn("executive")
        async def stream(self, _fence=None):
            yield "chunk-1"
            # Where the durable tail would run — guarded by the fence.
            async with _fence.ahold() as ok:
                if ok:
                    tail_writes.append("committed")
            yield "chunk-2"

    async def _drive() -> list[str]:
        agen = _FakeExec().stream()
        out = [await agen.__anext__()]
        # Suspend with a live owner; expire the lease; detach the task
        # binding (generator object survives — the BO01 window); an
        # operator then legitimately reconciles the inert owner.
        lease_id = tb.blockers()[0]["turn_id"]
        from openexecutive.bo import db as bo_db

        with bo_db.get_conn() as conn:
            conn.execute(
                "UPDATE bo_turn_leases SET lease_expires_at='2000-01-01' "
                "WHERE turn_id=?", (lease_id,),
            )
        tb.unregister_turn_task(lease_id)
        tb.reconcile_turn(
            lease_id, resolution="verified", actor="op"
        )
        op = tb.begin_switch("activate:x")
        tb.end_switch(op)
        # Now the zombie resumes: every further boundary is fenced.
        try:
            while True:
                out.append(await agen.__anext__())
        except StopAsyncIteration:
            pass
        return out

    out = asyncio.run(_drive())
    assert out[0] == "chunk-1"
    # chunk-2 never streamed; the fence notice is the only late output.
    assert "chunk-2" not in out
    assert any("workspace change" in str(i) for i in out[1:])
    assert tail_writes == []


def test_zombie_audit_writes_never_cross_epoch_bump(barrier_db, monkeypatch):
    """Residual RA15-BO01-03 window: the loop's durable journal writes
    (tool_invocation rows) must not land after a reconcile+epoch-bump.

    Drives the real ``_stream_agent_loop`` detached (no decorator wrapper —
    the zombie the barrier must tolerate), suspends it at the real
    chip-yield boundary right after dispatch, performs the full
    expire→reconcile→epoch-bump chain, resumes — and asserts zero audit
    calls after the bump. Pre-fix the loop emitted ``tool_invocation``
    rows past this point (they landed in the swapped-in journal).
    """
    import asyncio
    from unittest.mock import patch

    from openexecutive.orchestrator import executive as ex_mod

    audit_calls: list[str] = []

    def _spy(event_type, summary, **kw):
        audit_calls.append(event_type)

    class _Text:
        type = "text"

        def __init__(self, t):
            self.text = t

    class _ToolUse:
        type = "tool_use"

        def __init__(self):
            self.id = "tu-1"
            self.name = "lookup_person"
            self.input = {"query": "x"}

    class _Final:
        usage = None

        def __init__(self, content, stop):
            self.content = content
            self.stop_reason = stop

    class _Stream:
        def __init__(self, msg):
            self._msg = msg

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def get_final_message(self):
            return self._msg

    class _Provider:
        def __init__(self):
            self._msgs = [
                _Final([_ToolUse()], "tool_use"),
                _Final([_Text("done")], "end_turn"),
            ]

        def messages_stream(self, **kw):
            return _Stream(self._msgs.pop(0))

    async def _ok_skill(_input):
        return "{}"

    chip = {"type": "action_chip", "label": "x"}
    lease = tb.admit_turn("executive", ref="a18", client_slug="A", wait_s=0)

    async def _drive():
        fence = tb.EffectFence(lease)
        agen = ex_mod.Executive()._stream_agent_loop(
            system_blocks=[],
            messages=[{"role": "user", "content": "hi"}],
            model="m",
            turn_id=lease.turn_id,
            _fence=fence,
        )
        bumped = False
        try:
            while True:
                try:
                    item = await agen.__anext__()
                except (StopAsyncIteration, ex_mod._TurnFencedError):
                    break
                if item == chip and not bumped:
                    _expire(barrier_db, lease)
                    tb.reconcile_turn(
                        lease.turn_id, resolution="verified", actor="op"
                    )
                    op = tb.begin_switch("activate")
                    tb.end_switch(op)
                    bumped = True
                    audit_calls.clear()  # only count post-bump writes
        finally:
            await agen.aclose()

    with (
        patch.object(ex_mod, "get_provider", return_value=_Provider()),
        patch.dict(ex_mod._ALL_SKILL_HANDLERS,
                   {"lookup_person": _ok_skill}),
        patch.object(ex_mod, "summarize_action", lambda **kw: dict(chip)),
        patch.object(ex_mod, "audit_log", _spy),
    ):
        asyncio.run(_drive())

    # Under a valid epoch the tool_invocation row is written (inside the
    # dispatch hold); after the bump the zombie must emit nothing.
    assert audit_calls == []
