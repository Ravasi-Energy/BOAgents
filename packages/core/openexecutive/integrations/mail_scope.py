"""Scoped processing identity for inbound email (RA11-B01).

The pre-scope poller keyed every durable marker by the bare provider
message id: ``email_processed:{mid}``, ``email_attempt:{mid}:{n}`` and so
on. A provider id is only unique inside one mailbox, so the same ``mid``
arriving under a different account — or the same journal serving a
different client context after a slot switch — was consumed on the other
context's evidence: marked read, declared processed, never executed.

This module defines the *processing scope*: a versioned, hashed tuple of
``(tenant, client/slot, mailbox)`` that every dedup family now carries.
The scope is bound durably into the audit journal itself
(``mail_scope_binding:v{n}`` markers), so the journal declares which
contexts it has served instead of trusting whatever the current process
configuration happens to say.

Rules:

- An unbound journal with no prior mail evidence binds on first use.
- An unbound journal WITH evidence — legacy (unscoped) markers, scoped
  markers whose binding row was lost, or an orphaned binding marker —
  refuses until an operator attests the mailbox via
  ``bo.mail.scope.bound_mailbox`` — attributing foreign evidence to the
  current config would invent provenance the journal never recorded.
- The attestation is SINGLE-SHOT: it is consumed (cleared, with an
  audited settings change) when it authorizes a bind, so a forgotten
  value can never silently adopt a future journal.
- A bound journal must match the live (tenant, client, mailbox). A
  mailbox/client change requires the admin attestation; a tenant
  mismatch never auto-bridges — the journal belongs to another
  installation.
- The NEWEST well-formed binding row is authoritative and must verify
  (marker back-reference, field types, token re-derivation). A forged
  or torn rebind refuses — the journal never falls back to an older
  verified binding, which would silently downgrade its declared scope.
- Legacy markers stay untouched (no backfill) and block the affected
  message for human reconciliation — never a silent consume.

Nothing here asks the provider to prove the mailbox identity: the
configured address is recorded as *declared*, and the binding +
attestation contract is what prevents silent cross-account reuse.
"""
from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SCOPE_FORMAT = "s1"
BINDING_EVENT = "mail_scope_bound"
BINDING_PREFIX = "mail_scope_binding:"

# Dedup families written by pre-scope pollers. The scoped families use
# ``@`` after the name (``email_processed@s1.x:{mid}``), so a LIKE-prefix
# count over the ``":"``-joined legacy names sees exactly the pre-scope
# evidence and nothing newer.
LEGACY_PREFIXES = (
    "email_processed:",
    "email_attempt:",
    "email_attempt_result:",
    "email_fetch_fail:",
)

# Scoped families — non-legacy evidence. An UNBOUND journal holding any
# of these means its binding rows were lost while the evidence survived:
# binding fresh would attribute a foreign history to this install.
SCOPED_PREFIXES = (
    "email_processed@",
    "email_attempt@",
    "email_attempt_result@",
    "email_fetch_fail@",
)


@dataclass(frozen=True)
class MailScope:
    """One processing identity. ``token`` is what dedup keys carry."""

    token: str
    tenant: str
    client_key: str  # "slot:<slug>" or "install:<uuid>" (slot-less installs)
    mailbox: str
    version: int


def scope_token(tenant: str, client_key: str, mailbox: str) -> str:
    """Canonical, versioned scope serialization → short opaque token."""
    raw = f"{SCOPE_FORMAT}|{tenant}|{client_key}|{mailbox}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"{SCOPE_FORMAT}.{digest}"


def _all_binding_rows(logger_: Any) -> list[Any]:
    """Every mail_scope_bound row — paged so the scan is never
    truncated by the 1000-row query cap. Callers must not assume an
    order (``query`` returns id DESC)."""
    out: list[Any] = []
    offset = 0
    while True:
        page = logger_.query(
            event_type=BINDING_EVENT, limit=1000, offset=offset
        )
        out.extend(page)
        if len(page) < 1000:
            return out
        offset += 1000


def _verify_binding_row(logger_: Any, row: Any, details: dict[str, Any]) -> bool:
    """A binding row is trusted only when ALL of these hold:

    - field types are what ``_bind`` wrote (a corrupt/forgeable row with
      ``version="x"`` must refuse cleanly, never crash ``resolve``);
    - ``token`` re-derives from the row's own ``(tenant, client_key,
      mailbox)`` — a tampered row naming an arbitrary token would point
      the poller at another scope's marker families;
    - the ``mail_scope_binding:v{n}`` dedup marker exists and points
      back at this exact row — the only path that can mint the pair is
      ``_bind`` (``mail_scope_bound`` is deliberately absent from the
      public ``EVENT_TYPES``, so the generic audit endpoint cannot
      forge either half).
    """
    if (
        not isinstance(details["version"], int)
        or isinstance(details["version"], bool)
        or not isinstance(details["token"], str)
        or not isinstance(details["tenant"], str)
        or not isinstance(details["client_key"], str)
        or not isinstance(details["mailbox"], str)
    ):
        return False
    if details["token"] != scope_token(
        details["tenant"], details["client_key"], details["mailbox"]
    ):
        return False
    try:
        marker = logger_.dedup_lookup(
            f"{BINDING_PREFIX}v{details['version']}"
        )
    except Exception:
        raise
    return bool(
        marker is not None
        and marker.get("journal_row_present")
        and marker.get("audit_row_id") == row.id
    )


