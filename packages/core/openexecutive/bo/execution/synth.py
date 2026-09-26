"""Synthetic effect provider (VAL4-01 probes).

A countable, persistent, LOCAL effect — the proof target for the whole
lot. ``bo_synth_effects`` is the provider's own truth: one row per
accepted effect plus a running tenant counter. Counters prove the
absence of double effects (or the correct UNKNOWN state) across crashes,
restarts and concurrent workers — HTTP codes alone never could.

Two behaviors, both real:

* ``idempotent=True`` — ``submit`` with an already-seen key returns the
  SAME receipt without incrementing (at-least-once delivery +
  deduplication = exactly-once effect).
* ``idempotent=False`` — the provider does not deduplicate by key: a
  retry after an ambiguous timeout would DOUBLE the effect. Combined
  with ``fail_after_write`` (the effect is committed, then the answer is
  lost) this is the case where the engine MUST go UNKNOWN /
  RECONCILIATION_REQUIRED instead of blindly retrying.

``supports_receipts=False`` additionally removes the provider's receipt
lookup — the only resolution left is explicit reconciliation.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from openexecutive.bo.db import get_conn


class ProviderTimeout(Exception):
    """The answer was lost AFTER submission — the effect state is
    ambiguous by definition. Not proof of failure, not safe to retry
    against a non-idempotent provider."""


class ProviderError(Exception):
    """A definitive provider-side failure (the effect did not happen)."""


class EffectProvider(Protocol):
    """Structural contract of every effect provider the engine drives —
    the synthetic counter, the pilot HTTP adapter, any future real one."""

    name: str
    idempotent: bool
    retry_unknown: bool

    def submit(
        self, *, tenant: str, idempotency_key: str, payload_digest: str,
        amount: int,
    ) -> dict[str, Any]: ...

    def receipt_for(
        self, *, tenant: str, idempotency_key: str,
    ) -> dict[str, Any] | None: ...


class SyntheticCounterProvider:
    def __init__(
        self,
        *,
        name: str = "synth.counter",
        idempotent: bool,
        supports_receipts: bool = True,
        fail_after_write: bool = False,
        db_path: Path | None = None,
    ) -> None:
        self.name = name
        self.idempotent = idempotent
        # Explicit contract member (EffectProvider): the counter dedups by
        # key, so resuming after UNKNOWN is safe to retry through readback.
        self.retry_unknown = True
        self.supports_receipts = supports_receipts
        self.fail_after_write = fail_after_write
        self.db_path = db_path
        self.submit_calls = 0  # how many times the wire was touched

    def submit(
        self,
        *,
        tenant: str,
        idempotency_key: str,
        payload_digest: str,
        amount: int,
    ) -> dict[str, Any]:
        """One wire call. Persists the effect row + counter atomically."""
        self.submit_calls += 1
        now = datetime.now(UTC).isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"
        )
        with get_conn(self.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM bo_synth_effects "
                "WHERE tenant = ? AND effect_key = ?",
                (tenant, idempotency_key),
            ).fetchone()
            if row is not None:
                if self.idempotent:
                    return {
                        "receipt_ref": row["receipt_ref"],
                        "provider": row["provider"],
                        "effect_key": row["effect_key"],
                        "digest": row["digest"],
                        "amount": row["amount"],
                        "received_at": row["created_at"],
                        "deduplicated": True,
                    }
                # Non-idempotent: a duplicate key is a NEW effect. The
                # provider cannot and does not deduplicate — this is the
                # documented double-effect hazard.
                receipt = self._new_effect(
                    conn, tenant, idempotency_key, payload_digest, amount,
                    now, key_suffix=True,
                )
            else:
                receipt = self._new_effect(
                    conn, tenant, idempotency_key, payload_digest, amount, now
                )
        if self.fail_after_write:
            raise ProviderTimeout(
                "răspunsul s-a pierdut după comiterea efectului"
            )
        return receipt

    def _new_effect(
        self,
        conn: Any,
        tenant: str,
        key: str,
        digest: str,
        amount: int,
        now: str,
        *,
        key_suffix: bool = False,
    ) -> dict[str, Any]:
        if key_suffix:
            key = f"{key}#dup{uuid.uuid4().hex[:6]}"
        receipt_ref = f"rcp_{uuid.uuid4().hex[:20]}"
        conn.execute(
            """
            INSERT INTO bo_synth_effects (
                tenant, effect_key, digest, amount, receipt_ref, provider,
                created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (tenant, key, digest, amount, receipt_ref, self.name, now),
        )
        conn.execute(
            "INSERT INTO bo_synth_counter (tenant, total) VALUES (?, ?) "
            "ON CONFLICT (tenant) DO UPDATE SET total = total + excluded.total",
            (tenant, amount),
        )
        return {
            "receipt_ref": receipt_ref,
            "provider": self.name,
            "effect_key": key,
            "digest": digest,
            "amount": amount,
            "received_at": now,
            "deduplicated": False,
        }

    def receipt_for(
        self, *, tenant: str, idempotency_key: str
    ) -> dict[str, Any] | None:
        """Receipt lookup BEFORE retry — the only safe way to resolve an
        ambiguous SUBMITTED/UNKNOWN state without re-executing."""
        if not self.supports_receipts:
            return None
        with get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT * FROM bo_synth_effects "
                "WHERE tenant = ? AND effect_key = ?",
                (tenant, idempotency_key),
            ).fetchone()
        if row is None:
            return None
        return {
            "receipt_ref": row["receipt_ref"],
            "provider": row["provider"],
            "effect_key": row["effect_key"],
            "digest": row["digest"],
            "amount": row["amount"],
            "received_at": row["created_at"],
            "deduplicated": False,
        }

    def total(self, tenant: str) -> int:
        """The countable effect — what probes assert against."""
        with get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT total FROM bo_synth_counter WHERE tenant = ?",
                (tenant,),
            ).fetchone()
        return 0 if row is None else int(row["total"])

    def effect_count(self, tenant: str) -> int:
        with get_conn(self.db_path) as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM bo_synth_effects WHERE tenant = ?",
                (tenant,),
            ).fetchone()
        return int(row["n"])
