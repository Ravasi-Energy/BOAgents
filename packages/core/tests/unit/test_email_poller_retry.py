"""B1/B2 regression tests for the email poller.

B1 — the pre-fix poller fetched exactly one page of 10 unread messages.
Skipped messages (self-sent, automated senders) and anything already in
``_processed_ids`` stayed unread and occupied that single page on every
cycle — real mail behind them starved until restart, and the restart
repeated it. The fix walks ``next_page_token`` pages (bounded by
``bo.mail.poll.max_pages_per_cycle``) and consumes policy skips via
mark-read so they stop occupying the unread window.

B2 — the pre-fix handler ran ``_mark_read`` unconditionally after the
Executive call, so a failed turn consumed the message silently. The fix
returns an explicit outcome per message: pre-Executive failures (fetch
glitches, journal outages) stay unread and are retried within a bounded
DURABLE attempt budget (``email_fetch_fail@{scope}:`` markers — never
volatile in-process counters), while any message that ever started an
Executive
turn is terminal ``uncertain`` once it fails: a missing tool_invocation
row cannot prove zero effects, so automatic resubmit was removed —
a human reconciles fenced uncertain mail.
"""
from __future__ import annotations

import asyncio
from collections import deque
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import openexecutive.integrations.email_poller as poller
from openexecutive.audit import AuditLogger, set_audit_logger
from openexecutive.people import store as people_store

EXEC = "exec@example.com"


class FakeGateway:
    """Provider-side stub: scriptable search pages, canned message bodies,
    recorded mark-read calls. No network, no MCP — the poller's own logic
    is what is under test.
    """

    def __init__(
        self,
        pages: list[str] | None = None,
        contents: dict[str, str] | None = None,
        fail_mark: set[str] | None = None,
        mark_calls_before_fail: int | None = None,
    ) -> None:
        self.pages: deque[str] = deque(pages or [])
        self.contents = dict(contents or {})
        self.fail_mark = set(fail_mark or set())
        self.mark_calls_before_fail = mark_calls_before_fail
        self.search_args: list[dict[str, Any]] = []
        self.fetched: list[str] = []
        self.marked: list[str] = []
        self.labeled: list[tuple[str, list[str]]] = []
        # None → provider "doesn't know" the label yet → the poller must
        # call manage_gmail_label(create); a string answers list directly.
        self.labels_response: str | None = None
        self._mark_attempts = 0

    async def call_tool(self, payload: dict[str, Any]) -> str:
        name = payload["name"]
        args = payload["arguments"]
        if name.endswith("search_gmail_messages"):
            self.search_args.append(args)
            return self.pages.popleft() if self.pages else ""
        if name.endswith("list_gmail_labels"):
            if self.labels_response is not None:
                return self.labels_response
            return "No labels found."
        if name.endswith("manage_gmail_label"):
            return (
                "Label created successfully!\n"
                "Name: OE-Terminal\nID: Label_99"
            )
        if name.endswith("get_gmail_message_content"):
            self.fetched.append(args["message_id"])
            return self.contents.get(args["message_id"], "")
        if name.endswith("modify_gmail_message_labels"):
            if args.get("add_label_ids"):
                # Terminal fence — label add only, message stays UNREAD.
                self.labeled.append((args["message_id"], args["add_label_ids"]))
                return "OK"
            self._mark_attempts += 1
            if (
                self.mark_calls_before_fail is not None
                and self._mark_attempts > self.mark_calls_before_fail
            ):
                raise RuntimeError("provider refused the label change")
            mid = args["message_id"]
            if mid in self.fail_mark:
                raise RuntimeError("provider refused the label change")
            self.marked.append(mid)
            return "OK"
        raise AssertionError(f"unexpected tool call {name}")


def _settings() -> Any:
    return SimpleNamespace(
        exec_email_address=EXEC,
        email_poll_interval_seconds=60,
    )


def _page(*message_ids: str, next_token: str | None = None) -> str:
    lines = []
    for mid in message_ids:
        lines.append(f"Message ID: {mid}")
        lines.append(f"Thread ID: t-{mid}")
    raw = "\n".join(lines)
    if next_token:
        raw += (
            "\n\n📄 PAGINATION: To get the next page, call "
            f"search_gmail_messages again with page_token='{next_token}'"
        )
    return raw


