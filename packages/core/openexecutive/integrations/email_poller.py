"""MCP-based Gmail polling loop.

Polls Gmail via the Google Workspace MCP server (OAuth). Each unread message is
passed raw to the Executive, which decides what to do: reply, fetch attachments,
create an alert, or ignore.

Actual tool names (confirmed via tools/list on the live MCP server):
  google_workspace__search_gmail_messages          → plain-text list of Message IDs + Thread IDs
  google_workspace__get_gmail_message_content      → plain-text Subject/From/--- BODY ---/--- ATTACHMENTS ---
  google_workspace__modify_gmail_message_labels    → mark as read (Complete/Extended tier)

No reply logic, no attachment logic, no alert logic lives here — all of that is the
Executive's responsibility via its tool access.
"""
from __future__ import annotations

import asyncio
import logging
import re
import uuid
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any

from openexecutive.config import get_settings
from openexecutive.integrations import mail_scope as _mail_scope

if TYPE_CHECKING:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = get_settings().email_poll_interval_seconds

# Prevents reprocessing the same message within a run (cleared on restart).
# The audit journal carries the durable evidence across restarts — see
# _PROCESSED_DEDUP_PREFIX / _ATTEMPT_EVENT below. Entries are scoped:
# "{scope.token}:{mid}" so one mailbox's consumed ids can never suppress
# another account's same-id messages (RA11-B01).
_processed_ids: set[str] = set()

_SKIP_SENDERS = ("noreply", "no-reply", "mailer-daemon", "postmaster", "do-not-reply")

# Pagination contract — verified against workspace-mcp 1.21.1
# (gmail/gmail_tools.py): `search_gmail_messages` accepts `page_token` and
# appends `📄 PAGINATION: ... page_token='<token>'` when more results exist.
# Only that exact marker is honoured; an absent or malformed hint degrades to
# the old single-page behaviour instead of inventing a parameter.
_PAGE_TOKEN_RE = re.compile(r"page_token='([^']+)")

# Journal evidence types emitted by this module.
_ATTEMPT_EVENT = "email_process_attempt"
_RESULT_EVENT = "email_attempt_result"
_FETCH_FAIL_EVENT = "email_fetch_failed"
# Scoped dedup families (RA11-B01): the '@' separator keeps every marker
# under "<family>@{scope}:{mid}" disjoint from the legacy unscoped
# families ("<family>:{mid}") mail_scope counts when deciding whether a
# journal carries pre-scope evidence.
_PROCESSED_DEDUP_PREFIX = "email_processed@"
_ATTEMPT_DEDUP_PREFIX = "email_attempt@"
_RESULT_DEDUP_PREFIX = "email_attempt_result@"
# Failed content fetches are counted durably in their own dedup family —
# never mixed into the Executive-attempt brackets (an email_attempt row
# means the turn ran, and the effect question becomes unanswerable).
_FETCH_FAIL_DEDUP_PREFIX = "email_fetch_fail@"
_SCOPE_REFUSED_DEDUP_PREFIX = "email_scope_refused@"

# Provider-side durable fence for terminal states (uncertain/failed_final):
# a real Gmail label, added to the message but never marking it read. The
# unread query excludes it (`-label:OE-Terminal`), so terminally-blocked
# mail cannot re-occupy the first pages forever and starve legitimate mail
# behind it — while staying unread and filter-visible for a human.
# Contract verified on workspace-mcp 1.21.1 (gmail/gmail_tools.py):
# `list_gmail_labels` prints `  • NAME (ID: id)` per label,
# `manage_gmail_label(action="create", name=...)` answers `ID: <id>`,
# `modify_gmail_message_labels` takes `add_label_ids`.
_TERMINAL_LABEL_NAME = "OE-Terminal"
_TERMINAL_LABEL_ID_RE = re.compile(r"ID:\s*(\S+)")
# Provider label ids are per-mailbox: keyed by scope token so a label id
# resolved under one account is never applied to another (RA11-B01).
_terminal_label_ids: dict[str, str] = {}

# Single-owner frontier inside this process: two concurrent poll_once
# calls must serialize claim+execute so the same message cannot be handed
# to the Executive twice. Cross-process ownership comes from the atomic
# attempt-claim dedup row (see _handle_email) — the lock alone is NOT the
# durable guarantee, only the fast path.
_POLL_LOCK = asyncio.Lock()


def reset_mail_caches() -> None:
    """Drop every process-local mail cache. Called by the client-slot
    restore path — the journal and context just changed under this
    process, so verdicts cached under the previous scope must not leak
    into the new one."""
    _processed_ids.clear()
    _terminal_label_ids.clear()


def _current_scope_inputs() -> tuple[str, str | None, str] | None:
    """Live (tenant, client_slug, mailbox) from config + slot sentinel.

    None when the tenant identity itself is unreadable — the caller then
    refuses rather than attributing work to an unknown install."""
    settings = get_settings()
    mailbox = (settings.exec_email_address or "").strip().lower()
    try:
        from openexecutive.bo.identity import configured_tenant

        tenant = configured_tenant()
    except Exception:
        return None
    try:
        from openexecutive.clients.slots import get_active_client

        client_slug = get_active_client(settings)
    except Exception:
        # Settings doubles / minimal environments have no client dir —
        # treat as slot-less (install-scoped identity). Sentinel READ
        # failures stay inside get_active_client (it returns None); an
        # exception here means the settings object has no slot support.
        client_slug = None
    return tenant, client_slug, mailbox


_BOUND_MAILBOX_SETTING = "bo.mail.scope.bound_mailbox"

# Scope refusals that must defer rather than journal a durable refusal:
# a transient store/journal wobble is not an authorization verdict, and a
# durable "scope_refused" row would pin a lie into the audit trail.
_TRANSIENT_SCOPE_REFUSALS = frozenset({
    "scope_inputs_unreadable",
    "journal_unreadable",
    "binding_not_durable",
    "binding_conflict",
    "attestation_not_consumed",
})


def _consume_scope_attestation(expected: str) -> bool:
    """Single-shot consumption of ``bo.mail.scope.bound_mailbox``.

    Returns True when no attestation row remains for this tenant — the
    value was absent, already cleared, or we cleared exactly ``expected``
    through the settings store (CAS + audited ``bo_setting_change``). A
    live value different from ``expected`` belongs to a fresher operator
    decision and is left alone, failing the check — the pending bind then
    refuses rather than running on stale consent."""
    try:
        import json as _json

        from openexecutive.bo.db import DB_PATH as bo_db_path
        from openexecutive.bo.db import get_conn as bo_get_conn
        from openexecutive.bo.identity import configured_tenant
        from openexecutive.bo.settings import store as settings_store

        if not bo_db_path.exists():
            return False
        tenant = configured_tenant()
        with bo_get_conn(bo_db_path) as conn:
            row = conn.execute(
                "SELECT value_json, version FROM bo_settings "
                "WHERE tenant = ? AND key = ?",
                (tenant, _BOUND_MAILBOX_SETTING),
            ).fetchone()
        if row is None:
            return True
        current = _json.loads(row["value_json"])
        if not isinstance(current, str) or not current.strip():
            return True
        if current.strip().lower() != expected:
            return False
        settings_store.set_value(
            tenant,
            _BOUND_MAILBOX_SETTING,
            "",
            expected_version=int(row["version"]),
            actor="email-scope",
        )
        return True
    except Exception:
        logger.warning(
            "mail scope attestation could not be consumed", exc_info=True
        )
        return False


