"""CLI driver for VAL4-01 probes and the reproducible demo.

``uv run python -m openexecutive.bo.execution.cli <command>`` — every
command works on the real ``BOAGENTS_DB_PATH`` file, so two processes on
the same database demonstrate exactly what the mandate requires:
concurrent claims, crash/restart resume, receipt-based reconciliation
and the countable synthetic effect.

Commands:
  demo      — end-to-end: enable → mandate → submit → work → crash
              injection → restart/resume → reconcile → evidence summary
  work      — one bounded work cycle (the "second process" worker)
  status    — run/ledger/counter summary for the tenant
"""
from __future__ import annotations

import json
import sys
from decimal import Decimal
from typing import Any

from openexecutive.bo.db import initialize_db
from openexecutive.bo.execution import engine, store
from openexecutive.bo.execution.synth import SyntheticCounterProvider

TENANT_DEFAULT = "local"


def _tenant() -> str:
    import os

    return os.environ.get("BO_TENANT_ID", TENANT_DEFAULT)


def _enable(tenant: str) -> None:
    from openexecutive.bo.settings import store as settings_store

    try:
        current = settings_store.get_effective_value(
            tenant, "bo.exec.enabled"
        )
        version = 0
        from openexecutive.bo.db import get_conn

        with get_conn() as conn:
            row = conn.execute(
                "SELECT version FROM bo_settings WHERE tenant = ? AND key = ?",
                (tenant, "bo.exec.enabled"),
            ).fetchone()
        version = 0 if row is None else int(row["version"])
        if not current:
            settings_store.set_value(
                tenant, "bo.exec.enabled", True,
                expected_version=version, actor="cli",
            )
    except Exception:
        settings_store.set_value(
            tenant, "bo.exec.enabled", True,
            expected_version=0, actor="cli",
        )


def cmd_work(tenant: str, worker: str | None = None) -> dict[str, Any]:
    provider = SyntheticCounterProvider(idempotent=True)
    return engine.work_once(tenant, provider=provider, worker_id=worker)


def cmd_status(tenant: str) -> dict[str, Any]:
    provider = SyntheticCounterProvider(idempotent=True)
    runs = store.list_runs(tenant)
    return {
        "tenant": tenant,
        "enabled": engine.enabled(tenant),
        "runs": [
            {
                "run_id": r["run_id"], "state": r["state"],
                "current_step": r["current_step"],
                "steps": len(r["steps"]),
                "block_reason": r["block_reason"],
                "ledger": store.ledger_counts(tenant, r["run_id"]),
            }
            for r in runs
        ],
        "synthetic_total": provider.total(tenant),
        "synthetic_effects": provider.effect_count(tenant),
    }


def cmd_demo(tenant: str) -> dict[str, Any]:
    """One-command reproducible demo — all effects synthetic and local."""
    from datetime import UTC, datetime, timedelta

    initialize_db()
    _enable(tenant)
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")
    mandate = store.create_mandate(
        tenant,
        {
            "allowed_resources": ["synth.*"],
            "allowed_actions": ["increment"],
            "budget_limit": "10",
            "concurrency_limit": 2,
            "max_steps": 10,
            "max_depth": 1,
            "expires_at": expiry,
        },
        parent=None, principal_ref="actor_demo",
        policy_version=0, actor="demo", max_depth_cap=3,
    )
    provider = SyntheticCounterProvider(idempotent=True)
    run = engine.submit_execution(
        tenant, mandate.mandate_id,
        [
            {"action": "increment", "resource": "synth.counter",
             "payload": {"amount": 3}},
            {"action": "increment", "resource": "synth.counter",
             "payload": {"amount": 2}},
        ],
        budget_amount=Decimal("5"), correlation_id=None, actor="demo",
    )
    # Crash injection: worker claims, persists the step-0 checkpoint,
    # then "dies" — no effect, no transition. The run stays claimable
    # once the lease expires.
    import time

    claimed = store.claim_runs(
        tenant, worker_id="demo-w1", limit=1, lease_s=1
    )[0]
    store.write_checkpoint(
        tenant, run["run_id"], 0,
        {"phase": "pre", "step": 0, "action": "increment",
         "resource": "synth.counter"},
    )
    time.sleep(1.1)  # lease expiry — the "restart"
    outcome = engine.work_once(tenant, provider=provider, worker_id="demo-w2")
    checkpoints = store.list_checkpoints(tenant, run["run_id"])
    ledger = store.list_ledger(tenant, run["run_id"])
    return {
        "mandate_id": mandate.mandate_id,
        "run_id": run["run_id"],
        "crash_simulated": {
            "worker": "demo-w1",
            "claimed_lease_seq": claimed["lease_seq"],
            "checkpoint_written_before_crash": True,
            "effect_executed": False,
        },
        "resume": outcome,
        "final_state": store.get_run(tenant, run["run_id"])["state"],
        "checkpoints": len(checkpoints),
        "ledger": [
            {
                "step": e["step"], "status": e["status"],
                "receipt_ref": e["receipt_ref"],
                "idempotency_key": e["idempotency_key"],
            }
            for e in ledger
        ],
        "synthetic_total": provider.total(tenant),
        "synthetic_effects": provider.effect_count(tenant),
        "note": "toate efectele sunt sintetice și locale; counter-ul "
        "dovedește absența dublurilor",
    }


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    tenant = _tenant()
    initialize_db()
    if not args or args[0] == "demo":
        out = cmd_demo(tenant)
    elif args[0] == "work":
        out = cmd_work(tenant, worker=args[1] if len(args) > 1 else None)
    elif args[0] == "status":
        out = cmd_status(tenant)
    else:
        print(f"comandă necunoscută: {args[0]}", file=sys.stderr)
        return 2
    print(json.dumps(out, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