def _raw_email(from_value: str, subject: str = "Hello") -> str:
    return (
        f"Subject: {subject}\n"
        f"From: {from_value}\n"
        "\n"
        "--- BODY ---\n"
        "Body text here.\n"
    )


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolate every store the poller touches:

    - the audit journal (attempt brackets, dedup markers, outcome rows)
      gets a fresh tmp DB — durable cross-"restart" state is real SQLite,
      only the file location is redirected;
    - the people DB (roster lookups) is empty;
    - the bo settings DB points at a nonexistent path so ``_mail_setting``
      returns registry defaults;
    - the in-process caches are cleared so tests are order-independent.
    """
    audit = AuditLogger(db_path=tmp_path / "audit.db")
    set_audit_logger(audit)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    people_store.initialize_db()
    monkeypatch.setattr(
        "openexecutive.bo.db.DB_PATH", tmp_path / "nonexistent-bo.db"
    )
    poller.reset_mail_caches()
    yield audit
    set_audit_logger(None)
    poller.reset_mail_caches()


def _poll(
    gateway: FakeGateway,
    run_exec: AsyncMock | None = None,
    *,
    blocked: bool = False,
) -> AsyncMock:
    exec_mock = run_exec if run_exec is not None else AsyncMock()
    with (
        patch.object(poller, "get_settings", return_value=_settings()),
        patch.object(poller, "_run_executive", new=exec_mock),
        patch(
            "openexecutive.clients.slots.is_restore_blocked",
            return_value=blocked,
        ),
    ):
        asyncio.run(poller.poll_once(gateway))
    return exec_mock


def _handle(
    gateway: FakeGateway,
    message_id: str = "m1",
    thread_id: str = "t-m1",
    run_exec: AsyncMock | None = None,
) -> tuple[str, AsyncMock]:
    exec_mock = run_exec if run_exec is not None else AsyncMock()
    with (
        patch.object(poller, "get_settings", return_value=_settings()),
        patch.object(poller, "_run_executive", new=exec_mock),
    ):
        outcome = asyncio.run(
            poller._handle_email(gateway, message_id, thread_id, EXEC)
        )
    return outcome, exec_mock


def _test_scope(audit: AuditLogger) -> Any:
    """Bind + return the processing scope the patched test settings
    resolve to — the same (tenant, client, mailbox) inputs _handle/_poll
    run under (RA11-B01: every marker family is scope-keyed now)."""
    with patch.object(poller, "get_settings", return_value=_settings()):
        scope, refusal = poller._resolve_scope(audit)
    assert scope is not None, f"test scope refused: {refusal}"
    return scope


def _attempt_row(audit: AuditLogger, message_id: str, session_id: str, n: int) -> None:
    scope = _test_scope(audit)
    audit.log(
        "email_process_attempt",
        f"Processing attempt {n} for email {message_id}",
        actor="email",
        session_id=session_id,
        details={
            "message_id": message_id,
            "attempt": n,
            "owner": f"o{n}",
            "scope": scope.token,
            "mailbox": scope.mailbox,
        },
        dedup_key=f"email_attempt@{scope.token}:{message_id}:{n}",
    )


def _close_row(
    audit: AuditLogger,
    message_id: str,
    session_id: str,
    n: int,
    result: str = "executive_failed",
) -> None:
    """Close an attempt the way _handle_email does — an open attempt with
    no close row is an interrupted turn and is NEVER retryable."""
    scope = _test_scope(audit)
    audit.log(
        "email_attempt_result",
        f"Attempt {n} for email {message_id}: {result}",
        actor="email",
        session_id=session_id,
        details={
            "message_id": message_id,
            "attempt": n,
            "owner": f"o{n}",
            "result": result,
            "scope": scope.token,
            "mailbox": scope.mailbox,
        },
        dedup_key=f"email_attempt_result@{scope.token}:{message_id}:{n}",
    )


# ---------------------------------------------------------------- B1


def test_second_page_reached_when_first_page_is_all_skips() -> None:
    """B1 core: a first page full of policy skips must not starve the real
    mail behind it — the poller follows next_page_token and the real mail
    is processed in the SAME cycle."""
    skips = [f"s{i}" for i in range(10)]
    contents = {mid: _raw_email(EXEC) for mid in skips}  # all self-sent
    contents["real1"] = _raw_email("alice@example.com")
    gateway = FakeGateway(
        pages=[_page(*skips, next_token="tok2"), _page("real1")],
        contents=contents,
    )
    exec_mock = _poll(gateway)

    assert len(gateway.search_args) == 2
    assert gateway.search_args[1].get("page_token") == "tok2"
    # Every self-sent skip was consumed (marked read) so it stops
    # re-occupying the unread window.
    for mid in skips:
        assert mid in gateway.marked
    assert "real1" in gateway.marked
    assert exec_mock.await_count == 1


def test_no_pagination_line_means_single_page() -> None:
    """An absent token degrades to the old single-page behaviour."""
    gateway = FakeGateway(
        pages=[_page("m1")], contents={"m1": _raw_email("a@b.com")}
    )
    _poll(gateway)
    assert len(gateway.search_args) == 1


def test_malformed_pagination_line_is_ignored() -> None:
    """Only the exact workspace-mcp marker is honoured — a lookalike line
    must not invent a continuation parameter."""
    page = _page("m1") + "\n📄 PAGINATION: call again with page_token=tok2"
    gateway = FakeGateway(
        pages=[page, _page("m2")], contents={"m1": _raw_email("a@b.com")}
    )
    _poll(gateway)
    assert len(gateway.search_args) == 1


def test_page_budget_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token on every page must not loop forever: the cycle stops at the
    configured budget and leaves the rest for the next cycle."""
    monkeypatch.setattr(
        poller, "_mail_setting", lambda key: 3
    )
    pages = [_page(f"m{i}", next_token="tok") for i in range(10)]
    contents = {f"m{i}": _raw_email("a@b.com") for i in range(10)}
    gateway = FakeGateway(pages=pages, contents=contents)
    _poll(gateway)
    assert len(gateway.search_args) == 3


