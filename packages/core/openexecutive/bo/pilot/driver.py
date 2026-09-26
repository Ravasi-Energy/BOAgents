"""Finite synthetic pilot driver. Existing Python environment; no real ERP."""
import argparse
import json
import os
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--guardian", default="")
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--scenario", choices=["healthy", "delayed", "unknown", "recover"], required=True)
    parser.add_argument("--service-port", type=int, default=8325)
    args = parser.parse_args()
    args.work.mkdir(parents=True, exist_ok=True)
    os.environ["EPISODIC_DB_PATH"] = str(args.work / "audit.db")
    os.environ["BOAGENTS_DB_PATH"] = str(args.work / "bo.db")
    os.environ["BO_PACKAGES_DIR"] = str(args.work / "quarantine")
    os.environ["BO_TENANT_ID"] = args.tenant
    token = os.environ.get("BO_PILOT_SERVICE_TOKEN")
    if not token:
        parser.error("BO_PILOT_SERVICE_TOKEN este obligatoriu (numai fixture sintetic)")
    if args.guardian and not os.environ.get("BO_PILOT_GUARDIAN_MANDATE"):
        parser.error("BO_PILOT_GUARDIAN_MANDATE trebuie provisionat de owner Guardian; nu inventez autoritate")
    from openexecutive.bo import db
    from openexecutive.bo.execution import engine, store
    from openexecutive.bo.execution.synth import SyntheticCounterProvider
    from openexecutive.bo.identity import Identity
    from openexecutive.bo.packages import service as packages
    from openexecutive.bo.pilot import fixture, service
    from openexecutive.bo.settings import store as settings
    db.DB_PATH = args.work / "bo.db"
    db.initialize_db()
    identity = Identity("synthetic-admin", args.tenant, "admin", True, "shared_secret")
    endpoint = f"http://127.0.0.1:{args.service_port}"
    httpd = fixture.server(args.work / "service.db", {token: args.tenant}, args.service_port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    def call(path, body=None):
        request = Request(endpoint + path, data=json.dumps(body).encode() if body else None,
                          headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
        with urlopen(request, timeout=3) as response:
            return json.load(response)
    try:
        call("/scenario", {"scenario": args.scenario})
        checkpoint = args.work / "driver-state.json"
        if not checkpoint.exists():
            package_dir = fixture.package(args.work)
            values = {"bo.packages.enabled": True, "bo.exec.enabled": True,
                "bo.packages.trust_store_json": (args.work / "registry.json").read_text(),
                "bo.pilot.enabled": True, "bo.pilot.profile": "synthetic-loopback",
                "bo.pilot.endpoint": endpoint, "bo.pilot.allowlist": json.dumps([endpoint]),
                "bo.pilot.supervision": "required" if args.guardian else "standalone"}
            if args.guardian:
                values["bo.exec.guardian_endpoint"] = args.guardian
            for key, value in values.items():
                settings.set_value(args.tenant, key, value, expected_version=0, actor=identity.actor)
            row = packages.import_package(identity, package_dir)
            packages.promote_to_draft(identity, row["id"])
            service.change_activation(identity, row["id"], True, 0, "Aprobare explicită fixture sintetic")
            mandate = store.create_mandate(args.tenant, {
                "allowed_actions": ["diagnose"], "allowed_resources": ["synth.erp"],
                "budget_limit": "100", "concurrency_limit": 2, "max_steps": 1, "max_depth": 2,
                "expires_at": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
                parent=None, principal_ref="synthetic-admin", policy_version=1, actor=identity.actor,
                max_depth_cap=3, guardian_ref=os.environ.get("BO_PILOT_GUARDIAN_MANDATE"))
            state = {"mandate_id": mandate.mandate_id}
            checkpoint.write_text(json.dumps(state))
        else:
            state = json.loads(checkpoint.read_text())
        provider = SyntheticCounterProvider(idempotent=True)
        if args.scenario == "recover":
            if not state.get("run_id"):
                parser.error("recover cere rularea UNKNOWN existentă în același --work")
            engine.reconcile_run(args.tenant, state["run_id"], provider, resolution="receipt", actor=identity.actor)
            engine.resume_run(args.tenant, state["run_id"], actor=identity.actor)
        else:
            run = service.submit(identity, state["mandate_id"])
            state["run_id"] = run["run_id"]
            checkpoint.write_text(json.dumps(state))
        engine.work_once(args.tenant, provider=provider, limit=1)
        run = store.get_run(args.tenant, state["run_id"])
        ledger = store.list_ledger(args.tenant, state["run_id"])
        entry = ledger[0] if ledger else {}
        result = {"serviceRef": "synthetic-erp", "executionRef": run["run_id"],
            "correlationId": run["correlation_id"], "receiptRef": entry.get("receipt_ref"),
            "payloadDigest": entry.get("payload_digest"), "idempotencyKey": entry.get("idempotency_key"),
            "state": run["state"], **call("/stats"),
            "provenance": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()}
        if args.guardian:
            from openexecutive.bo.routing.delivery import deliver_pending
            result["delivery"] = deliver_pending(args.tenant)
        (args.work / f"{args.scenario}-result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main()
