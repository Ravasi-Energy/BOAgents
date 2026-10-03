"""ARR parsing in the onboarding wizard's business-model step.

Regression test for a crash that aborted onboarding entirely. The ARR
heuristic searched ``\\$?([\\d,]+)\\s*[Mm]``, whose character class matches a
bare comma with no digits. Any business-model answer containing a comma
followed by a word starting with M captured ``","``, which
``.replace(",", "")`` reduced to the empty string, and ``float("")`` raised
``ValueError`` out of ``build_profile_from_answers``. The wizard returned 500,
the profile was never written, and every subsequent step failed.

The trigger is an ordinary sentence, not a malformed one — see the parametrised
cases below.
"""
from __future__ import annotations

import pytest

from openexecutive.onboarding.wizard import build_profile_from_answers


@pytest.mark.parametrize(
    "text",
    [
        "Consultoría estratégica, marketing y operaciones",
        "Servicios profesionales, mantenimiento de sistemas",
        "Vendemos software B2B, modelo de suscripción",
        "We sell software, mostly to mid-market teams",
        "Design and build, maintenance included",
        # The exact answer from issue #84.
        "IT, marketing and video agency",
    ],
)
def test_comma_before_m_word_does_not_crash(text: str) -> None:
    """A comma followed by an m-word must not be read as a magnitude."""
    profile = build_profile_from_answers({"business_model": text})

    assert profile["target_customer"]["profile"] == text
    assert "annual_revenue_arr" not in profile


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("We do $2M in ARR", 2_000_000.0),
        ("Roughly 12M annually", 12_000_000.0),
        ("ARR is $1,500M across all lines", 1_500_000_000.0),
        # Phrasings the bare `[Mm]\b` narrowing silently dropped.
        ("roughly 50 million in revenue", 50_000_000.0),
        ("About 3 Million ARR", 3_000_000.0),
        ("$50MM ARR last year", 50_000_000.0),
        ("we closed the year at 7mm", 7_000_000.0),
    ],
)
def test_magnitude_still_parses(text: str, expected: float) -> None:
    """A real magnitude is still picked up after the narrowing."""
    profile = build_profile_from_answers({"business_model": text})

    assert profile["annual_revenue_arr"] == expected


def test_digits_before_an_m_word_are_not_a_magnitude() -> None:
    """The word boundary keeps "300 clients, marketing" from meaning $300M.

    Without it the capture starts at a real digit, so the crash guard alone
    would not help — it would silently record a fabricated ARR instead.
    """
    profile = build_profile_from_answers(
        {"business_model": "We have 300 clients, marketing is word of mouth"}
    )

    assert "annual_revenue_arr" not in profile


@pytest.mark.parametrize(
    "text",
    [
        "$5 minimum order, mostly SMBs",
        "We have 300 clients, marketing is word of mouth",
        "12 major accounts and growing",
    ],
)
def test_digits_before_a_non_magnitude_m_word_are_ignored(text: str) -> None:
    """The word boundary must hold across the whole alternation."""
    profile = build_profile_from_answers({"business_model": text})

    assert "annual_revenue_arr" not in profile


def test_no_magnitude_leaves_the_field_unset() -> None:
    profile = build_profile_from_answers({"business_model": "A boutique consultancy"})

    assert "annual_revenue_arr" not in profile
    assert profile["target_customer"]["pain_points"] == []


def test_long_digit_run_is_linear_time() -> None:
    """A "1,1,1,…" run with no magnitude after it must not go quadratic.

    Every digit in the run used to be a candidate match start, so a 32k-char
    answer took ~10s and a 320k one ~20 minutes on the event loop. With the
    lookbehind only the run's first digit is a candidate. If this regresses
    the test does not fail, it hangs — which is the point.
    """
    text = "1," * 100_000
    profile = build_profile_from_answers({"business_model": text, "financials": text})

    assert "annual_revenue_arr" not in profile
    assert "burn_rate_monthly" not in profile["financials"]


def test_long_whitespace_run_is_linear_time() -> None:
    """A digit followed by a long space run must not go quadratic either.

    The burn-rate pattern once had `\\s*[Kk]?\\s*`; with the K absent the two
    `\\s*` could split the run every possible way (400ms at 10k chars, 6s at
    40k). Sized past the API's answer bound so it does not depend on it.
    """
    text = "1" + " " * 200_000
    profile = build_profile_from_answers({"business_model": text, "financials": text})

    assert "annual_revenue_arr" not in profile
    assert "burn_rate_monthly" not in profile["financials"]


def test_long_digit_run_without_commas_is_linear_time() -> None:
    """Pure digits hit the runway and headcount parsers, not only ARR/burn.

    `(\\d+)\\s*month` without a lookbehind was quadratic (750ms at 10k),
    and int() on a 5000-digit headcount raises at Python's digit limit.
    """
    text = "9" * 200_000
    profile = build_profile_from_answers(
        {"business_model": text, "financials": text, "headcount_and_founding": text}
    )

    assert "annual_revenue_arr" not in profile
    assert profile["financials"] == {}
    assert profile["headcount"] == 999_999_999


