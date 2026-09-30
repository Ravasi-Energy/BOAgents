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
from email.utils import parseaddr
from typing import TYPE_CHECKING, Any

from openexecutive.config import get_settings

if TYPE_CHECKING:
    from openexecutive.orchestrator.mcp_gateway import MCPGateway

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = get_settings().email_poll_interval_seconds

# Prevents reprocessing the same message within a run (cleared on restart).
# The audit journal carries the durable evidence across restarts — see
# _PROCESSED_DEDUP_PREFIX / _ATTEMPT_EVENT below.
_processed_ids: set[str] = set()

# In-process attempt counter — complements the persisted attempt rows (an
# executive-free failure like an empty fetch leaves no attempt row).
_retry_counts: dict[str, int] = {}

_SKIP_SENDERS = ("noreply", "no-reply", "mailer-daemon", "postmaster", "do-not-reply")

# Pagination contract — verified against workspace-mcp 1.21.1
# (gmail/gmail_tools.py): `search_gmail_messages` accepts `page_token` and
# appends `📄 PAGINATION: ... page_token='<token>'` when more results exist.
# Only that exact marker is honoured; an absent or malformed hint degrades to
# the old single-page behaviour instead of inventing a parameter.
_PAGE_TOKEN_RE = re.compile(r"page_token='([^']+)")

# Journal evidence types emitted by this module.
_ATTEMPT_EVENT = "email_process_attempt"
_PROCESSED_DEDUP_PREFIX = "email_processed:"
_ATTEMPT_DEDUP_PREFIX = "email_attempt:"

# Tools whose invocation is an externally visible effect. If a previous
# attempt of the same message already called one of these and then failed,
# re-running the Executive could duplicate the send/invite/share — that case
# is reported as "uncertain" instead of retried. The sets are the egress
# gate's own, so the check tracks whatever the gateway considers effectful.
_EXTERNAL_EFFECT_TOOLS: frozenset[str] | None = None


def _external_effect_tools() -> frozenset[str]:
    global _EXTERNAL_EFFECT_TOOLS
    if _EXTERNAL_EFFECT_TOOLS is None:
        from openexecutive.orchestrator.mcp_gateway import (
            _GATED_CALENDAR_TOOLS,
            _GATED_DRIVE_TOOLS,
            _GATED_GMAIL_TOOLS,
        )

        _EXTERNAL_EFFECT_TOOLS = (
            _GATED_GMAIL_TOOLS | _GATED_CALENDAR_TOOLS | _GATED_DRIVE_TOOLS
        )
    return _EXTERNAL_EFFECT_TOOLS


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
            "query": "is:unread in:inbox",
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
        if not mid or mid in _processed_ids:
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
            outcome = await _handle_email(gateway, mid, tid, user_email)
        except Exception:
            logger.exception("failed for message=%s", mid)
            outcome = "failed"
        # Only terminal outcomes enter the processed set: a failed message
        # must NOT be recorded as handled (it stays unread at the provider),
        # and a message whose mark-read call failed is re-evaluated next
        # cycle — the journal dedup marker then proves processing already
        # happened, so the Executive is never re-run for it.
        if outcome in ("processed", "skipped", "uncertain", "failed_final"):
            _processed_ids.add(mid)
        elif outcome == "failed":
            _retry_counts[mid] = _retry_counts.get(mid, 0) + 1


def _attempt_number(audit_logger: Any, message_id: str, session_id: str) -> int:
    """1-based attempt number for this message — the max of the in-process
    counter and the persisted ``email_process_attempt`` rows, so the retry
    bound holds across restarts (the journal survives; ``_retry_counts``
    does not)."""
    persisted = 0
    try:
        persisted = sum(
            1
            for e in audit_logger.query(
                event_type=_ATTEMPT_EVENT, session_id=session_id, limit=1000
            )
            if e.details.get("message_id") == message_id
        )
    except Exception:
        logger.warning(
            "audit journal unreadable while counting attempts for "
            "message=%s — falling back to the in-process counter",
            message_id,
        )
    return max(persisted, _retry_counts.get(message_id, 0)) + 1


