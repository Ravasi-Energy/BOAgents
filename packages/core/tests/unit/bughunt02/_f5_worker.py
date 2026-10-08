"""Subprocess worker for test_contract_f5 — NOT a test module.

The F5 registry rows (B-R-2 requeue lease, B-R-7 outbox+mail two
processes, B-R-9 two tenants on one SQLite) require REAL process
boundaries — thread-level claims prove the SQL barrier under BEGIN
IMMEDIATE, but only a second process proves it against the registry's
own scenario. Every mode below invokes the production code paths with
state taken exclusively from the environment (EPISODIC_DB_PATH,
BOAGENTS_DB_PATH, BO_TENANT_ID, BO_TELEMETRY_*), exactly the way a real
second worker/scheduler process would boot.

Modes:
  sched-claim   — claim due scheduled actions → {"claimed": [ids]}
  sched-hold    — claim then sleep; the test SIGKILLs it mid-"dispatch"
                  → a crashed worker with a live lease on disk
  sched-requeue — requeue_orphaned_running(stale_after_seconds)
                  → {"requeued": n}
  sched-exec    — claim → append "effect:<id>" to a shared effects file
                  → mark done → {"done": [ids]}
  exec-setup    — create mandate + submit run under env tenant
                  → {"run_id": str}
  exec-claim    — claim_runs for env tenant → {"claimed": [run_ids]}
  exec-list     — list_runs for env tenant → {"runs": [run_ids]}
  exec-get      — get_run(env tenant, argv run_id) → {"found": bool}
  mail-claim    — poller._claim_attempt on a shared audit journal
                  → {"verdict": "claimed"|"lost"|"journal_error"}
  outbox-deliver — deliver_pending for env tenant → {"sent","failed",...}
"""
import json
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path


def _write(out: str, payload: dict) -> None:
    Path(out).write_text(json.dumps(payload))


def _bo_init() -> None:
    """Production lifespan init — idempotent, safe in every process."""
    from openexecutive.bo import db as bo_db

    bo_db.initialize_db()


def _sched_claim(out: str) -> None:
    from openexecutive.memory import episodic

    rows = episodic.claim_due_actions(datetime.now(UTC))
    _write(out, {"claimed": [a.id for a in rows]})


def _sched_hold(out: str, ready: str) -> None:
    """Claim every due action, announce, then wait to be SIGKILLed.

    The row stays 'running' with a fresh claimed_at — on disk this is
    indistinguishable from a still-working process, which is exactly the
    point of the lease: nobody may recycle it while the lease is alive.
    """
    from openexecutive.memory import episodic

    rows = episodic.claim_due_actions(datetime.now(UTC))
    _write(out, {"claimed": [a.id for a in rows]})
    Path(ready).touch()
    time.sleep(120)


def _sched_requeue(out: str, stale_s: str) -> None:
    from openexecutive.memory import episodic

    n = episodic.requeue_orphaned_running(
        stale_after_seconds=int(stale_s)
    )
    _write(out, {"requeued": n})


def _sched_exec(out: str, effects: str) -> None:
    from openexecutive.memory import episodic

    rows = episodic.claim_due_actions(datetime.now(UTC))
    done: list[int] = []
    for a in rows:
        # The effect is external: append to the shared ledger BEFORE the
        # durable mark — crash between the two is what the idempotent
        # re-claim protects against (same row, not a second insert).
        with open(effects, "a") as f:
            f.write(f"effect:{a.id}\n")
        episodic.mark_action_done(a.id)
        done.append(a.id)
    _write(out, {"done": done})