@pytest.mark.parametrize(
    ("text", "expected_ccy"),
    [
        ("We do $2M in ARR", "USD"),
        ("about 20 million EUR", "EUR"),
        ("revenue of 5M euro this year", "EUR"),
        ("around 3M RON annually", "RON"),
        ("roughly 8M lei", "RON"),
        ("near 10M pounds", "GBP"),
        # No currency marker at all → unknown, never an assumed USD.
        ("Roughly 12M annually", None),
        ("About 3 Million ARR", None),
    ],
)
def test_arr_currency_from_explicit_markers(text: str, expected_ccy) -> None:
    profile = build_profile_from_answers({"business_model": text})

    assert "annual_revenue_arr" in profile
    assert profile["annual_revenue_arr_currency"] == expected_ccy


@pytest.mark.parametrize(
    ("text", "expected_ccy"),
    [
        # The defect from RA16-BO01-MONEY-01: a currency stated for a
        # *different* amount must not bleed into the ARR attribution.
        ("ARR 20M, costs in USD", None),
        ("ARR 20M, costs in EUR", None),
        ("ARR 20M. Our costs are in USD.", None),
        ("ARR 20M, monthly burn $80k", None),
        # Currency adjacent to the ARR amount still attaches, on either side.
        ("ARR $20M, costs in EUR", "USD"),
        ("ARR 20M USD, costs in EUR", "USD"),
        ("ARR EUR 20M", "EUR"),
        ("ARR in USD 20M", "USD"),
        ("ARR 20M in RON", "RON"),
        ("ARR 20M, in USD", "USD"),
        # Two amounts with their own markers — each keeps its own.
        ("ARR 20M RON, costs $5M", "RON"),
        # Markers on the same expression that disagree → conflict, unknown.
        ("ARR $20M EUR", None),
        ("ARR 20M USD or EUR", None),
    ],
)
def test_arr_currency_binds_to_the_amount_expression(
    text: str, expected_ccy
) -> None:
    profile = build_profile_from_answers({"business_model": text})

    assert "annual_revenue_arr" in profile
    assert profile["annual_revenue_arr_currency"] == expected_ccy


@pytest.mark.parametrize(
    ("text", "expected_arr"),
    [
        # Field binding: the ARR keyword picks its own amount, not another
        # field's ("costs $5M, ARR 20M" must not yield 5M).
        ("costs $5M, ARR 20M", 20_000_000.0),
        ("costs $5M, ARR20M", 20_000_000.0),
        ("ARR 20M, costs $5M", 20_000_000.0),
        ("revenue 50M, ARR 20M", 20_000_000.0),
        ("We do $2M in ARR", 2_000_000.0),
        ("costs $5M, ARR 0.5M", 500_000.0),
        ("ARR 0M EUR", 0.0),
        # Ambiguous magnitudes — the field stays unset, never a guess.
        ("ARR 20M, ARR 30M", None),
        ("20M, 30M", None),
        ("$5M but also 4M EUR", None),
        ("We did 12M this year, 8M last year", None),
    ],
)
def test_arr_amount_binds_to_the_field(text: str, expected_arr) -> None:
    profile = build_profile_from_answers({"business_model": text})

    assert profile.get("annual_revenue_arr") == expected_arr


@pytest.mark.parametrize(
    "text",
    [
        # RA17-BO01-MONEY-02: a single magnitude is not automatically the
        # field's — when it explicitly belongs to a foreign field, or the
        # answer declares the target missing, ARR stays unset.
        "costs $5M",
        "burn $50k monthly",
        "spend 5M EUR",
        "costs: $5M",
        "ARR unavailable; costs $5M",
        "ARR not stated. Our costs are 5M EUR.",
        "no ARR yet, costs $5M",
        "ARR: n/a, spend 5M",
        "revenue undisclosed, we spend 5M",
        "ARR is TBD",
    ],
)
def test_arr_foreign_or_negated_single_candidate_stays_unset(text: str) -> None:
    profile = build_profile_from_answers({"business_model": text})

    assert "annual_revenue_arr" not in profile


@pytest.mark.parametrize(
    ("text", "expected_arr", "expected_ccy"),
    [
        # Foreign-bound figures drop out of candidacy; a sole remaining
        # unbound magnitude is still the historical univocal answer.
        ("costs $5M, 20M", 20_000_000.0, None),
        # The ARR figure keeps winning across clauses in either order,
        # including when label and value share one clause.
        ("our costs are $5M but ARR is $20M", 20_000_000.0, "USD"),
        ("ARR: $20M, costs: $5M", 20_000_000.0, "USD"),
        ("we make $2M ARR with $500k costs monthly", 2_000_000.0, "USD"),
    ],
)
def test_arr_foreign_candidates_are_excluded_not_selected(
    text: str, expected_arr: float, expected_ccy
) -> None:
    profile = build_profile_from_answers({"business_model": text})

    assert profile["annual_revenue_arr"] == expected_arr
    assert profile["annual_revenue_arr_currency"] == expected_ccy
