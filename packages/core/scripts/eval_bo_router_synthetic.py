"""Synthetic evaluation for the observe-mode router (VAL3-01).

Two DISJOINT sets, both synthetic — no real traffic, no provider calls:

* ``CALIBRATION`` — used to pick the quality threshold that best separates
  acceptable from unacceptable candidates.
* ``TEST`` — a different population (different providers/scores/edges) used
  only to report how the calibrated threshold generalizes, including
  adverse cases (stale evidence, denied providers, missing data).

Run from packages/core:

    uv run python scripts/eval_bo_router_synthetic.py

Prints a markdown-ready report; the numbers are deterministic (fixed
populations, fixed ``now``).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from openexecutive.bo.routing.catalog import CatalogEntry, Cost, Quality
from openexecutive.bo.routing.engine import Policy, TaskContext, recommend

NOW = datetime(2026, 9, 24, tzinfo=UTC)


def _q(score: float | None, days: int = 5, task: str = "specialist") -> Quality:
    return Quality(
        score=score,
        methodology="synthetic-harness",
        task_kind=task,
        eval_set_ref="synth-v1",
        eval_set_version="v1",
        observed_at=(NOW - timedelta(days=days))
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        sample_count=64,
    )


def _e(
    i: str,
    *,
    provider: str = "p-a",
    score: float | None = 0.8,
    days: int = 5,
    regions: tuple[str, ...] = ("eu",),
    caps: tuple[str, ...] = ("analysis",),
    state: str = "ACTIVE",
    cost: str = "1.00",
    task: str = "specialist",
) -> CatalogEntry:
    return CatalogEntry(
        entry_id=i, provider=provider, model_id=f"m-{i}",
        model_version=None, state=state, capabilities=caps,
        regions=regions,
        cost=Cost(cost, cost, "USD", "2027-12-31"),
        quality=_q(score, days, task),
        purpose="eval", source="synthetic",
    )


def _pol(**kw) -> Policy:
    base = dict(
        allowed_providers=None, allowed_regions=None,
        required_capabilities=frozenset(), min_quality=0.6,
        eval_max_age_days=90, max_estimated_cost=None, cost_currency=None,
    )
    base.update(kw)
    return Policy(**base)


CTX = TaskContext("specialist", input_tokens=1000, output_tokens=200)

# --- CALIBRATION: 12 labeled cases (expected ROUTE/REFUSE) ---------------- #
CALIBRATION: list[tuple[str, list[CatalogEntry], Policy, str]] = [
    ("calib-accept-high", [_e("a", score=0.9)], _pol(), "ROUTE"),
    ("calib-accept-threshold", [_e("a", score=0.6)], _pol(), "ROUTE"),
    ("calib-refuse-below", [_e("a", score=0.59)], _pol(), "REFUSE"),
    ("calib-refuse-unevaluated", [_e("a", score=None)], _pol(), "REFUSE"),
    ("calib-refuse-provider", [_e("a", provider="p-x")],
     _pol(allowed_providers=frozenset({"p-a"})), "REFUSE"),
    ("calib-refuse-region", [_e("a", regions=("cn",))],
     _pol(allowed_regions=frozenset({"eu"})), "REFUSE"),
    ("calib-refuse-stale", [_e("a", days=200)], _pol(), "REFUSE"),
    ("calib-refuse-disabled", [_e("a", state="DISABLED")], _pol(), "REFUSE"),
    ("calib-refuse-capability",
     [_e("a", caps=("analysis",))],
     _pol(required_capabilities=frozenset({"vision"})), "REFUSE"),
    ("calib-refuse-budget", [_e("a", cost="999.00")],
     _pol(max_estimated_cost=Decimal("0.001"), cost_currency="USD"),
     "REFUSE"),
    ("calib-accept-two-candidates",
     [_e("a", score=0.7), _e("b", score=0.9, provider="p-b")],
     _pol(), "ROUTE"),
    ("calib-refuse-empty", [], _pol(), "REFUSE"),
]

# --- TEST: disjoint population (different ids, scores, providers) --------- #
TEST: list[tuple[str, list[CatalogEntry], Policy, str]] = [
    ("test-accept", [_e("t1", score=0.75, provider="p-c")], _pol(), "ROUTE"),
    ("test-refuse-low", [_e("t2", score=0.3)], _pol(), "REFUSE"),
    ("test-refuse-mixed",
     [_e("t3", score=0.9, provider="p-x"), _e("t4", score=0.4)],
     _pol(allowed_providers=frozenset({"p-a"})), "REFUSE"),
    ("test-accept-fallback-eligible",
     [_e("t5", score=0.9, state="DISABLED"), _e("t6", score=0.7)],
     _pol(), "ROUTE"),
    ("test-refuse-all-disabled",
     [_e("t7", state="DISABLED"), _e("t8", state="DEPRECATED")],
     _pol(), "REFUSE"),
    ("test-refuse-stale+eval-missing",
     [_e("t9", days=500), _e("t10", score=None)], _pol(), "REFUSE"),
    ("test-refuse-mismatch-task",
     [_e("t11", score=0.95, task="triage")], _pol(), "REFUSE"),
    ("test-accept-multi-region",
     [_e("t12", score=0.8, regions=("eu", "us"))],
     _pol(allowed_regions=frozenset({"eu"})), "ROUTE"),
]


def _run(cases, min_quality: float) -> tuple[int, list[str]]:
    ok = 0
    misses: list[str] = []
    for name, catalog, pol, expected in cases:
        from dataclasses import replace

        pol = replace(pol, min_quality=min_quality)
        d = recommend(catalog, pol, CTX, now=NOW)
        if d.decision == expected:
            ok += 1
        else:
            misses.append(
                f"{name}: expected {expected}, got {d.decision} "
                f"({d.reasons})"
            )
    return ok, misses


def main() -> None:
    print("# Evaluare sintetică — router observe (VAL3-01)\n")
    print(f"`now` fixat: {NOW.isoformat()} · task_kind: {CTX.task_kind}\n")

    # Calibration: sweep the threshold on the calibration set only.
    print("## Calibrare (12 cazuri sintetice)\n")
    best, best_ok = 0.6, -1
    for q in (0.0, 0.4, 0.5, 0.6, 0.7, 0.8):
        ok, misses = _run(CALIBRATION, q)
        print(f"- min_quality={q}: {ok}/12 corecte"
              + (f" — {misses}" if misses else ""))
        if ok > best_ok:
            best, best_ok = q, ok
    print(f"\nPrag ales pe calibrare: **{best}** ({best_ok}/12)\n")

    # Test: disjoint set, fixed threshold from calibration.
    print("## Test (8 cazuri sintetice, disjuncte)\n")
    ok, misses = _run(TEST, best)
    print(f"Rezultat: **{ok}/8** corecte cu pragul {best}")
    for m in misses:
        print(f"- MISS {m}")
    print()
    print("## Limite\n")
    print("- Populații sintetice mici (12+8); acoperă decizia ROUTE/REFUSE și")
    print("  motivele de eliminare, nu calitatea reală a vreunui model.")
    print("- Scorurile/costurile sunt inventate; nu fac afirmații despre")
    print("  provideri reali. Niciun apel la provider, nicio cheie.")
    print("- `metBar` este o proprietate a deciziei observate — pragul nu")
    print("  blochează și nu redirecționează apelul real (observe-only).")


if __name__ == "__main__":
    main()