def test_duplicate_message_ids_across_pages_processed_once() -> None:
    gateway = FakeGateway(
        pages=[
            _page("m1", next_token="tok2"),
            _page("m1", "m2"),  # provider re-emitted m1
        ],
        contents={
            "m1": _raw_email("a@b.com"),
            "m2": _raw_email("c@d.com"),
        },
    )
    exec_mock = _poll(gateway)
    assert gateway.fetched.count("m1") == 1
    assert gateway.marked.count("m1") == 1
    assert exec_mock.await_count == 2


def test_restore_block_mid_cycle_leaves_rest_unread() -> None:
    """A restore marker landing mid-loop stops the cycle before the next
    message is fetched or marked."""
    calls = {"n": 0}

    def _blocked() -> bool:
        calls["n"] += 1
        # Call 1 is the pre-cycle check; call 2 gates m1 (clear); the
        # marker lands before m2's gate (call 3).
        return calls["n"] > 2

    gateway = FakeGateway(
        pages=[_page("m1", "m2")],
        contents={
            "m1": _raw_email("a@b.com"),
            "m2": _raw_email("c@d.com"),
        },
    )
    exec_mock = AsyncMock()
    with (
        patch.object(poller, "get_settings", return_value=_settings()),
        patch.object(poller, "_run_executive", new=exec_mock),
        patch(
            "openexecutive.clients.slots.is_restore_blocked",
            side_effect=_blocked,
        ),
    ):
        asyncio.run(poller.poll_once(gateway))
    assert gateway.fetched == ["m1"]
    assert "m2" not in gateway.marked


# ---------------------------------------------------------------- B2


def test_executive_failure_is_uncertain_and_never_retried() -> None:
    """B2 core + RA-B2-05: a failed Executive turn must NOT consume the
    message — and must NOT be retried either. A missing tool_invocation
    row can never prove no external effect landed (the journal write is
    post-dispatch and best-effort), so the honest outcome is uncertain:
    unread, fenced, left for a human. Auto-retry is deliberately gone."""
    gateway = FakeGateway(
        contents={"m1": _raw_email("a@b.com")},
    )
    failing = AsyncMock(side_effect=RuntimeError("LLM exploded"))
    outcome, _ = _handle(gateway, run_exec=failing)
    assert outcome == "uncertain"
    assert gateway.marked == []

    # A later cycle re-evaluates: the closed executive_failed attempt is
    # terminal — the Executive never runs for this message again.
    outcome, ok = _handle(gateway)
    assert outcome == "uncertain"
    assert ok.await_count == 0
    assert gateway.marked == []


