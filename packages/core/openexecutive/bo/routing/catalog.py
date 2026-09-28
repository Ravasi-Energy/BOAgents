"""Model catalog entry for the BO observe-mode router (VAL3-01).

The catalog is *administered* — BOAgents is the authority for its own routing
catalog (common decision §2). Entries carry everything the deterministic
engine needs to filter before scoring: provider/model/version identity,
capabilities, regions, availability state, cost evidence and quality
evidence. Money is always a decimal string + ISO-4217 currency + a validity
date (``bo.model-observation.v1`` ``money`` shape); unknown stays unknown —
``None`` is never coerced to zero.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

_OPAQUE_RE = re.compile(r"^[^@\s]+$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TS_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d+)?(Z|\+00:00)$"
)
_MONEY_RE = re.compile(r"^\d+(\.\d{1,6})?$")
_CCY_RE = re.compile(r"^[A-Z]{3}$")

STATES = ("ACTIVE", "DISABLED", "DEPRECATED")

MAX_CAPABILITIES = 16
MAX_REGIONS = 16


class CatalogValidationError(ValueError):
    """Raised when a catalog entry fails validation → HTTP 422."""


def _opaque(value: Any, *, field: str, max_len: int = 128) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > max_len
        or not _OPAQUE_RE.match(value)
    ):
        raise CatalogValidationError(
            f"{field}: referință opacă invalidă (fără spații/@, max {max_len})"
        )
    return value


def _csv(value: Any, *, field: str, max_items: int) -> tuple[str, ...]:
    if not isinstance(value, list) or len(value) > max_items:
        raise CatalogValidationError(f"{field}: așteptată listă de max {max_items}")
    out: list[str] = []
    for item in value:
        out.append(_opaque(item, field=f"{field}[]", max_len=64))
    return tuple(dict.fromkeys(out))


def _money_str(value: Any, *, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _MONEY_RE.match(value):
        raise CatalogValidationError(f"{field}: așteptat șir zecimal, ex. \"2.50\"")
    # Normalize via Decimal so "2.500000" and "2.5" stay equal semantically.
    try:
        return format(Decimal(value).normalize(), "f")
    except InvalidOperation as exc:
        raise CatalogValidationError(f"{field}: zecimal invalid") from exc


@dataclass(frozen=True, slots=True)
class Quality:
    """Evaluation evidence for one entry — NULL score = never evaluated."""

    score: float | None
    methodology: str
    task_kind: str
    eval_set_ref: str
    eval_set_version: str
    observed_at: str
    sample_count: int

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Quality:
        """Admin/storage shape (snake_case) — symmetric with ``to_dict``.
        The wire conversion to the contract's camelCase lives in
        ``serialize.py``."""
        score = d.get("score")
        if score is not None:
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise CatalogValidationError("quality.score: așteptat număr [0,1] sau null")
            score = float(score)
            if not (0.0 <= score <= 1.0):
                raise CatalogValidationError("quality.score: în afara intervalului [0,1]")
        sample = d.get("sample_count", 0)
        if isinstance(sample, bool) or not isinstance(sample, int) or sample < 0:
            raise CatalogValidationError("quality.sample_count: așteptat întreg ≥ 0")
        return cls(
            score=score,
            methodology=_opaque(d.get("methodology"), field="quality.methodology"),
            task_kind=_opaque(d.get("task_kind"), field="quality.task_kind"),
            eval_set_ref=_opaque(d.get("eval_set_ref"), field="quality.eval_set_ref"),
            eval_set_version=_opaque(
                d.get("eval_set_version"), field="quality.eval_set_version"
            ),
            observed_at=_ts(d.get("observed_at"), field="quality.observed_at"),
            sample_count=sample,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "methodology": self.methodology,
            "task_kind": self.task_kind,
            "eval_set_ref": self.eval_set_ref,
            "eval_set_version": self.eval_set_version,
            "observed_at": self.observed_at,
            "sample_count": self.sample_count,
        }

    def to_wire(self) -> dict[str, Any]:
        """The ``quality`` shape of bo.model-observation.v1 (camelCase)."""
        return {
            "score": self.score,
            "methodology": self.methodology,
            "taskKind": self.task_kind,
            "evalSetRef": self.eval_set_ref,
            "evalSetVersion": self.eval_set_version,
            "observedAt": self.observed_at,
            "sampleCount": self.sample_count,
        }


@dataclass(frozen=True, slots=True)
class Cost:
    """Price evidence — decimal strings per 1M tokens, currency, validity."""

    input_per_million: str | None
    output_per_million: str | None
    currency: str | None
    valid_until: str | None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Cost:
        currency = d.get("currency")
        if currency is not None and (
            not isinstance(currency, str) or not _CCY_RE.match(currency)
        ):
            raise CatalogValidationError("cost.currency: așteptat cod ISO-4217 (ex. USD)")
        valid_until = d.get("valid_until")
        if valid_until is not None and (
            not isinstance(valid_until, str) or not _DATE_RE.match(valid_until)
        ):
            raise CatalogValidationError("cost.valid_until: așteptat YYYY-MM-DD")
        return cls(
            input_per_million=_money_str(
                d.get("input_per_million"), field="cost.input_per_million"
            ),
            output_per_million=_money_str(
                d.get("output_per_million"), field="cost.output_per_million"
            ),
            currency=currency,
            valid_until=valid_until,
        )

    @property
    def complete(self) -> bool:
        return all(
            v is not None
            for v in (
                self.input_per_million,
                self.output_per_million,
                self.currency,
                self.valid_until,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "input_per_million": self.input_per_million,
            "output_per_million": self.output_per_million,
            "currency": self.currency,
            "valid_until": self.valid_until,
        }


def _ts(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not _TS_RE.match(value):
        raise CatalogValidationError(
            f"{field}: așteptat timestamp UTC (YYYY-MM-DDTHH:MM:SSZ)"
        )
    return value


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    """One administered catalog row (identity + eligibility + evidence)."""

    entry_id: str
    provider: str
    model_id: str
    model_version: str | None
    state: str
    capabilities: tuple[str, ...]
    regions: tuple[str, ...]
    cost: Cost
    quality: Quality | None
    purpose: str
    source: str
    version: int = 0
    updated_by: str | None = None
    updated_at: str | None = None

    @property
    def ref(self) -> dict[str, Any]:
        """The ``routeChoice``/``modelRef`` shape — version null = unknown."""
        return {
            "provider": self.provider,
            "modelId": self.model_id,
            "modelVersion": self.model_version,
        }

    @property
    def available(self) -> bool:
        return self.state == "ACTIVE"


def validate_fields(
    *,
    provider: Any,
    model_id: Any,
    model_version: Any,
    state: Any,
    capabilities: Any,
    regions: Any,
    cost: Any,
    quality: Any,
    purpose: Any,
    source: Any,
) -> dict[str, Any]:
    """Validate raw API/store input → normalized fields for CatalogEntry."""
    provider_v = _opaque(provider, field="provider")
    model_id_v = _opaque(model_id, field="model_id")
    if model_version is not None:
        model_version = _opaque(model_version, field="model_version")
    if state not in STATES:
        raise CatalogValidationError(
            f"state: valoare permisă: {', '.join(STATES)}"
        )
    if not isinstance(cost, dict):
        raise CatalogValidationError("cost: așteptat obiect")
    cost_v = Cost.from_dict(cost)
    quality_v = Quality.from_dict(quality) if quality is not None else None
    return {
        "provider": provider_v,
        "model_id": model_id_v,
        "model_version": model_version,
        "state": state,
        "capabilities": _csv(capabilities, field="capabilities", max_items=MAX_CAPABILITIES),
        "regions": _csv(regions, field="regions", max_items=MAX_REGIONS),
        "cost": cost_v,
        "quality": quality_v,
        "purpose": _opaque(purpose, field="purpose"),
        "source": _opaque(source, field="source"),
    }