def _prior_external_effect(
    audit_logger: Any, message_id: str, session_id: str
) -> bool:
    """True when an earlier attempt of THIS message already invoked an
    externally-visible tool (send/draft/event/drive-share).

    Each attempt is bracketed by an ``email_process_attempt`` row; the
    poller handles messages strictly sequentially, so a mutating
    ``tool_invocation`` between this message's attempt marker and the next
    attempt marker (of any message) is attributable to that attempt. The
    check is deliberately session-scoped but message-attributed: a reply
    sent for an earlier mail in the same thread must NOT poison the next
    message's processing.

    An unreadable journal cannot rule a prior effect out → fail closed.
    """
    try:
        attempts = audit_logger.query(
            event_type=_ATTEMPT_EVENT, session_id=session_id, limit=1000
        )
        tools = audit_logger.query(
            event_type="tool_invocation", session_id=session_id, limit=1000
        )
    except Exception:
        logger.warning(
            "audit journal unreadable — cannot rule out a prior effect "
            "for message=%s; treating as uncertain",
            message_id,
        )
        return True
    boundaries = sorted(e.id for e in attempts)
    effectful = _external_effect_tools()
    for attempt in attempts:
        if attempt.details.get("message_id") != message_id:
            continue
        upper = next((b for b in boundaries if b > attempt.id), None)
        for tool_row in tools:
            if (
                attempt.id < tool_row.id
                and (upper is None or tool_row.id < upper)
                and tool_row.details.get("tool") in effectful
            ):
                return True
    return False


async def _consume_skipped(
    gateway: MCPGateway,
    message_id: str,
    user_email: str,
    session_id: str,
    from_addr: str,
    reason: str,
) -> str:
    """Deliberate policy skip — evaluated, evidenced, marked read.

    Skipped mail is CONSUMED: leaving it unread would re-occupy every
    ``is:unread`` page on every cycle (and after every restart), which is
    exactly the starvation the single-page traversal had. The
    ``email_processed:`` dedup marker is written for the same reason — the
    skip decision is terminal.
    """
    from openexecutive.audit import log_event as audit_log

    audit_log(
        "integration_inbound",
        f"Skipped inbound email {message_id} ({reason})",
        actor="email",
        session_id=session_id,
        details={
            "channel": "email",
            "message_id": message_id,
            "from": from_addr,
            "outcome": reason,
        },
        dedup_key=f"{_PROCESSED_DEDUP_PREFIX}{message_id}",
    )
    return "skipped" if await _mark_read(gateway, message_id, user_email) else "mark_read_failed"