def test_empty_fetch_is_not_consumed(isolated_state: AuditLogger) -> None:
    """An empty fetch stays unread and retriable — and is now counted
    DURABLY (email_fetch_fail marker), so the bound survives restarts
    and cannot diverge across processes."""
    gateway = FakeGateway(contents={"m1": ""})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "failed"
    assert exec_mock.await_count == 0
    assert gateway.marked == []
    tok = _test_scope(isolated_state).token
    ff = f"email_fetch_fail@{tok}:m1:"
    assert isolated_state.count_dedup_prefix(ff) == 1
    # Second failure — the journal, not any in-process counter, holds it.
    outcome2, _ = _handle(gateway)
    assert outcome2 == "failed"
    assert isolated_state.count_dedup_prefix(ff) == 2
    # Third reaches the registry default budget (3) → terminal.
    outcome3, _ = _handle(gateway)
    assert outcome3 == "failed_final"
    assert isolated_state.count_dedup_prefix(ff) == 3


def test_skip_marks_read_and_journals() -> None:
    """Policy skips are consumed deliberately: the dedup marker proves the
    decision terminal, so a restart does not re-evaluate it."""
    gateway = FakeGateway(contents={"m1": _raw_email(EXEC)})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "skipped"
    assert gateway.marked == ["m1"]
    assert exec_mock.await_count == 0
    # Second call (post-restart analogue): the marker short-circuits.
    outcome2, exec2 = _handle(gateway)
    assert outcome2 == "processed"
    assert exec2.await_count == 0


def test_mark_read_failure_replays_only_mark_read() -> None:
    """Provider refused the label change AFTER the Executive succeeded:
    the journal marker proves processing, so the next cycle replays the
    mark-read and never re-runs the Executive."""
    gateway = FakeGateway(
        contents={"m1": _raw_email("a@b.com")},
        fail_mark={"m1"},
    )
    outcome, exec_mock = _handle(gateway)
    assert outcome == "mark_read_failed"
    assert exec_mock.await_count == 1

    gateway.fail_mark.clear()
    outcome2, exec2 = _handle(gateway)
    assert outcome2 == "processed"
    assert exec2.await_count == 0  # no duplicate Executive run
    assert gateway.marked == ["m1"]


def test_prior_external_effect_refuses_rerun(isolated_state: AuditLogger) -> None:
    """A prior CLOSED-failed attempt whose window contains a send must
    never be re-run — reported uncertain, left unread for a human."""
    session = "email:t-m1"
    _attempt_row(isolated_state, "m1", session, 1)
    isolated_state.log(
        "tool_invocation",
        "send_gmail_message",
        session_id=session,
        actor="executive",
        details={"tool": "google_workspace__send_gmail_message"},
    )
    _close_row(isolated_state, "m1", session, 1)
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "uncertain"
    assert exec_mock.await_count == 0
    assert gateway.marked == []


def test_open_attempt_without_close_is_uncertain(
    isolated_state: AuditLogger,
) -> None:
    """RA-B2-01: an attempt bracket with no close row is an interrupted
    turn — an external effect may have landed and cannot be disproved.
    Never retried automatically, even with zero tool rows on record."""
    session = "email:t-m1"
    _attempt_row(isolated_state, "m1", session, 1)  # no close row
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "uncertain"
    assert exec_mock.await_count == 0
    assert gateway.marked == []


def test_closed_failed_attempt_is_uncertain_even_with_clean_window(
    isolated_state: AuditLogger,
) -> None:
    """RA-B2-05: a closed executive_failed attempt whose window shows only
    read-only tools is STILL uncertain — a missing tool_invocation row is
    not proof of zero effects, so the old 'demonstrably clean → retry'
    path is gone by design. The honest degradation is a human reconcile."""
    session = "email:t-m1"
    _attempt_row(isolated_state, "m1", session, 1)
    isolated_state.log(
        "tool_invocation",
        "read-only fetch",
        session_id=session,
        actor="executive",
        details={"tool": "google_workspace__get_gmail_message_content"},
    )
    _close_row(isolated_state, "m1", session, 1)
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "uncertain"
    assert exec_mock.await_count == 0
    assert gateway.marked == []


def test_fetch_failure_budget_exhausted_reports_final(
    isolated_state: AuditLogger,
) -> None:
    """Durable fetch-failure markers bound retries across restarts — no
    in-process counter is consulted, so clearing process state (restart
    analogue) cannot reset the budget."""
    audit = isolated_state
    tok = _test_scope(audit).token
    for n in range(1, 4):  # registry default max_attempts = 3
        audit.log(
            "email_fetch_failed",
            f"Content fetch failed for email m1 (failure {n})",
            actor="email",
            session_id="email:t-m1",
            details={"message_id": "m1", "fetch_failure": n},
            dedup_key=f"email_fetch_fail@{tok}:m1:{n}",
        )
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "failed_final"
    assert exec_mock.await_count == 0
    assert gateway.marked == []


