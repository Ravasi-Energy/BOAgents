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
_RESULT_EVENT = "email_attempt_result"
_PROCESSED_DEDUP_PREFIX = "email_processed:"
_ATTEMPT_DEDUP_PREFIX = "email_attempt:"
_RESULT_DEDUP_PREFIX = "email_attempt_result:"

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
_terminal_label_id: str | None = None

# Single-owner frontier inside this process: two concurrent poll_once
# calls must serialize claim+execute so the same message cannot be handed
# to the Executive twice. Cross-process ownership comes from the atomic
# attempt-claim dedup row (see _handle_email) — the lock alone is NOT the
# durable guarantee, only the fast path.
_POLL_LOCK = asyncio.Lock()


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
    gateway: MCPGateway, user_email: str
) -> str | None:
    """Resolve the provider id of the terminal-fence label, creating it on
    first use. Returns None when the provider refuses — the caller then
    leaves the message enumerated (degraded, but never mislabeled and
    never silently marked read)."""
    global _terminal_label_id
    if _terminal_label_id:
        return _terminal_label_id
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
            _terminal_label_id = lid.strip()
            return _terminal_label_id
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
        _terminal_label_id = m.group(1)
    return _terminal_label_id


async def _label_terminal(
    gateway: MCPGateway, message_id: str, user_email: str
) -> None:
    """Fence a terminally-blocked message provider-side: add the
    OE-Terminal label WITHOUT touching UNREAD — the mail stays visibly
    unread for a human, but the unread query excludes it so it cannot
    starve the pages ahead of legitimate mail. Removing the label in
    Gmail re-enumerates it; nothing here pretends the mail was handled."""
    label_id = await _resolve_terminal_label_id(gateway, user_email)
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
            # The lock serializes claim+execute inside this process: a
            # second poll_once racing the same message waits, then sees
            # the first owner's dedup marker instead of re-running the
            # Executive. Cross-process races are fenced by the atomic
            # claim row itself (RA-B2-03).
            async with _POLL_LOCK:
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
        if outcome in ("uncertain", "failed_final"):
            # Provider-side fence so the terminal message stops occupying
            # the leading unread pages. It stays unread — never a false
            # success — and a human reconciles by removing the label.
            await _label_terminal(gateway, mid, user_email)


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
    audit_logger: Any, message_id: str, session_id: str, max_attempts: int
) -> tuple[str, int]:
    """Evaluate this message's durable attempt lifecycle.

    Returns ``(state, next_attempt_no)`` where state is:

    ``"clean"``     — no attempts yet, or every prior attempt is CLOSED
                      with result=executive_failed AND a complete window
                      scan proves no effectful tool ran. Retry is
                      legitimate: absence of effect is demonstrated.
    ``"uncertain"`` — an attempt row exists without its close row
                      (interrupted mid-turn → an effect may have landed),
                      a marker/close row is orphaned (the journal lost the
                      referenced evidence), or a prior attempt's window
                      contains a non-read-only tool call. NEVER retried
                      automatically — a human reconciles.
    ``"exhausted"`` — the durable attempt budget is spent.
    ``"failed"``    — the journal could not be read; nothing may be
                      inferred, so the message simply stays unread.

    Per-message state lives in the dedup-keyed bracket rows themselves
    (``email_attempt:{mid}:{n}`` opens, ``email_attempt_result:{mid}:{n}``
    closes) — counts come from COUNT over the dedup table and every
    marker read propagates errors, so history truncation can neither
    reset the budget nor hide an effect.
    """
    try:
        attempt_count = audit_logger.count_dedup_prefix(
            f"{_ATTEMPT_DEDUP_PREFIX}{message_id}:"
        )
    except Exception:
        logger.warning(
            "journal unreadable counting attempts for message=%s — "
            "deferring (unknown is not absent)", message_id,
        )
        return "failed", 0

    if attempt_count == 0:
        prior = _retry_counts.get(message_id, 0)
        if prior >= max_attempts:
            return "exhausted", prior + 1
        return "clean", prior + 1

    # Fetch this message's attempt rows in id order — complete scan keyed
    # on the exact details JSON substring, never a session-wide page.
    try:
        attempts = sorted(
            _query_all(
                audit_logger,
                event_type=_ATTEMPT_EVENT,
                session_id=session_id,
                details_substr=f'"message_id": "{message_id}"',
            ),
            key=lambda e: e.id,
        )
    except Exception:
        logger.warning(
            "journal unreadable reading attempts for message=%s — "
            "deferring (unknown is not absent)", message_id,
        )
        return "failed", 0

    if len(attempts) != attempt_count:
        # A claim marker exists whose attempt row the journal lost —
        # an attempt ran whose window cannot be located → unknown.
        return "uncertain", attempt_count + 1

    # Boundaries in the shared session stream: a tool_invocation inside
    # (open_id, next_bracket_or_close_id) belongs to that attempt.
    try:
        session_brackets = sorted(
            e.id
            for e in _query_all(
                audit_logger,
                event_type=_ATTEMPT_EVENT,
                session_id=session_id,
            )
        )
    except Exception:
        logger.warning(
            "journal unreadable reading session brackets for "
            "message=%s — deferring", message_id,
        )
        return "failed", 0

    from openexecutive.orchestrator.tool_effects import has_external_effect

    for attempt in attempts:
        n = int(attempt.details.get("attempt") or 0)
        if n <= 0:
            # Malformed bracket: cannot order the window → unknown.
            return "uncertain", attempt_count + 1
        try:
            close = audit_logger.dedup_lookup(
                f"{_RESULT_DEDUP_PREFIX}{message_id}:{n}"
            )
        except Exception:
            logger.warning(
                "journal unreadable reading attempt result for "
                "message=%s attempt=%d — deferring", message_id, n,
            )
            return "failed", 0
        if close is None or not close.get("journal_row_present"):
            # Open-without-close (interrupted mid-turn) or an orphaned
            # close marker: whether an external effect landed is
            # unprovable → UNKNOWN, blocked for auto-retry.
            return "uncertain", attempt_count + 1
        try:
            close_row = audit_logger.get(close["audit_row_id"])
        except Exception:
            close_row = None
        if close_row is None:
            # Marker points at a row the journal can no longer return —
            # incomplete evidence is unknown, never retryable.
            return "uncertain", attempt_count + 1
        result = (close_row.details or {}).get("result")
        if result == "executed":
            # Executive completed but the processed marker did not
            # survive/land — replaying would duplicate its effects.
            return "uncertain", attempt_count + 1
        if result != "executive_failed":
            return "uncertain", attempt_count + 1
        upper = next(
            (b for b in session_brackets if b > attempt.id),
            close["audit_row_id"],
        )
        upper = min(upper, close["audit_row_id"] + 1)
        try:
            window_tools = _query_all(
                audit_logger,
                event_type="tool_invocation",
                session_id=session_id,
                min_id=attempt.id,
                max_id=upper,
            )
        except Exception:
            logger.warning(
                "journal unreadable scanning attempt window for "
                "message=%s — treating as uncertain", message_id,
            )
            return "uncertain", attempt_count + 1
        for tool_row in window_tools:
            tool_name = str((tool_row.details or {}).get("tool") or "")
            if has_external_effect(tool_name):
                return "uncertain", attempt_count + 1

    next_no = max(attempt_count, _retry_counts.get(message_id, 0)) + 1
    if next_no > max_attempts:
        return "exhausted", next_no
    return "clean", next_no