def _exec_setup(out: str) -> None:
    """Own-config tenant: everything resolves from BO_TENANT_ID/env."""
    _bo_init()
    from openexecutive.bo import identity
    from openexecutive.bo.execution import store

    tenant = identity.configured_tenant()
    fields = {
        "allowed_resources": ["synth.*"],
        "allowed_actions": ["increment"],
        "budget_limit": "10",
        "concurrency_limit": 2,
        "max_steps": 10,
        "max_depth": 2,
        "expires_at": (
            datetime.now(UTC) + timedelta(days=7)
        ).isoformat(),
    }
    mandate = store.create_mandate(
        tenant,
        fields,
        parent=None,
        principal_ref="f5-probe",
        policy_version=1,
        actor="admin@f5",
        max_depth_cap=8,
    )
    run = store.submit_run(
        tenant,
        mandate,
        [{"action": "increment", "resource": "synth.counter",
          "payload": {"amount": 1}}],
        budget_amount=Decimal("1"),
        slots=1,
        correlation_id=os.environ["F5_CORRELATION"],
        actor="admin@f5",
    )
    _write(out, {"run_id": run["run_id"], "tenant": tenant})


def _exec_claim(out: str) -> None:
    _bo_init()
    from openexecutive.bo import identity
    from openexecutive.bo.execution import store

    tenant = identity.configured_tenant()
    rows = store.claim_runs(
        tenant, worker_id="f5-proc", limit=10, lease_s=60
    )
    _write(out, {"claimed": [r["run_id"] for r in rows], "tenant": tenant})


def _exec_list(out: str) -> None:
    _bo_init()
    from openexecutive.bo import identity
    from openexecutive.bo.execution import store

    tenant = identity.configured_tenant()
    _write(
        out,
        {"runs": [r["run_id"] for r in store.list_runs(tenant)],
         "tenant": tenant},
    )


def _exec_get(out: str, run_id: str) -> None:
    _bo_init()
    from openexecutive.bo import identity
    from openexecutive.bo.execution import store
    from openexecutive.bo.execution.store import NotFoundError

    tenant = identity.configured_tenant()
    try:
        store.get_run(tenant, run_id)
        found = True
    except NotFoundError:
        found = False
    _write(out, {"found": found, "tenant": tenant})


def _mail_claim(out: str, audit_db: str, mid: str, attempt: str) -> None:
    from openexecutive.audit import AuditLogger
    from openexecutive.integrations import email_poller, mail_scope

    tenant = os.environ.get("BO_TENANT_ID", "local")
    scope = mail_scope.MailScope(
        tenant=tenant,
        client_key="install:f5",
        mailbox="acct-a@example.com",
        token=mail_scope.scope_token(tenant, "install:f5", "acct-a@example.com"),
        version=1,
    )
    audit = AuditLogger(db_path=Path(audit_db))
    verdict = email_poller._claim_attempt(
        audit,
        message_id=mid,
        session_id=f"sess-{os.getpid()}",
        attempt_no=int(attempt),
        owner=f"owner-{os.getpid()}",
        scope=scope,
    )
    _write(out, {"verdict": verdict, "pid": os.getpid()})


def _outbox_deliver(out: str) -> None:
    _bo_init()
    from openexecutive.bo import identity
    from openexecutive.bo.routing import delivery

    tenant = identity.configured_tenant()
    res = delivery.deliver_pending(tenant)
    _write(out, res)


def main() -> None:
    mode = sys.argv[1]
    if mode == "sched-claim":
        _sched_claim(sys.argv[2])
    elif mode == "sched-hold":
        _sched_hold(sys.argv[2], sys.argv[3])
    elif mode == "sched-requeue":
        _sched_requeue(sys.argv[2], sys.argv[3])
    elif mode == "sched-exec":
        _sched_exec(sys.argv[2], sys.argv[3])
    elif mode == "exec-setup":
        _exec_setup(sys.argv[2])
    elif mode == "exec-claim":
        _exec_claim(sys.argv[2])
    elif mode == "exec-list":
        _exec_list(sys.argv[2])
    elif mode == "exec-get":
        _exec_get(sys.argv[2], sys.argv[3])
    elif mode == "mail-claim":
        _mail_claim(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif mode == "outbox-deliver":
        _outbox_deliver(sys.argv[2])
    else:
        raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