def test_tool_calls_of_other_messages_never_unlock_retry(
    isolated_state: AuditLogger,
) -> None:
    """RA-B2-07: two messages sharing one thread/session — m2's bracket
    interleaves m1's attempt rows. The old session-range window scan
    misattributed boundaries and could hide m1's effect; now ANY exec
    attempt on m1 is terminal-uncertain, regardless of what surrounds it."""
    session = "email:t-m1"  # same thread — the hard case
    _attempt_row(isolated_state, "other", session, 1)
    isolated_state.log(
        "tool_invocation",
        "send for the other mail",
        session_id=session,
        actor="executive",
        details={"tool": "google_workspace__send_gmail_message"},
    )
    _close_row(isolated_state, "other", session, 1)
    _attempt_row(isolated_state, "m1", session, 1)
    _close_row(isolated_state, "m1", session, 1)  # executive_failed
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "uncertain"
    assert exec_mock.await_count == 0
    assert gateway.marked == []


# ------------------------------------------------------- settings bounds


def test_mail_settings_have_bounded_validation() -> None:
    from openexecutive.bo.settings.registry import (
        REGISTRY,
        SettingValidationError,
    )

    for key, lo, hi in (
        ("bo.mail.poll.max_pages_per_cycle", 1, 50),
        ("bo.mail.processing.max_attempts", 1, 10),
    ):
        spec = REGISTRY[key]
        assert spec.validate(lo) == lo
        assert spec.validate(hi) == hi
        with pytest.raises(SettingValidationError):
            spec.validate(0)  # mandatory control cannot be disabled
        with pytest.raises(SettingValidationError):
            spec.validate(hi + 1)


def test_mail_setting_falls_back_to_default_on_store_failure() -> None:
    assert poller._mail_setting("bo.mail.poll.max_pages_per_cycle") == 10
    assert poller._mail_setting("bo.mail.processing.max_attempts") == 3


# ------------------------------------------------------- REM-AUDIT-03
#
# Reproducerea reziduurilor raportate de auditul SOL02 la head 021f7ec.


def test_terminal_messages_fenced_out_of_enumeration(
    isolated_state: AuditLogger,
) -> None:
    """RA-B1-01: a page of terminal (uncertain) mail must not re-occupy
    the leading unread pages forever — each gets the OE-Terminal label
    provider-side (stays UNREAD — never a false success), and the unread
    query itself carries the `-label:` exclusion."""
    session = "email:t-x"
    for i in range(3):
        _attempt_row(isolated_state, f"term{i}", session, 1)  # open → uncertain
    gateway = FakeGateway(
        pages=[_page("term0", "term1", "term2", "real1")],
        contents={
            "term0": _raw_email("a@b.com"),
            "term1": _raw_email("a@b.com"),
            "term2": _raw_email("a@b.com"),
            "real1": _raw_email("alice@example.com"),
        },
    )
    exec_mock = _poll(gateway)
    # Every terminal fenced via add_label_ids, none marked read.
    assert sorted(mid for mid, _ in gateway.labeled) == [
        "term0", "term1", "term2"
    ]
    assert all(ids == ["Label_99"] for _, ids in gateway.labeled)
    for mid in ("term0", "term1", "term2"):
        assert mid not in gateway.marked
    # Legit mail behind them still processed in the same bounded cycle.
    assert "real1" in gateway.marked
    assert exec_mock.await_count == 1
    # The provider query itself excludes the fenced label.
    assert "-label:OE-Terminal" in gateway.search_args[0]["query"]


def test_terminal_label_created_when_missing(
    isolated_state: AuditLogger,
) -> None:
    """First fence on a mailbox without the label: list → miss → create
    → label applied with the created id."""
    session = "email:t-x"
    _attempt_row(isolated_state, "term0", session, 1)
    calls: list[str] = []

    class G(FakeGateway):
        async def call_tool(self, payload):  # type: ignore[override]
            calls.append(payload["name"])
            return await super().call_tool(payload)

    gateway = G(
        pages=[_page("term0")],
        contents={"term0": _raw_email("a@b.com")},
    )
    _poll(gateway)
    assert "google_workspace__list_gmail_labels" in calls
    assert "google_workspace__manage_gmail_label" in calls
    assert gateway.labeled == [("term0", ["Label_99"])]


