"""Auth-facing surface — the People roster as the administered input to who
can sign in via Google OAuth on the UI (BUGHUNT-02 R4 contract).

The Next.js layer's ``auth.ts`` calls ``GET /auth/allowed-emails`` during
the NextAuth ``signIn`` callback. The response is a single typed shape::

    {"administered": bool, "emails": [{"email": str, "person_id": int}]}

``administered`` is a *durable server fact*, not a deduction: it becomes
true at the first administrative roster write (create/patch/archive — in
``people.store``) and stays true across restarts and even after every
person is archived. Client contract:

- ``administered: true`` → the roster is authoritative, even when
  ``emails`` is empty (an empty administered roster revokes everyone not
  re-added — it never falls back to env).
- ``administered: false`` → the server has never been administered; only
  then may the UI fall back to its ``ALLOWED_EMAILS`` env bootstrap.
- A fetch error/timeout is **not** ``administered: false`` — the client
  must fail closed and must not let an error revive revoked access.
  Non-administered is demonstrated explicitly by the server, never
  inferred from a failed request.

``POST /auth/roster/recover-env`` is the separate, explicit, audited
recovery path: it clears the flag so ``administered`` reports false and
env bootstrap is permitted again. Gated to the service identity
(``x-api-key`` — the infrastructure operator) or a delegated admin; a
viewer gets 403. Every call — including a no-op one — writes an
``auth_roster_recovery`` audit row with the acting identity.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from openexecutive.audit import log_event
from openexecutive.bo import identity as bo_identity
from openexecutive.people import store as people_store

router = APIRouter()


class AllowedEmail(BaseModel):
    email: str
    person_id: int


class AllowedEmails(BaseModel):
    administered: bool
    emails: list[AllowedEmail]


@router.get("/auth/allowed-emails", response_model=AllowedEmails)
def allowed_emails() -> AllowedEmails:
    """Report the roster and its administered state for the sign-in gate.

    ``emails`` lists non-archived people with an email (lower-cased).
    ``administered`` is the durable server fact the UI uses to decide
    whether the roster is authoritative (true) or the ``ALLOWED_EMAILS``
    env bootstrap still applies (false). Fresh installs report
    ``administered: false`` with an empty list — that is the explicit,
    demonstrable non-administered state, distinct from a fetch failure.
    """
    return AllowedEmails(
        administered=people_store.roster_administered(),
        emails=[
            AllowedEmail(email=p.email.lower(), person_id=p.id)  # type: ignore[arg-type]
            for p in people_store.list_people()
            if p.email and p.id is not None
        ],
    )


def _recovery_identity(request: Request) -> bo_identity.Identity:
    return bo_identity.require_http(request, "auth:recover")


@router.post("/auth/roster/recover-env")
def recover_env_bootstrap(
    ident: Annotated[bo_identity.Identity, Depends(_recovery_identity)],
) -> dict[str, bool]:
    """Explicit, audited recovery: reset the roster-administered flag.

    After this call ``GET /auth/allowed-emails`` reports
    ``administered: false`` and the UI may bootstrap access from its
    ``ALLOWED_EMAILS`` env var again. This is the only way back once a
    roster exists — it is how an operator recovers from a lockout caused
    by archiving the wrong principal. Audited with the acting identity
    on every call, including no-ops.
    """
    cleared = people_store.reset_roster_administered()
    log_event(
        "auth_roster_recovery",
        f"roster administered flag reset by {ident.actor} "
        f"({'cleared' if cleared else 'no-op'})",
        actor=ident.actor,
        details={
            "actor": ident.actor,
            "role": ident.role,
            "auth_source": ident.auth_source,
            "cleared": cleared,
        },
    )
    return {"administered": False, "cleared": cleared}
