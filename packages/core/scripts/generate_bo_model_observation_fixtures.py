"""Regenerate fixtures/bo/model-observation/valid/*.json from the REAL
serializer + adapter (VAL3-01, bo.model-observation.v1 — the A02 contract).

Produced, not hand-written: the script drives a live TelemetryAdapter with a
BufferedTransport and the real ``bo.routing.serialize`` code paths, then
writes the envelopes. A02's receiver consumes these byte-for-byte.

Run from packages/core:

    uv run python scripts/generate_bo_model_observation_fixtures.py

Idempotent: ids/timestamps regenerate each run; the contract test requires
schema validity, not byte stability.
"""
from __future__ import annotations

import json
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "fixtures" / "bo" / "model-observation" / "valid"

import openexecutive.bo.db as bo_db  # noqa: E402

_tmp = Path(tempfile.mkdtemp(prefix="bo-obs-fixtures-"))
bo_db.DB_PATH = _tmp / "bo_agents.db"
bo_db.initialize_db(bo_db.DB_PATH)

from openexecutive.bo.routing import engine, serialize, store  # noqa: E402
from openexecutive.bo.routing.engine import Policy, TaskContext  # noqa: E402
from openexecutive.bo.settings import store as settings_store  # noqa: E402
from openexecutive.bo.telemetry.adapter import (  # noqa: E402
    BufferedTransport,
    TelemetryAdapter,
    set_adapter,
)

TENANT = "tenant-fixture"
NOW = datetime.now(UTC)
FRESH = (NOW - timedelta(days=3)).isoformat(timespec="milliseconds").replace(
    "+00:00", "Z"
)

CATALOG_ROWS = [
    {
        "provider": "anthropic", "model_id": "claude-fixture-a",
        "model_version": "2026-09-01", "state": "ACTIVE",
        "capabilities": ["analysis", "json"], "regions": ["eu", "us"],
        "cost": {"input_per_million": "3.00", "output_per_million": "15.00",
                 "currency": "USD", "valid_until": "2027-12-31"},
        "quality": {"score": 0.91, "methodology": "synthetic-pairwise",
                    "task_kind": "specialist", "eval_set_ref": "calib-01",
                    "eval_set_version": "v1", "observed_at": FRESH,
                    "sample_count": 128},
        "purpose": "specialist-analysis", "source": "admin",
    },
    {
        "provider": "anthropic", "model_id": "claude-fixture-b",
        "model_version": None, "state": "ACTIVE",
        "capabilities": ["analysis"], "regions": ["eu"],
        "cost": {"input_per_million": "0.80", "output_per_million": "4.00",
                 "currency": "USD", "valid_until": "2027-12-31"},
        "quality": {"score": 0.55, "methodology": "synthetic-pairwise",
                    "task_kind": "specialist", "eval_set_ref": "calib-01",
                    "eval_set_version": "v1", "observed_at": FRESH,
                    "sample_count": 128},
        "purpose": "specialist-analysis", "source": "admin",
    },
    {
        "provider": "openrouter", "model_id": "fixture-cheap",
        "model_version": None, "state": "DISABLED",
        "capabilities": ["analysis"], "regions": ["us"],
        "cost": {"input_per_million": "0.10", "output_per_million": "0.40",
                 "currency": "USD", "valid_until": "2027-12-31"},
        "quality": None,
        "purpose": "batch", "source": "admin",
    },
]


def main() -> None:
    for row in CATALOG_ROWS:
        store.create_entry(TENANT, dict(row), actor="fixture-gen")

    policy = Policy(
        allowed_providers=None, allowed_regions=None,
        required_capabilities=frozenset(), min_quality=0.6,
        eval_max_age_days=90, max_estimated_cost=None, cost_currency=None,
    )
    ctx = TaskContext("specialist", input_tokens=1200, output_tokens=300)
    decision = engine.recommend(store.list_catalog(TENANT), policy, ctx)

    transport = BufferedTransport()
    set_adapter(TelemetryAdapter(enabled=True, transport=transport))

    occurred = NOW.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    from openexecutive.bo.telemetry.adapter import get_adapter
    adapter = get_adapter()

    # 1. ROUTE observation (met bar)
    adapter.emit_model_observation(
        tenant=TENANT,
        occurred_at=occurred,
        body=serialize.routing_body(
            correlation_id="turn-fixture-001",
            policy_version="pol_fixture01",
            catalog_version=f"cat_v{store.catalog_version(TENANT)}",
            task_kind="specialist",
            recommendation=decision.recommendation,
            actual_route={"provider": "anthropic",
                          "modelId": "claude-fixture-a",
                          "modelVersion": "2026-09-01"},
            met_bar=decision.met_bar,
            reasons=decision.reasons,
            cost_estimate=decision.cost_estimate,
            measured={"inputTokens": 1200, "outputTokens": 300,
                      "requests": 1},
            billed={"amount": "0.0081", "currency": "USD",
                    "validUntil": occurred[:10],
                    "evidenceRef": "provider:usage.cost"},
        ),
    )

    # 2. REFUSE observation (quality bar unmet — min_quality raised)
    strict = Policy(
        allowed_providers=None, allowed_regions=None,
        required_capabilities=frozenset(), min_quality=0.95,
        eval_max_age_days=90, max_estimated_cost=None, cost_currency=None,
    )
    refuse = engine.recommend(store.list_catalog(TENANT), strict, ctx)
    adapter.emit_model_observation(
        tenant=TENANT,
        occurred_at=occurred,
        body=serialize.routing_body(
            correlation_id="turn-fixture-002",
            policy_version="pol_fixture02",
            catalog_version=f"cat_v{store.catalog_version(TENANT)}",
            task_kind="specialist",
            recommendation=refuse.recommendation,
            actual_route={"provider": "anthropic",
                          "modelId": "claude-fixture-a",
                          "modelVersion": "2026-09-01"},
            met_bar=refuse.met_bar,
            reasons=refuse.reasons,
            cost_estimate=refuse.cost_estimate,
            measured={"inputTokens": 1200, "outputTokens": 300,
                      "requests": 1},
            billed=None,
        ),
    )

    # 3. Catalog inventory sync (models[])
    adapter.emit_model_observation(
        tenant=TENANT,
        occurred_at=occurred,
        body=serialize.models_body(
            store.list_catalog(TENANT),
            owner_ref=f"tenant:{TENANT}",
            last_seen=occurred,
            sync_id="sync_fixture_001",
            complete=True,
        ),
    )

    OUT.mkdir(parents=True, exist_ok=True)
    names = ["routing-route.json", "routing-refuse.json", "models-sync.json"]
    for name, event in zip(names, transport.events, strict=True):
        (OUT / name).write_text(json.dumps(event, indent=2) + "\n")
        print(f"wrote {name}")

    # Validate every emitted doc against the vendored contract schema.
    import jsonschema
    schema = json.loads(
        (ROOT / "packages" / "core" / "tests" / "unit" / "fixtures"
         / "bo.model-observation.v1.schema.json").read_text()
    )
    for event in transport.events:
        jsonschema.validate(event, schema)
    print(f"{len(transport.events)} fixture(s) validate against "
          f"bo.model-observation.v1")


if __name__ == "__main__":
    main()