def test_orphan_processed_marker_is_uncertain(
    isolated_state: AuditLogger,
) -> None:
    """RA-B2-01a: an orphan `email_processed@{scope}` marker (journal
    lost the row it points to) is NOT proof of success — the message
    must be fenced uncertain, not mark-read as if processed."""
    audit = isolated_state
    tok = _test_scope(audit).token
    row_id = audit.log(
        "integration_inbound",
        "Processed email from a@b.com",
        actor="email",
        session_id="email:t-m1",
        details={"channel": "email", "message_id": "m1", "outcome": "processed"},
        dedup_key=f"email_processed@{tok}:m1",
    )
    # Simulate journal loss: the row is gone, the marker remains.
    from openexecutive.audit.logger import _get_conn

    with _get_conn(audit._db_path) as conn:  # noqa: SLF001
        conn.execute("DELETE FROM audit_log WHERE id = ?", (row_id,))
    marker = audit.dedup_lookup(f"email_processed@{tok}:m1")
    assert marker is not None and not marker["journal_row_present"]

    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "uncertain"
    assert exec_mock.await_count == 0
    assert gateway.marked == []


def test_unknown_tool_counts_as_effectful(isolated_state: AuditLogger) -> None:
    """RA-B2-04: a tool name outside every known read-only set — including
    broadcast/department dispatch and dynamically registered tools — is
    conservatively effectful and blocks the retry."""
    session = "email:t-m1"
    for i, tool in enumerate((
        "send_company_broadcast",
        "send_department_message",
        "acme_corp__post_webhook",   # unknown dynamic tool
        "run_workflow",              # internal mutator, not an egress tool
    )):
        mid = f"m{i}"
        _attempt_row(isolated_state, mid, session, 1)
        isolated_state.log(
            "tool_invocation",
            tool,
            session_id=session,
            actor="executive",
            details={"tool": tool},
        )
        _close_row(isolated_state, mid, session, 1)
        gateway = FakeGateway(contents={mid: _raw_email("a@b.com")})
        outcome, exec_mock = _handle(gateway, message_id=mid)
        assert outcome == "uncertain", tool
        assert exec_mock.await_count == 0


