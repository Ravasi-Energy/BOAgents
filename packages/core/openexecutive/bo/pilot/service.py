"""Signed diagnostic activation over the existing deterministic runtime.

Only the built-in diagnostic grammar is executable. Package artifacts are data,
never imported Python, shell commands or arbitrary network destinations.
"""
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from openexecutive.bo.db import get_conn
from openexecutive.bo.execution import engine, store
from openexecutive.bo.identity import Identity, require
from openexecutive.bo.packages import service as packages
from openexecutive.bo.packages import store as package_store
from openexecutive.bo.packages.verify import verify_package
from openexecutive.bo.settings import store as settings

PACKAGE_ID = "pkg.synthetic-erp"
RESOURCE = "synth.erp"
ACTION = "diagnose"
CONTENT = {
    "schema_version": "bo.bobot.v1", "trigger": {"type": "manual"},
    "steps": [{"id": "diagnose", "type": "note", "message": "Diagnostic ERP sintetic: health, versiune, coadă și receipt; fără LLM."}],
    "capability_refs": ["synth_erp:diagnose"], "policy_refs": [],
}


def initialize_db(db_path=None):
    with get_conn(db_path) as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_pilot_activation (
            tenant TEXT PRIMARY KEY, import_id TEXT NOT NULL, version INTEGER NOT NULL,
            active INTEGER NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
            updated_at TEXT NOT NULL)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS bo_pilot_observations (
            tenant TEXT NOT NULL, run_id TEXT NOT NULL, event_json TEXT NOT NULL,
            PRIMARY KEY(tenant,run_id))""")


def configuration(tenant, db_path=None):
    from openexecutive.bo.settings.registry import REGISTRY
    return {k.removeprefix("bo.pilot."): settings.get_effective_value(tenant, k, db_path=db_path)
            for k in REGISTRY if k.startswith("bo.pilot.")}


def config_hash(config):
    return hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()


def activation(tenant, db_path=None):
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM bo_pilot_activation WHERE tenant=?", (tenant,)).fetchone()
    return dict(row) if row else None


def verify_import(tenant, import_id, db_path=None):
    row = package_store.get_import(tenant, import_id, db_path=db_path)
    if row["status"] != "DRAFT" or row["package_id"] != PACKAGE_ID:
        raise package_store.StateError("Pilotul cere pachetul sintetic verificat și promovat la DRAFT")
    identity = Identity("pilot-verifier", tenant, "admin", True, "shared_secret")
    verdict = verify_package(Path(row["stored_path"]), packages._trust_registry(identity, db_path),
                             tenant_ref=tenant, host_version=packages.BO_PRODUCT_VERSION)
    if not verdict.accepted or verdict.manifest_digest != row["manifest_digest"]:
        raise package_store.StateError(f"Verificare curentă refuzată: {verdict.code}")
    manifest = json.loads((Path(row["stored_path"]) / "manifest.json").read_text())
    if set(manifest["artifactDigests"]) != {"bots/diagnostic.json"} or manifest["requestedCapabilities"] != ["synth_erp:diagnose"]:
        raise package_store.StateError("Pachetul nu declară exact diagnosticul sintetic")
    if json.loads((Path(row["stored_path"]) / "bots/diagnostic.json").read_text()) != CONTENT:
        raise package_store.StateError("Gramatică diagnostic incompatibilă; scripturile nu se execută")
    return row


def change_activation(identity, import_id, active, expected_version, reason, db_path=None):
    require(identity, "packages:write")
    if active:
        verify_import(identity.tenant, import_id, db_path)
    now = datetime.now(UTC).isoformat()
    with get_conn(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        old = conn.execute("SELECT * FROM bo_pilot_activation WHERE tenant=?", (identity.tenant,)).fetchone()
        if (old["version"] if old else 0) != expected_version:
            raise store.ConflictError("Activarea a fost modificată; reîncarcă înainte de salvare")
        if not active and (not old or old["import_id"] != import_id):
            raise package_store.StateError("Activare inexistentă")
        conn.execute("""INSERT INTO bo_pilot_activation VALUES (?,?,?,?,?,?,?)
            ON CONFLICT(tenant) DO UPDATE SET import_id=excluded.import_id,
            version=excluded.version,active=excluded.active,actor=excluded.actor,
            reason=excluded.reason,updated_at=excluded.updated_at""",
                     (identity.tenant, import_id, expected_version + 1, int(active), identity.actor, reason, now))
    packages._audit(identity.tenant, identity.actor, "bo_pilot_activation",
                    "Activare diagnostic sintetic" if active else "Dezactivare diagnostic; istoric păstrat",
                    {"import_id": import_id, "version": expected_version + 1, "active": active, "reason": reason})
    return activation(identity.tenant, db_path)


def guard(tenant, payload=None, db_path=None):
    from openexecutive.bo.pilot.config import endpoint
    config = configuration(tenant, db_path)
    if not config["enabled"] or config["profile"] != "synthetic-loopback":
        raise package_store.StateError("Pilot dezactivat sau profil sintetic neconfigurat")
    target = endpoint(config["endpoint"])
    if not target or target not in json.loads(config["allowlist"]):
        raise package_store.StateError("Endpointul nu este în allowlist-ul administrat")
    active = activation(tenant, db_path)
    if not active or not active["active"]:
        raise package_store.StateError("Pachetul nu este activat")
    row = verify_import(tenant, active["import_id"], db_path)
    if payload is not None and (payload.get("activation_version") != active["version"]
                               or payload.get("config_hash") != config_hash(config)
                               or payload.get("manifest_digest") != row["manifest_digest"]):
        raise package_store.StateError("Configurația/activarea diferă de plan; fără retrimitere pe alt endpoint")
    return config, active, row


def submit(identity, mandate_id, correlation_id=None, db_path=None):
    require(identity, "execution:write")
    config, active, row = guard(identity.tenant, db_path=db_path)
    if config["supervision"] == "required" and not any(
        m.guardian_ref for m in store.mandate_chain(identity.tenant, mandate_id, db_path=db_path)
    ):
        raise package_store.StateError("Pilotul cere autoritate Guardian; leagă un mandat existent")
    return engine.submit_execution(identity.tenant, mandate_id,
        [{"action": ACTION, "resource": RESOURCE, "payload": {
            "amount": 1, "activation_version": active["version"],
            "endpoint": config["endpoint"],
            "config_hash": config_hash(config), "manifest_digest": row["manifest_digest"],
        }}], budget_amount=Decimal("1"), correlation_id=correlation_id,
        actor=identity.actor, db_path=db_path)


def _decode_evidence(raw):
    """Persisted observation bytes → dict. Corrupt or non-object payloads are
    a controlled refusal (409 via StateError), never an unhandled decode
    error and never a fabricated observation."""
    try:
        event = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        raise package_store.StateError(
            "Dovada observației persistate este coruptă (JSON necitibil); "
            "nu se reemite și nu se fabrică o observație") from None
    if not isinstance(event, dict):
        raise package_store.StateError(
            "Dovada observației persistate nu este un plic valid; "
            "nu se reemite și nu se fabrică o observație")
    return event


def _evidence_usable(event):
    """Minimum shape the status view can render — ``eventId`` for the
    expected outbox identities, ``observedAt`` for staleness and
    ``service.queuePending`` for the health call. Anything else is corrupt
    evidence: marked as such, preserved untouched, never parsed further."""
    return (
        isinstance(event, dict)
        and isinstance(event.get("eventId"), str)
        and isinstance(event.get("observedAt"), str)
        and isinstance(event.get("service"), dict)
        and type(event["service"].get("queuePending")) is int
    )


def _corrupt_report(marker=None, error=None):
    """Telemetry posture for a run whose persisted evidence is unreadable:
    the bytes stay in storage, nothing is replayable and the receipt in the
    ledger remains the authority on the effect."""
    return {"status": "corrupt", "marker": marker,
            "error": error or "observation_corrupt", "expected": 0,
            "queued": 0, "delivered": 0, "dead": 0, "missing": 0,
            "replayable": False}


def _telemetry_report(tenant, event, ledger, db_path=None):
    """Operator-facing posture of the observation evidence on the durable
    outbox. The receipt remains the proof of effect; this report only
    describes whether the persisted telemetry reached the delivery queue —
    "queued" is a local claim state, never Guardian-confirmed delivery."""
    from openexecutive.bo.pilot.provider import expected_event_ids
    from openexecutive.bo.routing import store as outbox_store
    marker = error = None
    for entry in ledger:
        receipt = entry.get("receipt") or {}
        if receipt.get("telemetryStatus"):
            marker, error = receipt["telemetryStatus"], receipt.get("telemetryError")
    report = {"status": "none", "marker": marker, "error": error, "expected": 0,
              "queued": 0, "delivered": 0, "dead": 0, "missing": 0, "replayable": False}
    if not event:
        # A rejected/lost observation leaves no evidence to rebuild from;
        # a valid receipt alone cannot fabricate health/queue/version.
        report["status"] = "unavailable" if marker else "none"
        return report
    ids = expected_event_ids(event)
    try:
        rows = outbox_store.outbox_state(tenant, ids, db_path=db_path)
    except Exception:  # noqa: BLE001 — the storage outage itself must be
        # visible, not a 500: the report degrades with the marker kept, the
        # run stays readable and replayable once the outbox store is back.
        report.update(status="degraded", expected=len(ids), missing=len(ids),
                      replayable=True, error="outbox_unreachable")
        return report
    queued = sum(1 for i in ids if i in rows)
    missing = len(ids) - queued
    report.update(expected=len(ids), queued=queued, missing=missing,
                  delivered=sum(1 for i in ids if rows.get(i) == 1),
                  dead=sum(1 for i in ids if rows.get(i) == 2))
    if missing:
        report["status"], report["replayable"] = "degraded", True
    elif report["dead"]:
        report["status"] = "dead"
    elif marker:
        report["status"] = "incident"
    elif report["delivered"] < report["expected"]:
        report["status"] = "pending"
    else:
        report["status"] = "ok"
    return report


def replay_telemetry(identity, run_id, db_path=None):
    """Authorized re-queue of missing outbox envelopes from the persisted
    observation — a replay of kept evidence, never a new effect, provider
    call or invented event. Idempotent by outbox eventId; a second replay
    queues nothing. Dead-lettered envelopes keep their history and stay
    under the existing operator retry flow, untouched here."""
    require(identity, "execution:write")
    run = store.get_run(identity.tenant, run_id, db_path=db_path)
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT event_json FROM bo_pilot_observations WHERE tenant=? AND run_id=?",
                           (identity.tenant, run_id)).fetchone()
    if not row:
        raise package_store.StateError(
            "Nicio observație persistată pentru rulare; receiptul singur nu reconstruiește telemetria")
    event = _decode_evidence(row["event_json"])
    from openexecutive.bo.pilot.observation import validate
    try:
        validate(event, identity.tenant, run)
    except ValueError:
        raise package_store.StateError(
            "Dovada observației nu mai corespunde identității curente; fără reemisie") from None
    from openexecutive.bo.pilot.provider import emit_observation_events
    try:
        result = emit_observation_events(identity.tenant, run, event, db_path=db_path)
    except Exception:
        raise package_store.StateError(
            "Outboxul de telemetrie este indisponibil; reîncercați după "
            "restabilirea stocării cozii") from None
    packages._audit(identity.tenant, identity.actor, "bo_pilot_telemetry_replay",
                    "Reemisie telemetrie din dovada persistată; fără efect nou",
                    {"run_id": run_id, **result})
    ledger = store.list_ledger(identity.tenant, run_id, db_path=db_path)
    return {"run_id": run_id, **result,
            "telemetry": _telemetry_report(identity.tenant, event, ledger, db_path=db_path)}


def status(identity, db_path=None):
    require(identity, "execution:read")
    runs = [r for r in store.list_runs(identity.tenant, db_path=db_path)
            if any(s.get("resource") == RESOURCE for s in r["steps"])]
    config = configuration(identity.tenant, db_path)
    observations: dict[str, dict] = {}
    corrupt: set[str] = set()
    with get_conn(db_path) as conn:
        for row in conn.execute(
                "SELECT run_id, event_json FROM bo_pilot_observations WHERE tenant=?",
                (identity.tenant,)):
            try:
                event = json.loads(row["event_json"])
            except ValueError:
                event = None
            if _evidence_usable(event):
                observations[row["run_id"]] = event
            else:
                # Corrupt bytes are preserved in the table but never parsed
                # again: the run is marked, the rest of the tenant's status
                # keeps working.
                corrupt.add(row["run_id"])
    for run in runs:
        event = observations.get(run["run_id"])
        run["observation"] = event
        run["ledger"] = store.list_ledger(identity.tenant, run["run_id"], db_path=db_path)
        run["health"] = "UNKNOWN"
        if run["run_id"] in corrupt:
            marker = next((e["receipt"]["telemetryStatus"] for e in run["ledger"]
                           if isinstance(e.get("receipt"), dict)
                           and e["receipt"].get("telemetryStatus")), None)
            run["telemetry"] = _corrupt_report(marker=marker)
            continue
        run["telemetry"] = _telemetry_report(identity.tenant, event, run["ledger"], db_path=db_path)
        if event:
            try:
                age = (datetime.now(UTC) - datetime.fromisoformat(event["observedAt"].replace("Z", "+00:00"))).total_seconds()
                run["health"] = "STALE" if age > config["stale_s"] else (
                    "DEGRADED" if event["service"]["queuePending"] > config["max_queue"] else "HEALTHY")
            except (TypeError, ValueError):  # malformed timestamp → same
                run["observation"] = None    # controlled corrupt marking
                run["telemetry"] = _corrupt_report()
        if run["state"] != store.RUN_SUCCEEDED:
            run["health"] = "UNKNOWN"
    return {"synthetic": True, "role": identity.role, "activation": activation(identity.tenant, db_path),
            "config": config, "runs": runs,
            "note": "Observabilitatea nu acordă autoritate. UNKNOWN cere readback; fără ERP real sau LLM."}
