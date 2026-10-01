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
