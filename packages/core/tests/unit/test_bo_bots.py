"""BO-BOT-001/002/003: lifecycle, draft/version isolation, deterministic sim."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from openexecutive.bo.bots import examples, service, store
from openexecutive.bo.settings import store as settings_store

from .bo_testkit import capture_audit, use_tmp_db

TENANT = "tenant-a"
ACTOR = "admin@test"


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return use_tmp_db(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def _audit(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_audit(monkeypatch)


def _payload() -> dict:
    return json.loads(json.dumps(examples.HEARTBEAT_STALE))


def _created(db: Path) -> dict:
    return service.create(TENANT, ACTOR, _payload())


# --------------------------------------------------------------------------- #
# BO-BOT-001: definition + validation
# --------------------------------------------------------------------------- #

def test_create_makes_draft_only(db: Path) -> None:
    d = _created(db)
    assert d["status"] == "draft"
    assert d["draft_version"] == 1
    assert d["active_version_no"] is None
    versions = store.list_versions(TENANT, d["id"])
    assert [v["version_no"] for v in versions] == [1]
    assert versions[0]["status"] == "draft"


def test_example_fixture_parity() -> None:
    """The shipped example and the canonical fixture are the same object."""
    fixture = Path(__file__).resolve().parents[4] / \
        "fixtures/bo/bots/heartbeat_stale.bobot.json"
    assert json.loads(fixture.read_text()) == examples.HEARTBEAT_STALE


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["content"]["steps"].append({"id": "x", "type": "bogus"}),
        lambda p: p["content"]["steps"].append(
            {"id": "s1", "type": "check"}),  # check without predicate
        lambda p: p["content"]["steps"][0].update({"id": "BAD ID"}),
        lambda p: p["content"]["steps"].append(
            {"id": "flag", "type": "note", "message": "dup"}),  # duplicate id
        lambda p: p["content"].update(
            {"predicates": {"op": "eval", "path": "a"}}),  # unknown op
        lambda p: p["content"].update({"surprise": True}),  # closed grammar
        lambda p: p.update({"kind": "QUANTUM"}),
        lambda p: p.update({"name": ""}),
    ],
)
def test_invalid_definitions_rejected(db: Path, mutate) -> None:  # noqa: ANN001
    payload = _payload()
    mutate(payload)
    with pytest.raises(service.ValidationFailure):
        service.create(TENANT, ACTOR, payload)


# --------------------------------------------------------------------------- #
# BO-BOT-002: draft/version lifecycle
# --------------------------------------------------------------------------- #

def test_publish_activates_and_keeps_immutable(db: Path) -> None:
    d = _created(db)
    published = service.publish(TENANT, ACTOR, d["id"])
    assert published["status"] == "active"
    assert published["active_version_no"] == 1
    # A fresh draft (v2) is opened as a copy — publishing never leaves the
    # definition without an editable draft.
    draft = store.get_version(TENANT, d["id"], status="draft")
    assert draft["version_no"] == 2


def test_draft_edit_does_not_touch_active(db: Path) -> None:
    d = _created(db)
    service.publish(TENANT, ACTOR, d["id"])
    v1_before = store.get_version(TENANT, d["id"], version_no=1)

    updated = service.update_draft(TENANT, ACTOR, d["id"], {
        "expected_version": d["draft_version"],
        "content": {
            **_payload()["content"],
            "steps": [{"id": "only", "type": "note", "message": "altă ciornă"}],
        },
    })
    assert updated["draft_version"] == d["draft_version"] + 1

    v1_after = store.get_version(TENANT, d["id"], version_no=1)
    assert v1_after["content"] == v1_before["content"]
    assert v1_after["hash"] == v1_before["hash"]
    # Under three-way merge (BUGHUNT-02), a steps payload that shares no
    # base id is an addition — the published step stays and the new one
    # is appended.
    steps = store.get_version(TENANT, d["id"], status="draft")["content"]["steps"]
    assert "only" in {s["id"] for s in steps}


def test_draft_update_cas_conflict(db: Path) -> None:
    d = _created(db)
    with pytest.raises(store.ConflictError):
        service.update_draft(TENANT, ACTOR, d["id"], {
            "expected_version": 99, "name": "stale write"})


# --------------------------------------------------------------------------- #
# BO-BOT-003: deterministic simulation
# --------------------------------------------------------------------------- #

def _publish_heartbeat_bot(db: Path) -> dict:
    d = _created(db)
    return service.publish(TENANT, ACTOR, d["id"])


def test_simulate_produces_finding_and_not_executed_receipts(db: Path) -> None:
    d = _publish_heartbeat_bot(db)
    run = service.simulate(
        TENANT, ACTOR, d["id"],
        input_context={"service": {"name": "billing",
                                   "last_heartbeat_age_minutes": 45}},
    )
    assert run["status"] == "SUCCEEDED"
    result = run["result"]
    assert result["simulation"] is True
    assert result["external_effects"] == 0
    assert result["predicate_result"] == "TRUE"
    assert len(result["findings"]) == 1
    assert result["findings"][0]["finding_key"] == "heartbeat-stale"
    assert result["findings"][0]["effect_status"] == "NOT_EXECUTED"
    assert "billing" in result["findings"][0]["message"]

    timeline = run["timeline"]
    assert len(timeline) == 4
    assert all(s["status"] == "SIMULATED" for s in timeline)
    assert all(s["receipt"]["effect_status"] == "NOT_EXECUTED" for s in timeline)


def test_simulate_is_deterministic(db: Path) -> None:
    d = _publish_heartbeat_bot(db)
    ctx = {"service": {"name": "billing", "last_heartbeat_age_minutes": 45}}
    r1 = service.simulate(TENANT, ACTOR, d["id"], input_context=ctx)
    r2 = service.simulate(TENANT, ACTOR, d["id"], input_context=ctx)
    assert r1["plan_hash"] == r2["plan_hash"]
    assert r1["version_hash"] == r2["version_hash"]


def test_missing_input_is_unknown_and_gates_plan(db: Path) -> None:
    d = _publish_heartbeat_bot(db)
    # service.name present, heartbeat age absent → gt → UNKNOWN → gated.
    run = service.simulate(
        TENANT, ACTOR, d["id"],
        input_context={"service": {"name": "billing"}},
    )
    result = run["result"]
    assert result["predicate_result"] == "UNKNOWN"
    assert result["gated_by_predicate"] is True
    assert result["findings"] == []
    assert all(s["status"] == "SKIPPED" for s in run["timeline"])
    assert run["timeline"][0]["detail"]["reason"] == "predicate_not_true"


def test_check_step_false_stops_pipeline(db: Path) -> None:
    # No top-level predicate — the step-level check gate does the work.
    payload = _payload()
    payload["content"]["predicates"] = None
    payload["content"]["steps"] = [
        {"id": "gate", "type": "check", "on_false": "stop",
         "predicate": {"op": "gt", "path": "n", "value": 10}},
        {"id": "after", "type": "note", "message": "nu se ajunge aici"},
    ]
    d = service.create(TENANT, ACTOR, payload)
    service.publish(TENANT, ACTOR, d["id"])
    run = service.simulate(TENANT, ACTOR, d["id"], input_context={"n": 5})
    statuses = {s["step_id"]: s["status"] for s in run["timeline"]}
    assert statuses["gate"] == "SKIPPED"
    assert run["timeline"][0]["detail"]["gate"] == "not_true"
    assert statuses["after"] == "SKIPPED"
    assert run["result"]["findings"] == []
    assert run["status"] == "SUCCEEDED"


def test_step_limit_setting_applies(db: Path) -> None:
    settings_store.set_value(
        TENANT, "bo.bobot.simulation.max_steps", 2,
        expected_version=0, actor=ACTOR)
    d = _publish_heartbeat_bot(db)
    run = service.simulate(
        TENANT, ACTOR, d["id"],
        input_context={"service": {"name": "billing",
                                   "last_heartbeat_age_minutes": 45}},
    )
    assert run["status"] == "PARTIAL"
    assert run["result"]["capped_by_step_limit"] is True
    assert run["result"]["step_limit"] == 2
    # The run retains the configVersion it was simulated under.
    assert run["config_version"] == settings_store.config_version(TENANT)


def test_simulate_requires_active_version(db: Path) -> None:
    d = _created(db)  # draft only, never published
    with pytest.raises(service.SimulationRefused):
        service.simulate(TENANT, ACTOR, d["id"], input_context={})


def test_simulate_draft_preview_does_not_move_active(db: Path) -> None:
    d = _publish_heartbeat_bot(db)
    service.update_draft(TENANT, ACTOR, d["id"], {
        "expected_version": d["draft_version"],
        "content": {
            **_payload()["content"],
            "steps": [{"id": "only", "type": "note", "message": "preview"}],
        },
    })
    run = service.simulate(TENANT, ACTOR, d["id"], input_context={},
                           simulate_draft=True)
    assert run["version_no"] == 2  # the draft
    definition = store.get_definition(TENANT, d["id"])
    assert definition["active_version_no"] == 1  # untouched


def test_non_bot_kind_refused(db: Path) -> None:
    payload = _payload()
    payload["kind"] = "AI"
    d = service.create(TENANT, ACTOR, payload)
    service.publish(TENANT, ACTOR, d["id"])
    with pytest.raises(service.SimulationRefused):
        service.simulate(TENANT, ACTOR, d["id"], input_context={})


# --------------------------------------------------------------------------- #
# Retention (BO-SET-001: simulation history only)
# --------------------------------------------------------------------------- #

def test_retention_sweep_deletes_only_old_simulations(db: Path) -> None:
    d = _publish_heartbeat_bot(db)
    run = service.simulate(
        TENANT, ACTOR, d["id"],
        input_context={"service": {"name": "x",
                                   "last_heartbeat_age_minutes": 99}},
    )
    with sqlite3.connect(str(db)) as conn:
        conn.execute(
            "UPDATE bo_bot_runs SET started_at = '2020-01-01T00:00:00+00:00' "
            "WHERE id = ?", (run["id"],))
        conn.commit()
    swept = store.sweep_simulation_runs(TENANT, retention_days=30)
    assert swept == 1
    with pytest.raises(store.NotFoundError):
        store.get_run(TENANT, run["id"])
    with sqlite3.connect(str(db)) as conn:
        n = conn.execute(
            "SELECT COUNT(*) FROM bo_bot_step_runs WHERE run_id = ?",
            (run["id"],)).fetchone()[0]
    assert n == 0  # cascade removed the timeline too


def test_run_records_config_version(db: Path) -> None:
    settings_store.set_value(TENANT, "bo.ui.language", "en",
                             expected_version=0, actor=ACTOR)
    d = _publish_heartbeat_bot(db)
    run = service.simulate(TENANT, ACTOR, d["id"], input_context={})
    assert run["config_version"] == 1
    snap = run["config_snapshot"]
    assert snap["bo.ui.language"] == "en"
    assert snap["bo.bobot.simulation.max_steps"] == 50