def _resolve_scope(
    audit_logger: Any,
) -> tuple[_mail_scope.MailScope | None, str]:
    """The scope this process may consume mail under — or (None, reason)."""
    inputs = _current_scope_inputs()
    if inputs is None:
        return None, "scope_inputs_unreadable"
    tenant, client_slug, mailbox = inputs
    attested = str(_mail_setting(_BOUND_MAILBOX_SETTING) or "")
    attested = attested.strip().lower()

    def _consume() -> bool:
        return _consume_scope_attestation(attested)

    scope, refusal = _mail_scope.resolve(
        audit_logger,
        tenant=tenant,
        client_slug=client_slug,
        mailbox=mailbox,
        attested_mailbox=attested,
        consume_attestation=_consume,
    )
    # An attestation this resolve did not need is still a standing
    # authorization — consume it so it cannot adopt a future journal.
    # Best-effort: a clear failure leaves a warning, never a wedge.
    if (
        scope is not None
        and attested
        and not _consume_scope_attestation(attested)
    ):
        logger.warning(
            "mail scope attestation is set but could not be cleared — "
            "it remains a standing authorization; investigate the "
            "settings store"
        )
    return scope, refusal


def _scope_still_current(audit_logger: Any, scope: _mail_scope.MailScope) -> bool:
    """Re-validate a captured scope before each mutating step — a client
    switch or journal swap mid-operation must refuse the effect/mark-read
    in the new context instead of completing under stale identity."""
    inputs = _current_scope_inputs()
    if inputs is None:
        return False
    tenant, client_slug, mailbox = inputs
    return _mail_scope.still_current(
        audit_logger,
        scope,
        tenant=tenant,
        client_slug=client_slug,
        mailbox=mailbox,
    )


def _journal_scope_refusal(
    audit_logger: Any,
    message_id: str,
    scope: _mail_scope.MailScope | None,
    reason: str,
) -> str:
    """Evidence + outcome for a scope refusal: journaled once per
    (message, context), the message stays UNREAD and enumerated —
    reconciliation is a human act, not a retry."""
    from openexecutive.audit import log_event as audit_log

    ctx = scope.token if scope is not None else "unbound"
    audit_log(
        "integration_inbound",
        f"Email {message_id} refused: processing scope {reason}",
        actor="email",
        details={
            "channel": "email",
            "message_id": message_id,
            "outcome": "scope_refused",
            "reason": reason,
            "scope": ctx,
        },
        dedup_key=(
            f"{_SCOPE_REFUSED_DEDUP_PREFIX}{ctx}:{reason}:{message_id}"
        ),
    )
    logger.warning(
        "message=%s refused — mail scope %s; the message stays unread "
        "for operator reconciliation",
        message_id, reason,
    )
    return "scope_blocked"


def _marker_row_matches(
    audit_logger: Any,
    marker: dict[str, Any],
    *,
    scope: _mail_scope.MailScope,
    message_id: str,
) -> str:
    """'ok' | 'deferred' | 'mismatched' — does a committed dedup marker's
    journal row actually carry this scope+message? A mis-pointed marker
    must never stand in as this message's completion evidence."""
    try:
        row = audit_logger.get(marker["audit_row_id"])
    except Exception:
        return "deferred"
    details = (row.details or {}) if row else {}
    if (
        row is None
        or details.get("scope") != scope.token
        or details.get("message_id") != message_id
    ):
        return "mismatched"
    return "ok"


def _mail_setting(key: str) -> Any:
    """Operational bound from the BO settings registry (tenant-scoped).

    Any failure — missing bo_agents.db, unconfigured tenant, unreadable row —
    falls back to the registry default. A bound must never silently widen to
    "unlimited" because the store could not be read.
    """
    from openexecutive.bo.db import DB_PATH as bo_db_path
    from openexecutive.bo.settings import store as settings_store
    from openexecutive.bo.settings.registry import REGISTRY

    default = REGISTRY[key].default
    try:
        if not bo_db_path.exists():
            return default
        from openexecutive.bo.identity import configured_tenant

        value = settings_store.get_effective_value(configured_tenant(), key)
        return value if isinstance(value, type(default)) else default
    except Exception:
        return default


# Headers that can steer where a reply is sent. Stripped from the raw email
# before the Executive sees it. Lowercased for comparison.
_REPLY_REDIRECT_HEADERS = (
    "reply-to:",
    "resent-reply-to:",
    "mail-reply-to:",
    "mail-followup-to:",
)


def _strip_reply_to(raw: str) -> str:
    """Remove headers that could redirect a reply, plus their folded continuations.

    The Executive constructs outbound `to:` itself; if it sees a Reply-To-style
    header it may honor it instead of the From address. The egress gate in
    MCPGateway is the real enforcement, but stripping here removes the attack
    surface entirely so the Executive never has to choose.

    Stops processing at the header/body boundary (the first blank line) so
    body text that happens to contain `Reply-To: ...` is left alone.
    """
    out: list[str] = []
    in_drop = False
    in_body = False
    for line in raw.splitlines(keepends=True):
        if in_body:
            out.append(line)
            continue
        # Header/body boundary: a line that's only CR/LF.
        if line in ("\n", "\r\n", "\r"):
            in_body = True
            in_drop = False
            out.append(line)
            continue
        # Folded continuation of the previous header.
        if line and line[0] in (" ", "\t"):
            if in_drop:
                continue
            out.append(line)
            continue
        # Start of a new header.
        lower = line.lower()
        if any(lower.startswith(h) for h in _REPLY_REDIRECT_HEADERS):
            in_drop = True
            continue
        in_drop = False
        out.append(line)
    return "".join(out)


_RECIPIENT_HEADERS = ("to:", "cc:")
# Strips both `Name <addr@example.com>` and bare `addr@example.com` forms.
# Permissive on the local-part / domain — we only need to identify
# candidates that find_person_by_email then looks up exactly.
_EMAIL_RE = re.compile(r"[\w.+\-]+@[\w.\-]+\.[A-Za-z]{2,}")


def _parse_recipients(raw: str) -> list[str]:
    """Return distinct lowercase email addresses from the raw email's To+Cc headers.

    Mirrors :func:`_strip_reply_to`'s header walker: iterates lines
    until the first blank line (header/body boundary) and honours
    folded-header continuations (leading whitespace). Returns at most
    one entry per address, lowercased for downstream case-insensitive
    lookup via :func:`openexecutive.people.store.find_person_by_email`.
    """
    found: list[str] = []
    seen: set[str] = set()
    capturing_value = ""

    def _flush_value() -> None:
        nonlocal capturing_value
        if not capturing_value:
            return
        for addr in _EMAIL_RE.findall(capturing_value):
            low = addr.lower()
            if low not in seen:
                seen.add(low)
                found.append(low)
        capturing_value = ""

    in_recipient = False
    for line in raw.splitlines():
        # Header/body boundary.
        if not line:
            _flush_value()
            break
        # Folded continuation: appended to the current header value.
        if line[0] in (" ", "\t"):
            if in_recipient:
                capturing_value += " " + line.strip()
            continue
        # New header line — flush whatever we were collecting.
        _flush_value()
        lower = line.lower()
        in_recipient = any(lower.startswith(h) for h in _RECIPIENT_HEADERS)
        if in_recipient:
            # Strip "To:" / "Cc:" prefix; keep the rest as raw value.
            capturing_value = line.split(":", 1)[1] if ":" in line else ""
    # Body never seen (no blank line) — flush trailing header value.
    _flush_value()
    return found


def _parse_search_results(raw: str) -> list[dict[str, str]]:
    """Parse plain-text search_gmail_messages response into [{message_id, thread_id}].

    Confirmed response format (MCP server v3.3.1):
      Message ID: 19e3280dac59147f
      Thread ID:  19e3280c8d101120
    """
    messages = []
    msg_ids = re.findall(r"Message ID:\s*(\S+)", raw)
    thread_ids = re.findall(r"Thread ID:\s*(\S+)", raw)
    for mid, tid in zip(msg_ids, thread_ids, strict=False):
        messages.append({"message_id": mid, "thread_id": tid})
    for mid in msg_ids[len(messages):]:
        messages.append({"message_id": mid, "thread_id": ""})
    return messages


