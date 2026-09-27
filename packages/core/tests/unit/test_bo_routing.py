"""VAL3-01: observe-mode router — catalog, engine, observations, telemetry.

Probele cerute de mandat: replay determinist, lipsă evaluare/cost, prag
ratat, buget/regiune/provider interzis, fallback neeligibil, date stale,
izolare tenant, CAS concurent, pierdere receptor, nicio schimbare a
modelului real, BoBot cu LLM oprit (fără dependență de router).
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from openexecutive.bo import db as bo_db
from openexecutive.bo.routing import observe, serialize, store
from openexecutive.bo.routing.catalog import (
    CatalogEntry,
    CatalogValidationError,
    Cost,
    Quality,
)
from openexecutive.bo.routing.engine import Policy, TaskContext, recommend
from openexecutive.bo.settings import store as settings_store

from .bo_testkit import capture_audit, use_tmp_db

TENANT = "tenant-a"
NOW = datetime.now(UTC)
FRESH = (NOW - timedelta(days=5)).isoformat(timespec="milliseconds").replace(
    "+00:00", "Z"
)
STALE = (NOW - timedelta(days=400)).isoformat(timespec="milliseconds").replace(
    "+00:00", "Z"
)


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return use_tmp_db(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict]:
    """capture_audit + point the real journal at tmp — drain_audit_intents
    reads it back via has_detail and must never touch ./episodic_memory.db."""
    from openexecutive.audit import logger as audit_logger

    monkeypatch.setattr(
        audit_logger, "_default_logger",
        audit_logger.AuditLogger(tmp_path / "journal.db"),
    )
    return capture_audit(monkeypatch)


@pytest.fixture(autouse=True)
def _stop_delivery_worker():
    """Never leak the background delivery thread across tests."""
    yield
    from openexecutive.bo.routing import delivery

    delivery.stop_worker()


def _entry(
    *,
    entry_id: str = "c1",
    provider: str = "anthropic",
    model_id: str = "claude-a",
    model_version: str | None = None,
    state: str = "ACTIVE",
    capabilities: tuple[str, ...] = ("analysis",),
    regions: tuple[str, ...] = ("eu",),
    cost: Cost | None = None,
    quality: Quality | None = None,
) -> CatalogEntry:
    return CatalogEntry(
        entry_id=entry_id,
        provider=provider,
        model_id=model_id,
        model_version=model_version,
        state=state,
        capabilities=capabilities,
        regions=regions,
        cost=cost or Cost("3.00", "15.00", "USD", "2027-12-31"),
        quality=quality
        if quality is not None
        else Quality(0.9, "synthetic", "specialist", "setA", "v1", FRESH, 42),
        purpose="test",
        source="admin",
    )


def _policy(**kw: Any) -> Policy:
    base = dict(
        allowed_providers=None,
        allowed_regions=None,
        required_capabilities=frozenset(),
        min_quality=0.6,
        eval_max_age_days=90,
        max_estimated_cost=None,
        cost_currency=None,
    )
    base.update(kw)
    return Policy(**base)


CTX = TaskContext("specialist", input_tokens=1000, output_tokens=200)


# --------------------------------------------------------------------------- #
# Engine — determinism + filter order
# --------------------------------------------------------------------------- #

class TestEngine:
    def test_route_with_met_bar(self) -> None:
        d = recommend([_entry()], _policy(), CTX)
        assert d.decision == "ROUTE"
        assert d.met_bar is True
        assert d.recommendation == {
            "provider": "anthropic", "modelId": "claude-a",
            "modelVersion": None,
        }
        assert d.cost_estimate == {
            "amount": "0.006000", "currency": "USD", "validUntil": "2027-12-31"
        }

    def test_deterministic_replay(self) -> None:
        """Same inputs → byte-identical decision (the replay probe)."""
        catalog = [_entry(), _entry(entry_id="c2", model_id="claude-b")]
        args = (_policy(), CTX)
        now = datetime(2026, 9, 24, tzinfo=UTC)
        d1 = recommend(catalog, *args, now=now)
        d2 = recommend(list(reversed(catalog)), *args, now=now)
        assert json.dumps(d1.to_dict(), sort_keys=True) == json.dumps(
            d2.to_dict(), sort_keys=True
        )

    def test_tie_break_cost_then_identity(self) -> None:
        cheap = _entry(entry_id="b", model_id="m-b",
                       cost=Cost("1.00", "1.00", "USD", "2027-12-31"))
        dear = _entry(entry_id="a", model_id="m-a",
                      cost=Cost("9.00", "9.00", "USD", "2027-12-31"))
        d = recommend([dear, cheap], _policy(), CTX)
        assert d.recommendation["modelId"] == "m-b"

    def test_provider_denied(self) -> None:
        p = _policy(allowed_providers=frozenset({"other"}))
        d = recommend([_entry()], p, CTX)
        assert d.decision == "REFUSE"
        assert "PROVIDER_DENIED" in d.reasons

    def test_region_denied(self) -> None:
        p = _policy(allowed_regions=frozenset({"us"}))
        d = recommend([_entry(regions=("eu",))], p, CTX)
        assert "REGION_DENIED" in d.reasons
        assert d.decision == "REFUSE"

    def test_capability_missing(self) -> None:
        p = _policy(required_capabilities=frozenset({"vision"}))
        d = recommend([_entry()], p, CTX)
        assert "CAPABILITY_MISSING" in d.reasons

    def test_budget_exceeded(self) -> None:
        p = _policy(max_estimated_cost=Decimal("0.0001"), cost_currency="USD")
        d = recommend([_entry()], p, CTX)
        assert "BUDGET_EXCEEDED" in d.reasons
        assert d.decision == "REFUSE"

    def test_cost_missing_with_budget(self) -> None:
        no_cost = _entry(cost=Cost(None, None, None, None))
        p = _policy(max_estimated_cost=Decimal("1.00"), cost_currency="USD")
        d = recommend([no_cost], p, CTX)
        assert "COST_DATA_MISSING" in d.reasons

    def test_cost_missing_no_budget_still_eligible(self) -> None:
        """Unknown cost is not zero — without a cap it stays eligible."""
        no_cost = _entry(cost=Cost(None, None, None, None))
        d = recommend([no_cost], _policy(), CTX)
        assert d.decision == "ROUTE"
        assert d.cost_estimate is None

    def test_model_disabled(self) -> None:
        d = recommend([_entry(state="DISABLED")], _policy(), CTX)
        assert "MODEL_DISABLED" in d.reasons

    def test_eval_missing(self) -> None:
        no_eval = _entry(quality=Quality(
            None, "synthetic", "specialist", "setA", "v1", FRESH, 0))
        d = recommend([no_eval], _policy(), CTX)
        assert "EVAL_MISSING" in d.reasons
        assert d.decision == "REFUSE"

    def test_eval_task_mismatch(self) -> None:
        wrong = _entry(quality=Quality(
            0.95, "synthetic", "triage", "setA", "v1", FRESH, 10))
        d = recommend([wrong], _policy(), CTX)
        assert "EVAL_TASK_MISMATCH" in d.reasons

    def test_stale_evaluation(self) -> None:
        stale = _entry(quality=Quality(
            0.95, "synthetic", "specialist", "setA", "v1", STALE, 10))
        d = recommend([stale], _policy(eval_max_age_days=90), CTX)
        assert "STALE_EVALUATION" in d.reasons

    def test_quality_bar_unmet(self) -> None:
        weak = _entry(quality=Quality(
            0.4, "synthetic", "specialist", "setA", "v1", FRESH, 10))
        d = recommend([weak], _policy(min_quality=0.6), CTX)
        assert d.decision == "REFUSE"
        assert d.met_bar is False
        assert "QUALITY_BAR_UNMET" in d.reasons

    def test_fallback_does_not_relax_constraints(self) -> None:
        """A second-ranked candidate that fails filters stays eliminated —
        fallback never widens rights or budget."""
        ok = _entry(entry_id="ok")
        bad_region = _entry(entry_id="fb", model_id="claude-fb",
                            regions=("cn",))
        p = _policy(allowed_regions=frozenset({"eu"}))
        d = recommend([ok, bad_region], p, CTX)
        assert d.decision == "ROUTE"
        fb = [c for c in d.candidates if c.entry.entry_id == "fb"][0]
        assert fb.eligible is False and fb.reason == "REGION_DENIED"

    def test_catalog_empty(self) -> None:
        d = recommend([], _policy(), CTX)
        assert d.decision == "REFUSE"
        assert d.reasons == ["CATALOG_EMPTY"]

    def test_filters_run_before_scoring(self) -> None:
        """A high-scoring but provider-denied candidate must not win."""
        denied = _entry(quality=Quality(
            0.99, "synthetic", "specialist", "setA", "v1", FRESH, 10))
        ok = _entry(entry_id="ok", model_id="claude-ok",
                    provider="allowed-p")
        p = _policy(allowed_providers=frozenset({"allowed-p"}))
        d = recommend([denied, ok], p, CTX)
        assert d.recommendation["provider"] == "allowed-p"


# --------------------------------------------------------------------------- #
# Catalog store — CRUD, CAS, isolation
# --------------------------------------------------------------------------- #

def _fields(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "provider": "anthropic",
        "model_id": "claude-x",
        "model_version": None,
        "state": "ACTIVE",
        "capabilities": ["analysis"],
        "regions": ["eu"],
        "cost": {
            "input_per_million": "3.00",
            "output_per_million": "15.00",
            "currency": "USD",
            "valid_until": "2027-12-31",
        },
        "quality": {
            "score": 0.9, "methodology": "synthetic", "task_kind": "specialist",
            "eval_set_ref": "setA", "eval_set_version": "v1",
            "observed_at": FRESH, "sample_count": 42,
        },
        "purpose": "chat",
        "source": "admin",
    }
    base.update(kw)
    return base


class TestCatalogStore:
    def test_create_and_list(self, db: Path, audit: list[dict]) -> None:
        e = store.create_entry(TENANT, _fields(), actor="admin@t")
        assert e.version == 1
        assert store.catalog_version(TENANT) == 1
        listed = store.list_catalog(TENANT)
        assert [x.entry_id for x in listed] == [e.entry_id]
        assert listed[0].quality is not None and listed[0].quality.score == 0.9
        events = [a for a in audit if a["event_type"] == "bo_catalog_change"]
        assert events and events[0]["details"]["action"] == "create"

    def test_update_cas_conflict(self, db: Path) -> None:
        e = store.create_entry(TENANT, _fields(), actor="admin@t")
        store.update_entry(TENANT, e.entry_id, _fields(state="DISABLED"),
                           expected_version=1, actor="admin@t")
        with pytest.raises(store.ConflictError):
            store.update_entry(TENANT, e.entry_id, _fields(),
                               expected_version=1, actor="admin@t")

    def test_duplicate_identity_rejected(self, db: Path) -> None:
        store.create_entry(TENANT, _fields(), actor="admin@t")
        with pytest.raises(store.DuplicateEntryError):
            store.create_entry(TENANT, _fields(), actor="admin@t")

    def test_tenant_isolation(self, db: Path) -> None:
        store.create_entry("tenant-a", _fields(), actor="a@t")
        assert store.list_catalog("tenant-b") == []
        assert store.catalog_version("tenant-b") == 0
        with pytest.raises(store.NotFoundError):
            e = store.list_catalog("tenant-a")[0]
            store.get_entry("tenant-b", e.entry_id)

    def test_invalid_entry_rejected(self, db: Path) -> None:
        with pytest.raises(CatalogValidationError):
            store.create_entry(
                TENANT, _fields(provider="bad provider@x"), actor="a@t"
            )
        with pytest.raises(CatalogValidationError):
            store.create_entry(
                TENANT, _fields(quality={"score": 1.5, "methodology": "m",
                                         "task_kind": "t", "eval_set_ref": "r",
                                         "eval_set_version": "v",
                                         "observed_at": FRESH,
                                         "sample_count": 1}),
                actor="a@t",
            )


# --------------------------------------------------------------------------- #
# Observe hook — settings gate, persistence, telemetry, receiver loss
# --------------------------------------------------------------------------- #

class TestObserve:
    def _enable(self, db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BO_TENANT_ID", TENANT)
        settings_store.set_value(
            TENANT, "bo.router.observe_enabled", True,
            expected_version=0, actor="admin@t", db_path=db,
        )

    def test_disabled_by_default_no_write(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BO_TENANT_ID", TENANT)
        out = observe.observe_call(
            model="claude-sonnet-5", actor="specialist",
            counts={"input_tokens": 10, "output_tokens": 5},
        )
        assert out is None
        assert store.list_observations(TENANT) == []

    def test_observation_persisted(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._enable(db, monkeypatch)
        store.create_entry(TENANT, _fields(), actor="admin@t", db_path=db)
        out = observe.observe_call(
            model="claude-sonnet-5", actor="specialist",
            counts={"input_tokens": 1000, "output_tokens": 200,
                    "cost_usd": 0.006},
            turn_id="turn-1",
        )
        assert out is not None and out["decision"] == "ROUTE"
        rows = store.list_observations(TENANT)
        assert len(rows) == 1
        row = rows[0]
        assert row["correlation_id"] == "turn-1"
        assert row["task_kind"] == "specialist"
        assert row["actual_route"]["modelId"] == "claude-sonnet-5"
        assert row["actual_route"]["provider"] == "anthropic"
        assert row["met_bar"] is True
        assert row["measured"]["inputTokens"] == 1000
        assert row["billed"]["evidenceRef"] == "provider:usage.cost"
        # Telemetry disabled by default → persisted but undelivered (0).
        assert row["delivered"] == 0
        # REM-01: the stable eventId is persisted with the envelope.
        assert row["event_id"] and row["event_id"].startswith("evt_")

    def test_actual_route_unchanged_regardless_of_recommendation(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The observed REFUSE never rewrites the real route — the probe for
        'nicio schimbare a modelului real'."""
        self._enable(db, monkeypatch)
        store.create_entry(
            TENANT, _fields(provider="other-vendor"), actor="a@t", db_path=db)
        settings_store.set_value(
            TENANT, "bo.router.allowed_providers", "anthropic",
            expected_version=0, actor="admin@t", db_path=db)
        out = observe.observe_call(
            model="claude-sonnet-5", actor="specialist", counts=None)
        assert out is not None
        row = store.list_observations(TENANT)[0]
        assert row["decision"] == "REFUSE"
        assert row["actual_route"]["modelId"] == "claude-sonnet-5"
        assert "PROVIDER_DENIED" in row["reasons"]

    def test_receiver_loss_persists_and_flushes(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._enable(db, monkeypatch)
        store.create_entry(TENANT, _fields(), actor="a@t", db_path=db)

        from openexecutive.bo.telemetry import adapter as tel

        class Down:
            def send(self, event: dict) -> None:
                raise ConnectionError("guardian down")

        # The hook itself performs NO send — the row is persisted pending.
        monkeypatch.setattr(
            tel, "_adapter", tel.TelemetryAdapter(enabled=True, transport=Down())
        )
        observe.observe_call(
            model="claude-x", actor="specialist", counts=None)
        row = store.list_observations(TENANT)[0]
        assert row["delivered"] == 0

        # First delivery attempt fails → error recorded, still pending.
        res = observe.flush_pending(TENANT)
        assert res["sent"] == 0 and res["failed"] == 1
        row = store.list_observations(TENANT)[0]
        assert row["delivered"] == 0
        assert "guardian down" in (row["delivery_error"] or "")

        sent: list[dict] = []

        class Up:
            def send(self, event: dict) -> dict:
                sent.append(event)
                return {"status": "RECEIVED"}

        monkeypatch.setattr(
            tel, "_adapter", tel.TelemetryAdapter(enabled=True, transport=Up())
        )
        res = observe.flush_pending(TENANT)
        assert res["sent"] == 1 and res["failed"] == 0
        assert sent and sent[0]["schemaVersion"] == "bo.model-observation.v1"
        assert sent[0]["product"] == "BOAgents"
        assert sent[0]["tenantRef"] == TENANT
        assert store.list_observations(TENANT)[0]["delivered"] == 1


# --------------------------------------------------------------------------- #
# REM-01 — durable outbox: stable identity at retry, leased concurrent
# delivery, no network in the hook, dead-letter visibility
# --------------------------------------------------------------------------- #

class TestDeliveryOutbox:
    def _enable(self, db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BO_TENANT_ID", TENANT)
        settings_store.set_value(
            TENANT, "bo.router.observe_enabled", True,
            expected_version=0, actor="admin@t", db_path=db,
        )

    def _adapter(self, monkeypatch: pytest.MonkeyPatch, transport: Any):
        from openexecutive.bo.telemetry import adapter as tel

        ad = tel.TelemetryAdapter(enabled=True, transport=transport)
        monkeypatch.setattr(tel, "_adapter", ad)
        return ad

    def test_hook_never_touches_network(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REM-01/V3-R2: a transport that would block for the HTTP timeout is
        never invoked inside observe_call — zero Guardian I/O in the hook."""
        self._enable(db, monkeypatch)
        calls: list[dict] = []

        class SlowReceiver:
            def send(self, event: dict) -> None:
                calls.append(event)
                import time

                time.sleep(30)  # would blow up any synchronous call path

        self._adapter(monkeypatch, SlowReceiver())
        import time

        t0 = time.monotonic()
        out = observe.observe_call(
            model="claude-x", actor="specialist", counts=None)
        elapsed = time.monotonic() - t0
        assert out is not None
        assert calls == []          # transport never invoked by the hook
        assert elapsed < 5          # no HTTP timeout leaks into the call path
        row = store.list_observations(TENANT)[0]
        assert row["delivered"] == 0

    def test_ack_lost_retry_sends_identical_envelope(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """REM-01/V3-R1: receiver GOT the event but the ACK was lost — the
        retry re-sends byte-identical bytes (same eventId), which is what
        lets Guardian answer DUPLICATE instead of storing it twice."""
        self._enable(db, monkeypatch)
        received: list[str] = []

        class AckLost:
            def send(self, event: dict) -> None:
                received.append(json.dumps(event, sort_keys=True))
                raise TimeoutError("ack lost after write")

        self._adapter(monkeypatch, AckLost())
        observe.observe_call(model="m1", actor="specialist", counts=None)

        res = observe.flush_pending(TENANT)
        assert res["failed"] == 1 and received
        assert store.list_observations(TENANT)[0]["delivered"] == 0

        # Retry — identical bytes on the wire.
        res = observe.flush_pending(TENANT)
        assert res["failed"] == 1
        assert len(received) == 2
        assert received[0] == received[1]
        assert json.loads(received[0])["eventId"] == \
            store.list_observations(TENANT)[0]["event_id"]

    def test_restart_preserves_envelope_identity(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Restart between receive and confirm: a NEW adapter instance must
        re-send the persisted envelope unchanged — identity is read from the
        outbox row, not regenerated."""
        self._enable(db, monkeypatch)

        class Down:
            def send(self, event: dict) -> None:
                raise ConnectionError("down")

        self._adapter(monkeypatch, Down())
        observe.observe_call(model="m1", actor="specialist", counts=None)
        before = store.list_observations(TENANT)[0]["event_id"]
        observe.flush_pending(TENANT)

        sent: list[dict] = []

        class Up:
            def send(self, event: dict) -> dict:
                sent.append(event)
                return {"status": "RECEIVED"}

        # Fresh adapter instance = "process restarted".
        self._adapter(monkeypatch, Up())
        res = observe.flush_pending(TENANT)
        assert res["sent"] == 1
        assert sent[0]["eventId"] == before

    def test_new_observation_new_event_id(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A genuinely new emission gets a NEW eventId even when the body
        looks identical — dedup never collapses real observations."""
        self._enable(db, monkeypatch)
        observe.observe_call(model="m", actor="specialist", counts=None)
        observe.observe_call(model="m", actor="specialist", counts=None)
        rows = store.list_observations(TENANT)
        assert len(rows) == 2
        assert rows[0]["event_id"] != rows[1]["event_id"]

    def test_duplicate_ack_counts_as_delivered(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """RECEIVED/DUPLICATE both mean the receiver holds the event."""
        self._enable(db, monkeypatch)

        class DupAck:
            def send(self, event: dict) -> dict:
                return {"status": "DUPLICATE", "eventId": event["eventId"]}

        self._adapter(monkeypatch, DupAck())
        observe.observe_call(model="m", actor="specialist", counts=None)
        res = observe.flush_pending(TENANT)
        assert res["sent"] == 1 and res["failed"] == 0
        assert store.list_observations(TENANT)[0]["delivered"] == 1

    def test_concurrent_flush_no_duplicate_sends(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two flush workers racing on the same rows must not double-send:
        the lease claim under BEGIN IMMEDIATE gives each row to one worker."""
        import threading

        self._enable(db, monkeypatch)
        sent: list[str] = []
        lock = threading.Lock()

        class Up:
            def send(self, event: dict) -> dict:
                with lock:
                    sent.append(event["eventId"])
                return {"status": "RECEIVED"}

        adapter = self._adapter(monkeypatch, Up())
        for _ in range(6):
            observe.observe_call(model="m", actor="specialist", counts=None)

        results: list[dict] = []

        def worker() -> None:
            from openexecutive.bo.routing import delivery

            results.append(delivery.deliver_pending(TENANT, adapter=adapter))

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert len(sent) == 6                      # every event sent
        assert len(set(sent)) == 6                 # none sent twice
        assert sum(r["sent"] for r in results) == 6
        assert all(
            o["delivered"] == 1 for o in store.list_observations(TENANT)
        )

    def test_attempt_cap_marks_dead_and_visible(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Past delivery_max_attempts the envelope is dead-lettered —
        visible in status/UI, never silently dropped or retried forever."""
        self._enable(db, monkeypatch)
        settings_store.set_value(
            TENANT, "bo.router.delivery_max_attempts", 2,
            expected_version=0, actor="admin@t", db_path=db,
        )

        class Down:
            def send(self, event: dict) -> None:
                raise ConnectionError("still down")

        self._adapter(monkeypatch, Down())
        observe.observe_call(model="m", actor="specialist", counts=None)

        for _ in range(3):
            observe.flush_pending(TENANT)
        row = store.list_observations(TENANT)[0]
        assert row["delivered"] == 2
        assert row["delivery_error"] == "attempt cap reached"
        stats = store.observation_stats(TENANT)
        assert stats["dead_delivery"] == 1
        assert store.outbox_stats(TENANT)["outbox_dead"] == 1
        # Dead rows are excluded from further claims.
        res = observe.flush_pending(TENANT)
        assert res["claimed"] == 0

    def test_catalog_sync_persisted_and_retried(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Catalog sync goes through the same durable outbox — persisted
        before send, retried identically, coalesced while pending."""
        monkeypatch.setenv("BO_TENANT_ID", TENANT)
        store.create_entry(TENANT, _fields(), actor="a@t", db_path=db)

        class Down:
            def send(self, event: dict) -> None:
                raise ConnectionError("down")

        self._adapter(monkeypatch, Down())
        observe.emit_catalog_sync(TENANT, db_path=db)
        observe.emit_catalog_sync(TENANT, db_path=db)   # coalesces
        stats = store.outbox_stats(TENANT)
        assert stats["outbox_pending"] == 1

        sent: list[dict] = []

        class Up:
            def send(self, event: dict) -> dict:
                sent.append(event)
                return {"status": "RECEIVED"}

        self._adapter(monkeypatch, Up())
        res = observe.flush_pending(TENANT)
        assert res["sent"] == 1
        assert sent[0]["models"]
        assert "routing" not in sent[0]

    def test_outbox_tenant_isolation(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BO_TENANT_ID", TENANT)
        observe.emit_catalog_sync(TENANT, db_path=db)
        observe.emit_catalog_sync("tenant-b", db_path=db)
        stats_a = store.outbox_stats(TENANT)
        stats_b = store.outbox_stats("tenant-b")
        assert stats_a["outbox_pending"] == 1
        assert stats_b["outbox_pending"] == 1
        assert store.outbox_stats("tenant-c")["outbox_pending"] == 0

    def test_disabled_adapter_keeps_pending(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Telemetry disabled → flush marks the attempt failed (visible),
        envelope stays pending for a later retry."""
        self._enable(db, monkeypatch)
        from openexecutive.bo.telemetry import adapter as tel

        monkeypatch.setattr(tel, "_adapter", tel.TelemetryAdapter(enabled=False))
        observe.observe_call(model="m", actor="specialist", counts=None)
        res = observe.flush_pending(TENANT)
        assert res["sent"] == 0 and res["failed"] == 1
        row = store.list_observations(TENANT)[0]
        assert row["delivered"] == 0
        assert row["delivery_error"] == "telemetry disabled"

    def test_retention_sweep(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        self._enable(db, monkeypatch)
        observe.observe_call(model="m", actor="triage", counts=None)
        deleted = store.sweep_observations(TENANT, 0 + 1)
        assert deleted == 0  # fresh row survives
        assert store.sweep_observations(TENANT, 1) == 0
        # A row backdated beyond retention is removed.
        with bo_db.get_conn() as conn:
            conn.execute(
                "UPDATE bo_route_observations SET occurred_at = ?",
                ("2020-01-01T00:00:00Z",),
            )
        assert store.sweep_observations(TENANT, 1) == 1


# --------------------------------------------------------------------------- #
# S-01 — per-tenant worker cadence: interval re-read on every cycle,
# 0 = manual only, a slow tenant never delays a fast one
# --------------------------------------------------------------------------- #

class TestPerTenantWorker:
    def _adapter(self, monkeypatch: pytest.MonkeyPatch):
        from openexecutive.bo.telemetry import adapter as tel

        ad = tel.TelemetryAdapter(enabled=True, transport=tel.BufferedTransport())
        monkeypatch.setattr(tel, "_adapter", ad)
        return ad

    def _pending(self, tenant: str, ref: str, db: Path) -> None:
        store.enqueue_outbox(
            tenant, "models", ref,
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": f"evt_{tenant}_{ref}", "tenantRef": tenant},
            db_path=db,
        )

    def _set_interval(self, tenant: str, seconds: int, db: Path) -> None:
        with bo_db.get_conn(db) as conn:
            row = conn.execute(
                "SELECT version FROM bo_settings WHERE tenant = ? AND key = ?",
                (tenant, "bo.router.delivery_interval_s"),
            ).fetchone()
        settings_store.set_value(
            tenant, "bo.router.delivery_interval_s", seconds,
            expected_version=0 if row is None else int(row["version"]),
            actor="admin@t", db_path=db,
        )

    def test_interval_zero_is_manual_even_with_active_tenant(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Tenant A at interval 0 must NOT be drained just because tenant B
        (interval 30) has an active schedule — the per-tenant read replaces
        the old global max()."""
        from openexecutive.bo.routing import delivery

        adapter = self._adapter(monkeypatch)
        self._pending("tenant-manual", "m1", db)
        self._pending("tenant-auto", "a1", db)
        self._set_interval("tenant-manual", 0, db)
        self._set_interval("tenant-auto", 30, db)

        due: dict[str, float] = {}
        now = 1000.0
        delivery._worker_cycle(now, due, adapter, db)
        assert "tenant-manual" not in due          # never even scheduled
        assert due["tenant-auto"] == now + 30
        delivery._worker_cycle(now + 31, due, adapter, db)  # past due
        assert store.outbox_stats("tenant-manual", db_path=db)["outbox_pending"] == 1
        assert store.outbox_stats("tenant-auto", db_path=db)["outbox_pending"] == 0

        # Manual flush is still the drain for the interval-0 tenant.
        res = observe.flush_pending("tenant-manual", db_path=db)
        assert res["sent"] == 1
        assert store.outbox_stats("tenant-manual", db_path=db)["outbox_pending"] == 0

    def test_fast_tenant_not_delayed_by_slow_tenant(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A 3600s tenant cannot push a 5s tenant's wake: the sleep is the
        MINIMUM due time, never the maximum interval."""
        from openexecutive.bo.routing import delivery

        adapter = self._adapter(monkeypatch)
        self._pending("tenant-fast", "f", db)
        self._pending("tenant-slow", "s", db)
        self._set_interval("tenant-fast", 5, db)
        self._set_interval("tenant-slow", 3600, db)

        clock = [0.0]
        monkeypatch.setattr(delivery.time, "monotonic", lambda: clock[0])
        due: dict[str, float] = {}
        wait = delivery._worker_cycle(0.0, due, adapter, db)
        assert wait == 5
        assert due["tenant-slow"] == 3600
        clock[0] = 5.0
        delivery._worker_cycle(5.0, due, adapter, db)
        assert store.outbox_stats("tenant-fast", db_path=db)["outbox_pending"] == 0
        assert store.outbox_stats("tenant-slow", db_path=db)["outbox_pending"] == 1
        # the slow tenant's own due time was not consumed by the fast drain
        assert due["tenant-slow"] == 3600

    def test_live_interval_transitions_without_restart(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cycle-level live re-read: 30→0 drops the pending schedule, 0→10
        reschedules fresh — no process restart, no instant overdue fire."""
        from openexecutive.bo.routing import delivery

        adapter = self._adapter(monkeypatch)
        self._pending("tenant-live", "l1", db)
        self._set_interval("tenant-live", 30, db)

        clock = [0.0]
        monkeypatch.setattr(delivery.time, "monotonic", lambda: clock[0])
        due: dict[str, float] = {}
        delivery._worker_cycle(0.0, due, adapter, db)
        assert due["tenant-live"] == 30

        # 30 → 0 while the schedule is pending: the tenant is excluded and
        # the stale due entry is dropped — its envelope stays pending.
        self._set_interval("tenant-live", 0, db)
        clock[0] = 40.0
        delivery._worker_cycle(40.0, due, adapter, db)   # would have been due
        assert "tenant-live" not in due
        assert store.outbox_stats("tenant-live", db_path=db)["outbox_pending"] == 1

        # 0 → 10 re-enables automatic delivery with a FRESH countdown —
        # the envelope does not fire instantly on the stale schedule.
        self._set_interval("tenant-live", 10, db)
        clock[0] = 50.0
        delivery._worker_cycle(50.0, due, adapter, db)
        assert due["tenant-live"] == 60.0
        assert store.outbox_stats("tenant-live", db_path=db)["outbox_pending"] == 1
        clock[0] = 61.0
        delivery._worker_cycle(61.0, due, adapter, db)
        assert store.outbox_stats("tenant-live", db_path=db)["outbox_pending"] == 0

    def test_disabled_telemetry_tenant_skipped_by_cycle(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Administered bo.telemetry.enabled=false is honored per tick even
        though the bootstrap adapter is enabled (S-02 wiring into S-01)."""
        from openexecutive.bo.routing import delivery

        adapter = self._adapter(monkeypatch)   # bootstrap enabled
        self._pending("tenant-off", "x", db)
        self._set_interval("tenant-off", 30, db)
        settings_store.set_value(
            "tenant-off", "bo.telemetry.enabled", False,
            expected_version=0, actor="admin@t", db_path=db,
        )
        clock = [0.0]
        monkeypatch.setattr(delivery.time, "monotonic", lambda: clock[0])
        due: dict[str, float] = {}
        delivery._worker_cycle(0.0, due, adapter, db)
        clock[0] = 40.0
        delivery._worker_cycle(40.0, due, adapter, db)   # past the 30s mark
        assert "tenant-off" not in due
        assert store.outbox_stats("tenant-off", db_path=db)["outbox_pending"] == 1

    def test_ensure_worker_ignores_manual_only_tenants(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from openexecutive.bo.routing import delivery

        self._adapter(monkeypatch)
        self._pending("tenant-manual", "m", db)
        self._set_interval("tenant-manual", 0, db)
        assert delivery.ensure_worker(db_path=db) is False
        assert delivery._worker_thread is None
        # …but one auto tenant among manual ones does start it.
        self._pending("tenant-auto", "a", db)
        self._set_interval("tenant-auto", 30, db)
        assert delivery.ensure_worker(db_path=db) is True

    def test_worker_thread_live_transition(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The real daemon thread: interval-1s tenant drains automatically,
        then a live switch to 0 stops auto-delivery without a restart."""
        from openexecutive.bo.routing import delivery

        self._adapter(monkeypatch)
        self._set_interval("tenant-live", 1, db)
        self._pending("tenant-live", "t1", db)
        assert delivery.ensure_worker(db_path=db) is True

        deadline = time.time() + 6
        while time.time() < deadline:
            if store.outbox_stats("tenant-live", db_path=db)["outbox_pending"] == 0:
                break
            time.sleep(0.05)
        assert store.outbox_stats("tenant-live", db_path=db)["outbox_pending"] == 0

        self._set_interval("tenant-live", 0, db)
        self._pending("tenant-live", "t2", db)
        time.sleep(2.5)  # several ticks at the 1s cadence would have fired
        assert store.outbox_stats("tenant-live", db_path=db)["outbox_pending"] == 1

    def test_wait_after_send_covers_rescheduled_due(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PILOT-06/S-01: after a tenant drains, its RESCHEDULED due time
        must bound the wait — a 5s tenant is delivered again at ~t+5, not
        after the 30s recheck bound."""
        from openexecutive.bo.routing import delivery

        adapter = self._adapter(monkeypatch)
        self._pending("tenant-fast", "f", db)
        self._pending("tenant-slow", "s", db)
        self._set_interval("tenant-fast", 5, db)
        self._set_interval("tenant-slow", 3600, db)

        clock = [0.0]
        monkeypatch.setattr(delivery.time, "monotonic", lambda: clock[0])
        due: dict[str, float] = {}
        clock[0] = 100.0
        assert delivery._worker_cycle(100.0, due, adapter, db) == 5

        clock[0] = 105.0  # fast due → drains; next due reschedules to 110
        wait = delivery._worker_cycle(105.0, due, adapter, db)
        assert wait == pytest.approx(5, abs=0.5)  # NOT the 30s recheck bound
        assert store.outbox_stats("tenant-fast", db_path=db)["outbox_pending"] == 0

        # Second delivery lands at ~110, not ~135.
        self._pending("tenant-fast", "f2", db)
        due["tenant-fast"] = 110.0
        clock[0] = 110.0
        delivery._worker_cycle(110.0, due, adapter, db)
        assert store.outbox_stats("tenant-fast", db_path=db)["outbox_pending"] == 0
        assert store.outbox_stats("tenant-slow", db_path=db)["outbox_pending"] == 1


# --------------------------------------------------------------------------- #
# PILOT-06 — outbox destination binding: the destination + SecretRef resolved
# at enqueue time travel with the envelope; a settings change never re-routes
# persisted rows, and legacy unbound rows need an explicit audited rebind.
# --------------------------------------------------------------------------- #

class TestDestinationBinding:
    def _http_adapter(self, monkeypatch: pytest.MonkeyPatch, endpoint: str,
                      token: str):
        from openexecutive.bo.telemetry import adapter as tel

        ad = tel.TelemetryAdapter(
            enabled=True, transport=tel.HttpTransport(endpoint, token))
        monkeypatch.setattr(tel, "_adapter", ad)
        return ad

    def _capture(self, monkeypatch: pytest.MonkeyPatch) -> list:
        import io
        import urllib.request

        calls: list = []

        def cap(req, **kw):  # noqa: ANN001
            calls.append(req)
            resp = io.BytesIO(b'{"status":"RECEIVED"}')
            resp.status = 200  # guardian._request reads resp.status
            return resp

        monkeypatch.setattr(urllib.request, "urlopen", cap)
        return calls

    def _set(self, tenant: str, key: str, value, db: Path,
             versions: dict) -> None:
        version = versions.get((tenant, key), 0)
        settings_store.set_value(tenant, key, value, expected_version=version,
                                 actor="admin@t", db_path=db)
        versions[tenant, key] = version + 1

    def test_backlog_keeps_enqueue_time_destination(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An envelope persisted under destination A keeps going to A with
        credential A even after the tenant endpoint is re-administered to B."""
        from openexecutive.bo.routing import delivery

        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-token-a")
        monkeypatch.setenv("TEL_DEST_B", "synthetic-token-b")
        monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "TEL_DEST_B")
        calls = self._capture(monkeypatch)
        adapter = self._http_adapter(
            monkeypatch, "https://dest-a.invalid/v1/telemetry",
            "synthetic-token-a")

        store.enqueue_outbox(
            TENANT, "models", "ref-old",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_old", "tenantRef": TENANT},
            db_path=db)

        # Admin re-destines the tenant — WITH a provisioned credential.
        versions: dict = {}
        self._set(TENANT, "bo.telemetry.endpoint",
                  "https://dest-b.invalid/v1/telemetry", db, versions)
        self._set(TENANT, "bo.telemetry.token_ref", "TEL_DEST_B", db, versions)
        store.enqueue_outbox(
            TENANT, "models", "ref-new",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_new", "tenantRef": TENANT},
            db_path=db)

        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["sent"] == 2 and res["failed"] == 0
        assert len(calls) == 2
        by_event = {json.loads(c.data)["eventId"]: c for c in calls}
        # Old row: ORIGINAL destination + ORIGINAL credential — not the new one.
        assert by_event["evt_old"].full_url == "https://dest-a.invalid/v1/telemetry"
        assert by_event["evt_old"].get_header("Authorization") == \
            "Bearer synthetic-token-a"
        # New row: the administered destination + the administered credential.
        assert by_event["evt_new"].full_url == "https://dest-b.invalid/v1/telemetry"
        assert by_event["evt_new"].get_header("Authorization") == \
            "Bearer synthetic-token-b"

    def test_endpoint_override_without_ref_blocks_new_not_backlog(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Endpoint override without token_ref: the OLD bound row still
        delivers to its recorded destination; NEW envelopes bind to the
        administered endpoint but are refused — no bootstrap credential."""
        from openexecutive.bo.routing import delivery

        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-token-a")
        monkeypatch.delenv("BO_TELEMETRY_SECRET_REFS", raising=False)
        calls = self._capture(monkeypatch)
        adapter = self._http_adapter(
            monkeypatch, "https://dest-a.invalid/v1/telemetry",
            "synthetic-token-a")

        store.enqueue_outbox(
            TENANT, "models", "ref-old",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_old", "tenantRef": TENANT},
            db_path=db)
        self._set(TENANT, "bo.telemetry.endpoint",
                  "https://dest-b.invalid/v1/telemetry", db, {})
        store.enqueue_outbox(
            TENANT, "models", "ref-new",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_new", "tenantRef": TENANT},
            db_path=db)

        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["sent"] == 1 and res["failed"] == 1
        assert [json.loads(c.data)["eventId"] for c in calls] == ["evt_old"]
        assert calls[0].full_url == "https://dest-a.invalid/v1/telemetry"
        assert calls[0].get_header("Authorization") == \
            "Bearer synthetic-token-a"
        pending = [e for e in store.list_outbox(TENANT, db_path=db)
                   if e["delivered"] == 0]
        assert [e["event_id"] for e in pending] == ["evt_new"]

    def test_legacy_unbound_row_refuses_until_audited_rebind(
        self, db: Path, monkeypatch: pytest.MonkeyPatch, audit: list[dict]
    ) -> None:
        """Rows persisted before the binding columns exist get NO implicit
        destination — controlled refusal until an admin rebinds explicitly."""
        from openexecutive.bo.routing import delivery

        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-token-a")
        calls = self._capture(monkeypatch)
        adapter = self._http_adapter(
            monkeypatch, "https://dest-a.invalid/v1/telemetry",
            "synthetic-token-a")

        store.enqueue_outbox(
            TENANT, "models", "ref-legacy",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_legacy", "tenantRef": TENANT},
            db_path=db)
        # Simulate a pre-migration row: no destination association at all.
        with bo_db.get_conn(db) as conn:
            conn.execute(
                "UPDATE bo_telemetry_outbox SET dest_bound = NULL, "
                "dest_endpoint = NULL, dest_ref = NULL")

        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["sent"] == 0 and res["failed"] == 1
        assert calls == []  # never delivered to ANY guessed destination
        entry = store.list_outbox(TENANT, db_path=db)[0]
        assert entry["delivered"] == 0
        assert "rebind" in (entry["last_error"] or "")

        # Explicit, audited rebind to the tenant's CURRENT effective config.
        out = store.rebind_outbox(
            TENANT, actor="admin@t", reason="destinație nouă după migrare",
            db_path=db)
        assert out["rebound"] == 1
        assert any(a["event_type"] == "bo_outbox_rebind" for a in audit)

        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["sent"] == 1
        assert calls[0].full_url == "https://dest-a.invalid/v1/telemetry"

    def test_bound_row_with_missing_credential_never_sends(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Bound to B + provisioned ref; the env vanishes before delivery →
        controlled refusal, and the bootstrap token is never substituted."""
        from openexecutive.bo.routing import delivery

        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-token-a")
        monkeypatch.setenv("TEL_DEST_B", "synthetic-token-b")
        monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "TEL_DEST_B")
        calls = self._capture(monkeypatch)
        adapter = self._http_adapter(
            monkeypatch, "https://dest-a.invalid/v1/telemetry",
            "synthetic-token-a")

        versions: dict = {}
        self._set(TENANT, "bo.telemetry.endpoint",
                  "https://dest-b.invalid/v1/telemetry", db, versions)
        self._set(TENANT, "bo.telemetry.token_ref", "TEL_DEST_B", db, versions)
        store.enqueue_outbox(
            TENANT, "models", "ref-b",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_b", "tenantRef": TENANT},
            db_path=db)

        monkeypatch.delenv("TEL_DEST_B")  # credential de-provisioned
        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["sent"] == 0 and res["failed"] == 1
        assert calls == []  # zero network, zero credential substitution
        entry = store.list_outbox(TENANT, db_path=db)[0]
        assert entry["delivered"] == 0
        assert entry["dest_endpoint"] == "https://dest-b.invalid/v1/telemetry"
        assert entry["dest_ref"] == "TEL_DEST_B"

    def test_execution_rows_bind_guardian_destination(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """execution-kind envelopes bind the GUARDIAN channel (bo.exec.*),
        not the telemetry one — authority separation preserved."""
        from openexecutive.bo.routing import delivery

        monkeypatch.setenv("BO_GUARDIAN_TOKEN", "synthetic-guardian-token")
        monkeypatch.delenv("BO_TELEMETRY_ENDPOINT", raising=False)
        calls = self._capture(monkeypatch)
        adapter = self._http_adapter(
            monkeypatch, "https://dest-a.invalid/v1/telemetry",
            "synthetic-token-a")
        versions: dict = {}
        self._set(TENANT, "bo.exec.guardian_endpoint",
                  "https://guardian.invalid", db, versions)

        store.enqueue_outbox(
            TENANT, "execution", "run-1",
            {"schemaVersion": "bo.execution-control.event.v1",
             "eventId": "evt_exec", "tenantRef": TENANT},
            db_path=db)
        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["sent"] == 1
        assert calls[0].full_url == \
            "https://guardian.invalid/v1/execution-events"
        assert calls[0].get_header("Authorization") == \
            "Bearer synthetic-guardian-token"

    def test_execution_binding_records_effective_bootstrap_ref(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Gate deployment: only BO_TELEMETRY_* provisioned, no
        BO_GUARDIAN_TOKEN. The bound row must record the ref that
        actually supplies the token — not a ref that never resolves."""
        from openexecutive.bo.routing import delivery

        monkeypatch.delenv("BO_GUARDIAN_TOKEN", raising=False)
        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-gate-token")
        monkeypatch.setenv(
            "BO_TELEMETRY_ENDPOINT", "https://gate.invalid/v1/telemetry")
        calls = self._capture(monkeypatch)
        adapter = self._http_adapter(
            monkeypatch, "https://gate.invalid/v1/telemetry",
            "synthetic-gate-token")

        store.enqueue_outbox(
            TENANT, "execution", "run-1",
            {"schemaVersion": "bo.execution-control.event.v1",
             "eventId": "evt_exec", "tenantRef": TENANT},
            db_path=db)
        row = store.list_outbox(TENANT, db_path=db)[0]
        assert row["dest_ref"] == "BO_TELEMETRY_TOKEN"
        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["sent"] == 1
        assert calls[0].full_url == \
            "https://gate.invalid/v1/execution-events"
        assert calls[0].get_header("Authorization") == \
            "Bearer synthetic-gate-token"

    def test_administered_guardian_endpoint_never_inherits_bootstrap(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Administered Guardian endpoint + guardian ref missing from env:
        the bootstrap BO_TELEMETRY_TOKEN is NEVER substituted — the row
        stays pending until the recorded ref is provisioned or rebound."""
        from openexecutive.bo.routing import delivery

        monkeypatch.delenv("BO_GUARDIAN_TOKEN", raising=False)
        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-gate-token")
        calls = self._capture(monkeypatch)
        adapter = self._http_adapter(
            monkeypatch, "https://gate.invalid/v1/telemetry",
            "synthetic-gate-token")
        self._set(TENANT, "bo.exec.guardian_endpoint",
                  "https://admin-guardian.invalid", db, {})

        store.enqueue_outbox(
            TENANT, "execution", "run-1",
            {"schemaVersion": "bo.execution-control.event.v1",
             "eventId": "evt_exec", "tenantRef": TENANT},
            db_path=db)
        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["sent"] == 0 and res["failed"] == 1
        assert calls == []  # zero network — credential never substituted
        entry = store.list_outbox(TENANT, db_path=db)[0]
        assert entry["delivered"] == 0
        assert entry["dest_endpoint"] == "https://admin-guardian.invalid"
        assert entry["dest_ref"] == "BO_GUARDIAN_TOKEN"

    def test_descoped_guardian_ref_refuses_at_delivery(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Coordinator precision: a bound ref re-scoped to ANOTHER tenant
        (``NAME@other``) must refuse at delivery — the credential never
        crosses tenant scope even though the env var still exists."""
        from openexecutive.bo.routing import delivery

        monkeypatch.setenv("BO_GUARDIAN_SECRET_REFS", "G_BOUND")
        monkeypatch.setenv("G_BOUND", "synthetic-g-bound")
        calls = self._capture(monkeypatch)
        adapter = self._http_adapter(
            monkeypatch, "https://gate.invalid/v1/telemetry",
            "synthetic-gate-token")
        versions: dict = {}
        self._set(TENANT, "bo.exec.guardian_endpoint",
                  "https://guardian-b.invalid", db, versions)
        self._set(TENANT, "bo.exec.guardian_secret_ref", "G_BOUND",
                  db, versions)
        store.enqueue_outbox(
            TENANT, "execution", "run-1",
            {"schemaVersion": "bo.execution-control.event.v1",
             "eventId": "evt_exec_scope", "tenantRef": TENANT},
            db_path=db)
        # Operator re-scopes the ref to another tenant — bound row refuses.
        monkeypatch.setenv("BO_GUARDIAN_SECRET_REFS", "G_BOUND@other-tenant")
        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["sent"] == 0 and res["failed"] == 1
        assert calls == []
        entry = store.list_outbox(TENANT, db_path=db)[0]
        assert entry["delivered"] == 0
        assert "provisionat" in (entry["last_error"] or "")

    def test_sink_bound_row_refuses_later_http_destination(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An envelope queued while telemetry was a sink is bound to
        ('', ''): a LATER administered http destination must not receive
        it — controlled refusal until an explicit rebind."""
        from openexecutive.bo.routing import delivery
        from openexecutive.bo.telemetry import adapter as adapter_mod

        adapter = adapter_mod.TelemetryAdapter(
            enabled=True, transport=adapter_mod.BufferedTransport())
        monkeypatch.setattr(adapter_mod, "_adapter", adapter)
        store.enqueue_outbox(
            TENANT, "models", "ref-sink",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_sink", "tenantRef": TENANT},
            db_path=db)
        row = store.list_outbox(TENANT, db_path=db)[0]
        assert row["dest_endpoint"] == "" and row["dest_bound"] == 1

        # Admin now configures a real HTTP destination.
        monkeypatch.setenv("TEL_DEST_B", "synthetic-token-b")
        monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "TEL_DEST_B")
        calls = self._capture(monkeypatch)
        versions: dict = {}
        self._set(TENANT, "bo.telemetry.transport", "http", db, versions)
        self._set(TENANT, "bo.telemetry.endpoint",
                  "https://dest-b.invalid/v1/telemetry", db, versions)
        self._set(TENANT, "bo.telemetry.token_ref", "TEL_DEST_B", db, versions)

        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["sent"] == 0 and res["failed"] == 1
        assert calls == []  # never re-routed to the new destination
        entry = store.list_outbox(TENANT, db_path=db)[0]
        assert "rebind" in (entry["last_error"] or "")

    def test_unbound_legacy_row_claimed_once_no_attempt_burn(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permanently-undeliverable legacy row records its refusal once
        and is then left alone — no attempt-cap burn, no queue starvation."""
        from openexecutive.bo.routing import delivery

        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-token-a")
        calls = self._capture(monkeypatch)
        adapter = self._http_adapter(
            monkeypatch, "https://dest-a.invalid/v1/telemetry",
            "synthetic-token-a")
        store.enqueue_outbox(
            TENANT, "models", "ref-legacy",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_legacy", "tenantRef": TENANT},
            db_path=db)
        store.enqueue_outbox(
            TENANT, "models", "ref-ok",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_ok", "tenantRef": TENANT},
            db_path=db)
        with bo_db.get_conn(db) as conn:
            conn.execute(
                "UPDATE bo_telemetry_outbox SET dest_bound = NULL, "
                "dest_endpoint = NULL, dest_ref = NULL "
                "WHERE event_id = 'evt_legacy'")

        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["sent"] == 1 and res["failed"] == 1
        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["claimed"] == 0  # refusal recorded once, then left alone
        assert len(calls) == 1

    def test_rebind_conflicts_and_lease_safety(
        self, db: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Store-level guards: reason mandatory, delivered rows conflict,
        actively-leased rows are skipped instead of rebound mid-flight."""
        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-token-a")
        self._capture(monkeypatch)
        adapter = self._http_adapter(
            monkeypatch, "https://dest-a.invalid/v1/telemetry",
            "synthetic-token-a")
        store.enqueue_outbox(
            TENANT, "models", "ref-1",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_1", "tenantRef": TENANT},
            db_path=db)
        store.enqueue_outbox(
            TENANT, "models", "ref-2",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_2", "tenantRef": TENANT},
            db_path=db)

        with pytest.raises(store.ConflictError):
            store.rebind_outbox(
                TENANT, actor="admin@t", reason="   ", db_path=db)

        # Delivered rows are history — naming one is a conflict.
        from openexecutive.bo.routing import delivery

        res = delivery.deliver_pending(TENANT, adapter=adapter, db_path=db)
        assert res["sent"] == 2
        with pytest.raises(store.ConflictError):
            store.rebind_outbox(
                TENANT, event_ids=["evt_1"], actor="admin@t",
                reason="test", db_path=db)

        # A row under an active lease is skipped, not rebound mid-flight.
        store.enqueue_outbox(
            TENANT, "models", "ref-3",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_3", "tenantRef": TENANT},
            db_path=db)
        claimed = store.claim_outbox(
            TENANT, worker_id="w1", limit=10, lease_s=300, db_path=db)
        assert [r["event_id"] for r in claimed] == ["evt_3"]
        out = store.rebind_outbox(
            TENANT, event_ids=["evt_3"], actor="admin@t",
            reason="test", db_path=db)
        assert out["rebound"] == 0 and out["skipped_leased"] == 1


# --------------------------------------------------------------------------- #
# Wire contract — serializer output validates against bo.model-observation.v1
# --------------------------------------------------------------------------- #

class TestWireContract:
    SCHEMA = json.loads(
        (Path(__file__).parent / "fixtures"
         / "bo.model-observation.v1.schema.json").read_text()
    )

    def _validate(self, doc: dict[str, Any]) -> None:
        import jsonschema

        jsonschema.validate(doc, self.SCHEMA)

    def test_routing_doc_validates(self) -> None:
        body = serialize.routing_body(
            correlation_id="turn-1", policy_version="pol_x",
            catalog_version="cat_v3", task_kind="specialist",
            recommendation={"provider": "anthropic", "modelId": "claude-a",
                            "modelVersion": None},
            actual_route={"provider": "anthropic", "modelId": "claude-b",
                          "modelVersion": None},
            met_bar=True, reasons=[],
            cost_estimate={"amount": "0.006000", "currency": "USD",
                           "validUntil": "2027-12-31"},
            measured={"inputTokens": 1000, "outputTokens": 200,
                      "requests": 1},
            billed={"amount": "0.006", "currency": "USD",
                    "validUntil": "2026-09-24",
                    "evidenceRef": "provider:usage.cost"},
        )
        doc = {
            "schemaVersion": "bo.model-observation.v1",
            "eventId": "evt_1", "producerId": "boagents",
            "product": "BOAgents", "installationId": "inst-1",
            "tenantRef": "tenant-a", "observedAt": FRESH,
            **body,
        }
        self._validate(doc)

    def test_refuse_doc_validates(self) -> None:
        body = serialize.routing_body(
            correlation_id="turn-2", policy_version="pol_x",
            catalog_version="cat_v0", task_kind="specialist",
            recommendation=None, actual_route=None,
            met_bar=False, reasons=["QUALITY_BAR_UNMET"],
            cost_estimate=None, measured=None, billed=None,
        )
        doc = {
            "schemaVersion": "bo.model-observation.v1",
            "eventId": "evt_2", "producerId": "boagents",
            "product": "BOAgents", "installationId": "inst-1",
            "tenantRef": "tenant-a", "observedAt": FRESH,
            **body,
        }
        self._validate(doc)

    def test_models_doc_validates(self) -> None:
        e = _entry()
        body = serialize.models_body(
            [e], owner_ref="tenant:tenant-a", last_seen=FRESH,
            sync_id="sync_1", complete=True)
        doc = {
            "schemaVersion": "bo.model-observation.v1",
            "eventId": "evt_3", "producerId": "boagents",
            "product": "BOAgents", "installationId": "inst-1",
            "tenantRef": "tenant-a", "observedAt": FRESH,
            **body,
        }
        self._validate(doc)
        model = doc["models"][0]
        assert model["quality"]["score"] == 0.9
        assert model["modelVersion"] is None  # unknown stays explicit


# --------------------------------------------------------------------------- #
# BoBots must not gain an LLM/router dependency
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# HTTP surface — RBAC, CAS over the wire, tenant isolation
# --------------------------------------------------------------------------- #

class TestRoutes:
    ADMIN = {"x-caller-email": "admin@test", "x-caller-proxy-secret": "test-proxy-only"}
    VIEWER = {"x-caller-email": "viewer@test", "x-caller-proxy-secret": "test-proxy-only"}

    @pytest.fixture()
    def client(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from openexecutive.api.routes import bo as bo_route

        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TENANT_ID", "tenant-a")
        monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@test")
        monkeypatch.setenv("BACKEND_PROXY_SECRET", "test-proxy-only")
        app = FastAPI()
        app.include_router(bo_route.router)
        bo_route.register_error_handlers(app)
        return TestClient(app)

    def test_catalog_requires_admin_write(self, client) -> None:
        resp = client.post("/bo/routing/catalog", headers=self.VIEWER,
                           json=_fields())
        assert resp.status_code == 403
        resp = client.get("/bo/routing/catalog", headers=self.VIEWER)
        assert resp.status_code == 200
        assert resp.json()["entries"] == []

    def test_catalog_crud_and_cas_over_http(self, client) -> None:
        resp = client.post("/bo/routing/catalog", headers=self.ADMIN,
                           json=_fields())
        assert resp.status_code == 201, resp.text
        entry = resp.json()["entry"]
        assert entry["version"] == 1

        # stale CAS → 409
        resp = client.put(
            f"/bo/routing/catalog/{entry['entry_id']}", headers=self.ADMIN,
            json={**_fields(state="DISABLED"), "expected_version": 99})
        assert resp.status_code == 409

        resp = client.put(
            f"/bo/routing/catalog/{entry['entry_id']}", headers=self.ADMIN,
            json={**_fields(state="DISABLED"), "expected_version": 1})
        assert resp.status_code == 200
        assert resp.json()["entry"]["state"] == "DISABLED"
        assert resp.json()["entry"]["version"] == 2

    def test_catalog_validation_422(self, client) -> None:
        resp = client.post("/bo/routing/catalog", headers=self.ADMIN,
                           json=_fields(provider="bad provider"))
        assert resp.status_code == 422

    def test_status_and_observations_shape(self, client) -> None:
        resp = client.get("/bo/routing/status", headers=self.VIEWER)
        assert resp.status_code == 200
        body = resp.json()
        assert body["observe_enabled"] is False
        assert body["mode"] == "observare"
        resp = client.get("/bo/routing/observations", headers=self.VIEWER)
        assert resp.status_code == 200
        assert resp.json()["observations"] == []

    def test_settings_new_keys_roundtrip(self, client) -> None:
        resp = client.put(
            "/bo/settings/bo.router.observe_enabled", headers=self.ADMIN,
            json={"value": True, "expected_version": 0})
        assert resp.status_code == 200, resp.text
        resp = client.get("/bo/routing/status", headers=self.VIEWER)
        assert resp.json()["observe_enabled"] is True

        # invalid CSV rejected
        resp = client.put(
            "/bo/settings/bo.router.allowed_providers", headers=self.ADMIN,
            json={"value": "bad provider x", "expected_version": 0})
        assert resp.status_code == 422

        # invalid cost cap rejected
        resp = client.put(
            "/bo/settings/bo.router.max_estimated_cost", headers=self.ADMIN,
            json={"value": "abc", "expected_version": 0})
        assert resp.status_code == 422
        resp = client.put(
            "/bo/settings/bo.router.max_estimated_cost", headers=self.ADMIN,
            json={"value": "0.05 USD", "expected_version": 0})
        assert resp.status_code == 200

    def test_audit_intents_evidence_export(
        self, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """GET /execution/audit-intents — bounded, tenant-scoped evidence
        correlating intent ↔ journal row; an orphan marker is shown as
        ``journal_row_present=false``, never implied delivered."""
        import openexecutive.audit as audit_pkg
        from openexecutive.audit import logger as audit_logger

        journal = audit_logger.AuditLogger(tmp_path / "journal.db")
        monkeypatch.setattr(audit_logger, "_default_logger", journal)
        monkeypatch.setattr(audit_pkg, "log_event", audit_logger.log_event)

        store.enqueue_outbox(
            TENANT, "models", "ref-a",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": "evt_ev", "tenantRef": TENANT})
        with bo_db.get_conn() as conn:
            conn.execute(
                "UPDATE bo_telemetry_outbox SET dest_bound=NULL, "
                "dest_endpoint=NULL, dest_ref=NULL WHERE event_id='evt_ev'")
        out = store.rebind_outbox(
            TENANT, actor="admin@t", reason="evidence")
        assert out["audit"] == "delivered"

        resp = client.get("/bo/execution/audit-intents", headers=self.VIEWER)
        assert resp.status_code == 200
        body = resp.json()
        assert body["journal_reachable"] is True and body["total"] == 1
        intent = body["intents"][0]
        assert intent["status"] == "delivered"
        assert intent["audit_row_id"] is not None
        assert intent["journal_row_present"] is True
        assert "details_json" not in intent
        assert {"drain_owner", "drain_until", "attempts"} <= set(intent)

        # Orphan marker: journal row gone (restore/repair) → flagged,
        # never presented as live evidence.
        with audit_logger._get_conn(journal._db_path) as conn:
            conn.execute("DELETE FROM audit_log")
        resp = client.get("/bo/execution/audit-intents", headers=self.VIEWER)
        intent = resp.json()["intents"][0]
        assert intent["journal_row_present"] is False

        # Bounded + status filter (bogus values degrade to unfiltered,
        # never an error) — read capability is viewer-level like the
        # outbox listing; tenant scoping comes from the identity.
        resp = client.get(
            "/bo/execution/audit-intents?status=bogus&limit=5",
            headers=self.VIEWER)
        assert resp.status_code == 200
        resp = client.get(
            "/bo/execution/audit-intents?status=failed",
            headers=self.VIEWER)
        assert resp.status_code == 200
        assert resp.json()["intents"] == []


def test_bobots_have_no_router_dependency() -> None:
    """Static check: no bo.bots module imports bo.routing or providers."""
    import ast

    bots_dir = (
        Path(__file__).parents[2] / "openexecutive" / "bo" / "bots"
    )
    for src in bots_dir.glob("*.py"):
        tree = ast.parse(src.read_text())
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for n in names:
                assert "bo.routing" not in n and "providers" not in n, (
                    f"{src.name} imports {n} — BoBots stay LLM-free"
                )


# --------------------------------------------------------------------------- #
# PILOT-07 — audit durabil pentru rebind: intența se persistă în aceeași
# tranzacție cu mutația rutei; reconcilierea către jurnal e idempotentă.
# --------------------------------------------------------------------------- #

class TestRebindAuditDurability:
    def _journal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """Real journal pointed at a tmp file — the honest sink. Also
        restores the real ``log_event`` (the autouse ``audit`` fixture
        replaces it with a capture sink)."""
        import openexecutive.audit as audit_pkg
        from openexecutive.audit import logger as audit_logger

        instance = audit_logger.AuditLogger(tmp_path / "journal.db")
        monkeypatch.setattr(audit_logger, "_default_logger", instance)
        monkeypatch.setattr(audit_pkg, "log_event", audit_logger.log_event)
        return instance

    def _legacy_row(self, db: Path, event_id: str = "evt_aud") -> None:
        store.enqueue_outbox(
            TENANT, "models", "ref-a",
            {"schemaVersion": "bo.model-observation.v1",
             "eventId": event_id, "tenantRef": TENANT},
            db_path=db)
        with bo_db.get_conn(db) as conn:
            conn.execute(
                "UPDATE bo_telemetry_outbox SET dest_bound = NULL, "
                "dest_endpoint = NULL, dest_ref = NULL "
                "WHERE event_id = ?", (event_id,))

    def test_rebind_intent_persists_when_journal_down(
        self, db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Crash between route commit and journal write: the mutation AND
        the audit intent commit atomically — nothing is lost silently."""
        from openexecutive.audit import logger as audit_logger

        self._journal(tmp_path, monkeypatch)
        self._legacy_row(db)
        # Journal write fails at drain time — mutation must still commit
        # with a durable pending intent, honestly reported.
        monkeypatch.setattr(
            audit_logger.AuditLogger, "log", lambda *a, **kw: None)
        out = store.rebind_outbox(
            TENANT, actor="admin@t", reason="audit durability", db_path=db)
        assert out["rebound"] == 1
        assert out["audit"] in ("pending", "failed")
        intents = store.pending_audit_intents(db_path=db)
        assert len(intents) == 1
        assert intents[0]["event"] == "bo_outbox_rebind"

        # Journal recovers; reconciliation delivers exactly once.
        monkeypatch.undo()
        journal2 = self._journal(tmp_path, monkeypatch)
        res = store.drain_audit_intents(db_path=db)
        assert res["delivered"] == 1
        assert store.pending_audit_intents(db_path=db) == []
        with audit_logger._get_conn(journal2._db_path) as conn:
            rows = conn.execute(
                "SELECT details_json FROM audit_log "
                "WHERE event_type = 'bo_outbox_rebind'").fetchall()
        assert len(rows) == 1
        assert "intent_id" in (rows[0]["details_json"] or "")
        # Replay — no duplicate rows in the journal.
        store.drain_audit_intents(db_path=db)
        with audit_logger._get_conn(journal2._db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE event_type = 'bo_outbox_rebind'").fetchone()[0] == 1

    def test_reconcile_dedup_when_mark_failed(
        self, db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Emit succeeded but the mark-delivered crashed: the intent stays
        pending, and reconcile must NOT emit a second journal row."""
        from openexecutive.audit import logger as audit_logger

        journal = self._journal(tmp_path, monkeypatch)
        self._legacy_row(db)
        out = store.rebind_outbox(
            TENANT, actor="admin@t", reason="reconcile", db_path=db)
        assert out["audit"] == "delivered"
        intent = store.pending_audit_intents(db_path=db)
        assert intent == []  # delivered
        # Simulate the crash window: journal has the row, intent reverts to
        # pending (mark never committed); the dead drainer's lease expired.
        with bo_db.get_conn(db) as conn:
            conn.execute(
                "UPDATE bo_audit_intents SET status = 'pending', "
                "delivered_at = NULL, drain_owner = NULL, "
                "drain_until = '2020-01-01T00:00:00Z'")
        res = store.drain_audit_intents(db_path=db)
        assert res["delivered"] == 1
        with audit_logger._get_conn(journal._db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_log "
                "WHERE event_type = 'bo_outbox_rebind'").fetchone()[0] == 1

    def test_drain_claim_is_exclusive(
        self, db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PILOT-08/AA-A01-02: the atomic claim — not the journal read —
        is the single-emitter gate. An intent held by a live drain lease
        is skipped, never emitted twice."""
        from openexecutive.audit import logger as audit_logger

        journal = self._journal(tmp_path, monkeypatch)
        self._legacy_row(db)
        # Journal down at rebind time → intent stays pending for the test.
        monkeypatch.setattr(
            audit_logger.AuditLogger, "log", lambda *a, **kw: None)
        store.rebind_outbox(
            TENANT, actor="admin@t", reason="claim", db_path=db)
        monkeypatch.undo()
        journal = self._journal(tmp_path, monkeypatch)
        iid = store.pending_audit_intents(db_path=db)[0]["intent_id"]
        now = datetime.now(UTC).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        # Another drainer holds a live claim.
        with bo_db.get_conn(db) as conn:
            assert store._claim_audit_intent(conn, iid, "other", now, 60)
            assert not store._claim_audit_intent(conn, iid, "me", now, 60)
        res = store.drain_audit_intents(db_path=db, owner="me")
        assert res["delivered"] == 0 and res["skipped"] == 1
        with audit_logger._get_conn(journal._db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
        # Lease expired → reclaimable; emit happens exactly once now.
        with bo_db.get_conn(db) as conn:
            conn.execute(
                "UPDATE bo_audit_intents SET drain_until = '2020-01-01T00:00:00Z'")
        res = store.drain_audit_intents(db_path=db, owner="me")
        assert res["delivered"] == 1
        with audit_logger._get_conn(journal._db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1

    def test_failed_intents_parked_then_requeued(
        self, db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dead journal parks the intent as ``failed`` after the
        administered attempt budget — excluded from auto-drain (no
        unlimited loop), durable evidence; the explicit requeue recovers
        it once the journal is back."""
        from openexecutive.audit import logger as audit_logger

        journal = self._journal(tmp_path, monkeypatch)
        self._legacy_row(db)
        monkeypatch.setattr(
            audit_logger.AuditLogger, "log", lambda *a, **kw: None)
        out = store.rebind_outbox(
            TENANT, actor="admin@t", reason="down", db_path=db)
        assert out["audit"] == "pending"
        # Attempt budget: 3 by default → 2 more drains park it.
        store.drain_audit_intents(db_path=db)
        res = store.drain_audit_intents(db_path=db)
        assert res["failed"] == 1
        intents = store.pending_audit_intents(db_path=db)
        assert [i["status"] for i in intents] == ["failed"]
        # Auto-drain must NOT loop on a parked intent.
        res = store.drain_audit_intents(db_path=db)
        assert res == {"delivered": 0, "failed": 0, "skipped": 0,
                       "preempted": 0}
        # Journal restored; explicit requeue → delivered, audited.
        monkeypatch.undo()
        journal = self._journal(tmp_path, monkeypatch)
        out = store.requeue_failed_audit_intents(
            TENANT, actor="admin@t", db_path=db)
        assert out["requeued"] == 1
        assert store.pending_audit_intents(db_path=db) == []
        with audit_logger._get_conn(journal._db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_log").fetchone()[0] == 2

    def test_drain_attempts_setting_bound(
        self, db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The failed-threshold is administered per tenant via
        ``bo.router.audit_drain_attempts`` (1–10) — here budget 1 parks
        after the very first emit failure."""
        from openexecutive.audit import logger as audit_logger
        from openexecutive.bo.settings import store as settings_store

        self._journal(tmp_path, monkeypatch)
        settings_store.set_value(
            TENANT, "bo.router.audit_drain_attempts", 1,
            expected_version=0, actor="admin@t", db_path=db)
        self._legacy_row(db)
        monkeypatch.setattr(
            audit_logger.AuditLogger, "log", lambda *a, **kw: None)
        store.rebind_outbox(
            TENANT, actor="admin@t", reason="budget", db_path=db)
        # rebind's own drain already burned the single attempt.
        assert [i["status"] for i in
                store.pending_audit_intents(db_path=db)] == ["failed"]

    def test_journal_dedup_marker_is_atomic(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Journal-level exactly-once + content contract: ``dedup_key=k``
        commits marker + row atomically; a same-content retry returns the
        original row id, while a different-content emit on the same key is
        an observable conflict — never presented as proof of the new op."""
        from openexecutive.audit import logger as audit_logger

        journal = self._journal(tmp_path, monkeypatch)
        first = journal.log(
            "bo_outbox_rebind", "s1", actor="a",
            details={"intent_id": "iid-x"}, dedup_key="iid-x")
        # Same key + same content → retry returns the original proof.
        retry = journal.log(
            "bo_outbox_rebind", "s1", actor="a",
            details={"intent_id": "iid-x"}, dedup_key="iid-x")
        # Same key + DIFFERENT content → conflict, refused, no new row.
        conflict = journal.log(
            "bo_outbox_rebind", "s2", actor="b",
            details={"intent_id": "iid-x"}, dedup_key="iid-x")
        assert first is not None and retry == first and conflict is None
        with audit_logger._get_conn(journal._db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_dedup").fetchone()[0] == 1

    def test_orphan_dedup_marker_recovers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A marker whose audit row is absent (restored/repaired journal)
        must not be returned as proof of a nonexistent row — the emit
        re-writes the evidence and re-points the marker atomically."""
        from openexecutive.audit import logger as audit_logger

        journal = self._journal(tmp_path, monkeypatch)
        # Orphan marker: points at a row that doesn't exist.
        with audit_logger._get_conn(journal._db_path) as conn:
            conn.execute(
                "INSERT INTO audit_dedup (dedup_key, audit_row_id, "
                "created_at) VALUES ('iid-orphan', 999999, 't')")
        row_id = journal.log(
            "bo_outbox_rebind", "recovered", actor="a",
            details={"intent_id": "iid-orphan"}, dedup_key="iid-orphan")
        assert row_id is not None and row_id != 999999
        with audit_logger._get_conn(journal._db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
            marker = conn.execute(
                "SELECT audit_row_id FROM audit_dedup "
                "WHERE dedup_key='iid-orphan'").fetchone()
            assert marker["audit_row_id"] == row_id

    def test_legacy_row_read_error_never_duplicates(
        self, db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """IA01-01: a legacy intent already emitted in a pre-``audit_dedup``
        journal — one transient read error must NOT produce a new row;
        the intent stays honestly pending until the read confirms."""
        from openexecutive.audit import logger as audit_logger

        journal = self._journal(tmp_path, monkeypatch)
        self._legacy_row(db)
        store.rebind_outbox(
            TENANT, actor="admin@t", reason="legacy", db_path=db)
        iid = store.pending_audit_intents(db_path=db)
        assert iid == []  # delivered immediately — journal reachable
        # Simulate the pre-dedup journal: strip the marker, re-pend intent.
        with audit_logger._get_conn(journal._db_path) as conn:
            conn.execute("DELETE FROM audit_dedup")
        with bo_db.get_conn(db) as conn:
            conn.execute(
                "UPDATE bo_audit_intents SET status='pending', "
                "delivered_at=NULL")
        # First drain: read fails once → NO emit, attempt burned.
        real_detail = audit_logger.AuditLogger.detail_row_id
        calls = {"n": 0}

        def flaky(self, *a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("transient read error")
            return real_detail(self, *a, **kw)

        monkeypatch.setattr(
            audit_logger.AuditLogger, "detail_row_id", flaky)
        res = store.drain_audit_intents(db_path=db)
        assert res["delivered"] == 0 and res["failed"] == 1
        with audit_logger._get_conn(journal._db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
        # Second drain: read recovers → confirmed, backfilled, delivered —
        # still exactly one journal row.
        res = store.drain_audit_intents(db_path=db)
        assert res["delivered"] == 1
        with audit_logger._get_conn(journal._db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_log").fetchone()[0] == 1
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_dedup").fetchone()[0] == 1

    def test_requeue_delivered_only_when_evidence_missing(
        self, db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Restore recovery: a ``delivered`` intent may be demoted only
        when the journal verifiably lost its row — live evidence refuses.
        The demoted intent re-emits exactly once at the next drain."""
        from openexecutive.audit import logger as audit_logger

        journal = self._journal(tmp_path, monkeypatch)
        self._legacy_row(db)
        store.rebind_outbox(
            TENANT, actor="admin@t", reason="r1", db_path=db)
        with bo_db.get_conn(db) as conn:
            iid = conn.execute(
                "SELECT intent_id FROM bo_audit_intents WHERE "
                "status='delivered'").fetchone()["intent_id"]

        # Evidence lives → demotion refused.
        with pytest.raises(ValueError, match="live journal evidence"):
            store.requeue_failed_audit_intents(
                TENANT, actor="admin@t", intent_id=iid, db_path=db)

        # Journal restored without the row (and the marker) → missing
        # evidence → explicit requeue → exactly one new row.
        with audit_logger._get_conn(journal._db_path) as conn:
            conn.execute("DELETE FROM audit_log")
            conn.execute("DELETE FROM audit_dedup")
        out = store.requeue_failed_audit_intents(
            TENANT, actor="admin@t", intent_id=iid, db_path=db)
        assert out["requeued"] == 1
        with audit_logger._get_conn(journal._db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE details_json "
                "LIKE ?", (f"%{iid}%",)).fetchone()[0] == 1

    def test_fenced_update_stale_owner(
        self, db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The final UPDATEs are conditioned on (owner, generation): an
        owner whose claim expired and was re-taken cannot overwrite the
        new owner's in-flight state."""
        from openexecutive.audit import logger as audit_logger

        self._journal(tmp_path, monkeypatch)
        self._legacy_row(db)
        monkeypatch.setattr(
            audit_logger.AuditLogger, "log", lambda *a, **kw: None)
        store.rebind_outbox(
            TENANT, actor="admin@t", reason="fence", db_path=db)
        monkeypatch.undo()
        self._journal(tmp_path, monkeypatch)
        iid = store.pending_audit_intents(db_path=db)[0]["intent_id"]
        now = datetime.now(UTC).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        with bo_db.get_conn(db) as conn:
            gen_a = store._claim_audit_intent(conn, iid, "A", now, 60)
            assert gen_a
        # Lease A expires; B re-claims with a new generation.
        with bo_db.get_conn(db) as conn:
            conn.execute(
                "UPDATE bo_audit_intents SET drain_until='2020-01-01T00:00:00Z'")
        with bo_db.get_conn(db) as conn:
            gen_b = store._claim_audit_intent(conn, iid, "B", now, 60)
            assert gen_b and gen_b != gen_a
            # Stale A tries to mark delivered — fenced UPDATE must no-op.
            cur = conn.execute(
                "UPDATE bo_audit_intents SET status='delivered', "
                "drain_owner=NULL, drain_until=NULL "
                "WHERE intent_id=? AND drain_owner=? AND drain_until=?",
                (iid, "A", gen_a))
            assert cur.rowcount == 0
            row = conn.execute(
                "SELECT status, drain_owner FROM bo_audit_intents "
                "WHERE intent_id=?", (iid,)).fetchone()
            assert row["status"] == "pending" and row["drain_owner"] == "B"

    def test_lease_seconds_administered(
        self, db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``bo.router.audit_drain_lease_s`` is live-read: a claim writes
        drain_until ≈ now + administered lease, not the 60s fallback."""
        from openexecutive.audit import logger as audit_logger
        from openexecutive.bo.settings import store as settings_store

        self._journal(tmp_path, monkeypatch)
        settings_store.set_value(
            TENANT, "bo.router.audit_drain_lease_s", 5,
            expected_version=0, actor="admin@t", db_path=db)
        self._legacy_row(db)
        monkeypatch.setattr(
            audit_logger.AuditLogger, "log", lambda *a, **kw: None)
        store.rebind_outbox(
            TENANT, actor="admin@t", reason="lease", db_path=db)
        monkeypatch.undo()
        iid = store.pending_audit_intents(db_path=db)[0]["intent_id"]
        before = datetime.now(UTC)
        now = before.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        with bo_db.get_conn(db) as conn:
            gen = store._claim_audit_intent(conn, iid, "me", now, 5)
            assert gen is not None
            until = datetime.fromisoformat(gen.replace("Z", "+00:00"))
            assert (until - before).total_seconds() <= 6

    def test_stale_claim_visible_in_status(
        self, db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``audit_intents_status`` exposes live vs stale claims — the
        trace of a crashed emitter is visible, not hidden."""
        from openexecutive.audit import logger as audit_logger

        self._journal(tmp_path, monkeypatch)
        self._legacy_row(db)
        monkeypatch.setattr(
            audit_logger.AuditLogger, "log", lambda *a, **kw: None)
        store.rebind_outbox(
            TENANT, actor="admin@t", reason="status", db_path=db)
        monkeypatch.undo()
        iid = store.pending_audit_intents(db_path=db)[0]["intent_id"]
        now = datetime.now(UTC).isoformat(
            timespec="milliseconds").replace("+00:00", "Z")
        with bo_db.get_conn(db) as conn:
            assert store._claim_audit_intent(conn, iid, "me", now, 60)
        st = store.audit_intents_status(TENANT, db_path=db)
        assert st["claimed"] == 1 and st["stale_claim"] == 0
        with bo_db.get_conn(db) as conn:
            conn.execute(
                "UPDATE bo_audit_intents SET drain_until='2020-01-01T00:00:00Z'")
        st = store.audit_intents_status(TENANT, db_path=db)
        assert st["stale_claim"] == 1 and st["claimed"] == 0
        stats = store.outbox_stats(TENANT, db_path=db)
        assert stats["audit_stale_claim"] == 1
