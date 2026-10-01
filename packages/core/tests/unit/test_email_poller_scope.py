"""RA11-B01 — scoped processing identity for the email poller.

The pre-scope poller keyed every durable marker by the bare provider
message id, so a second mailbox/client sharing a journal could consume
the first context's evidence. These tests pin the new contract:

- ``resolve`` refuses unambiguously (legacy evidence, foreign tenant,
  mailbox/client drift) instead of degrading to a guess;
- the operator attestation ``bo.mail.scope.bound_mailbox`` bridges a
  mailbox/client change by binding a new version — never a tenant;
- ``still_current`` refuses mutations under a drifted context;
- ``rebind_for_client`` re-points only the client component of an
  existing binding after a slot restore;
- ``scope_blocked`` mail stays unread and journals its refusal;
- two scopes process the same ``message_id`` independently.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

import openexecutive.integrations.email_poller as poller
import openexecutive.integrations.mail_scope as mail_scope
from openexecutive.audit import AuditLogger, set_audit_logger
from openexecutive.people import store as people_store

TENANT = "local"  # configured_tenant() default when BO_TENANT_ID unset
MAIL_A = "acct-a@example.com"
MAIL_B = "acct-b@example.com"


def _raw(from_addr: str) -> str:
    return (
        f"Subject: Hi\nFrom: {from_addr}\n\n--- BODY ---\nBody.\n"
    )


class FakeGateway:
    def __init__(self, contents: dict[str, str]) -> None:
        self.contents = dict(contents)
        self.fetched: list[str] = []
        self.marked: list[str] = []

    async def call_tool(self, payload: dict[str, Any]) -> str:
        name = payload["name"]
        args = payload["arguments"]
        if name.endswith("get_gmail_message_content"):
            self.fetched.append(args["message_id"])
            return self.contents.get(args["message_id"], "")
        if name.endswith("modify_gmail_message_labels"):
            self.marked.append(args["message_id"])
            return "OK"
        raise AssertionError(f"unexpected tool call {name}")


class Ctx:
    """One processing context: mailbox + optional client slot sentinel +
    per-key overrides for the BO settings store."""

    def __init__(
        self,
        tmp_path: Path,
        name: str,
        mailbox: str,
        *,
        client_slug: str | None = None,
        attested: str = "",
    ) -> None:
        company = tmp_path / f"company-{name}"
        (company / "_client_slots").mkdir(parents=True, exist_ok=True)
        if client_slug:
            (company / "_client_slots" / ".active_client").write_text(
                client_slug
            )
        self.settings = SimpleNamespace(
            exec_email_address=mailbox,
            email_poll_interval_seconds=60,
            company_profile_path=company / "profile.yaml",
        )
        self.mailbox = mailbox
        self.company = company
        self.values = {"bo.mail.scope.bound_mailbox": attested}

    def _consume_attestation(self, expected: str) -> bool:
        """Faithful single-shot model of the store-backed consume:
        clears a matching live value, reports True when none remains."""
        v = self.values.get("bo.mail.scope.bound_mailbox", "")
        if isinstance(v, str) and v.strip():
            if v.strip().lower() != expected:
                return False
            self.values["bo.mail.scope.bound_mailbox"] = ""
        return True

    def enter(self, exec_mock: Any | None = None):
        from openexecutive.bo.settings.registry import REGISTRY

        patches = [
            patch.object(poller, "get_settings",
                         return_value=self.settings),
            patch.object(
                poller, "_mail_setting",
                lambda key: self.values.get(key, REGISTRY[key].default),
            ),
            patch.object(
                poller, "_consume_scope_attestation",
                self._consume_attestation,
            ),
        ]
        if exec_mock is not None:
            patches.append(
                patch.object(poller, "_run_executive", new=exec_mock))
        return _Stack(patches)

    def set_client(self, slug: str | None) -> None:
        sentinel = self.company / "_client_slots" / ".active_client"
        if slug is None:
            sentinel.unlink(missing_ok=True)
        else:
            sentinel.write_text(slug)


class _Stack:
    def __init__(self, patches: list[Any]) -> None:
        self._p = patches

    def __enter__(self):
        for p in self._p:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._p):
            p.stop()
        return False


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    audit = AuditLogger(db_path=tmp_path / "audit.db")
    set_audit_logger(audit)
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    people_store.initialize_db()
    monkeypatch.setattr(
        "openexecutive.bo.db.DB_PATH", tmp_path / "bo.db"
    )
    from openexecutive.bo import turn_barrier

    turn_barrier.initialize_db()
    poller.reset_mail_caches()
    yield audit
    set_audit_logger(None)
    poller.reset_mail_caches()


def _resolve(audit: AuditLogger, ctx: Ctx):
    with ctx.enter():
        return poller._resolve_scope(audit)


def _handle(ctx: Ctx, gw: FakeGateway, mid: str = "m1",
            exec_mock: Any | None = None):
    m = exec_mock if exec_mock is not None else AsyncMock()
    with ctx.enter(exec_mock=m):
        outcome = asyncio.run(
            poller._handle_email(gw, mid, f"t-{mid}", ctx.mailbox))
    return outcome, m


# ---------------------------------------------------------- resolve lattice


def test_unbound_journal_binds_on_first_use(isolated_state, tmp_path) -> None:
    ctx = Ctx(tmp_path, "a", MAIL_A)
    scope, refusal = _resolve(isolated_state, ctx)
    assert scope is not None and refusal == ""
    assert scope.mailbox == MAIL_A and scope.tenant == TENANT
    # Stable across re-resolution (restart analogue).
    scope2, _ = _resolve(isolated_state, ctx)
    assert scope2 is not None and scope2.token == scope.token


def test_legacy_journal_refuses_until_attested(isolated_state, tmp_path) -> None:
    isolated_state.log(
        "integration_inbound", "legacy", actor="email",
        details={"message_id": "m1", "outcome": "processed"},
        dedup_key="email_processed:m1",
    )
    ctx = Ctx(tmp_path, "a", MAIL_A)
    scope, refusal = _resolve(isolated_state, ctx)
    assert scope is None and refusal == "legacy_journal_unbound"
    # Attestation binds v1 — but the legacy message stays blocked (below).
    ctx.values["bo.mail.scope.bound_mailbox"] = MAIL_A
    scope2, refusal2 = _resolve(isolated_state, ctx)
    assert scope2 is not None and refusal2 == ""


def test_bound_journal_refuses_foreign_mailbox(isolated_state, tmp_path) -> None:
    ctx_a = Ctx(tmp_path, "a", MAIL_A)
    scope, _ = _resolve(isolated_state, ctx_a)
    assert scope is not None
    ctx_b = Ctx(tmp_path, "b", MAIL_B)
    scope_b, refusal = _resolve(isolated_state, ctx_b)
    assert scope_b is None and refusal == "scope_mismatch"
    # Attestation bridges the mailbox change with a NEW binding version.
    ctx_b.values["bo.mail.scope.bound_mailbox"] = MAIL_B
    scope_b2, refusal2 = _resolve(isolated_state, ctx_b)
    assert scope_b2 is not None and refusal2 == ""
    assert scope_b2.token != scope.token and scope_b2.version == 2


def test_tenant_mismatch_never_bridged(isolated_state,
                                     monkeypatch, tmp_path) -> None:
    ctx_a = Ctx(tmp_path, "a", MAIL_A)
    scope, _ = _resolve(isolated_state, ctx_a)
    assert scope is not None
    monkeypatch.setenv("BO_TENANT_ID", "other-tenant")
    try:
        scope2, refusal = _resolve(isolated_state, ctx_a)
        assert scope2 is None and refusal == "tenant_mismatch"
    finally:
        monkeypatch.delenv("BO_TENANT_ID")


def test_forged_binding_row_is_not_trusted(isolated_state, tmp_path) -> None:
    """A bare mail_scope_bound event with no dedup marker (e.g. injected
    via a path that cannot mint markers) is never adopted."""
    isolated_state.log(
        "mail_scope_bound", "forged", actor="unknown",
        details={"version": 9, "token": "s1.x", "tenant": TENANT,
                 "client_key": "install:evil", "mailbox": MAIL_A},
    )
    ctx = Ctx(tmp_path, "a", MAIL_A)
    scope, refusal = _resolve(isolated_state, ctx)
    assert scope is None and refusal == "binding_malformed"


# -------------------------------------------------------- handler outcomes


def test_scope_refusal_stays_unread_and_journaled(isolated_state, tmp_path) -> None:
    isolated_state.log(
        "email_process_attempt", "legacy attempt", actor="email",
        session_id="email:t-m1",
        details={"message_id": "m1", "attempt": 1, "owner": "old"},
        dedup_key="email_attempt:m1:1",
    )
    ctx = Ctx(tmp_path, "a", MAIL_A)
    gw = FakeGateway({"m1": _raw("a@x.com")})
    outcome, exec_mock = _handle(ctx, gw)
    assert outcome == "scope_blocked"
    assert exec_mock.await_count == 0
    assert gw.fetched == [] and gw.marked == []
    rows = isolated_state.query(
        event_type="integration_inbound", limit=10)
    assert any(
        (r.details or {}).get("outcome") == "scope_refused"
        and (r.details or {}).get("reason") == "legacy_journal_unbound"
        for r in rows
    )


def test_two_scopes_process_same_mid_independently(isolated_state, tmp_path) -> None:
    """The RA11-B01 core: account A's processed marker must not consume
    account B's same-id message — B refuses unattested, then processes
    independently once the operator attests the mailbox change."""
    ctx_a = Ctx(tmp_path, "a", MAIL_A)
    gw_a = FakeGateway({"m1": _raw("a@x.com")})
    oa, ea = _handle(ctx_a, gw_a)
    assert oa == "processed" and ea.await_count == 1
    tok_a = mail_scope.bound_binding(isolated_state)["token"]

    ctx_b = Ctx(tmp_path, "b", MAIL_B)
    gw_b = FakeGateway({"m1": _raw("b@y.com")})
    ob, eb = _handle(ctx_b, gw_b)
    assert ob == "scope_blocked"
    assert eb.await_count == 0 and gw_b.fetched == [] and gw_b.marked == []

    ctx_b.values["bo.mail.scope.bound_mailbox"] = MAIL_B
    ob2, eb2 = _handle(ctx_b, gw_b)
    tok_b = mail_scope.bound_binding(isolated_state)["token"]
    assert ob2 == "processed" and eb2.await_count == 1
    assert tok_b != tok_a
    assert isolated_state.dedup_lookup(
        f"email_processed@{tok_b}:m1") is not None


def test_scope_drift_blocks_mark_read(isolated_state, tmp_path) -> None:
    """A scope captured under context A must not mutate the mailbox after
    the live context moved to client B (mid-operation client switch)."""
    ctx_a = Ctx(tmp_path, "a", MAIL_A,
                client_slug="client-a")
    scope_a, _ = _resolve(isolated_state, ctx_a)
    assert scope_a is not None

    ctx_b = Ctx(tmp_path, "b", MAIL_A)
    ctx_b.set_client("client-b")  # same journal, different client
    gw = FakeGateway({"m1": _raw("a@x.com")})
    with ctx_b.enter():
        ok = asyncio.run(
            poller._mark_read(gw, "m1", MAIL_A, scope_a, isolated_state))
    assert ok is False and gw.marked == []


def test_still_current_and_rebind_for_client(isolated_state, tmp_path) -> None:
    """The slot-restore hook re-points only the client component; a scope
    captured before the restore is stale afterwards."""
    from openexecutive.integrations.mail_scope import rebind_for_client

    ctx_a = Ctx(tmp_path, "a", MAIL_A,
                client_slug="client-a")
    scope_a, _ = _resolve(isolated_state, ctx_a)
    assert scope_a is not None and scope_a.client_key == "slot:client-a"
    with ctx_a.enter():
        assert mail_scope.still_current(
            isolated_state, scope_a, tenant=TENANT,
            client_slug="client-a", mailbox=MAIL_A)

    # Restore to client-b: activation is the attestation.
    assert rebind_for_client(
        isolated_state, tenant=TENANT, client_slug="client-b")
    bound = mail_scope.bound_binding(isolated_state)
    assert bound["client_key"] == "slot:client-b"
    assert bound["mailbox"] == MAIL_A and bound["version"] == 2
    # The pre-restore scope token is stale for the new context.
    ctx_b = Ctx(tmp_path, "b", MAIL_A,
                client_slug="client-b")
    with ctx_b.enter():
        assert not mail_scope.still_current(
            isolated_state, scope_a, tenant=TENANT,
            client_slug="client-b", mailbox=MAIL_A)
        scope_b, refusal = poller._resolve_scope(isolated_state)
    assert scope_b is not None and scope_b.client_key == "slot:client-b"


def test_rebind_never_binds_unbound_or_crosses_tenant(
    isolated_state, monkeypatch, tmp_path,
) -> None:
    from openexecutive.integrations.mail_scope import rebind_for_client

    assert not rebind_for_client(
        isolated_state, tenant=TENANT, client_slug="client-b")

    ctx_a = Ctx(tmp_path, "a", MAIL_A,
                client_slug="client-a")
    scope_a, _ = _resolve(isolated_state, ctx_a)
    assert scope_a is not None
    assert not rebind_for_client(
        isolated_state, tenant="other-tenant", client_slug="client-b")
    assert mail_scope.bound_binding(
        isolated_state)["client_key"] == "slot:client-a"


# -------------------------------------------------- review-hardened edges


def test_attestation_is_single_shot(isolated_state, tmp_path) -> None:
    """A forgotten attestation must not silently adopt a FUTURE foreign
    journal: the value is consumed when it authorizes a bind."""
    ctx_a = Ctx(tmp_path, "a", MAIL_A)
    scope_a, _ = _resolve(isolated_state, ctx_a)
    assert scope_a is not None

    ctx_b = Ctx(tmp_path, "b", MAIL_B)
    ctx_b.values["bo.mail.scope.bound_mailbox"] = MAIL_B
    scope_b, refusal = _resolve(isolated_state, ctx_b)
    assert scope_b is not None and refusal == ""
    # Consumed on use — no standing authorization survives the bind.
    assert ctx_b.values["bo.mail.scope.bound_mailbox"] == ""

    # A third mailbox is now refused — the consumed attestation does not
    # stretch to it.
    ctx_c = Ctx(tmp_path, "c", "acct-c@example.com")
    scope_c, refusal_c = _resolve(isolated_state, ctx_c)
    assert scope_c is None and refusal_c == "scope_mismatch"


def test_unbound_journal_with_scoped_evidence_refuses(
    isolated_state, tmp_path,
) -> None:
    """Binding rows lost but scoped markers survived → refuse, never
    re-bind v1 over a foreign history."""
    isolated_state.log(
        "integration_inbound", "prior scoped evidence", actor="email",
        details={"message_id": "old", "outcome": "processed",
                 "scope": "s1.deadbeefdeadbeef"},
        dedup_key="email_processed@s1.deadbeefdeadbeef:old",
    )
    ctx = Ctx(tmp_path, "a", MAIL_A)
    scope, refusal = _resolve(isolated_state, ctx)
    assert scope is None and refusal == "legacy_journal_unbound"


def test_orphan_binding_marker_blocks_first_use(
    isolated_state, tmp_path,
) -> None:
    """An orphaned ``mail_scope_binding:v1`` marker (its row wiped) is
    prior evidence — the journal is not a blank slate."""
    import sqlite3

    row_id = isolated_state.log(
        "mail_scope_bound", "lost binding", actor="email-scope",
        details={"version": 1, "token": "s1.t", "tenant": TENANT,
                 "client_key": "install:x", "mailbox": MAIL_A},
        dedup_key="mail_scope_binding:v1",
    )
    assert row_id is not None
    # Wipe the row — the marker is now an orphan, exactly the "journal
    # restored without the binding row" case.
    with sqlite3.connect(isolated_state._db_path) as conn:
        conn.execute("DELETE FROM audit_log WHERE id = ?", (row_id,))
    ctx = Ctx(tmp_path, "a", MAIL_A)
    scope, refusal = _resolve(isolated_state, ctx)
    assert scope is None and refusal == "legacy_journal_unbound"


def test_newest_unverified_binding_refuses_no_fallback(
    isolated_state, tmp_path,
) -> None:
    """A forged/torn NEWER binding row must refuse the journal — not
    silently roll back to the older verified scope."""
    ctx = Ctx(tmp_path, "a", MAIL_A)
    scope, _ = _resolve(isolated_state, ctx)
    assert scope is not None
    isolated_state.log(
        "mail_scope_bound", "forged v2", actor="unknown",
        details={"version": 2, "token": "s1.forged", "tenant": TENANT,
                 "client_key": "install:evil", "mailbox": MAIL_A},
    )
    scope2, refusal = _resolve(isolated_state, ctx)
    assert scope2 is None and refusal == "binding_malformed"


def test_malformed_binding_field_types_refuse(
    isolated_state, tmp_path,
) -> None:
    """A binding row with corrupt field types refuses cleanly instead of
    crashing the resolve."""
    row_id = isolated_state.log(
        "mail_scope_bound", "corrupt", actor="unknown",
        details={"version": "x", "token": "s1.t", "tenant": TENANT,
                 "client_key": "slot:a", "mailbox": MAIL_A},
        dedup_key="mail_scope_binding:v1",
    )
    assert row_id is not None
    ctx = Ctx(tmp_path, "a", MAIL_A)
    scope, refusal = _resolve(isolated_state, ctx)
    assert scope is None and refusal == "binding_malformed"


def test_empty_mailbox_never_binds(isolated_state, tmp_path) -> None:
    ctx = Ctx(tmp_path, "a", "")
    scope, refusal = _resolve(isolated_state, ctx)
    assert scope is None and refusal == "mailbox_unconfigured"
