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
returns an explicit outcome per message: failures stay unread and are
retried within a bounded attempt budget (persisted in the audit journal
so it survives restarts), and a prior attempt that already produced an
externally-visible tool call is never re-run — it is reported
``uncertain`` for a human instead of risking a duplicate send.
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
        self._mark_attempts = 0

    async def call_tool(self, payload: dict[str, Any]) -> str:
        name = payload["name"]
        args = payload["arguments"]
        if name.endswith("search_gmail_messages"):
            self.search_args.append(args)
            return self.pages.popleft() if self.pages else ""
        if name.endswith("get_gmail_message_content"):
            self.fetched.append(args["message_id"])
            return self.contents.get(args["message_id"], "")
        if name.endswith("modify_gmail_message_labels"):
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
    poller._processed_ids.clear()
    poller._retry_counts.clear()
    monkeypatch.setattr(poller, "_EXTERNAL_EFFECT_TOOLS", None)
    yield audit
    set_audit_logger(None)
    poller._processed_ids.clear()
    poller._retry_counts.clear()


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


def _attempt_row(audit: AuditLogger, message_id: str, session_id: str, n: int) -> None:
    audit.log(
        "email_process_attempt",
        f"Processing attempt {n} for email {message_id}",
        actor="email",
        session_id=session_id,
        details={"message_id": message_id, "attempt": n},
        dedup_key=f"email_attempt:{message_id}:{n}",
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


def test_executive_failure_leaves_message_unread_and_retried() -> None:
    """B2 core: a failed Executive turn must NOT consume the message —
    no mark-read, and the next cycle retries it."""
    gateway = FakeGateway(
        contents={"m1": _raw_email("a@b.com")},
    )
    failing = AsyncMock(side_effect=RuntimeError("LLM exploded"))
    outcome, _ = _handle(gateway, run_exec=failing)
    assert outcome == "failed"
    assert gateway.marked == []

    # Retry on a later cycle succeeds and only then is it consumed.
    outcome, ok = _handle(gateway)
    assert outcome == "processed"
    assert ok.await_count == 1
    assert gateway.marked == ["m1"]


def test_empty_fetch_is_not_consumed() -> None:
    gateway = FakeGateway(contents={"m1": ""})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "failed"
    assert exec_mock.await_count == 0
    assert gateway.marked == []


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
    """A prior attempt that already sent mail must never be re-run —
    reported uncertain, left unread for a human."""
    session = "email:t-m1"
    _attempt_row(isolated_state, "m1", session, 1)
    isolated_state.log(
        "tool_invocation",
        "send_gmail_message",
        session_id=session,
        actor="executive",
        details={"tool": "google_workspace__send_gmail_message"},
    )
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "uncertain"
    assert exec_mock.await_count == 0
    assert gateway.marked == []


def test_prior_attempt_without_effect_retries(isolated_state: AuditLogger) -> None:
    """A failed attempt that only ran read-only tools is safely retried."""
    session = "email:t-m1"
    _attempt_row(isolated_state, "m1", session, 1)
    isolated_state.log(
        "tool_invocation",
        "read-only fetch",
        session_id=session,
        actor="executive",
        details={"tool": "google_workspace__get_gmail_message_content"},
    )
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "processed"
    assert exec_mock.await_count == 1


def test_attempt_budget_exhausted_reports_final(isolated_state: AuditLogger) -> None:
    """The persisted attempt rows bound retries across restarts — the
    in-process counter is cleared (restart analogue) and the journal
    alone must enforce the budget."""
    session = "email:t-m1"
    for n in range(1, 4):  # registry default max_attempts = 3
        _attempt_row(isolated_state, "m1", session, n)
    poller._retry_counts.clear()  # "restart": in-process state gone
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "failed_final"
    assert exec_mock.await_count == 0
    assert gateway.marked == []


def test_tool_calls_of_other_messages_do_not_poison_this_one(
    isolated_state: AuditLogger,
) -> None:
    """Attribution is per-message: a send on another mail's attempt window
    must not poison this message's retry."""
    session = "email:t-m1"  # same thread — the hard case
    _attempt_row(isolated_state, "other", session, 1)
    isolated_state.log(
        "tool_invocation",
        "send for the other mail",
        session_id=session,
        actor="executive",
        details={"tool": "google_workspace__send_gmail_message"},
    )
    _attempt_row(isolated_state, "m1", session, 1)  # m1's failed attempt, no effects
    gateway = FakeGateway(contents={"m1": _raw_email("a@b.com")})
    outcome, exec_mock = _handle(gateway)
    assert outcome == "processed"
    assert exec_mock.await_count == 1


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
