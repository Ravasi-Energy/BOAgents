"""Burn-rate parsing in the onboarding wizard's financials step.

Sibling of the ARR regression in ``test_onboarding_wizard_arr_parse.py``:
the burn-rate heuristic used the same ``[\\d,]+`` character class, so a bare
comma before "monthly", "/month", "per month" or "burn" captured ``","`` and
crashed ``build_profile_from_answers`` on ``float("")``.
"""
from __future__ import annotations

import pytest

from openexecutive.onboarding.wizard import build_profile_from_answers


@pytest.mark.parametrize(
    "text",
    [
        "Runway is fine, monthly costs are low",
        "We are profitable, burn is zero",
    ],
)
def test_comma_before_burn_keyword_does_not_crash(text: str) -> None:
    profile = build_profile_from_answers({"financials": text})

    assert "burn_rate_monthly" not in profile["financials"]


def test_comma_case_still_records_runway() -> None:
    """Used to crash on the bare comma before "monthly".

    The burn figure here follows its keyword, which the magnitude-first
    heuristic does not read; this test only pins the crash and the runway.
    """
    profile = build_profile_from_answers(
        {"financials": "12 months runway, monthly burn 100k"}
    )

    assert profile["financials"]["runway_months"] == 12.0


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("burn 50k monthly", 50_000.0),
        ("$120,000 per month, 18 months runway", 120_000.0),
        ("We spend 30K/month", 30_000.0),
        ("about 2,500 monthly burn", 2_500.0),
    ],
)
def test_burn_rate_still_parses(text: str, expected: float) -> None:
    profile = build_profile_from_answers({"financials": text})

    assert profile["financials"]["burn_rate_monthly"] == expected


def test_burn_rate_currency_from_explicit_markers() -> None:
    usd = build_profile_from_answers({"financials": "burning $40k monthly"})
    ron = build_profile_from_answers({"financials": "burn is 90k monthly, in RON"})
    bare = build_profile_from_answers({"financials": "about 50k monthly burn"})

    assert usd["financials"]["burn_rate_currency"] == "USD"
    assert ron["financials"]["burn_rate_currency"] == "RON"
    assert bare["financials"]["burn_rate_currency"] is None


@pytest.mark.parametrize(
    ("text", "expected_burn", "expected_ccy"),
    [
        # Currency belonging to another figure must not attach to burn.
        ("burn 50k monthly, revenue in USD", 50_000.0, None),
        ("burn 50k monthly. ARR is EUR 20M.", 50_000.0, None),
        # Code or symbol adjacent to the burn amount, either side.
        ("burn EUR 50k monthly", 50_000.0, "EUR"),
        ("burn 50k EUR monthly", 50_000.0, "EUR"),
        ("burn 50k USD monthly", 50_000.0, "USD"),
        ("50k RON per month", 50_000.0, "RON"),
        # Same-expression conflict stays unknown.
        ("burn $50k EUR monthly", 50_000.0, None),
    ],
)
def test_burn_currency_binds_to_the_amount_expression(
    text: str, expected_burn, expected_ccy
) -> None:
    profile = build_profile_from_answers({"financials": text})
    fin = profile["financials"]

    assert fin["burn_rate_monthly"] == expected_burn
    assert fin["burn_rate_currency"] == expected_ccy


@pytest.mark.parametrize(
    ("text", "expected_burn"),
    [
        # Field binding: "burn" outranks generic monthly figures, and
        # costs-bound amounts only win when nothing names burn.
        ("revenue 50k monthly, burn 30k monthly", 30_000.0),
        ("burn 30k monthly, revenue 50k monthly", 30_000.0),
        ("burn 50k monthly, costs 30k monthly", 50_000.0),
        ("costs 30k monthly, burn 50k monthly", 50_000.0),
        # Ambiguous: equally-bound figures stay unset.
        ("costs 30k monthly, spend 20k monthly", None),
        ("burn 50k monthly, burn 30k monthly", None),
        ("burn 50k monthly, burn 30k monthly", None),
    ],
)
def test_burn_amount_binds_to_the_field(text: str, expected_burn) -> None:
    profile = build_profile_from_answers({"financials": text})

    assert profile["financials"].get("burn_rate_monthly") == expected_burn