def bound_binding(logger_: Any) -> dict[str, Any] | None:
    """The authoritative binding row's details, or None when unbound.

    The NEWEST well-formed ``mail_scope_bound`` row is the journal's
    declared binding. It must verify (field types, re-derived token,
    dedup marker back-reference); a forged or torn row returns
    ``{"_malformed": True}`` and every caller refuses — silently falling
    back to an older verified binding would downgrade the journal's
    declared scope without an operator act. Rows that lack the required
    keys entirely are skipped as noise; when binding-shaped rows exist
    but none is well-formed, the journal is still ``_malformed``."""
    try:
        rows = _all_binding_rows(logger_)
    except Exception:
        raise
    if not rows:
        return None
    ignored = 0
    for row in sorted(rows, key=lambda r: r.id, reverse=True):
        details = dict(row.details or {})
        required = {"version", "token", "tenant", "client_key", "mailbox"}
        if not required.issubset(details):
            ignored += 1
            continue
        if _verify_binding_row(logger_, row, details):
            if ignored:
                logger.warning(
                    "mail scope: %d unverified mail_scope_bound row(s) "
                    "ignored — they lack the binding dedup marker",
                    ignored,
                )
            return details
        logger.warning(
            "mail scope: newest mail_scope_bound row (id=%s) does not "
            "verify — forged or torn binding; refusing to guess a scope",
            row.id,
        )
        return {"_malformed": True}
    if ignored:
        logger.warning(
            "mail scope: %d mail_scope_bound row(s) present but none "
            "is well-formed — refusing to guess a scope", ignored,
        )
        return {"_malformed": True}
    return None


def has_legacy_markers(logger_: Any, message_id: str | None = None) -> bool:
    """Any pre-scope (unscoped) mail marker — journal-wide, or for one
    message when ``message_id`` is given."""
    if message_id is None:
        return any(
            logger_.count_dedup_prefix(prefix) for prefix in LEGACY_PREFIXES
        )
    return (
        logger_.dedup_lookup(f"email_processed:{message_id}") is not None
        or logger_.count_dedup_prefix(f"email_attempt:{message_id}:") > 0
        or logger_.count_dedup_prefix(
            f"email_attempt_result:{message_id}:"
        ) > 0
        or logger_.count_dedup_prefix(f"email_fetch_fail:{message_id}:") > 0
    )


def journal_has_prior_evidence(logger_: Any) -> bool:
    """Any mail-processing evidence at all in an UNBOUND journal:
    legacy markers, scoped markers whose binding rows were lost, or an
    orphaned ``mail_scope_binding:`` marker. A first-use bind on such a
    journal would silently adopt a foreign history — refuse instead."""
    return (
        has_legacy_markers(logger_)
        or any(
            logger_.count_dedup_prefix(prefix) for prefix in SCOPED_PREFIXES
        )
        or logger_.count_dedup_prefix(BINDING_PREFIX) > 0
    )


def _bind(
    logger_: Any,
    *,
    tenant: str,
    client_key: str,
    mailbox: str,
    version: int,
) -> tuple[MailScope | None, str]:
    """Commit binding version ``version`` and prove it is ours.

    Same claim discipline as the attempt brackets: a racing binder on the
    same version key either emits identical content (dedup returns the
    shared row — token match) or conflicts (log() returns None and the
    marker points at the winner's row — token mismatch → refuse)."""
    token = scope_token(tenant, client_key, mailbox)
    dedup_key = f"{BINDING_PREFIX}v{version}"
    logger_.log(
        BINDING_EVENT,
        f"Mail processing scope bound (v{version}): "
        f"{client_key} mailbox={mailbox}",
        actor="email-scope",
        details={
            "version": version,
            "token": token,
            "tenant": tenant,
            "client_key": client_key,
            "mailbox": mailbox,
        },
        dedup_key=dedup_key,
    )
    try:
        marker = logger_.dedup_lookup(dedup_key)
        if marker is None or not marker.get("journal_row_present"):
            return None, "binding_not_durable"
        row = logger_.get(marker["audit_row_id"])
    except Exception:
        return None, "journal_unreadable"
    if not row or (row.details or {}).get("token") != token:
        # The version slot is held by a different binding — a racing or
        # divergent binder won; never run on someone else's scope.
        return None, "binding_conflict"
    return (
        MailScope(
            token=token,
            tenant=tenant,
            client_key=client_key,
            mailbox=mailbox,
            version=version,
        ),
        "",
    )