def test_journal_read_error_is_not_absence(
    isolated_state: AuditLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RA-B2-02: a failing journal read must not count as 'no attempts' —
    the message defers (failed), Executive never runs."""

    def _boom(*a: Any, **k: Any) -> int:
        raise RuntimeError("journal corrupt")

    monkeypatch.setattr(
        isolated_state, "count_dedup_prefix", _boom
    )
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "failed"
    assert exec_mock.await_count == 0
    assert gateway.marked == []


def test_deep_history_does_not_reset_budget_or_hide_effect(
    isolated_state: AuditLogger,
) -> None:
    """RA-B2-02: with >1000 unrelated rows between the attempt evidence
    and now, the truncated-window reasoning of the old code would have
    seen 'no attempts'. The dedup-keyed count still enforces the budget
    and still finds the effectful tool inside the window."""
    audit = isolated_state
    session = "email:t-m1"
    _attempt_row(audit, "m1", session, 1)
    audit.log(
        "tool_invocation",
        "send_gmail_message",
        session_id=session,
        actor="executive",
        details={"tool": "google_workspace__send_gmail_message"},
    )
    _close_row(audit, "m1", session, 1)
    # Bury the evidence under 1100 unrelated rows in the same session —
    # the old limit=1000 query would have returned none of the attempt
    # rows and retried the send.
    for i in range(1100):
        audit.log(
            "tool_invocation",
            f"noise {i}",
            session_id=session,
            actor="executive",
            details={"tool": "google_workspace__search_gmail_messages"},
        )
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "uncertain"
    assert exec_mock.await_count == 0


def test_concurrent_handle_runs_executive_once(
    isolated_state: AuditLogger,
) -> None:
    """RA-B2-03: two racing handlers for the same message — the atomic
    dedup claim admits exactly one owner; the loser sees 'claimed' and
    never runs the Executive. Exactly one external effect possible."""
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    barrier = asyncio.Event()
    calls = {"n": 0}

    async def _exec(*a: Any, **k: Any) -> None:
        calls["n"] += 1
        barrier.set()
        await asyncio.sleep(0)  # widen the interleave window

    async def _two() -> list[str]:
        with (
            patch.object(poller, "get_settings", return_value=_settings()),
            patch.object(poller, "_run_executive", new=_exec),
        ):
            return await asyncio.gather(
                poller._handle_email(gateway, "m1", "t-m1", EXEC),
                poller._handle_email(gateway, "m1", "t-m1", EXEC),
            )

    outcomes = asyncio.run(_two())
    assert calls["n"] == 1
    # Winner processed; loser bailed on the claim or saw the open attempt
    # — either way no second run, exactly one mark-read.
    assert "processed" in outcomes
    assert set(outcomes) - {"processed", "claimed", "uncertain"} == set()
    assert gateway.marked.count("m1") == 1


def test_poll_once_concurrent_cycles_single_effect(
    isolated_state: AuditLogger,
) -> None:
    """RA-B2-03 at the loop level: two gather'd poll_once calls over the
    same unread page produce exactly one Executive run and one effectful
    dispatch path — the in-process lock serializes the frontier."""
    gateway = FakeGateway(
        pages=[_page("m1"), _page("m1")],
        contents={"m1": _raw_email("a@b.com")},
    )
    calls = {"n": 0}

    async def _exec(*a: Any, **k: Any) -> None:
        calls["n"] += 1
        await asyncio.sleep(0.01)

    async def _two_cycles() -> None:
        with (
            patch.object(poller, "get_settings", return_value=_settings()),
            patch.object(poller, "_run_executive", new=_exec),
            patch(
                "openexecutive.clients.slots.is_restore_blocked",
                return_value=False,
            ),
        ):
            await asyncio.gather(
                poller.poll_once(gateway), poller.poll_once(gateway)
            )

    asyncio.run(_two_cycles())
    assert calls["n"] == 1


# ------------------------------------------------------- REM-AUDIT-07
#
# Fail-closed attempt lifecycle: an Executive attempt row is always
# terminal (uncertain) — auto-retry is gone. Claim identity is purely
# durable; evidence rows carry the explicit attempt_ref.


def test_deceptive_read_shaped_names_are_effectful() -> None:
    """RA-B4-01: a mutator behind a get_/list_/check_-shaped MCP name must
    NEVER classify as read-only — only exact registered identifiers do."""
    from openexecutive.orchestrator.tool_effects import (
        has_external_effect,
        is_read_only_tool,
    )

    for name in (
        "acme_corp__get_inventory",
        "acme_corp__list_things",
        "acme_corp__check_stock",
        "acme_corp__fetch_report",
        "google_workspace__get_secret_keys",  # right prefix, wrong server row
        "evil__search_and_destroy",
        "",
    ):
        assert not is_read_only_tool(name), name
        assert has_external_effect(name), name


def test_registered_read_only_names_still_classify() -> None:
    """Positive control: the exact attested identifiers remain read-only."""
    from openexecutive.orchestrator.tool_effects import (
        has_external_effect,
        is_read_only_tool,
    )

    for name in (
        "consult_specialist",
        "web_search",
        "google_workspace__search_gmail_messages",
        "google_workspace__get_gmail_message_content",
        "google_workspace__list_gmail_labels",
    ):
        assert is_read_only_tool(name), name
        assert not has_external_effect(name), name


def test_divergent_histories_converge_on_one_claim_key(
    isolated_state: AuditLogger,
) -> None:
    """RA-B2-06: two "processes" with different local histories must race
    on the SAME durable claim key. Process A suffered a fetch failure
    (now journaled, not a volatile counter); both then evaluate
    attempt_count=0 and claim email_attempt@{scope}:m1:1 — BEGIN IMMEDIATE
    serializes them, exactly one Executive run."""
    audit = isolated_state
    tok = _test_scope(audit).token
    # A's fetch failure — durable, visible to every process.
    audit.log(
        "email_fetch_failed",
        "Content fetch failed for email m1",
        actor="email",
        session_id="email:t-m1",
        details={"message_id": "m1", "fetch_failure": 1},
        dedup_key=f"email_fetch_fail@{tok}:m1:1",
    )
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    calls = {"n": 0}

    async def _exec(*a: Any, **k: Any) -> None:
        calls["n"] += 1
        await asyncio.sleep(0)

    async def _two() -> list[str]:
        with (
            patch.object(poller, "get_settings", return_value=_settings()),
            patch.object(poller, "_run_executive", new=_exec),
        ):
            return await asyncio.gather(
                poller._handle_email(gateway, "m1", "t-m1", EXEC),
                poller._handle_email(gateway, "m1", "t-m1", EXEC),
            )

    outcomes = asyncio.run(_two())
    assert calls["n"] == 1
    assert "processed" in outcomes
    # ONE attempt marker — divergent in-process counters cannot fork the
    # key space any more (there are none).
    assert audit.count_dedup_prefix(f"email_attempt@{tok}:m1:") == 1
    assert gateway.marked.count("m1") == 1


def test_attempt_ref_stamped_on_turn_rows(
    isolated_state: AuditLogger,
) -> None:
    """RA-B4-02 evidence fix: rows emitted inside the attempt scope —
    including ones with session_id NULL — carry the explicit
    attempt_ref for operator attribution."""
    from openexecutive.audit import log_event

    async def _exec_with_orphan_row(*a: Any, **k: Any) -> None:
        # A broadcast-style emit with NO bound turn context.
        log_event(
            "tool_invocation",
            "send_company_broadcast",
            actor="executive",
            details={"tool": "send_company_broadcast"},
        )

    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, _ = _handle(gateway, run_exec=AsyncMock(side_effect=_exec_with_orphan_row))
    assert outcome == "processed"

    rows = isolated_state.query(event_type="tool_invocation", limit=10)
    assert len(rows) == 1
    row = rows[0]
    assert row.session_id is None  # the broadcast-style NULL session
    ref = (row.details or {}).get("attempt_ref")
    tok = _test_scope(isolated_state).token
    assert ref is not None and ref.startswith(f"{tok}:m1:1:")


def test_uncommitted_close_row_is_uncertain(
    isolated_state: AuditLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Executive success whose close row the journal swallows
    (log()→None) must NOT be presented as processed — the next-cycle
    evidence is an open attempt, i.e. uncertain. Fail closed now."""
    real_log = isolated_state.log

    def _swallow_close(event_type: str, *a: Any, **k: Any) -> Any:
        if event_type == "email_attempt_result":
            return None
        return real_log(event_type, *a, **k)

    monkeypatch.setattr(isolated_state, "log", _swallow_close)
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "uncertain"
    assert exec_mock.await_count == 1
    assert gateway.marked == []


def test_uncommitted_processed_marker_is_uncertain(
    isolated_state: AuditLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """log()→None on the email_processed marker must not degrade to a
    silent consume — no mark-read without durable proof of processing."""
    real_log = isolated_state.log
    tok = _test_scope(isolated_state).token

    def _swallow_marker(event_type: str, *a: Any, **k: Any) -> Any:
        if k.get("dedup_key") == f"email_processed@{tok}:m1":
            return None
        return real_log(event_type, *a, **k)

    monkeypatch.setattr(isolated_state, "log", _swallow_marker)
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "uncertain"
    assert exec_mock.await_count == 1
    assert gateway.marked == []


def test_skip_without_durable_marker_stays_unread(
    isolated_state: AuditLogger, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A policy skip whose marker never committed must defer — marking
    read without evidence would make the mail vanish with no trace."""
    real_log = isolated_state.log
    tok = _test_scope(isolated_state).token

    def _swallow_marker(event_type: str, *a: Any, **k: Any) -> Any:
        if k.get("dedup_key") == f"email_processed@{tok}:m1":
            return None
        return real_log(event_type, *a, **k)

    monkeypatch.setattr(isolated_state, "log", _swallow_marker)
    gateway = FakeGateway(contents={"m1": _raw_email(EXEC)})  # self-sent
    outcome, exec_mock = _handle(gateway)
    assert outcome == "failed"
    assert exec_mock.await_count == 0
    assert gateway.marked == []


def test_null_session_tool_row_does_not_unlock_retry(
    isolated_state: AuditLogger,
) -> None:
    """RA-B4-02: a broadcast-style tool row with session_id NULL was
    invisible to the old session-range window scan and unlocked a retry.
    Attempt presence alone now decides — never the window."""
    session = "email:t-m1"
    _attempt_row(isolated_state, "m1", session, 1)
    isolated_state.log(
        "tool_invocation",
        "send_company_broadcast",
        actor="executive",
        session_id=None,  # emitted outside a bound turn
        details={"tool": "send_company_broadcast"},
    )
    _close_row(isolated_state, "m1", session, 1)
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "uncertain"
    assert exec_mock.await_count == 0