def _next_page_token(raw: str) -> str | None:
    """Extract the continuation token from a search response, or None.

    Only the workspace-mcp pagination hint is honoured; anything else is
    ignored rather than guessed at (the upstream contract is the
    `page_token='<token>'` marker in the 📄 PAGINATION line).
    """
    m = _PAGE_TOKEN_RE.search(raw)
    return m.group(1) if m else None


async def _collect_unread(
    gateway: MCPGateway, user_email: str, max_pages: int
) -> list[dict[str, str]]:
    """Walk the unread inbox pages and return every distinct message ref.

    The unread set is enumerated BEFORE any message is processed, so marking
    read mid-cycle cannot shift page boundaries under the iteration. Pages
    are bounded by ``bo.mail.poll.max_pages_per_cycle`` — a cap reached while
    a token remains is logged (drainage resumes next cycle), never silent.
    Duplicate message ids across pages (provider may re-emit rows) are
    de-duplicated here.
    """
    messages: list[dict[str, str]] = []
    seen: set[str] = set()
    token: str | None = None
    pending_token = False
    for page in range(max(1, max_pages)):
        arguments: dict[str, Any] = {
            # `-label:` excludes durably-fenced terminal mail so it cannot
            # re-occupy the leading pages; a nonexistent label name simply
            # matches nothing, so the query is safe before the first fence.
            "query": f"is:unread in:inbox -label:{_TERMINAL_LABEL_NAME}",
            "user_google_email": user_email,
            "page_size": 10,
        }
        if token:
            arguments["page_token"] = token
        try:
            raw = await gateway.call_tool({
                "name": "google_workspace__search_gmail_messages",
                "arguments": arguments,
            })
        except Exception:
            logger.exception("search_gmail_messages failed (page %d)", page + 1)
            return messages
        if not raw or not raw.strip():
            return messages
        for msg in _parse_search_results(raw):
            mid = msg["message_id"]
            if mid and mid not in seen:
                seen.add(mid)
                messages.append(msg)
        token = _next_page_token(raw)
        if not token:
            return messages
        pending_token = True
    if pending_token:
        logger.warning(
            "email poller: page budget (%d pages) exhausted with more unread "
            "mail pending — the remainder is picked up on the next cycle",
            max_pages,
        )
    return messages


async def _resolve_terminal_label_id(
    gateway: MCPGateway, user_email: str, scope: _mail_scope.MailScope
) -> str | None:
    """Resolve the provider id of the terminal-fence label, creating it on
    first use. Returns None when the provider refuses — the caller then
    leaves the message enumerated (degraded, but never mislabeled and
    never silently marked read). Cached per scope: label ids only exist
    inside their own mailbox."""
    cached = _terminal_label_ids.get(scope.token)
    if cached:
        return cached
    try:
        raw = await gateway.call_tool({
            "name": "google_workspace__list_gmail_labels",
            "arguments": {"user_google_email": user_email},
        }) or ""
    except Exception:
        logger.warning("terminal label: list_gmail_labels failed")
        return None
    for name, lid in re.findall(
        r"[•\-]?\s*([^\n(]+?)\s*\(ID:\s*([^)]+)\)", raw
    ):
        if name.strip() == _TERMINAL_LABEL_NAME:
            _terminal_label_ids[scope.token] = lid.strip()
            return _terminal_label_ids[scope.token]
    try:
        created = await gateway.call_tool({
            "name": "google_workspace__manage_gmail_label",
            "arguments": {
                "user_google_email": user_email,
                "action": "create",
                "name": _TERMINAL_LABEL_NAME,
                "message_list_visibility": "show",
            },
        }) or ""
    except Exception:
        logger.warning("terminal label: manage_gmail_label create failed")
        return None
    m = _TERMINAL_LABEL_ID_RE.search(created)
    if m:
        _terminal_label_ids[scope.token] = m.group(1)
    return _terminal_label_ids.get(scope.token)


async def _label_terminal(
    gateway: MCPGateway,
    message_id: str,
    user_email: str,
    scope: _mail_scope.MailScope,
    audit_logger: Any,
) -> None:
    """Fence a terminally-blocked message provider-side: add the
    OE-Terminal label WITHOUT touching UNREAD — the mail stays visibly
    unread for a human, but the unread query excludes it so it cannot
    starve the pages ahead of legitimate mail. Removing the label in
    Gmail re-enumerates it; nothing here pretends the mail was handled.
    Refuses when the scope drifted mid-operation or a restore block
    started — the fence would land on a different context than the one
    that evaluated the message."""
    from openexecutive.clients.slots import is_restore_blocked

    if is_restore_blocked() or not _scope_still_current(
        audit_logger, scope
    ):
        logger.warning(
            "message=%s: scope changed mid-operation — refusing the "
            "terminal fence in the new context", message_id,
        )
        return
    label_id = await _resolve_terminal_label_id(gateway, user_email, scope)
    # The label lookup crosses the network — re-check before mutating.
    if not _scope_still_current(audit_logger, scope):
        logger.warning(
            "message=%s: scope changed while resolving the fence label — "
            "refusing the label change in the new context", message_id,
        )
        return
    if not label_id:
        logger.warning(
            "message=%s is terminal but the fence label is unavailable — "
            "it stays enumerated and will be re-evaluated next cycle",
            message_id,
        )
        return
    try:
        await gateway.call_tool({
            "name": "google_workspace__modify_gmail_message_labels",
            "arguments": {
                "message_id": message_id,
                "user_google_email": user_email,
                "add_label_ids": [label_id],
            },
        })
        logger.info(
            "message=%s fenced with %s — excluded from unread enumeration, "
            "still unread for operator reconciliation",
            message_id, _TERMINAL_LABEL_NAME,
        )
    except Exception:
        logger.warning(
            "message=%s: failed to apply %s label — stays enumerated",
            message_id, _TERMINAL_LABEL_NAME,
        )


async def poll_once(gateway: MCPGateway) -> None:
    """One poll cycle: enumerate unread mail, hand each to the Executive."""
    from openexecutive.clients.slots import is_restore_blocked

    # Socket-side channel like Slack/Discord: never traverses the HTTP gate.
    # Skipping the WHOLE poll (before _mark_read/_processed_ids) keeps
    # inbound mail unread and retriable — otherwise every message arriving
    # during a restore block would be consumed and silently dropped.
    if is_restore_blocked():
        logger.warning(
            "email poller: instance is restore-blocked — skipping cycle; "
            "unread mail stays queued until recovery completes"
        )
        return

    settings = get_settings()
    user_email = settings.exec_email_address

    # Resolve the processing scope BEFORE enumerating: a journal that
    # cannot be attributed to this install/client/mailbox must not
    # consume, fence or mark anything (RA11-B01). The refusal is durable
    # (one journaled row per context) and the mail stays unread.
    from openexecutive.audit import get_audit_logger
    from openexecutive.audit import log_event as audit_log

    audit_logger = get_audit_logger()
    scope, scope_refusal = _resolve_scope(audit_logger)
    if scope is None:
        if scope_refusal in _TRANSIENT_SCOPE_REFUSALS:
            # Transient: defer the whole cycle without durable evidence —
            # a busy journal is not an authorization verdict.
            logger.warning(
                "email poller: cycle deferred — mail scope %s",
                scope_refusal,
            )
            return
        inputs = _current_scope_inputs()
        ctx = ""
        if inputs is not None:
            import hashlib as _hashlib

            ctx = _hashlib.sha256(
                "|".join(part or "" for part in inputs).encode("utf-8")
            ).hexdigest()[:12]
        audit_log(
            "integration_inbound",
            "Mail poll refused: processing scope "
            f"{scope_refusal}",
            actor="email",
            details={
                "channel": "email",
                "outcome": "scope_refused",
                "reason": scope_refusal,
            },
            dedup_key=f"mail_scope_refused:{ctx}:{scope_refusal}",
        )
        logger.warning(
            "email poller: cycle refused — mail scope %s; mail stays "
            "unread until the binding/attestation is fixed",
            scope_refusal,
        )
        return

    messages = await _collect_unread(
        gateway,
        user_email,
        _mail_setting("bo.mail.poll.max_pages_per_cycle"),
    )
    if not messages:
        return
    logger.debug("poll cycle — %d unread message(s)", len(messages))

    for msg in messages:
        mid = msg["message_id"]
        tid = msg.get("thread_id", "")
        if not mid or f"{scope.token}:{mid}" in _processed_ids:
            continue
        # Re-check per message: a marker can land mid-loop, and processing
        # the next mail would still consume it via _mark_read.
        if is_restore_blocked():
            logger.warning(
                "email poller: restore block landed mid-cycle — leaving "
                "remaining mail unread"
            )
            return
        try:
            # The lock serializes claim+execute inside this process: a
            # second poll_once racing the same message waits, then sees
            # the first owner's dedup marker instead of re-running the
            # Executive. Cross-process races are fenced by the atomic
            # claim row itself (RA-B2-03).
            async with _POLL_LOCK:
                outcome = await _handle_email(
                    gateway, mid, tid, user_email, scope=scope
                )
        except Exception:
            logger.exception("failed for message=%s", mid)
            outcome = "failed"
        # Only terminal outcomes enter the processed set: a failed message
        # must NOT be recorded as handled (it stays unread at the provider),
        # and a message whose mark-read call failed is re-evaluated next
        # cycle — the journal dedup marker then proves processing already
        # happened, so the Executive is never re-run for it.
        if outcome in ("processed", "skipped", "uncertain", "failed_final"):
            _processed_ids.add(f"{scope.token}:{mid}")
        if outcome in ("uncertain", "failed_final"):
            # Provider-side fence so the terminal message stops occupying
            # the leading unread pages. It stays unread — never a false
            # success — and a human reconciles by removing the label.
            await _label_terminal(gateway, mid, user_email, scope, audit_logger)