def resolve(
    logger_: Any,
    *,
    tenant: str,
    client_slug: str | None,
    mailbox: str,
    attested_mailbox: str,
    consume_attestation: Callable[[], bool] | None = None,
) -> tuple[MailScope | None, str]:
    """Resolve the current processing scope, binding when unambiguous.

    ``attested_mailbox`` is the operator-side assertion (BO setting
    ``bo.mail.scope.bound_mailbox``) that the configured mailbox is the
    rightful one for this journal — required before adopting a journal
    with prior evidence or bridging a mailbox/client change. The
    attestation is single-shot: ``consume_attestation()`` must clear the
    setting BEFORE the authorized bind commits, so a forgotten value can
    never silently adopt a future journal; a consume failure refuses.
    Returns the scope, or ``(None, reason)`` — the caller must refuse
    the work, not degrade.
    """
    client_slug = client_slug or None
    if not mailbox:
        # An unconfigured mailbox can never carry a scope — including the
        # degenerate attest-empty-to-empty corner.
        return None, "mailbox_unconfigured"

    def _attested() -> bool:
        """True when attestation authorizes the pending bind: the value
        names the configured mailbox AND was consumed (single-shot)."""
        if not attested_mailbox or attested_mailbox != mailbox:
            return False
        if consume_attestation is None:
            return False
        try:
            return bool(consume_attestation())
        except Exception:
            return False

    try:
        bound = bound_binding(logger_)
    except Exception:
        return None, "journal_unreadable"

    if bound is None:
        try:
            prior = journal_has_prior_evidence(logger_)
        except Exception:
            return None, "journal_unreadable"
        if prior:
            if not attested_mailbox or attested_mailbox != mailbox:
                return None, "legacy_journal_unbound"
            if not _attested():
                return None, "attestation_not_consumed"
        client_key = (
            f"slot:{client_slug}"
            if client_slug
            else f"install:{uuid.uuid4().hex}"
        )
        return _bind(
            logger_,
            tenant=tenant,
            client_key=client_key,
            mailbox=mailbox,
            version=1,
        )

    if bound.get("_malformed"):
        return None, "binding_malformed"
    if bound["tenant"] != tenant:
        # A journal bound to another installation is never adopted —
        # attestations only cover mailbox/client within THIS tenant.
        return None, "tenant_mismatch"

    if client_slug:
        want_client = f"slot:{client_slug}"
    elif bound["client_key"].startswith("install:"):
        # Slot-less install keeps its generated identity from the binding.
        want_client = bound["client_key"]
    else:
        want_client = None  # bound to a slot but running unslotted

    if want_client == bound["client_key"] and bound["mailbox"] == mailbox:
        return (
            MailScope(
                token=bound["token"],
                tenant=tenant,
                client_key=bound["client_key"],
                mailbox=mailbox,
                version=bound["version"],
            ),
            "",
        )

    if _attested():
        if want_client is None:
            want_client = f"install:{uuid.uuid4().hex}"
        return _bind(
            logger_,
            tenant=tenant,
            client_key=want_client,
            mailbox=mailbox,
            version=bound["version"] + 1,
        )
    if attested_mailbox and attested_mailbox == mailbox:
        return None, "attestation_not_consumed"
    return None, "scope_mismatch"


def still_current(
    logger_: Any,
    scope: MailScope,
    *,
    tenant: str,
    client_slug: str | None,
    mailbox: str,
) -> bool:
    """Re-validate a captured scope against the LIVE inputs and binding.

    Read-only on purpose: mid-operation checks must not mint bindings.
    False on any drift — sentinel/slot switch, config change, or a journal
    swap (the bound token read back no longer equals the captured one) —
    so effects and mark-read never land in a different context than the
    one that claimed the work."""
    try:
        if tenant != scope.tenant or mailbox != scope.mailbox:
            return False
        if client_slug is not None:
            if scope.client_key != f"slot:{client_slug}":
                return False
        elif not scope.client_key.startswith("install:"):
            return False
        bound = bound_binding(logger_)
        return (
            bound is not None
            and not bound.get("_malformed")
            and bound.get("token") == scope.token
        )
    except Exception:
        return False


def rebind_for_client(
    logger_: Any, *, tenant: str, client_slug: str
) -> bool:
    """Re-point an EXISTING journal binding to a just-activated slot.

    Called by the slot-restore path: the activation itself is the
    operator's attestation that this journal now belongs to
    ``client_slug``. Deliberately narrow — it never binds an unbound
    journal (first-use and legacy rules belong to the poller), never
    crosses a tenant, and never rewrites the recorded mailbox (a mailbox
    mismatch stays refused until the admin attestation)."""
    try:
        bound = bound_binding(logger_)
    except Exception:
        return False
    if bound is None or bound.get("_malformed"):
        return False
    if bound["tenant"] != tenant:
        return False
    want = f"slot:{client_slug}"
    if bound["client_key"] == want:
        return True
    scope, _ = _bind(
        logger_,
        tenant=tenant,
        client_key=want,
        mailbox=bound["mailbox"],
        version=bound["version"] + 1,
    )
    return scope is not None