def _claim_attempt(
    audit_logger: Any,
    message_id: str,
    session_id: str,
    attempt_no: int,
    owner: str,
) -> str:
    """Atomically claim attempt ``attempt_no`` for ``message_id``.

    The claim row is written with a per-owner nonce in ``details`` so its
    dedup fingerprint is unique per claimant — two racing owners on the
    same dedup_key can never both think they won: the loser's conflicting
    fingerprint makes ``log`` return None, and only the winner's row
    carries the winner's nonce. Returns "claimed", "lost" (another owner
    holds it), or "journal_error" (nothing durable → nobody may run).
    """
    dedup_key = f"{_ATTEMPT_DEDUP_PREFIX}{message_id}:{attempt_no}"
    row_id = audit_logger.log(
        _ATTEMPT_EVENT,
        f"Processing attempt {attempt_no} for email {message_id}",
        actor="email",
        session_id=session_id,
        details={
            "message_id": message_id,
            "attempt": attempt_no,
            "owner": owner,
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
) -> None:
    """Write the attempt's close row (``executed``/``executive_failed``).
    Best-effort: if it does not commit, the next cycle sees an open
    attempt and fails closed as ``uncertain`` — never as retryable."""
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
        },
        dedup_key=f"{_RESULT_DEDUP_PREFIX}{message_id}:{attempt_no}",
    )


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
    ``uncertain``        Prior attempt evidence is incomplete (open
                         bracket, orphan marker, lost close row) or the
                         attempt window contains a non-read-only tool
                         call — never re-run automatically; fenced with
                         the provider-side OE-Terminal label.
    ``failed_final``     Attempt budget exhausted — stays unread, fenced.
    ``claimed``          A concurrent owner holds the attempt claim —
                         not ours to run; re-evaluated next cycle.
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
        if not marker.get("journal_row_present"):
            # Orphan marker: the journal lost the evidence row it points
            # to — whether the Executive completed is unprovable. Not a
            # success and NOT a retry; left for human reconciliation.
            logger.warning(
                "orphan processed marker for message=%s — incomplete "
                "evidence, refusing to infer completion", message_id,
            )
            return "uncertain"
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

    # Durable per-message attempt lifecycle (open row + close row, both
    # dedup-keyed and journal-verified). An open attempt without a close
    # row is an interrupted turn — an external effect may have landed —
    # so it is never retried automatically. A closed-failed attempt may
    # retry ONLY when a complete window scan proves every tool call it
    # made was read-only: absence of evidence is not evidence of absence.
    max_attempts = _mail_setting("bo.mail.processing.max_attempts")
    state, attempt_no = _attempts_state(
        audit_logger, message_id, session_id, max_attempts
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
                "attempts": attempt_no - 1,
            },
        )
        logger.warning(
            "message=%s exceeded the processing attempt budget (%d) — left "
            "unread for human review",
            message_id, max_attempts,
        )
        return "failed_final"
    if state == "uncertain":
        audit_log(
            "integration_inbound",
            f"Email processing left in uncertain state for message "
            f"{message_id} — prior attempt evidence is incomplete or "
            "already produced a non-read-only effect",
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
            "message=%s: prior attempt evidence incomplete/effectful — "
            "re-run refused to avoid duplicate effects; left unread",
            message_id,
        )
        return "uncertain"

    # Atomic claim BEFORE the Executive runs: the bracket row carries a
    # unique owner nonce, so its dedup fingerprint differs per claimant —
    # two racing owners on the same dedup_key cannot both commit. The
    # claim must be PROVABLY durable AND ours: running without it would
    # let a crash strand an external effect with no evidence to
    # attribute it, or run the same send twice across two owners.
    owner = uuid.uuid4().hex
    claim = _claim_attempt(audit_logger, message_id, session_id, attempt_no, owner)
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
    try:
        await _run_executive(
            gateway, _strip_reply_to(raw), message_id, thread_id, from_addr, session_id
        )
    except Exception:
        logger.exception("Executive raised for message=%s", message_id)
        # Close as failed: a retry is legitimate only if the window scan
        # proves this attempt produced nothing effectful. If the close
        # row itself fails to commit, the open attempt reads as
        # interrupted → uncertain next cycle (fail closed).
        _close_attempt(
            audit_logger, message_id, session_id, attempt_no, owner,
            "executive_failed",
        )
        return "failed"

    _close_attempt(
        audit_logger, message_id, session_id, attempt_no, owner, "executed",
    )

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
