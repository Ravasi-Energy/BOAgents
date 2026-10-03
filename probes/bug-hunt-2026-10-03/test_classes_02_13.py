"""Clasele 2–13. Aserțiunile descriu comportamentul sigur.

Nu sunt colectate de CI (`pytest tests/unit/`).
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
import yaml

BUCHAREST = ZoneInfo("Europe/Bucharest")
TENANT = "probe"


def _note(label: str, before: object, after: object, expected: object) -> None:
    line = f"{label} inainte={before} dupa={after} asteptat={expected}"
    print(line)
    assert after == expected, line


def _future() -> str:
    return (datetime.now(UTC) + timedelta(hours=4)).isoformat(
        timespec="milliseconds"
    ).replace("+00:00", "Z")


def _mandate(db: Path):
    from openexecutive.bo.execution import store

    return store.create_mandate(
        TENANT,
        {
            "allowed_resources": ["synth.*"],
            "allowed_actions": ["increment"],
            "budget_limit": "10",
            "concurrency_limit": 8,
            "max_steps": 5,
            "max_depth": 1,
            "expires_at": _future(),
        },
        parent=None,
        principal_ref="actor_root",
        policy_version=1,
        actor="admin@probe.local",
        max_depth_cap=8,
        db_path=db,
    )


def test_class2_duplicate_run_same_correlation(bo_db: Path) -> None:
    from openexecutive.bo.execution import store

    mandate = _mandate(bo_db)
    step = [{"action": "increment", "resource": "synth.counter", "payload": {"amount": 1}}]
    first = store.submit_run(
        TENANT, mandate, step, budget_amount=Decimal("1"), slots=1,
        correlation_id="corr-acelasi", actor="a@probe.local", db_path=bo_db,
    )
    second = store.submit_run(
        TENANT, mandate, step, budget_amount=Decimal("1"), slots=1,
        correlation_id="corr-acelasi", actor="a@probe.local", db_path=bo_db,
    )
    with sqlite3.connect(bo_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM bo_exec_runs WHERE tenant = ?", (TENANT,)
        ).fetchone()[0]
    print(f"C2 dublu submit run1={first['run_id']} run2={second['run_id']} count={count}")
    _note("C2 rulari cu acelasi correlation_id", 0, count, 1)


def test_class3_cancel_pending_before_effect(bo_db: Path) -> None:
    from openexecutive.bo.execution import engine, store
    from openexecutive.bo.execution.synth import SyntheticCounterProvider
    from openexecutive.bo.settings import store as settings_store

    settings_store.set_value(
        TENANT, "bo.exec.enabled", True, expected_version=0,
        actor="admin", db_path=bo_db,
    )
    mandate = _mandate(bo_db)
    step = [{"action": "increment", "resource": "synth.counter", "payload": {"amount": 1}}]
    run = store.submit_run(
        TENANT, mandate, step, budget_amount=Decimal("1"), slots=1,
        correlation_id="corr-cancel", actor="a@probe.local", db_path=bo_db,
    )
    store.request_flag(
        TENANT, run["run_id"], "cancel_requested",
        actor="b@probe.local", reason="anulat", db_path=bo_db,
    )
    provider = SyntheticCounterProvider(idempotent=True, db_path=bo_db)
    result = engine.work_once(
        TENANT, provider=provider, worker_id="w1", db_path=bo_db,
    )
    fresh = store.get_run(TENANT, run["run_id"], db_path=bo_db)
    print(
        f"C3 anulare PENDING apoi work result={result} "
        f"state={fresh['state']} submit_calls={provider.submit_calls}"
    )
    assert fresh["state"] == store.RUN_CANCELLED
    assert result["claimed"] == 0
    assert provider.submit_calls == 0


def test_class3_publish_ignores_reviewed_version(bo_db: Path) -> None:
    from openexecutive.bo.bots import service as bot_service
    from openexecutive.bo.bots.store import ConflictError

    created = bot_service.create(TENANT, "a@probe.local", {
        "name": "Probe",
        "content": {
            "schema_version": "bo.bobot.v1",
            "trigger": {"type": "manual"},
            "steps": [{"id": "s1", "type": "note", "message": "vechi"}],
        },
    }, db_path=bo_db)
    reviewed = created["draft_version"]
    bot_service.update_draft(TENANT, "a@probe.local", created["id"], {
        "expected_version": reviewed,
        "content": {
            "schema_version": "bo.bobot.v1",
            "trigger": {"type": "manual"},
            "steps": [{"id": "s1", "type": "note", "message": "mesaj-A"}],
        },
    }, db_path=bo_db)
    try:
        bot_service.publish(
            TENANT, "b@probe.local", created["id"],
            expected_version=reviewed, db_path=bo_db,
        )
    except TypeError as exc:
        print(f"C3 publish() nu accepta versiunea citita de B: {exc}")
        raise AssertionError(
            "publicarea trebuie sa refuze versiunea veche a ciornei"
        ) from exc
    except ConflictError:
        return
    raise AssertionError("publicarea a trecut cu versiunea veche")


def test_class4_budget_parallel_does_not_exceed(bo_db: Path) -> None:
    from openexecutive.bo.execution import store

    mandate = _mandate(bo_db)
    step = [{"action": "increment", "resource": "synth.counter", "payload": {}}]
    ok = 0
    errors: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def once() -> None:
        start.wait()
        try:
            store.submit_run(
                TENANT, mandate, step, budget_amount=Decimal("3"), slots=1,
                correlation_id=f"corr-{threading.get_ident()}",
                actor="a@probe.local", db_path=bo_db,
            )
            with lock:
                nonlocal ok
                ok += 1
        except Exception as exc:  # noqa: BLE001 — proba numără refuzurile
            with lock:
                errors.append(type(exc).__name__)

    threads = [threading.Thread(target=once) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    with sqlite3.connect(bo_db) as conn:
        reserved = conn.execute(
            "SELECT COALESCE(SUM(CAST(amount AS REAL)), 0) FROM bo_budget_reservations "
            "WHERE tenant = ? AND state = 'RESERVED'",
            (TENANT,),
        ).fetchone()[0]
    print(f"C4 ok={ok} erori={errors} rezervat={reserved} limita=10")
    assert ok >= 1, errors
    assert set(errors) <= {"BudgetExceededError"}
    assert reserved <= 10


def test_class5_blank_slot_wipe_deletes_auth_roster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.clients import slots
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store

    db = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    for name in ("unu", "doi", "trei"):
        people_store.upsert_person(
            full_name=name, email=f"{name}@probe.local",
            is_principal=True, db_path=db,
        )
    other = tmp_path / "slot-b" / "marker.txt"
    other.parent.mkdir()
    other.write_text("ramane", encoding="utf-8")
    before = len(people_store.list_people(db_path=db))
    slots._wipe_per_client_tables()
    after = len(people_store.list_people(db_path=db))
    print(f"C5 wipe people inainte={before} dupa={after} slot_b={other.read_text()}")
    assert other.read_text() == "ramane"
    # Wipe-ul privat, nu activate_client_slot. Activarea salvează slotul înainte.
    _note("C5 wipe privat people", before, after, 0)


def test_class6_fixture_seed_replaces_people(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.cli.fixture_loader import _seed_people
    from openexecutive.people import store as people_store

    db = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    people_store.initialize_db(db)
    for name in ("unu", "doi", "trei"):
        people_store.upsert_person(full_name=name, email=f"{name}@probe.local", db_path=db)
    yaml_path = tmp_path / "people.yaml"
    yaml_path.write_text(yaml.safe_dump({"people": [{
        "full_name": "patru",
        "email": "patru@probe.local",
        "role": "nou",
    }]}), encoding="utf-8")
    before = len(people_store.list_people(db_path=db))
    _seed_people(yaml_path)
    names = [p.full_name for p in people_store.list_people(db_path=db)]
    print(f"C6 seed fixture inainte={before} dupa={names}")
    _note("C6 oameni dupa import cu unul nou", before, len(names), 4)


def test_class6_notion_state_replace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.knowledge import notion_sync

    profile = tmp_path / "profile.yaml"
    profile.write_text("company: {}\n", encoding="utf-8")
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(profile))
    state = {"watermark": "2026-10-01T00:00:00Z", "pages": {
        "p1": {"title": "unu"}, "p2": {"title": "doi"}, "p3": {"title": "trei"},
    }}
    notion_sync.save_state(state)
    notion_sync.save_state({"watermark": None, "pages": {"p4": {"title": "patru"}}})
    loaded = notion_sync.load_state()
    pages = loaded.get("pages") or {}
    print(f"C6 notion pages={sorted(pages)}")
    _note("C6 pagini notion", 3, len(pages), 4)


def test_class8_wizard_romanian_amount() -> None:
    from openexecutive.onboarding.wizard import build_profile_from_answers

    profile = build_profile_from_answers({
        "financials": "costuri lunare 1.234,56 lei monthly",
        "business_model": "ARR 1.005 million lei",
    })
    burn = (profile.get("financials") or {}).get("burn_rate_monthly")
    burn_ccy = (profile.get("financials") or {}).get("burn_rate_currency")
    arr = profile.get("annual_revenue_arr")
    print(f"C8 wizard burn={burn!r} ccy={burn_ccy!r} arr={arr!r}")
    assert Decimal(str(burn)) == Decimal("1234.56")
    assert burn_ccy == "RON"
    assert Decimal(str(arr)) == Decimal("1005000")


def test_class8_profile_float_1005(tmp_path: Path) -> None:
    from openexecutive.memory.company_profile import CompanyProfile

    path = tmp_path / "profile.yaml"
    profile = CompanyProfile(
        name="Firma",
        annual_revenue_arr=float("1.005"),
        annual_revenue_arr_currency="RON",
    )
    profile.save_to_yaml(path)
    loaded = CompanyProfile.load_from_yaml(path)
    stored = loaded.annual_revenue_arr
    print(
        "C8 float 1.005 "
        f"type={type(stored).__name__} repr={stored!r} "
        f"Decimal(float)={Decimal(stored) if isinstance(stored, float) else stored}"
    )
    assert loaded.annual_revenue_arr_currency == "RON"
    assert not isinstance(stored, float)
    assert stored == Decimal("1.005")


def test_class8_mixed_currency_budget(bo_db: Path) -> None:
    from openexecutive.bo.routing.catalog import CatalogEntry, Cost, Quality
    from openexecutive.bo.routing.engine import Policy, TaskContext, recommend

    fresh = "2026-09-01T00:00:00Z"
    entry = CatalogEntry(
        entry_id="eur-1",
        provider="anthropic",
        model_id="model-eur",
        model_version="v1",
        state="ACTIVE",
        capabilities=("analysis",),
        regions=("eu",),
        cost=Cost("10.00", "10.00", "EUR", "2027-12-31"),
        quality=Quality(0.9, "synthetic", "specialist", "setA", "v1", fresh, 10),
        purpose="test",
        source="admin",
    )
    policy = Policy(
        allowed_providers=None,
        allowed_regions=None,
        required_capabilities=frozenset(),
        min_quality=0.5,
        eval_max_age_days=90,
        max_estimated_cost=Decimal("0.05"),
        cost_currency="USD",
    )
    decision = recommend(
        [entry], policy,
        TaskContext("specialist", 1000, 0),
        now=datetime(2026, 10, 3, tzinfo=UTC),
    )
    print(
        "C8 monede amestecate "
        f"decision={decision.decision} reasons={decision.reasons} "
        f"estimate={decision.cost_estimate}"
    )
    assert decision.decision == "REFUSE"
    assert any("CURRENC" in reason for reason in decision.reasons)


def test_class9_schedule_naive_as_utc(monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.orchestrator import schedule_tools

    captured: dict[str, Any] = {}

    def fake_insert(**kwargs: Any) -> int:
        captured.update(kwargs)
        return 7

    monkeypatch.setattr(
        "openexecutive.memory.episodic.insert_scheduled_action", fake_insert,
    )
    monkeypatch.setattr(
        "openexecutive.memory.episodic.count_pending_for_channel_ref",
        lambda *a, **k: 0,
    )
    monkeypatch.setattr(
        "openexecutive.memory.episodic.count_pending_global", lambda *a, **k: 0,
    )
    token = schedule_tools.current_session.set(SimpleNamespace(
        seen_channel_refs={("email", "a@probe.local")},
        session_id="s1",
    ))
    try:
        import asyncio
        raw = asyncio.run(schedule_tools.handle_schedule_followup({
            "run_at": "2026-10-20T23:30:00",
            "channel": "email",
            "channel_ref": "a@probe.local",
            "intent": "inchide luna",
        }))
    finally:
        schedule_tools.current_session.reset(token)
    stored = datetime.fromisoformat(captured["run_at"])
    local = stored.astimezone(BUCHAREST)
    expected = datetime(2026, 10, 20, 23, 30, tzinfo=BUCHAREST).astimezone(UTC)
    print(f"C9 23:30 handler={raw} stored={stored.isoformat()} local={local.isoformat()} asteptat_utc={expected.isoformat()}")
    assert stored == expected
    assert local.date().isoformat() == "2026-10-20"
    assert (local.hour, local.minute) == (23, 30)


def test_class9_cadence_month_end_and_dst() -> None:
    from openexecutive.departments.cadence import _parse_cadence_spec

    month_end = _parse_cadence_spec(
        "daily@23:30", datetime(2027, 1, 31, 20, 0, tzinfo=UTC),
    )
    assert month_end is not None
    local_end = month_end.astimezone(BUCHAREST)
    dst = _parse_cadence_spec(
        "daily@02:30", datetime(2026, 10, 24, 22, 0, tzinfo=UTC),
    )
    assert dst is not None
    local_dst = dst.astimezone(BUCHAREST)
    print(
        "C9 cadence "
        f"month_end_utc={month_end.isoformat()} local={local_end.isoformat()} "
        f"dst_utc={dst.isoformat()} local={local_dst.isoformat()}"
    )
    assert local_end.date().isoformat() == "2027-01-31"
    assert (local_end.hour, local_end.minute) == (23, 30)
    # 02:30 local in the fold of 25 Oct 2026 must stay on that local morning,
    # not be read as 02:30 UTC.
    assert local_dst.date().isoformat() == "2026-10-25"
    assert (local_dst.hour, local_dst.minute) == (2, 30)


def test_class10_people_create_has_no_role(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from openexecutive.api.routes.people import router
    from openexecutive.people import store as people_store

    db = tmp_path / "people.db"
    monkeypatch.setattr(people_store, "DB_PATH", db)
    people_store.initialize_db(db)
    app = FastAPI()
    app.include_router(router)
    response = TestClient(app).post("/people", json={
        "full_name": "Strain",
        "email": "strain@probe.local",
        "is_principal": True,
        "department_slugs": [],
    })
    print(f"C10 POST /people fara rol status={response.status_code}")
    assert response.status_code == 403


def test_class10_other_mutations_have_no_role_parameter() -> None:
    import ast

    root = Path("/workspace/packages/core/openexecutive/api/routes")
    wanted = {
        "company_profile.py": {"update_company_profile"},
        "departments.py": {"create_department", "delete_department"},
        "documents.py": {"upload_document"},
        "onboarding.py": {"commit_interview"},
    }
    open_doors: list[str] = []
    for filename, names in wanted.items():
        tree = ast.parse((root / filename).read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef) and node.name in names:
                args = [a.arg for a in node.args.args]
                if "ident" not in args:
                    open_doors.append(f"{filename}:{node.name}")
    print(f"C10 usi fara parametru de rol={open_doors}")
    assert open_doors == []


def test_class11_parked_slot_file_survives_wipe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.clients import slots
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store

    db = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    monkeypatch.setattr(people_store, "DB_PATH", db)
    episodic.initialize_db(db)
    people_store.initialize_db(db)
    for name in ("unu", "doi", "trei"):
        people_store.upsert_person(full_name=name, email=f"{name}@firma-a.local", db_path=db)
    parked = tmp_path / "_client_slots" / "firma-b" / "state.db"
    parked.parent.mkdir(parents=True)
    parked.write_bytes(b"slot-b-intact")
    before = len(people_store.list_people(db_path=db))
    slots._wipe_per_client_tables()
    after = len(people_store.list_people(db_path=db))
    print(
        f"C11 slot_b={parked.read_bytes()!r} oameni_live inainte={before} dupa={after}"
    )
    assert parked.read_bytes() == b"slot-b-intact"
    _note("C11 wipe privat pe jurnalul live", before, after, 0)


def test_class12_archived_env_user_still_allowed() -> None:
    import subprocess
    script = Path(__file__).with_name("session_allow.mjs")
    completed = subprocess.run(
        ["node", "--experimental-strip-types", str(script)],
        check=False, capture_output=True, text=True,
    )
    print(completed.stdout)
    print(completed.stderr)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_class13_requeue_double_claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from openexecutive.memory import episodic

    db = tmp_path / "episodic.db"
    monkeypatch.setattr(episodic, "DB_PATH", db)
    episodic.initialize_db(db)
    action_id = episodic.insert_scheduled_action(
        run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        channel="email",
        channel_ref="a@probe.local",
        intent_text="trimite reminder",
        db_path=db,
    )
    first = episodic.claim_due_actions(datetime.now(UTC), db_path=db)
    requeued = episodic.requeue_orphaned_running(db_path=db)
    second = episodic.claim_due_actions(datetime.now(UTC), db_path=db)
    print(
        "C13 "
        f"id={action_id} prima={[a.id for a in first]} "
        f"requeued={requeued} a_doua={[a.id for a in second]}"
    )
    assert [a.id for a in first] == [action_id]
    _note("C13 a doua revendicare dupa requeue in timpul lucrului", 1, len(second), 0)


def test_class8_node_money_roundtrip() -> None:
    import subprocess
    script = Path(__file__).with_name("money_roundtrip.mjs")
    completed = subprocess.run(
        ["node", "--experimental-strip-types", str(script)],
        check=False, capture_output=True, text=True,
    )
    print(completed.stdout)
    print(completed.stderr)
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_class2_settings_same_version_conflicts(bo_db: Path) -> None:
    from openexecutive.bo.settings import store as settings_store
    from openexecutive.bo.settings.store import ConfigConflictError

    settings_store.set_value(
        TENANT, "bo.ui.display_name", "Nume-A",
        expected_version=0, actor="a", db_path=bo_db,
    )
    with pytest.raises(ConfigConflictError):
        settings_store.set_value(
            TENANT, "bo.ui.display_name", "Nume-B",
            expected_version=0, actor="b", db_path=bo_db,
        )
    value = settings_store.get_effective_value(
        TENANT, "bo.ui.display_name", db_path=bo_db,
    )
    print(f"C2/C3 CAS versiune veche value={value}")
    assert value == "Nume-A"
