"""Deterministic observe-mode recommendation engine (VAL3-01).

Pure function over (catalog snapshot, policy snapshot, task context, now):
the same inputs always produce byte-identical output — no clock, no RNG, no
I/O. This is what the probes replay.

Contract (common decision §5 + SPEC-ROUTER-OBSERVE §4): eligibility filters
run BEFORE any scoring, in this fixed order:

    1. provider allowlist          → PROVIDER_DENIED
    2. region/data policy          → REGION_DENIED
    3. capabilities                → CAPABILITY_MISSING
    4. budget                      → BUDGET_EXCEEDED / COST_DATA_MISSING
    5. availability                → MODEL_DISABLED
    6. evidence freshness          → EVAL_MISSING / EVAL_TASK_MISMATCH /
                                     STALE_EVALUATION

Only survivors are scored: quality.score desc → estimated cost asc →
(provider, model_id, model_version) lexicographic — the documented,
deterministic tie-break. ``metBar`` = top survivor's score ≥ min_quality.
``metBar=false`` never yields an accepted recommendation — the decision is
REFUSE and reasons name why. Unknown stays unknown: missing quality or cost
data is an explicit elimination reason when that data is required, never a
zero score.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from openexecutive.bo.routing.catalog import CatalogEntry

REASONS = (
    "PROVIDER_DENIED",
    "REGION_DENIED",
    "CAPABILITY_MISSING",
    "BUDGET_EXCEEDED",
    "COST_DATA_MISSING",
    "MODEL_DISABLED",
    "EVAL_MISSING",
    "EVAL_TASK_MISMATCH",
    "STALE_EVALUATION",
    "QUALITY_BAR_UNMET",
    "CATALOG_EMPTY",
)


@dataclass(frozen=True, slots=True)
class Policy:
    """Tenant routing policy — assembled from ``bo.router.*`` settings."""

    allowed_providers: frozenset[str] | None  # None = no restriction
    allowed_regions: frozenset[str] | None
    required_capabilities: frozenset[str]
    min_quality: float  # [0,1]
    eval_max_age_days: int
    max_estimated_cost: Decimal | None
    cost_currency: str | None


@dataclass(frozen=True, slots=True)
class TaskContext:
    """What the observed call needed — task kind + measured usage."""

    task_kind: str
    input_tokens: int | None
    output_tokens: int | None


@dataclass(frozen=True, slots=True)
class CandidateOutcome:
    entry: CatalogEntry
    eligible: bool
    reason: str | None
    estimated_cost: Decimal | None
    score: float | None


@dataclass(frozen=True, slots=True)
class Decision:
    """Engine output — serializes deterministically for replay."""

    decision: str  # "ROUTE" | "REFUSE"
    recommendation: dict[str, Any] | None
    met_bar: bool
    reasons: list[str]
    cost_estimate: dict[str, Any] | None
    candidates: list[CandidateOutcome]

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "recommendation": self.recommendation,
            "met_bar": self.met_bar,
            "reasons": list(self.reasons),
            "cost_estimate": self.cost_estimate,
            "candidates": [
                {
                    "ref": c.entry.ref,
                    "eligible": c.eligible,
                    "reason": c.reason,
                    "estimated_cost": (
                        str(c.estimated_cost) if c.estimated_cost is not None else None
                    ),
                    "score": c.score,
                }
                for c in self.candidates
            ],
        }


def _estimate_cost(
    entry: CatalogEntry, ctx: TaskContext
) -> Decimal | None:
    """What THIS call would have cost on the candidate — decimal math only."""
    cost = entry.cost
    if (
        not cost.complete
        or ctx.input_tokens is None
        or ctx.output_tokens is None
    ):
        return None
    per_m = Decimal(1_000_000)
    return (
        Decimal(cost.input_per_million or "0") * ctx.input_tokens
        + Decimal(cost.output_per_million or "0") * ctx.output_tokens
    ) / per_m


def _eval_age_days(entry: CatalogEntry, now: datetime) -> float | None:
    if entry.quality is None:
        return None
    observed = datetime.fromisoformat(
        entry.quality.observed_at.replace("Z", "+00:00")
    )
    return (now - observed).total_seconds() / 86400.0


def recommend(
    catalog: list[CatalogEntry],
    policy: Policy,
    ctx: TaskContext,
    *,
    now: datetime | None = None,
) -> Decision:
    """Deterministic recommendation. Never mutates, never does I/O."""
    now = now or datetime.now(UTC)
    outcomes: list[CandidateOutcome] = []

    for entry in catalog:
        # --- filter 1: provider/model policy (tenant scope already applied
        # by the store query — the catalog itself is per-tenant)
        if (
            policy.allowed_providers is not None
            and entry.provider not in policy.allowed_providers
        ):
            outcomes.append(CandidateOutcome(entry, False, "PROVIDER_DENIED", None, None))
            continue

        # --- filter 2: region / data policy. Empty regions = unrestricted.
        if (
            policy.allowed_regions is not None
            and entry.regions
            and not set(entry.regions) & set(policy.allowed_regions)
        ):
            outcomes.append(CandidateOutcome(entry, False, "REGION_DENIED", None, None))
            continue

        # --- filter 3: capabilities required by the task kind
        if not set(policy.required_capabilities) <= set(entry.capabilities):
            outcomes.append(
                CandidateOutcome(entry, False, "CAPABILITY_MISSING", None, None)
            )
            continue

        # --- filter 4: budget. A configured cap makes cost data *required*;
        # missing cost is an explicit elimination, not a zero.
        est = _estimate_cost(entry, ctx)
        if policy.max_estimated_cost is not None:
            if est is None:
                outcomes.append(
                    CandidateOutcome(entry, False, "COST_DATA_MISSING", None, None)
                )
                continue
            if est > policy.max_estimated_cost:
                outcomes.append(
                    CandidateOutcome(entry, False, "BUDGET_EXCEEDED", est, None)
                )
                continue

        # --- filter 5: availability
        if not entry.available:
            outcomes.append(CandidateOutcome(entry, False, "MODEL_DISABLED", est, None))
            continue

        # --- filter 6: evaluation evidence freshness
        if entry.quality is None or entry.quality.score is None:
            outcomes.append(CandidateOutcome(entry, False, "EVAL_MISSING", est, None))
            continue
        if entry.quality.task_kind != ctx.task_kind:
            outcomes.append(
                CandidateOutcome(
                    entry, False, "EVAL_TASK_MISMATCH", est, entry.quality.score
                )
            )
            continue
        age = _eval_age_days(entry, now)
        if age is None or age > policy.eval_max_age_days:
            outcomes.append(
                CandidateOutcome(
                    entry, False, "STALE_EVALUATION", est, entry.quality.score
                )
            )
            continue

        outcomes.append(
            CandidateOutcome(entry, True, None, est, entry.quality.score)
        )

    # Candidate outcomes are emitted in canonical order — replay must be
    # byte-identical regardless of the input catalog ordering.
    outcomes.sort(
        key=lambda o: (o.entry.provider, o.entry.model_id,
                       o.entry.model_version or "")
    )

    # --- score survivors only: quality desc → cost asc → identity asc
    eligible = [o for o in outcomes if o.eligible]
    eligible.sort(
        key=lambda o: (
            -(o.score or 0.0),
            o.estimated_cost if o.estimated_cost is not None else Decimal("Infinity"),
            o.entry.provider,
            o.entry.model_id,
            o.entry.model_version or "",
        )
    )

    reasons: list[str] = []
    seen: set[str] = set()
    for o in outcomes:
        if o.reason and o.reason not in seen:
            seen.add(o.reason)
            reasons.append(o.reason)

    top = eligible[0] if eligible else None
    met_bar = top is not None and (top.score or 0.0) >= policy.min_quality

    if top is not None and met_bar:
        decision = "ROUTE"
        recommendation = top.entry.ref
    else:
        decision = "REFUSE"
        recommendation = None
        if not catalog:
            reasons = ["CATALOG_EMPTY"]
        elif top is not None:
            reasons.append("QUALITY_BAR_UNMET")

    cost_estimate: dict[str, Any] | None = None
    if top is not None and top.estimated_cost is not None and top.entry.cost.complete:
        cost_estimate = {
            "amount": format(
                top.estimated_cost.quantize(Decimal("0.000001")), "f"
            ),
            "currency": top.entry.cost.currency,
            "validUntil": top.entry.cost.valid_until,
        }

    return Decision(
        decision=decision,
        recommendation=recommendation,
        met_bar=met_bar,
        reasons=reasons[:16],
        cost_estimate=cost_estimate,
        candidates=outcomes,
    )


__all__ = [
    "CandidateOutcome",
    "Decision",
    "Policy",
    "REASONS",
    "TaskContext",
    "recommend",
]
