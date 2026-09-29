"""VAL4-03 P5 — exercițiu sintetic de upgrade + restore.

Rulează pe o COPIE sintetică — niciodată pe DB-ul real:

    python tools/val4/upgrade_restore_a01.py --dir /tmp/upg

Scenariul reprodus:

1. Stare „VAL4-01" (înainte de REM-01): DB cu execuție în curs, o
   intrare de ledger UNKNOWN (efect comis, răspuns pierdut) și plicuri
   în coadă — apoi coloana ``guardian_ref`` e eliminată din
   ``bo_exec_mandates`` pentru a reproduce schema pre-migrare.
2. Upgrade: ``initialize_db`` re-adaugă coloana (ALTER idempotent) —
   datele rămân byte-identice, mandatele vechi rămân NELEGATE
   (standalone) — migrația nu inventează legături.
3. Restart + recuperare: ``work_once`` pe runul UNKNOWN face întâi
   lookup de chitanță la provider — efectul există → SUCCEEDED fără
   reexecuție (contorul nu se dublează).
4. Restore: se restaurează backupul DB-ului de produs făcut ÎNAINTE de
   rezolvare. Providerul trăiește pe FIȘIER SEPARAT — restore-ul nu
   anulează efectul extern (contorul rămâne), iar intrarea restaurată
   UNKNOWN e rezolvată prin chitanță, nu prin retry orb.

Precondiții/limite (documentate): exercițiu local, un singur proces,
fără Guardian (autorizarea externă e probată separat în
driver_a01.py probe). „Restore" = copierea unui snapshot consistent —
nu se simulează restore dintr-un backup corupt/parțial. Rollback =
restaurarea backupului + rularea codului vechi; efectele externe NU
revin — ele trebuie reconciliate, nu reexecutate.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from decimal import Decimal
from pathlib import Path


def _setup(db: Path) -> None:
    os.environ["BOAGENTS_DB_PATH"] = str(db)
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]
                          / "packages" / "core"))


def _snapshot(db: Path) -> dict[str, str]:
    """Amprenta fiecărui tabel — dovada că upgrade-ul nu atinge datele."""
    out: dict[str, str] = {}
    with sqlite3.connect(db) as conn:
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name LIKE 'bo_%' ORDER BY name")]
        for t in tables:
            rows = conn.execute(f"SELECT * FROM {t} ORDER BY rowid")
            h = hashlib.sha256()
            for row in rows:
                h.update(repr(row).encode())
            out[t] = h.hexdigest()[:16]
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True, help="director de lucru nou")
    args = ap.parse_args()
    work = Path(args.dir)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    db = work / "product.db"
    prov_db = work / "provider.db"  # adevărul extern — supraviețuiește restore-ului
    _setup(db)

    from openexecutive.bo.db import initialize_db
    from openexecutive.bo.execution import engine, store
    from openexecutive.bo.execution.synth import SyntheticCounterProvider
    from openexecutive.bo.routing import store as routing_store
    from openexecutive.bo.settings import store as settings_store

    report: dict[str, object] = {"dir": str(work)}

    # -- 1. Stare „în curs" ------------------------------------------------
    initialize_db()
    initialize_db(db_path=prov_db)  # schema providerului pe fișierul lui
    tenant = "tenant-alpha"
    settings_store.set_value(
        tenant, "bo.exec.enabled", True, expected_version=0, actor="upg")
    from datetime import UTC, datetime, timedelta
    expiry = (datetime.now(UTC) + timedelta(hours=48)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    mandate = store.create_mandate(
        tenant,
        {"allowed_resources": ["synth.*"], "allowed_actions": ["increment"],
         "budget_limit": "10", "concurrency_limit": 2,
         "max_steps": 10, "max_depth": 1,
         "expires_at": expiry},
        parent=None, principal_ref="actor_upg", policy_version=0,
        actor="upg", max_depth_cap=3,
    )
    run = engine.submit_execution(
        tenant, mandate.mandate_id,
        [{"action": "increment", "resource": "synth.counter",
          "payload": {"amount": 3}}],
        budget_amount=Decimal("3"), correlation_id=None, actor="upg")
    # UNKNOWN: providerul comite efectul, răspunsul se pierde.
    prov = SyntheticCounterProvider(
        idempotent=True, fail_after_write=True, db_path=prov_db)
    out = engine.work_once(
        tenant, provider=prov, worker_id="w-old", db_path=db)
    assert out["outcomes"][0]["state"] == store.RUN_UNKNOWN
    assert prov.total(tenant) == 3  # efectul EXISTĂ extern
    routing_store.enqueue_outbox(
        tenant, "execution", None,
        {"schemaVersion": "bo.execution-control.event.v1",
         "eventId": "ev-upg-pending", "eventType": "checkpoint",
         "observedAt": "2026-09-25T10:00:00Z",
         "correlationId": "corr-upg", "tenantRef": tenant,
         "checkpoint": {"executionRef": run["run_id"],
                        "mandateRef": mandate.mandate_id, "step": 0,
                        "state": "PENDING", "policyVersion": "pol_0"}})
    before = _snapshot(db)

    # Simulează schema pre-REM-01: fără coloana guardian_ref.
    with sqlite3.connect(db) as conn:
        conn.execute(
            "ALTER TABLE bo_exec_mandates DROP COLUMN guardian_ref")
    report["pre_upgrade"] = {
        "run_state": store.get_run(tenant, run["run_id"])["state"],
        "provider_total": prov.total(tenant),
        "tables": before,
    }

    # -- 2. Upgrade (migrația REM-01) --------------------------------------
    initialize_db()  # re-adaugă guardian_ref, datele neatcinse
    with sqlite3.connect(db) as conn:
        cols = [r[1] for r in conn.execute(
            "PRAGMA table_info(bo_exec_mandates)")]
    assert "guardian_ref" in cols
    after = _snapshot(db)
    unchanged = {
        t: before[t] == after[t]
        for t in before if t != "bo_exec_mandates"
    }
    report["upgrade"] = {
        "guardian_ref_readded": True,
        "data_unchanged": all(unchanged.values()),
        "tables_compared": unchanged,
        "old_mandate_still_unbound": (
            store.get_mandate(tenant, mandate.mandate_id).guardian_ref
            is None
        ),
    }

    # -- 3. Restart + recuperare -------------------------------------------
    # Backup ÎNAINTE de rezolvare — restore-ul va readuce UNKNOWN-ul.
    backup = work / "product.backup.db"
    shutil.copy2(db, backup)
    prov2 = SyntheticCounterProvider(idempotent=True, db_path=prov_db)
    engine.resume_run(tenant, run["run_id"], actor="upg")
    out = engine.work_once(
        tenant, provider=prov2, worker_id="w-new", db_path=db)
    report["recovery"] = {
        "run_state": out["outcomes"][0]["state"],
        "provider_total": prov2.total(tenant),
        "submit_calls": prov2.submit_calls,
    }
    assert out["outcomes"][0]["state"] == store.RUN_SUCCEEDED
    assert prov2.total(tenant) == 3  # aceeași cantitate — fără dublare
    assert prov2.submit_calls == 0  # rezolvat prin chitanță, nu retry

    # -- 4. Restore — efectul extern NU se anulează -------------------------
    shutil.copy2(backup, db)  # restore: UNKNOWN revine în DB
    prov3 = SyntheticCounterProvider(idempotent=True, db_path=prov_db)
    restored_state = store.get_run(tenant, run["run_id"])["state"]
    entry = store.list_ledger(tenant, run["run_id"])[0]
    out = engine.work_once(
        tenant, provider=prov3, worker_id="w-restored", db_path=db)
    # run UNKNOWN nu e claimable — reluare explicită mai întâi.
    if restored_state == store.RUN_UNKNOWN and out["claimed"] == 0:
        engine.resume_run(tenant, run["run_id"], actor="upg")
        out = engine.work_once(
            tenant, provider=prov3, worker_id="w-restored", db_path=db)
    report["restore"] = {
        "restored_state": restored_state,
        "restored_ledger_status": entry["status"],
        "provider_total_after_restore": prov3.total(tenant),
        "submit_calls_after_restore": prov3.submit_calls,
        "final_state": out["outcomes"][0]["state"]
        if out.get("outcomes") else None,
        "nota": "restore-ul a readus starea UNKNOWN, dar contorul "
                "extern a rămas 3 — efectul nu s-a anulat; rezolvarea "
                "s-a făcut prin chitanță (0 submit-uri), nu retry orb.",
    }
    assert prov3.total(tenant) == 3
    assert prov3.submit_calls == 0

    report["verdict"] = "PASS"
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
