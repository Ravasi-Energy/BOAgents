"""BO-I01 remediation: BoBot simulation -> bo.telemetry.v1 events -> local
Guardian receiver, over an injectable transport with zero external effects.

The receiver is a test double that models the ingestion contract the real
BOGuardian route enforces: enrollment of (producerId, installationId) to a
tenant, a shared credential, strict schema validation, and eventId dedup.
It lives in the test — no Guardian code is imported or required.
"""
from __future__ import annotations

import pytest

from openexecutive.bo.bots import service
from openexecutive.bo.bots.examples import HEARTBEAT_STALE
from openexecutive.bo.telemetry import schema
from openexecutive.bo.telemetry.adapter import (
    BufferedTransport,
    TelemetryAdapter,
    set_adapter,
)
from openexecutive.bo.telemetry.schema import TelemetrySchemaError

from .bo_testkit import capture_audit, use_tmp_db


class LocalGuardianReceiver:
    """Minimal Guardian-ingestion double: enrollment + credential + schema
    + dedup. ``ingest`` returns True on accept, raises on reject."""

    def __init__(self) -> None:
        self.enrolled: dict[tuple[str, str], str] = {}
        self.credentials: dict[tuple[str, str], str] = {}
        self.seen_ids: set[str] = set()
        self.received: list[dict] = []

    def enroll(self, producer_id: str, installation_id: str,
               tenant: str, credential: str) -> None:
        key = (producer_id, installation_id)
        self.enrolled[key] = tenant
        self.credentials[key] = credential

    def ingest(self, event: dict, credential: str) -> bool:
        key = (event.get("producerId"), event.get("installationId"))
        if key not in self.enrolled:
            raise PermissionError("producător neînrolat")
        if credential != self.credentials[key]:
            raise PermissionError("credențial invalid")
        if event.get("tenantRef") != self.enrolled[key]:
            raise PermissionError("tenantRef nu aparține înrolării")
        schema.validate_event(event)  # the shared contract gate
        if event["eventId"] in self.seen_ids:
            raise ValueError("eventId duplicat")
        self.seen_ids.add(event["eventId"])
        self.received.append(event)
        return True


@pytest.fixture(autouse=True)
def transport(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    use_tmp_db(tmp_path, monkeypatch)
    capture_audit(monkeypatch)
    transport = BufferedTransport()
    adapter = TelemetryAdapter(
        enabled=True, transport=transport,
        producer_id="boagents", installation_id="install-e2e")
    set_adapter(adapter)
    yield transport
    set_adapter(None)


def _simulate_run(transport: BufferedTransport) -> dict:  # noqa: ANN001
    definition = service.create("tenant-a", "ops@corp.dev", {
        "name": "hb", "kind": "BOT",
        "content": HEARTBEAT_STALE["content"],
    })
    service.publish("tenant-a", "ops@corp.dev", definition["id"],
                    expected_version=definition["draft_version"])
    return service.simulate("tenant-a", "ops@corp.dev", definition["id"],
                            input_context={
                                "service": {"name": "svc-x",
                                            "last_heartbeat_age_minutes": 45}
                            })


def test_simulation_events_reach_guardian_receiver(
        transport: BufferedTransport) -> None:
    receiver = LocalGuardianReceiver()
    receiver.enroll("boagents", "install-e2e", "tenant-a", "cred-1")

    run = _simulate_run(transport)

    kinds = [e["kind"] for e in transport.events]
    assert kinds == ["RunStarted", "RunFinished", "VerificationFinding"]
    for event in transport.events:
        assert receiver.ingest(event, credential="cred-1") is True
    assert len(receiver.received) == 3
    # The received events all point back at the same run — correlation rides
    # the envelope, the run ref is never duplicated inside data.
    assert {e["runRef"] for e in receiver.received} == {run["id"]}
    assert {e["correlationId"] for e in receiver.received} == {run["id"]}
    assert "runRef" not in receiver.received[0]["data"]


def test_receiver_rejects_unenrolled_producer(
        transport: BufferedTransport) -> None:
    _simulate_run(transport)
    receiver = LocalGuardianReceiver()  # nobody enrolled
    for event in transport.events:
        with pytest.raises(PermissionError):
            receiver.ingest(event, credential="cred-1")


def test_receiver_rejects_bad_credential_and_tenant_mismatch(
        transport: BufferedTransport) -> None:
    _simulate_run(transport)
    receiver = LocalGuardianReceiver()
    receiver.enroll("boagents", "install-e2e", "tenant-a", "cred-1")
    event = transport.events[0]
    with pytest.raises(PermissionError):
        receiver.ingest(event, credential="wrong")
    cross = dict(event, tenantRef="tenant-b")
    with pytest.raises(PermissionError):
        receiver.ingest(cross, credential="cred-1")


def test_receiver_rejects_replay_and_tampered_payload(
        transport: BufferedTransport) -> None:
    _simulate_run(transport)
    receiver = LocalGuardianReceiver()
    receiver.enroll("boagents", "install-e2e", "tenant-a", "cred-1")
    for event in transport.events:
        receiver.ingest(event, credential="cred-1")
    # Replay: same eventId twice
    with pytest.raises(ValueError, match="duplicat"):
        receiver.ingest(transport.events[0], credential="cred-1")
    # Tampered payload still carries a fresh id -> schema gate catches it
    tampered = dict(transport.events[2])
    tampered["eventId"] = "evt_tampered1"
    tampered["data"] = dict(tampered["data"], severity="warning")
    with pytest.raises(TelemetrySchemaError):
        receiver.ingest(tampered, credential="cred-1")


def test_emitted_events_carry_contract_fields(
        transport: BufferedTransport) -> None:
    _simulate_run(transport)
    for event in transport.events:
        schema.validate_event(event)
        assert isinstance(event["configVersion"], str)
        assert event["configVersion"]
        assert "@" not in json_dumps(event["data"])
    started, finished, finding = transport.events
    assert set(started["data"]) <= {
        "trigger", "definitionRef", "versionNo", "runKind"}
    assert set(finished["data"]) <= {
        "executionStatus", "verificationStatus", "planHash", "durationMs"}
    assert finding["data"]["severity"] in schema.SEVERITY
    assert finding["data"]["ownerRef"].startswith("actor_")
    assert finding["data"]["evidenceRefs"] == [started["runRef"]]


def json_dumps(obj: object) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False)