async def _handle_email(
    gateway: MCPGateway,
    message_id: str,
    thread_id: str,
    user_email: str,
) -> str:
    """Process one unread message; returns an explicit outcome:

    ``processed``        Executive ran; marked read.
    ``skipped``          Policy skip (self/automated); marked read.
    ``mark_read_failed`` Executive ran (or journal already proves it did)
                         but the provider refused the label change — the
                         dedup marker prevents a duplicate run next cycle.
    ``failed``           Transient/executive failure — message stays unread
                         and retriable within the bounded attempt budget.
    ``uncertain``        A previous attempt already produced an external
                         effect before failing — never re-run, reported.
    ``failed_final``     Attempt budget exhausted — stays unread, reported.
    """
    from openexecutive.audit import get_audit_logger
    from openexecutive.audit import log_event as audit_log

    audit_logger = get_audit_logger()

    # Durable dedup BEFORE any provider fetch: a committed marker proves the
    # Executive already completed this message (processed or policy-skipped)
    # — possibly in a previous process lifetime. Only the provider-side
    # mark-read can still be outstanding. A journal read failure is NOT
    # absence of evidence (see dedup_lookup's contract): refusing to guess
    # keeps a maybe-sent mail from being re-run blindly.
    dedup_key = f"{_PROCESSED_DEDUP_PREFIX}{message_id}"
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
        logger.info(
            "message=%s already evidenced in the journal — re-marking read",
            message_id,
        )
        return (
            "processed"
            if await _mark_read(gateway, message_id, user_email)
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
    # message must stay unread and retriable rather than be consumed unseen.
    if not raw or not raw.strip():
        logger.warning("empty content for message=%s", message_id)
        return "failed"

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
            gateway, message_id, user_email, session_id, from_addr, "self_sent"
        )
    if any(p in from_line.lower() for p in _SKIP_SENDERS):
        logger.debug("skipping automated sender for message=%s", message_id)
        return await _consume_skipped(
            gateway, message_id, user_email, session_id, from_addr,
            "automated_sender",
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

    # Bounded retry — the journal rows are the durable counter (a crash mid-
    # turn loses _retry_counts but not the evidence), the in-process counter
    # covers failures that never reached an attempt row.
    attempt_no = _attempt_number(audit_logger, message_id, session_id)
    max_attempts = _mail_setting("bo.mail.processing.max_attempts")
    if attempt_no > max_attempts:
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
                "attempts": attempt_no - 1,
            },
        )
        logger.warning(
            "message=%s exceeded the processing attempt budget (%d) — left "
            "unread for human review",
            message_id, max_attempts,
        )
        return "failed_final"

    # Duplicate-effect guard: if a previous attempt of THIS message already
    # ran an externally-visible tool before dying, re-running could repeat
    # the send/invite/share. Report the case as uncertain — the mail stays
    # unread for a human — rather than risking a doubled effect.
    if attempt_no > 1 and _prior_external_effect(
        audit_logger, message_id, session_id
    ):
        audit_log(
            "integration_inbound",
            f"Email processing left in uncertain state for message "
            f"{message_id} — a previous attempt already invoked an "
            "externally-visible tool before failing",
            actor="email",
            session_id=session_id,
            details={
                "channel": "email",
                "message_id": message_id,
                "thread_id": thread_id,
                "from": from_addr,
                "outcome": "uncertain_partial_effect",
                "attempt": attempt_no,
            },
        )
        logger.warning(
            "message=%s: prior attempt produced an external effect — "
            "re-run refused to avoid duplicate sends; left unread",
            message_id,
        )
        return "uncertain"

    # Bracket the attempt BEFORE the Executive runs — its row id is the
    # window start _prior_external_effect attributes tool calls to. And the
    # bracket must be PROVABLY durable: log_event is fire-and-forget (a write
    # failure is swallowed), so the marker is re-read. Running the Executive
    # without a committed bracket would let a crash strand an external
    # effect with no evidence to attribute it — the exact duplicate-send
    # hole this bookkeeping exists to close.
    attempt_dedup = f"{_ATTEMPT_DEDUP_PREFIX}{message_id}:{attempt_no}"
    audit_log(
        _ATTEMPT_EVENT,
        f"Processing attempt {attempt_no} for email {message_id}",
        actor="email",
        session_id=session_id,
        details={"message_id": message_id, "attempt": attempt_no},
        dedup_key=attempt_dedup,
    )
    try:
        bracket_committed = audit_logger.dedup_lookup(attempt_dedup) is not None
    except Exception:
        bracket_committed = False
    if not bracket_committed:
        logger.warning(
            "attempt bracket for message=%s attempt=%d not durable — "
            "deferring rather than risking unattributed external effects",
            message_id, attempt_no,
        )
        return "failed"
    try:
        await _run_executive(
            gateway, _strip_reply_to(raw), message_id, thread_id, from_addr, session_id
        )
    except Exception:
        logger.exception("Executive raised for message=%s", message_id)
        # NOT consumed: no mark-read, no journal success row — the message
        # stays unread and is retried until the attempt budget above.
        return "failed"

    # The evidence row + dedup marker land atomically BEFORE the provider
    # label change: a mark-read failure (or a crash in between) leaves the
    # message unread but provably processed — the next cycle replays only
    # the mark-read, never the Executive.
    audit_log(
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
        },
        dedup_key=dedup_key,
    )
    if await _mark_read(gateway, message_id, user_email):
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
    gateway: MCPGateway, message_id: str, user_email: str
) -> bool:
    """True when the provider confirmed the label change. A failure leaves
    the message unread — the caller decides what that means (a processed
    message re-plays only the mark-read via the journal dedup marker; it is
    never handed to the Executive twice)."""
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
