"""Consumer validation of SOL-03's published service-observation v1 mapping."""
import os
import re
from datetime import UTC, datetime


def validate(event, tenant, run):
    keys = {"schemaVersion", "eventId", "producerId", "installationId", "tenantRef", "product", "observedAt", "correlationId", "service"}
    service_keys = {"serviceRef", "ownerRef", "version", "synthetic", "queuePending", "oldestPendingAt", "executionRef", "evidenceRefs"}
    if not isinstance(event, dict) or set(event) != keys:
        raise ValueError("Observation envelope invalid")
    expected = {"schemaVersion": "bo.service-observation.v1", "tenantRef": tenant, "product": "BOAgents",
                "producerId": os.environ.get("BO_TELEMETRY_PRODUCER_ID", "boagents"),
                "installationId": os.environ.get("BO_INSTALLATION_ID", "local-installation"),
                "correlationId": run["correlation_id"]}
    if any(event.get(k) != v for k, v in expected.items()):
        raise ValueError("Observation identity mismatch")
    service = event["service"]
    if not isinstance(service, dict) or set(service) != service_keys or service["synthetic"] is not True \
            or service["executionRef"] != run["run_id"]:
        raise ValueError("Observation service mismatch")
    for value in [event["eventId"], service["serviceRef"], service["ownerRef"], service["version"]]:
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
            raise ValueError("Observation opaque reference invalid")
    refs = service["evidenceRefs"]
    if not isinstance(refs, list) or not 1 <= len(refs) <= 16:
        raise ValueError("Observation evidence invalid")
    for ref in refs:
        if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", ref):
            raise ValueError("Observation evidence reference invalid")
    count = service["queuePending"]
    if type(count) is not int or not 0 <= count <= 1000000 or ((count == 0) != (service["oldestPendingAt"] is None)):
        raise ValueError("Observation queue invalid")
    for stamp in [event["observedAt"], *([service["oldestPendingAt"]] if count else [])]:
        if not isinstance(stamp, str) or not stamp.endswith("Z"):
            raise ValueError("Observation timestamp invalid")
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        if parsed > datetime.now(UTC):
            raise ValueError("Observation future timestamp")