def _query_all(
    audit_logger: Any, **filters: Any
) -> list[Any]:
    """Complete filtered scan — pages internally so the caller NEVER
    reasons from a truncated 1000-row window (RA-B2-02). A query error
    propagates: unreadable evidence is treated as unknown, not absent."""
    out: list[Any] = []
    offset = 0
    while True:
        page = audit_logger.query(limit=1000, offset=offset, **filters)
        out.extend(page)
        if len(page) < 1000:
            return out
        offset += 1000


def _attempts_state(
    audit_logger: Any,
    message_id: str,
    max_attempts: int,
    scope: _mail_scope.MailScope,
) -> tuple[str, int, str]:
    """Evaluate this message's durable attempt lifecycle.

    Returns ``(state, next_attempt_no, reason)`` where state is:

    ``"clean"``     — no Executive attempt exists yet and the durable
                      fetch-failure budget is not spent. Only then is a
                      first attempt legitimate.
    ``"uncertain"`` — an Executive attempt row exists: open-without-close
                      (interrupted mid-turn), orphaned/malformed evidence,
                      ``executive_failed`` (a missing tool_invocation row
                      can NEVER prove no effect landed — the journal
                      write is post-dispatch and best-effort), or
                      ``executed`` without its processed marker. NEVER
                      retried automatically — a human reconciles.
    ``"exhausted"`` — the durable failure budget is spent.
    ``"failed"``    — the journal could not be read; nothing may be
                      inferred, so the message simply stays unread.

    Attempt identity is message-and-scope-scoped and fully durable: the
    claim key is ``email_attempt@{scope}:{mid}:{n}`` with ``n`` derived
    ONLY from the journal's attempt-marker count — never from volatile
    in-process state — so two processes with divergent histories converge
    on the same key and BEGIN IMMEDIATE serializes them (RA-B2-06). The
    scope component keeps a second mailbox's same-id message out of this
    history entirely (RA11-B01). No session-wide row-range inference is
    used anywhere: a shared session_id cannot misattribute one message's
    rows to another (RA-B2-07), and a NULL-session tool row can never
    become invisible evidence (RA-B4-02).
    """
    try:
        attempt_count = audit_logger.count_dedup_prefix(
            f"{_ATTEMPT_DEDUP_PREFIX}{scope.token}:{message_id}:"
        )
    except Exception:
        logger.warning(
            "journal unreadable counting attempts for message=%s — "
            "deferring (unknown is not absent)", message_id,
        )
        return "failed", 0, "journal_unreadable"

    if attempt_count == 0:
        # No Executive turn ever started for this message — the only
        # durable failures to account for are content-fetch failures.
        try:
            fetch_fails = audit_logger.count_dedup_prefix(
                f"{_FETCH_FAIL_DEDUP_PREFIX}{scope.token}:{message_id}:"
            )
        except Exception:
            logger.warning(
                "journal unreadable counting fetch failures for "
                "message=%s — deferring", message_id,
            )
            return "failed", 0, "journal_unreadable"
        if fetch_fails >= max_attempts:
            return "exhausted", fetch_fails + 1, "fetch_attempts_exhausted"
        return "clean", 1, ""

    # Any Executive attempt row means a turn ran for this message. Every
    # completion state below is terminal-uncertain: whether an external
    # effect landed is unprovable from journal contents alone, because a
    # tool_invocation row can be silently absent (best-effort write after
    # the call returned). We still read the rows to report a precise
    # operator-facing reason.
    try:
        attempts = sorted(
            (
                e
                for e in _query_all(
                    audit_logger,
                    event_type=_ATTEMPT_EVENT,
                    details_substr=f'"message_id": "{message_id}"',
                )
                # Only this scope's rows — other mailboxes' same-id history
                # must not enter this lifecycle (and legacy rows carry no
                # scope at all).
                if (e.details or {}).get("scope") == scope.token
            ),
            key=lambda e: e.id,
        )
    except Exception:
        logger.warning(
            "journal unreadable reading attempts for message=%s — "
            "deferring (unknown is not absent)", message_id,
        )
        return "failed", 0, "journal_unreadable"

    if len(attempts) != attempt_count:
        # A claim marker exists whose attempt row the journal lost —
        # an attempt ran whose evidence cannot be located → unknown.
        return "uncertain", attempt_count + 1, "attempt_evidence_lost"

    for attempt in attempts:
        try:
            n = int(attempt.details.get("attempt") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return "uncertain", attempt_count + 1, "malformed_attempt_row"
        try:
            close = audit_logger.dedup_lookup(
                f"{_RESULT_DEDUP_PREFIX}{scope.token}:{message_id}:{n}"
            )
        except Exception:
            logger.warning(
                "journal unreadable reading attempt result for "
                "message=%s attempt=%d — deferring", message_id, n,
            )
            return "failed", 0, "journal_unreadable"
        if close is None or not close.get("journal_row_present"):
            # Open-without-close (interrupted mid-turn, dead owner) or an
            # orphaned close marker — an effect may have landed and no
            # lease/expiry may downgrade that to retryable.
            return "uncertain", attempt_count + 1, "open_or_orphaned_attempt"
        try:
            close_row = audit_logger.get(close["audit_row_id"])
        except Exception:
            close_row = None
        if close_row is None:
            # Marker points at a row the journal can no longer return —
            # incomplete evidence is unknown, never retryable.
            return "uncertain", attempt_count + 1, "close_row_lost"
        result = (close_row.details or {}).get("result")
        if result == "executed":
            # Executive completed but the processed marker did not
            # survive/land — replaying would duplicate its effects.
            return "uncertain", attempt_count + 1, "executed_without_marker"
        if result == "executive_failed":
            # The turn started and failed; absent tool rows are NOT proof
            # of zero effects (RA-B2-05). Auto-retry ends here.
            return "uncertain", attempt_count + 1, "executive_failed_unproven"
        return "uncertain", attempt_count + 1, "unknown_attempt_result"

    # Unreachable — every attempt outcome maps to a state above — but
    # never fall through to clean on inconsistent evidence.
    return "uncertain", attempt_count + 1, "inconsistent_attempt_state"


def _claim_attempt(
    audit_logger: Any,
    message_id: str,
    session_id: str,
    attempt_no: int,
    owner: str,
    scope: _mail_scope.MailScope,
) -> str:
    """Atomically claim attempt ``attempt_no`` for ``message_id``.

    The claim row is written with a per-owner nonce in ``details`` so its
    dedup fingerprint is unique per claimant — two racing owners on the
    same dedup_key can never both think they won: the loser's conflicting
    fingerprint makes ``log`` return None, and only the winner's row
    carries the winner's nonce. Returns "claimed", "lost" (another owner
    holds it), or "journal_error" (nothing durable → nobody may run).
    """
    dedup_key = f"{_ATTEMPT_DEDUP_PREFIX}{scope.token}:{message_id}:{attempt_no}"
    row_id = audit_logger.log(
        _ATTEMPT_EVENT,
        f"Processing attempt {attempt_no} for email {message_id}",
        actor="email",
        session_id=session_id,
        details={
            "message_id": message_id,
            "attempt": attempt_no,
            "owner": owner,
            "scope": scope.token,
            "mailbox": scope.mailbox,
        },
        dedup_key=dedup_key,
    )
    try:
        marker = audit_logger.dedup_lookup(dedup_key)
    except Exception:
        return "journal_error"
    if marker is None:
        # The claim row did not commit durably — running now would leave
        # an unattributed external effect on a crash.
        return "journal_error"
    if row_id is None or not marker.get("journal_row_present"):
        return "lost"
    row = audit_logger.get(marker["audit_row_id"])
    if not row or (row.details or {}).get("owner") != owner:
        return "lost"
    return "claimed"


def _close_attempt(
    audit_logger: Any,
    message_id: str,
    session_id: str,
    attempt_no: int,
    owner: str,
    result: str,
    scope: _mail_scope.MailScope,
) -> bool:
    """Write the attempt's close row (``executed``/``executive_failed``)
    and prove it committed durably under our owner nonce.

    Returns True only when the dedup marker exists, its journal row is
    live, and the row is ours. ``log()`` returns None on failure instead
    of raising — a swallowed write must never be read as "nothing
    happened": the caller maps an unproven close to ``uncertain``, and an
    orphaned open attempt also fails closed as ``uncertain`` next cycle.

    Scope-gated like every other mutating step: a journal swap mid-turn
    must not land a stale-token close row in the incoming context — the
    open attempt in the ORIGINAL journal already reads as uncertain,
    which is the honest state."""
    if not _scope_still_current(audit_logger, scope):
        return False
    dedup_key = f"{_RESULT_DEDUP_PREFIX}{scope.token}:{message_id}:{attempt_no}"
    audit_logger.log(
        _RESULT_EVENT,
        f"Attempt {attempt_no} for email {message_id}: {result}",
        actor="email",
        session_id=session_id,
        details={
            "message_id": message_id,
            "attempt": attempt_no,
            "owner": owner,
            "result": result,
            "scope": scope.token,
            "mailbox": scope.mailbox,
        },
        dedup_key=dedup_key,
    )
    try:
        marker = audit_logger.dedup_lookup(dedup_key)
    except Exception:
        return False
    if marker is None or not marker.get("journal_row_present"):
        return False
    try:
        row = audit_logger.get(marker["audit_row_id"])
    except Exception:
        return False
    return bool(row and (row.details or {}).get("owner") == owner)


def _record_fetch_failure(
    audit_logger: Any,
    message_id: str,
    session_id: str,
    max_attempts: int,
    scope: _mail_scope.MailScope,
) -> str:
    """Account durably for a failed/empty content fetch. Returns the
    outcome to report: ``failed`` (retriable), ``failed_final`` (budget
    spent — fenced for a human), or ``uncertain`` when an Executive
    attempt already exists (its lifecycle governs; a transient fetch
    failure must not restart or re-label that history)."""
    if not _scope_still_current(audit_logger, scope):
        logger.warning(
            "message=%s: scope changed mid-operation — fetch failure not "
            "accounted in the new context", message_id,
        )
        return "failed"
    try:
        if audit_logger.count_dedup_prefix(
            f"{_ATTEMPT_DEDUP_PREFIX}{scope.token}:{message_id}:"
        ) > 0:
            return "uncertain"
        prior = audit_logger.count_dedup_prefix(
            f"{_FETCH_FAIL_DEDUP_PREFIX}{scope.token}:{message_id}:"
        )
    except Exception:
        logger.warning(
            "journal unreadable recording fetch failure for message=%s — "
            "deferring (unknown is not absent)", message_id,
        )
        return "failed"
    dedup_key = f"{_FETCH_FAIL_DEDUP_PREFIX}{scope.token}:{message_id}:{prior + 1}"
    audit_logger.log(
        _FETCH_FAIL_EVENT,
        f"Content fetch failed for email {message_id} "
        f"(failure {prior + 1})",
        actor="email",
        session_id=session_id,
        details={
            "message_id": message_id,
            "fetch_failure": prior + 1,
            "owner": uuid.uuid4().hex,
            "scope": scope.token,
            "mailbox": scope.mailbox,
        },
        dedup_key=dedup_key,
    )
    try:
        marker = audit_logger.dedup_lookup(dedup_key)
        committed = marker is not None and marker.get("journal_row_present")
    except Exception:
        committed = False
    total = prior + 1 if committed else prior
    if total >= max_attempts:
        from openexecutive.audit import log_event as audit_log

        audit_log(
            "integration_inbound",
            f"Email fetch gave up for message {message_id} after "
            f"{total} failed fetch attempt(s)",
            actor="email",
            session_id=session_id,
            details={
                "channel": "email",
                "message_id": message_id,
                "outcome": "failed_final",
                "reason": "fetch_attempts_exhausted",
                "attempts": total,
            },
        )
        logger.warning(
            "message=%s exceeded the fetch attempt budget (%d) — left "
            "unread for human review", message_id, max_attempts,
        )
        return "failed_final"
    return "failed"


async def _consume_skipped(
    gateway: MCPGateway,
    message_id: str,
    user_email: str,
    session_id: str,
    from_addr: str,
    reason: str,
    scope: _mail_scope.MailScope,
) -> str:
    """Deliberate policy skip — evaluated, evidenced, marked read.

    Skipped mail is CONSUMED: leaving it unread would re-occupy every
    ``is:unread`` page on every cycle (and after every restart), which is
    exactly the starvation the single-page traversal had. The
    ``email_processed@{scope}:`` dedup marker is written for the same
    reason — the skip decision is terminal for THIS processing context
    only (another account's same-id mail decides independently).
    """
    from openexecutive.audit import get_audit_logger

    audit_logger = get_audit_logger()
    if not _scope_still_current(audit_logger, scope):
        logger.warning(
            "message=%s: scope changed mid-operation — skip evidence not "
            "written, message left unread", message_id,
        )
        return "failed"
    dedup_key = f"{_PROCESSED_DEDUP_PREFIX}{scope.token}:{message_id}"
    audit_logger.log(
        "integration_inbound",
        f"Skipped inbound email {message_id} ({reason})",
        actor="email",
        session_id=session_id,
        details={
            "channel": "email",
            "message_id": message_id,
            "from": from_addr,
            "outcome": reason,
            "scope": scope.token,
            "mailbox": scope.mailbox,
        },
        dedup_key=dedup_key,
    )
    # Consume ONLY if the marker is provably committed — marking read
    # without durable evidence would make a skipped mail vanish with no
    # trace (a swallowed log() write must never become "nothing happened").
    try:
        marker = audit_logger.dedup_lookup(dedup_key)
        marker_committed = bool(
            marker is not None and marker.get("journal_row_present")
        )
    except Exception:
        marker_committed = False
    if not marker_committed or marker is None:
        logger.warning(
            "skip evidence for message=%s not durable — deferring instead "
            "of consuming without a trace", message_id,
        )
        return "failed"
    row_state = _marker_row_matches(
        audit_logger, marker, scope=scope, message_id=message_id
    )
    if row_state != "ok":
        logger.warning(
            "skip marker for message=%s does not resolve to this "
            "message's row (%s) — deferring", message_id, row_state,
        )
        return "failed" if row_state == "deferred" else "uncertain"
    return (
        "skipped"
        if await _mark_read(gateway, message_id, user_email, scope, audit_logger)
        else "mark_read_failed"
    )


async def _handle_email(
    gateway: MCPGateway,
    message_id: str,
    thread_id: str,
    user_email: str,
    scope: _mail_scope.MailScope | None = None,
) -> str:
    """Process one unread message; returns an explicit outcome:

    ``processed``        Executive ran; marked read.
    ``skipped``          Policy skip (self/automated); marked read.
    ``mark_read_failed`` Executive ran (or journal already proves it did)
                         but the provider refused the label change — the
                         dedup marker prevents a duplicate run next cycle.
    ``failed``           Transient failure BEFORE any Executive turn —
                         fetch glitch or journal unreadable — retriable
                         within the bounded durable budget.
    ``uncertain``        An Executive attempt exists (open, closed
                         failed, executed-without-marker) or required
                         evidence could not be committed — never re-run
                         automatically; fenced with the provider-side
                         OE-Terminal label for a human to reconcile.
    ``failed_final``     Durable failure budget exhausted — stays
                         unread, fenced.
    ``claimed``          A concurrent owner holds the attempt claim —
                         not ours to run; re-evaluated next cycle.
    ``scope_blocked``    The journal cannot be attributed to the current
                         install/client/mailbox (RA11-B01), or legacy
                         unscoped markers exist for this message — the
                         mail stays unread and enumerated for operator
                         reconciliation. Never auto-retried into the
                         wrong context.
    """
    from openexecutive.audit import get_audit_logger
    from openexecutive.audit import log_event as audit_log

    audit_logger = get_audit_logger()

    # Scope BEFORE any consume: the dedup/counter keys are only meaningful
    # under a bound processing identity. An unresolvable scope (foreign
    # journal, legacy evidence without attestation, tenant mismatch) is a
    # refusal — never a degraded guess. An UNREADABLE journal/identity is
    # merely deferred: the message stays unread and retriable like any
    # other transient journal outage.
    if scope is None:
        scope, scope_refusal = _resolve_scope(audit_logger)
        if scope is None:
            if scope_refusal in _TRANSIENT_SCOPE_REFUSALS:
                logger.warning(
                    "message=%s deferred — mail scope unreadable (%s); "
                    "unknown is not absent",
                    message_id, scope_refusal,
                )
                return "failed"
            return _journal_scope_refusal(
                audit_logger, message_id, None, scope_refusal
            )

    # Legacy (pre-scope) markers for this message cannot be attributed to
    # any scope — the journal never recorded which account produced them.
    # Block consume+mark-read; the message stays unread for a human.
    try:
        legacy = _mail_scope.has_legacy_markers(audit_logger, message_id)
    except Exception:
        logger.warning(
            "journal unreadable checking legacy markers for message=%s — "
            "deferring (unknown is not absent)", message_id,
        )
        return "failed"
    if legacy:
        return _journal_scope_refusal(
            audit_logger, message_id, scope, "legacy_marker_ambiguous"
        )

    # Durable dedup BEFORE any provider fetch: a committed marker proves the
    # Executive already completed this message (processed or policy-skipped)
    # — possibly in a previous process lifetime. Only the provider-side
    # mark-read can still be outstanding. A journal read failure is NOT
    # absence of evidence (see dedup_lookup's contract): refusing to guess
    # keeps a maybe-sent mail from being re-run blindly.
    dedup_key = f"{_PROCESSED_DEDUP_PREFIX}{scope.token}:{message_id}"
    try:
        marker = audit_logger.dedup_lookup(dedup_key)
    except Exception:
        logger.warning(
            "audit journal unreadable for message=%s — deferring instead of "
            "risking a duplicate run",
            message_id,
        )
        return "failed"
    if marker is not None:
        if not marker.get("journal_row_present"):
            # Orphan marker: the journal lost the evidence row it points
            # to — whether the Executive completed is unprovable. Not a
            # success and NOT a retry; left for human reconciliation.
            logger.warning(
                "orphan processed marker for message=%s — incomplete "
                "evidence, refusing to infer completion", message_id,
            )
            return "uncertain"
        row_state = _marker_row_matches(
            audit_logger, marker, scope=scope, message_id=message_id
        )
        if row_state == "deferred":
            logger.warning(
                "journal unreadable resolving processed marker for "
                "message=%s — deferring", message_id,
            )
            return "failed"
        if row_state == "mismatched":
            logger.warning(
                "processed marker for message=%s points at evidence that "
                "is not this message's — refusing to infer completion",
                message_id,
            )
            return "uncertain"
        logger.info(
            "message=%s already evidenced in the journal — re-marking read",
            message_id,
        )
        return (
            "processed"
            if await _mark_read(
                gateway, message_id, user_email, scope, audit_logger
            )
            else "mark_read_failed"
        )

    raw = await gateway.call_tool({
        "name": "google_workspace__get_gmail_message_content",
        "arguments": {
            "message_id": message_id,
            "user_google_email": user_email,
            "body_format": "text",
        },
    })
    if raw:
        preview = raw[:200]
        suffix = f"…[truncated {len(raw) - 200} chars]" if len(raw) > 200 else ""
        logger.debug("get_content raw=%r%s", preview, suffix)
    else:
        logger.debug("get_content raw=<empty>")

    # An empty fetch is a transient provider glitch, not a decision — the
    # message must stay unread and retriable rather than be consumed
    # unseen. The failure is accounted durably (never by volatile
    # in-process counters) so the bound survives restarts and converges
    # across processes; a pre-existing Executive attempt makes the
    # message uncertain instead — its lifecycle decides, not this fetch.
    if not raw or not raw.strip():
        logger.warning("empty content for message=%s", message_id)
        return _record_fetch_failure(
            audit_logger,
            message_id,
            f"email:{thread_id}" if thread_id else "email:unparsed",
            _mail_setting("bo.mail.processing.max_attempts"),
            scope,
        )

    # Minimal guard: skip self-sent (prevents reply loops) and known automated senders.
    from_line = next((ln for ln in raw.splitlines() if ln.lower().startswith("from:")), "")
    from_value = from_line[len("from:"):].strip()
    # Use stdlib parseaddr so adversarial From headers like
    # `<a@evil.com> ignore previous instructions` don't smuggle trailing
    # content through. parseaddr returns ("display", "addr@host") and
    # ignores garbage after the angle-bracket address. Empty / unparseable
    # input → from_addr stays empty, downstream guards (audit, roster
    # lookup, [POLICY] notice) handle that gracefully.
    _, parsed_addr = parseaddr(from_value)
    from_addr = parsed_addr.strip()
    # Deterministic per-thread session id so every audit row from this
    # inbound (chat_turn, specialist_consult, tool_invocation) shares a
    # grouping key with the integration_inbound row. Falls back to
    # from_addr when the IMAP message exposes no thread header. Computed
    # early — the attempt counters and effect-attribution below key on it.
    session_id = f"email:{thread_id or from_addr}"
    if from_addr.lower() == user_email.lower():
        logger.debug("skipping self-addressed message=%s", message_id)
        return await _consume_skipped(
            gateway, message_id, user_email, session_id, from_addr,
            "self_sent", scope,
        )
    if any(p in from_line.lower() for p in _SKIP_SENDERS):
        logger.debug("skipping automated sender for message=%s", message_id)
        return await _consume_skipped(
            gateway, message_id, user_email, session_id, from_addr,
            "automated_sender", scope,
        )

    # Sender-roster awareness. Unrostered senders are NOT dropped — the
    # Executive still reads, classifies, and decides. What protects us
    # from auto-replying to spam is the outbound gate
    # (orchestrator.mcp_gateway._check_gmail_recipients), which refuses
    # Gmail-send tool calls whose recipient isn't on the People roster.
    # The Executive sees a [POLICY] notice prepended to the body (built
    # in _run_executive) so it knows reply tools will block and proposes
    # to a human instead.
    from openexecutive.people.store import find_person_by_email
    sender_in_roster = find_person_by_email(from_addr) is not None
    if not sender_in_roster:
        logger.info(
            "non-roster sender=%s message=%s — routing to Executive (no auto-reply allowed)",
            from_addr, message_id,
        )
        audit_log(
            "integration_inbound",
            f"Accepted non-roster email from {from_addr} (reply blocked at outbound gate)",
            actor="email",
            details={
                "channel": "email",
                "from": from_addr,
                "message_id": message_id,
                "outcome": "accepted_non_roster",
            },
        )

    logger.info("routing message=%s to Executive", message_id)
    subject_line = next(
        (ln for ln in raw.splitlines() if ln.lower().startswith("subject:")), ""
    )
    subject = subject_line[len("subject:"):].strip()[:160] if subject_line else ""
    audit_log(
        "integration_inbound",
        f"Inbound email from {from_addr}: {subject}" if subject else f"Inbound email from {from_addr}",
        actor="email",
        session_id=session_id,
        details={
            "channel": "email",
            "message_id": message_id,
            "thread_id": thread_id,
            "from": from_addr,
            "subject": subject,
        },
    )

    # Durable per-message attempt lifecycle (open row + close row, both
    # dedup-keyed and journal-verified). Once ANY Executive attempt row
    # exists, the message is terminal-uncertain: an interrupted turn may
    # have produced an external effect, and a missing tool_invocation row
    # can never prove otherwise (the write is post-dispatch and
    # best-effort — RA-B2-05). Automatic retry is deliberately reduced to
    # the pre-Executive stages only; an operator reconciles uncertain
    # mail manually. This degradation is reported, never presented as
    # success.
    max_attempts = _mail_setting("bo.mail.processing.max_attempts")
    state, attempt_no, reason = _attempts_state(
        audit_logger, message_id, max_attempts, scope
    )
    if state == "failed":
        return "failed"
    if state == "exhausted":
        audit_log(
            "integration_inbound",
            f"Email processing gave up for message {message_id} "
            f"after {attempt_no - 1} attempt(s)",
            actor="email",
            session_id=session_id,
            details={
                "channel": "email",
                "message_id": message_id,
                "thread_id": thread_id,
                "from": from_addr,
                "outcome": "failed_final",
                "reason": reason,
                "attempts": attempt_no - 1,
            },
        )
        logger.warning(
            "message=%s exceeded the processing attempt budget (%d, %s) — left "
            "unread for human review",
            message_id, max_attempts, reason,
        )
        return "failed_final"
    if state == "uncertain":
        audit_log(
            "integration_inbound",
            f"Email processing left in uncertain state for message "
            f"{message_id} — a prior Executive attempt exists and "
            f"absence of effect cannot be proven ({reason})",
            actor="email",
            session_id=session_id,
            details={
                "channel": "email",
                "message_id": message_id,
                "thread_id": thread_id,
                "from": from_addr,
                "outcome": "uncertain_partial_effect",
                "reason": reason,
                "attempt": attempt_no,
            },
        )
        logger.warning(
            "message=%s: prior attempt evidence incomplete (%s) — "
            "re-run refused to avoid duplicate effects; left unread",
            message_id, reason,
        )
        return "uncertain"

    # Atomic claim BEFORE the Executive runs: the bracket row carries a
    # unique owner nonce, so its dedup fingerprint differs per claimant —
    # two racing owners on the same dedup_key cannot both commit. The
    # claim must be PROVABLY durable AND ours: running without it would
    # let a crash strand an external effect with no evidence to
    # attribute it, or run the same send twice across two owners.
    # Scope is re-validated first: a client switch or journal swap since
    # the entry check must refuse the effect in the new context.
    if not _scope_still_current(audit_logger, scope):
        return _journal_scope_refusal(
            audit_logger, message_id, scope, "scope_changed_pre_claim"
        )
    owner = uuid.uuid4().hex
    claim = _claim_attempt(
        audit_logger, message_id, session_id, attempt_no, owner, scope
    )
    if claim == "lost":
        logger.info(
            "message=%s attempt=%d claimed by another owner — not re-running",
            message_id, attempt_no,
        )
        # "claimed" is neither terminal nor a failure: the message is not
        # recorded locally and consumes no retry budget — next cycle the
        # winner's marker (processed) or open attempt (uncertain) decides.
        return "claimed"
    if claim != "claimed":
        logger.warning(
            "attempt bracket for message=%s attempt=%d not durable — "
            "deferring rather than risking unattributed external effects",
            message_id, attempt_no,
        )
        return "failed"
    # Every audit row emitted inside the turn — including broadcast rows
    # with session_id=NULL — carries the explicit attempt identity so a
    # human can attribute effects to this exact message/attempt/owner.
    # Restore-block re-checked right before the turn: it shrinks (cannot
    # close) the window in which a journal swap strands the attempt's
    # evidence in a rolled-back DB.
    from openexecutive.audit.context import attempt_scope
    from openexecutive.clients.slots import is_restore_blocked

    if is_restore_blocked() or not _scope_still_current(
        audit_logger, scope
    ):
        logger.warning(
            "message=%s: scope changed mid-operation — refusing to start "
            "the Executive turn in the new context", message_id,
        )
        return "failed"
    attempt_ref = f"{scope.token}:{message_id}:{attempt_no}:{owner}"
    try:
        with attempt_scope(attempt_ref):
            await _run_executive(
                gateway, _strip_reply_to(raw), message_id, thread_id,
                from_addr, session_id,
            )
    except Exception:
        logger.exception("Executive raised for message=%s", message_id)
        # The turn started and failed. An absent tool_invocation row can
        # never prove zero effects (the journal write is post-dispatch
        # and best-effort), so the honest outcome is UNKNOWN — fenced,
        # never resubmitted automatically (RA-B2-05). An uncommitted
        # close row fails closed the same way next cycle.
        closed = _close_attempt(
            audit_logger, message_id, session_id, attempt_no, owner,
            "executive_failed", scope,
        )
        audit_log(
            "integration_inbound",
            f"Executive attempt failed for message {message_id} — "
            "effect presence unprovable; fenced for operator review "
            "(auto-retry removed)",
            actor="email",
            session_id=session_id,
            details={
                "channel": "email",
                "message_id": message_id,
                "thread_id": thread_id,
                "from": from_addr,
                "outcome": "uncertain_partial_effect",
                "reason": "executive_failed_unproven",
                "attempt": attempt_no,
                "close_committed": closed,
            },
        )
        return "uncertain"

    if not _close_attempt(
        audit_logger, message_id, session_id, attempt_no, owner,
        "executed", scope,
    ):
        # The turn ran to completion but its close row is not provably
        # durable — next cycle the open attempt reads as interrupted →
        # uncertain. Report it honestly now rather than pretending.
        logger.warning(
            "executed close row for message=%s attempt=%d not durable — "
            "reporting uncertain", message_id, attempt_no,
        )
        return "uncertain"

    # The evidence row + dedup marker land atomically BEFORE the provider
    # label change: a mark-read failure (or a crash in between) leaves the
    # message unread but provably processed — the next cycle replays only
    # the mark-read, never the Executive. The marker write itself is
    # verified: a swallowed log() must not become "nothing happened".
    # Scope re-validated: the turn may have run while the context changed
    # under us — the terminal evidence/mark-read must not land in the new
    # context (the open attempt in the original journal reads as
    # uncertain, which is the honest state).
    if not _scope_still_current(audit_logger, scope):
        return _journal_scope_refusal(
            audit_logger, message_id, scope, "scope_changed_post_effect"
        )
    audit_logger.log(
        "integration_inbound",
        f"Processed email from {from_addr}: {subject}" if subject
        else f"Processed email from {from_addr}",
        actor="email",
        session_id=session_id,
        details={
            "channel": "email",
            "message_id": message_id,
            "thread_id": thread_id,
            "from": from_addr,
            "subject": subject,
            "outcome": "processed",
            "attempt": attempt_no,
            "scope": scope.token,
            "mailbox": scope.mailbox,
        },
        dedup_key=dedup_key,
    )
    try:
        marker = audit_logger.dedup_lookup(dedup_key)
        marker_committed = bool(
            marker is not None and marker.get("journal_row_present")
        )
    except Exception:
        marker_committed = False
    if not marker_committed:
        logger.warning(
            "processed marker for message=%s not durable — the executed "
            "close row will read as uncertain next cycle; not marking read",
            message_id,
        )
        return "uncertain"
    if await _mark_read(gateway, message_id, user_email, scope, audit_logger):
        return "processed"
    return "mark_read_failed"


async def _run_executive(
    gateway: MCPGateway,
    raw_email: str,
    message_id: str,
    thread_id: str,
    from_addr: str = "",
    session_id: str | None = None,
) -> None:
    from openexecutive.knowledge.retriever import retrieve
    from openexecutive.memory.episodic import format_for_prompt
    from openexecutive.onboarding.profile_builder import load_or_create_profile
    from openexecutive.orchestrator.executive import Executive
    from openexecutive.orchestrator.session import Session

    profile = load_or_create_profile()
    session_kwargs: dict[str, Any] = {
        "company_profile": profile if not profile.is_empty() else None,
    }
    if session_id:
        session_kwargs["session_id"] = session_id
    session = Session(**session_kwargs)
    if from_addr:
        # Only register the sender as a schedulable channel_ref if they
        # are in the People roster. Without this guard, an attacker who
        # can spoof a From header could persuade the Executive (via
        # prompt injection in the body) to schedule outbound mail to
        # arbitrary third parties. The roster gate in _handle_email
        # already ensures we only get here for known senders, but
        # re-verify defensively — _run_executive is also reachable from
        # other code paths.
        from openexecutive.people.store import find_person_by_email

        settings = get_settings()
        if (
            from_addr.lower() == settings.exec_email_address.lower()
            or find_person_by_email(from_addr) is not None
        ):
            session.seen_channel_refs.add(("email", f"{from_addr}|{thread_id}"))
            session.seen_channel_refs.add(("email", from_addr))
    # Look up the OE Person record (case-insensitive by email) so Honcho
    # can key per-person memory off Person.id (shared across channels).
    # No match → person_id stays None and the Honcho layer no-ops.
    from openexecutive.people.store import find_person_by_email

    person_id: int | None = None
    if from_addr:
        person = find_person_by_email(from_addr)
        person_id = person.id if person else None

    # Multi-peer co-presence: parse To+Cc headers and resolve each
    # recipient to a Person via find_person_by_email. Skip the From
    # (already covered by person_id) and the OE exec's own address
    # (we ARE the executive — never a peer). Best-effort: parse
    # failures degrade to an empty list rather than blocking the turn.
    co_present_person_ids: list[int] = []
    try:
        recipients = _parse_recipients(raw_email)
        exec_email = get_settings().exec_email_address.lower()
        from_addr_lower = (from_addr or "").lower()
        for addr in recipients:
            addr_lower = addr.lower()
            if addr_lower in (exec_email, from_addr_lower):
                continue
            other = find_person_by_email(addr)
            if other and other.id is not None and other.id not in co_present_person_ids:
                co_present_person_ids.append(other.id)
    except Exception:
        logger.warning(
            "email: recipient parsing failed for message=%s — passing empty co-present list",
            message_id,
            exc_info=True,
        )

    # When the sender isn't on the People roster, prepend a [POLICY]
    # notice so the Executive doesn't waste a turn trying to auto-reply
    # (the MCP gateway's _check_gmail_recipients will block it anyway).
    # The notice lists the actions that ARE allowed so the model picks
    # the right path: classify, log, alert, or propose adding to roster.
    policy_notice = ""
    if from_addr and person_id is None:
        policy_notice = (
            f"[POLICY] This inbound is from {from_addr}, who is NOT on your team's "
            "People roster. You can classify it, log a decision, schedule an internal "
            "follow-up, alert the principal, or surface a proposal to add the sender "
            "to the roster. You cannot send an outbound reply directly to "
            f"{from_addr} — the email gateway will block it. To actually reply, the "
            "principal must add the sender to the People roster first.\n\n"
            "---\n\n"
        )

    # If this email is a reply to mail the Executive sent during another
    # session (e.g. web chat), hydrate the turn with that originating context
    # — the email analogue of the DM bots. channel_ref is the bare lowercased
    # sender address, matching what the gateway records at send time. No-op on
    # a miss, so a thread that already carries history is unaffected.
    base_message = (
        f"You have an inbound email (message_id={message_id}, thread_id={thread_id}).\n\n"
        f"{policy_notice}{raw_email}"
    )
    if from_addr:
        from openexecutive.integrations.inbound_hydration import (
            hydrate_user_message,
        )

        base_message = hydrate_user_message(
            channel="email",
            channel_ref=from_addr.lower(),
            user_message=base_message,
        )

    executive = Executive(mcp_gateway=gateway)
    # Standard (non-committee) path, same as the Slack and Discord
    # adapters. Committee review (draft + 3 critiques + revision, and a
    # deeper Honcho prefetch) is a per-request opt-in on /chat only; it
    # was previously forced on here for every inbound email, including
    # off-roster senders the gateway will not let us reply to anyway.
    await executive.chat(
        user_message=base_message,
        session=session,
        retrieved_context=retrieve(query=raw_email[:500]),
        episodic_context=format_for_prompt(),
        person_id=person_id,
        co_present_person_ids=co_present_person_ids or None,
    )


async def _mark_read(
    gateway: MCPGateway,
    message_id: str,
    user_email: str,
    scope: _mail_scope.MailScope,
    audit_logger: Any,
) -> bool:
    """True when the provider confirmed the label change. A failure leaves
    the message unread — the caller decides what that means (a processed
    message re-plays only the mark-read via the journal dedup marker; it is
    never handed to the Executive twice). A scope drift since the claim —
    client switch, journal swap, config change, restore block — refuses
    the mutation: marking read in a context that does not own the
    evidence would consume the mail under the wrong identity."""
    from openexecutive.clients.slots import is_restore_blocked

    if is_restore_blocked() or not _scope_still_current(
        audit_logger, scope
    ):
        logger.warning(
            "message=%s: scope changed mid-operation — refusing mark_read "
            "in the new context", message_id,
        )
        return False
    try:
        await gateway.call_tool({
            "name": "google_workspace__modify_gmail_message_labels",
            "arguments": {
                "message_id": message_id,
                "user_google_email": user_email,
                "remove_label_ids": ["UNREAD"],
            },
        })
        logger.debug("marked message=%s as read", message_id)
        return True
    except Exception:
        logger.warning("failed to mark message=%s as read", message_id)
        return False


async def _discover_gmail_tools(gateway: MCPGateway) -> None:
    """Discover Gmail MCP tools (extensible-mcp requires per-session discovery)."""
    queries = [
        "search gmail messages unread inbox",
        "get gmail message content subject body sender",
        "get gmail attachment content download base64",
        "modify gmail message labels mark read unread",
    ]
    for query in queries:
        result = await gateway.search_tools({"query": query})
        logger.debug(
            "search_tools(%r) -> %r",
            query, str(result)[:200] if result else "",
        )
    logger.info("Gmail tools discovered")


async def run_email_poller(gateway: MCPGateway) -> None:
    """Async polling loop. Run as a background task; cancelled on shutdown."""
    logger.info("started (interval=%ds)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            await _discover_gmail_tools(gateway)
            await poll_once(gateway)
        except asyncio.CancelledError:
            logger.info("cancelled")
            raise
        except Exception:
            logger.exception("unexpected error in poll cycle")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
