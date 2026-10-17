"""Delegation mandates (VAL4-01).

A mandate is the only authority under which an execution may produce an
effect. It binds: tenant, an opaque principal ref (server-derived — never
trusted from the request body), the parent delegation chain, the allowed
resources/actions, budget + concurrency + step + depth limits, expiry and
the policy version under which it was issued.

Children NEVER amplify: every limit is the intersection of the child's
request, the parent's grant and the tenant's administered caps
(``bo.exec.*``). Privilege or budget escalation is refused at creation;
revocation and expiry are re-checked at the EFFECT boundary, not only at
creation or approval.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

_TOKEN_RE = re.compile(r"^[a-z0-9][a-z0-9.*:_-]{0,63}$")


class MandateValidationError(ValueError):
    """The mandate request is malformed or asks for amplification."""


class MandateRevokedError(Exception):
    """The mandate (or an ancestor) was revoked — effects are closed."""


class MandateExpiredError(Exception):
    """The mandate (or an ancestor) expired — effects are closed."""


@dataclass(frozen=True, slots=True)
class Mandate:
    tenant: str
    mandate_id: str
    parent_mandate_id: str | None
    principal_ref: str
    depth: int
    allowed_resources: frozenset[str]
    allowed_actions: frozenset[str]
    budget_limit: Decimal
    concurrency_limit: int
    max_steps: int
    max_depth: int
    expires_at: str
    policy_version: int
    revoked_at: str | None
    revoked_reason: str | None
    created_by: str
    created_at: str
    guardian_ref: str | None = None
    budget_currency: str | None = None


def _validate_token(v: Any, *, label: str) -> str:
    if not isinstance(v, str) or not _TOKEN_RE.match(v.strip()):
        raise MandateValidationError(
            f"{label}: token invalid (a-z0-9.*:_-, max 64)"
        )
    return v.strip()


def _validate_token_set(v: Any, *, label: str, max_items: int = 64) -> frozenset[str]:
    if not isinstance(v, (list, tuple, set, frozenset)):
        raise MandateValidationError(f"{label}: așteptată listă de tokenuri")
    items = frozenset(_validate_token(x, label=label) for x in v)
    if not items:
        raise MandateValidationError(f"{label}: lista nu poate fi goală")
    if len(items) > max_items:
        raise MandateValidationError(f"{label}: cel mult {max_items} elemente")
    return items


def _validate_budget(v: Any) -> Decimal:
    if isinstance(v, bool):
        raise MandateValidationError("bugetul: valoare invalidă")
    if isinstance(v, float):
        raise MandateValidationError(
            "bugetul: float JSON nu este o reprezentare exactă — "
            "folosiți șir zecimal (ex. „100.00“) sau întreg"
        )
    try:
        amount = Decimal(str(v))
    except (InvalidOperation, ValueError) as exc:
        raise MandateValidationError("bugetul: format decimal invalid") from exc
    if amount.is_nan() or amount.is_infinite() or not (Decimal("0") < amount <= Decimal("1000000")):
        raise MandateValidationError("bugetul: în afara intervalului (0, 1000000]")
    return amount


def _validate_budget_currency(v: Any) -> str | None:
    """Optional ISO-4217 currency for the budget amount. ``None`` stays
    dimensionless (legacy abstract budgets) — never defaulted to a
    currency that was not declared (REGULA-SUME-VALUTE-V2)."""
    if v is None:
        return None
    from openexecutive.memory.company_profile import ISO_4217_CODES

    if not isinstance(v, str) or v not in ISO_4217_CODES:
        raise MandateValidationError(
            "moneda bugetului: așteptat cod ISO-4217 valid (ex. USD, EUR, RON)"
        )
    return v


def _validate_expiry(v: Any) -> str:
    if not isinstance(v, str):
        raise MandateValidationError("expiry: așteptat timestamp ISO-8601")
    raw = v.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise MandateValidationError("expiry: timestamp ISO-8601 invalid") from exc
    if parsed.tzinfo is None:
        raise MandateValidationError("expiry: lipsește fusul orar (folosește Z)")
    if parsed <= datetime.now(UTC):
        raise MandateValidationError("expiry: trebuie să fie în viitor")
    if parsed > datetime.now(UTC) + timedelta(days=365):
        raise MandateValidationError("expiry: cel mult 365 de zile în viitor")
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _validate_int_field(v: Any, *, minimum: int, maximum: int, label: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int):
        raise MandateValidationError(f"{label}: așteptat număr întreg")
    if not (minimum <= v <= maximum):
        raise MandateValidationError(
            f"{label}: valoarea trebuie să fie între {minimum} și {maximum}"
        )
    return v


def validate_mandate_fields(
    *,
    allowed_resources: Any,
    allowed_actions: Any,
    budget_limit: Any,
    concurrency_limit: Any,
    max_steps: Any,
    max_depth: Any,
    expires_at: Any,
    budget_currency: Any = None,
) -> dict[str, Any]:
    """Normalize + validate one mandate request (root or child fields)."""
    return {
        "allowed_resources": _validate_token_set(
            allowed_resources, label="resurse permise"
        ),
        "allowed_actions": _validate_token_set(
            allowed_actions, label="acțiuni permise"
        ),
        "budget_limit": _validate_budget(budget_limit),
        "budget_currency": _validate_budget_currency(budget_currency),
        "concurrency_limit": _validate_int_field(
            concurrency_limit, minimum=1, maximum=64, label="concurența"
        ),
        "max_steps": _validate_int_field(
            max_steps, minimum=1, maximum=500, label="limita de pași"
        ),
        "max_depth": _validate_int_field(
            max_depth, minimum=0, maximum=16, label="adâncimea maximă"
        ),
        "expires_at": _validate_expiry(expires_at),
    }


def check_child_intersection(
    parent: Mandate, fields: dict[str, Any]
) -> None:
    """Refuse any child that would amplify the parent's grant.

    Intersection, never union: resources/actions must be subsets, every
    numeric limit must be <= the parent's, and expiry must not outlive
    the parent's. A violation is a hard validation failure — the child
    mandate is never persisted.
    """
    def covered(child_pattern: str, parent_patterns: frozenset[str]) -> bool:
        """A child pattern is covered when some parent pattern matches it
        — ``synth.counter`` under parent ``synth.*`` is covered; the child
        wildcard ``synth.*`` under parent ``synth.counter`` is NOT (that
        would amplify)."""
        for p in parent_patterns:
            if p == child_pattern:
                return True
            if p.endswith(".*") and (
                child_pattern == p[:-2] or child_pattern.startswith(p[:-1])
            ):
                return True
        return False

    if not all(
        covered(r, parent.allowed_resources)
        for r in fields["allowed_resources"]
    ):
        raise MandateValidationError(
            "resursele copilului depășesc mandatul părinte (intersecție obligatorie)"
        )
    if not fields["allowed_actions"] <= parent.allowed_actions:
        raise MandateValidationError(
            "acțiunile copilului depășesc mandatul părinte (intersecție obligatorie)"
        )
    if fields["budget_limit"] > parent.budget_limit:
        raise MandateValidationError(
            "bugetul copilului depășește mandatul părinte"
        )
    # Currency intersects like the amount: a child cannot spend in a
    # different currency than the parent's budget (REGULA-SUME-VALUTE-V2 —
    # no implicit conversion and no mixed-currency ledgers). A child that
    # omits the currency inherits the parent's declared one.
    if parent.budget_currency is not None:
        if fields["budget_currency"] is None:
            fields["budget_currency"] = parent.budget_currency
        elif fields["budget_currency"] != parent.budget_currency:
            raise MandateValidationError(
                "moneda bugetului copilului diferă de cea a mandatului părinte"
            )
    elif fields["budget_currency"] is not None:
        raise MandateValidationError(
            "moneda nu poate apărea în lanț — mandatul părinte este "
            "nedeterminat ca valută (agregatele de lanț ar amesteca "
            "rezervări cu și fără monedă)"
        )
    if fields["concurrency_limit"] > parent.concurrency_limit:
        raise MandateValidationError(
            "concurența copilului depășește mandatul părinte"
        )
    if fields["max_steps"] > parent.max_steps:
        raise MandateValidationError(
            "limita de pași a copilului depășește mandatul părinte"
        )
    if fields["max_depth"] > parent.max_depth:
        raise MandateValidationError(
            "adâncimea copilului depășește mandatul părinte"
        )
    if fields["expires_at"] > parent.expires_at:
        raise MandateValidationError(
            "expiry-ul copilului depășește mandatul părinte"
        )
    if parent.depth + 1 > parent.max_depth:
        raise MandateValidationError(
            f"adâncimea de delegare {parent.depth + 1} depășește "
            f"max_depth={parent.max_depth} al părintelui"
        )


def mandate_state(mandate: Mandate, *, now: str | None = None) -> str:
    """``active`` | ``revoked`` | ``expired`` — evaluated at call time."""
    if mandate.revoked_at is not None:
        return "revoked"
    now = now or datetime.now(UTC).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )
    if mandate.expires_at <= now:
        return "expired"
    return "active"


def assert_active(mandate: Mandate, *, now: str | None = None) -> None:
    state = mandate_state(mandate, now=now)
    if state == "revoked":
        raise MandateRevokedError(
            f"mandatul {mandate.mandate_id} a fost revocat"
            + (f": {mandate.revoked_reason}" if mandate.revoked_reason else "")
        )
    if state == "expired":
        raise MandateExpiredError(f"mandatul {mandate.mandate_id} a expirat")


def new_mandate_id() -> str:
    return f"mnd_{uuid.uuid4().hex[:20]}"
