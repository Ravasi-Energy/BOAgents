"""@requirement HIR-REQ-302
@requirement HIR-REQ-137
@requirement HIR-REQ-138
@requirement HIR-REQ-139
@requirement HIR-REQ-315

Interfetele conectorilor. Aceleasi pentru simulator si pentru sistemele reale.

Separarea e stricta:
  SourceConnector  — doar citire. Maker are voie la asta.
  WriteConnector   — scriere. Doar executorul, doar dupa Approve.
  ReceiptReader    — readback optional: starea efectului dupa cheie.

Ce intoarce o scriere nu e „un JSON care a mers". E un receipt tipizat
(`bo.effect-receipt.v1`, CONTRACTE-V1): APPLIED inseamna efect confirmat cu
dovada; ACKNOWLEDGED inseamna primit, neaplicat; REJECTED inseamna refuzat de
owner; orice altceva — lipsa, forma gresita, ecou care nu se potriveste — este
UNKNOWN si cere reconciliere, nu retry orb.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Protocol, runtime_checkable


class ConnectorError(Exception):
    """Sursa nu a raspuns sau a raspuns prost. Clauza art. 5.5."""


class ConnectorNotConfigured(ConnectorError):
    """Lipsesc credentialele. Clauza art. 5.4."""


@dataclass(frozen=True)
class SourceDocument:
    """Un document brut, asa cum a venit din sursa."""

    external_id: str
    kind: str            # ubl | scan | csv
    content: str
    trust: str           # trusted | untrusted — HIR-REQ-136
    fetched_from: str


@dataclass(frozen=True)
class BankStatementLine:
    row: int
    booked_on: date
    amount: Decimal
    description: str
    counterparty_cui: str | None = None
    currency: str | None = None


@runtime_checkable
class SourceConnector(Protocol):
    """Citire. Fara efecte in afara."""

    name: str

    def list_documents(self, *, tenant_id: str, since: date | None = None) -> list[SourceDocument]:
        ...


@runtime_checkable
class BankConnector(Protocol):
    name: str

    def statement_lines(self, *, tenant_id: str, since: date | None = None) -> list[BankStatementLine]:
        ...


@runtime_checkable
class WriteConnector(Protocol):
    """Scriere. Se apeleaza doar din executor, doar cu idempotency_key."""

    name: str
    capability: str  # ex: oblio.write

    def execute(self, *, tenant_id: str, idempotency_key: str, params: dict) -> dict:
        ...


# ── receiptul efectului (bo.effect-receipt.v1) ────────────────────────────────

RECEIPT_SCHEMA = "bo.effect-receipt.v1"
RECEIPT_APLICAT = "APPLIED"
RECEIPT_CONFIRMARE = "ACKNOWLEDGED"
RECEIPT_RESPINS = "REJECTED"
RECEIPT_NECUNOSCUT = "UNKNOWN"
RECEIPT_STATUSURI = (
    RECEIPT_APLICAT,
    RECEIPT_CONFIRMARE,
    RECEIPT_RESPINS,
    RECEIPT_NECUNOSCUT,
)


@dataclass(frozen=True)
class EffectReceipt:
    """Receipt tipizat al unui efect. Un HTTP 200 nu e dovada; acesta e."""

    schema_version: str
    status: str
    operation_id: str | None = None
    idempotency_key: str | None = None
    owner_ref: str | None = None
    payload_hash: str | None = None
    resource_ref: str | None = None
    resource_version: str | None = None
    applied_at: str | None = None
    evidence: dict = field(default_factory=dict)
    identity_valid: bool = False


def parseaza_receipt(
    brut: object, *, idempotency_key: str, payload_hash: str | None = None
) -> EffectReceipt:
    """Transforma raspunsul conectorului in receipt, fail-closed.

    Orice forma pe care nu o putem verifica devine UNKNOWN — aceeasi cheie cu
    alt ecou, sau APPLIED fara cheie, digest si operationId, nu sunt confirmari.
    UNKNOWN nu inseamna esec si nici succes: cere reconciliere inainte de retry.
    """
    if not isinstance(brut, dict):
        return EffectReceipt(
            schema_version=RECEIPT_SCHEMA,
            status=RECEIPT_NECUNOSCUT,
            idempotency_key=None,
            evidence={"reason": "raspunsul nu e un obiect"},
        )
    status = brut.get("status")
    if status not in RECEIPT_STATUSURI:
        status = RECEIPT_NECUNOSCUT
    echo_key = brut.get("idempotencyKey")
    if echo_key is not None and echo_key != idempotency_key:
        # acelasi raspuns, dar despre alta operatie — ambiguu
        status = RECEIPT_NECUNOSCUT
    echo_hash = brut.get("payloadHash")
    if echo_hash is not None and payload_hash is not None and echo_hash != payload_hash:
        # ownerul spune ca a aplicat altceva decat ce s-a semnat — conflict
        status = RECEIPT_NECUNOSCUT
    operation_id = brut.get("operationId")
    identity_valid = (
        isinstance(echo_key, str) and bool(echo_key) and echo_key == idempotency_key
        and isinstance(echo_hash, str) and bool(echo_hash) and echo_hash == payload_hash
    )
    if status == RECEIPT_APLICAT and (
        not identity_valid or not isinstance(operation_id, str) or not operation_id.strip()
    ):
        # Succesul cere identitatea si digestul aprobate, ecou explicit al ownerului.
        status = RECEIPT_NECUNOSCUT
    evidence = brut.get("evidenceRefs")
    return EffectReceipt(
        schema_version=RECEIPT_SCHEMA,
        status=status,
        operation_id=operation_id if isinstance(operation_id, str) else None,
        idempotency_key=echo_key if isinstance(echo_key, str) else None,
        owner_ref=brut.get("ownerRef") if isinstance(brut.get("ownerRef"), str) else None,
        payload_hash=echo_hash if isinstance(echo_hash, str) else None,
        resource_ref=brut.get("resourceRef") if isinstance(brut.get("resourceRef"), str) else None,
        resource_version=(
            brut.get("resourceVersion") if isinstance(brut.get("resourceVersion"), str) else None
        ),
        applied_at=brut.get("appliedAt") if isinstance(brut.get("appliedAt"), str) else None,
        evidence=evidence if isinstance(evidence, dict) else {"raw": evidence},
        identity_valid=identity_valid,
    )


@runtime_checkable
class ReceiptReader(Protocol):
    """Readback optional: starea unui efect dupa cheie, la owner.

    Un conector fara readback nu poate reconcilia — itemul ramane in
    WAITING_RECEIPT pana la rezolvare, nu se retrimite pe ghicite.
    """

    def check(self, *, tenant_id: str, idempotency_key: str) -> dict:
        ...
