"""FastAPI routes for People.

Phase 3 surface: CRUD + archive + approver lookup.
All mutations invalidate the 60s registry cache so the next
Executive turn picks up the change.
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

# Module-attribute call (not `from ... import log_event`) so the suite's
# audit silencer — monkeypatch.setattr(audit, "log_event", ...) — applies.
from openexecutive import audit as _audit
from openexecutive.bo import identity as bo_identity
from openexecutive.people import registry as people_registry
from openexecutive.people import store as people_store
from openexecutive.people.models import (
    AuthorityScope,
    AvailabilityWindow,
    Person,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# --------------------------------------------------------------------------- #
# Request bodies
# --------------------------------------------------------------------------- #

class PersonCreate(BaseModel):
    full_name: str = Field(min_length=1, max_length=200)
    role: str = Field(default="", max_length=200)
    is_principal: bool = False
    department_slugs: list[str] = Field(default_factory=list)
    email: str | None = None
    slack_user_id: str | None = None
    telegram_chat_id: str | None = None
    discord_user_id: str | None = None
    preferred_channel: str = "any"
    response_sla_hours: int = Field(default=24, ge=1, le=8760)
    on_leave_until: date | None = None
    reports_to_person_id: int | None = None
    authority_scope: list[AuthorityScope] = Field(default_factory=list)
    availability: list[AvailabilityWindow] = Field(default_factory=list)


class PersonPatch(BaseModel):
    full_name: str | None = Field(default=None, max_length=200)
    role: str | None = Field(default=None, max_length=200)
    email: str | None = None
    slack_user_id: str | None = None
    telegram_chat_id: str | None = None
    discord_user_id: str | None = None
    preferred_channel: str | None = None
    response_sla_hours: int | None = Field(default=None, ge=1, le=8760)
    on_leave_until: date | None = None
    clear_on_leave: bool = False
    reports_to_person_id: int | None = None
    department_slugs: list[str] | None = None
    department_slugs_remove: list[str] | None = None
    # Durable CAS (CONTROL R12): required — a writer always declares the
    # version it read; missing → 422, stale → 409.
    expected_version: int
    authority_scope: list[AuthorityScope] | None = None
    availability: list[AvailabilityWindow] | None = None
    # Explicit clear ops for the nullable contact/reference fields —
    # sending the field as JSON null is refused (ambiguous), clearing is
    # only ever spelled out by name.
    clear_email: bool = False
    clear_slack_user_id: bool = False
    clear_telegram_chat_id: bool = False
    clear_discord_user_id: bool = False
    clear_reports_to: bool = False


# --------------------------------------------------------------------------- #
# Read routes
# --------------------------------------------------------------------------- #

@router.get("/people", response_model=list[Person])
def list_people(include_archived: bool = False) -> list[Person]:
    return people_store.list_people(include_archived=include_archived)


@router.get("/people/by-scope/{token}", response_model=list[Person])
def people_by_scope(token: str) -> list[Person]:
    """Return non-archived people who can approve the given scope token."""
    try:
        scope = AuthorityScope(token)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown scope token: {token!r}. Valid tokens: {[s.value for s in AuthorityScope]}",
        ) from exc
    return people_store.find_approvers(scope)


@router.get("/people/{person_id}", response_model=Person)
def get_person(person_id: int) -> Person:
    person = people_store.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=404, detail="Person not found")
    return person


# --------------------------------------------------------------------------- #
# Mutation routes — every one gated by the BO role model (BUGHUNT-02 P0-8/9/10).
# No identity / viewer / wrong tenant → 403; only an admin may mutate. The
# `ident` parameter also satisfies the static caller-inventory contract.
# --------------------------------------------------------------------------- #

def _admin_ident(request: Request) -> bo_identity.Identity:
    return bo_identity.require_http(request, "people:write")


@router.post("/people", response_model=Person, status_code=status.HTTP_201_CREATED)
def create_person(
    body: PersonCreate,
    ident: Annotated[bo_identity.Identity, Depends(_admin_ident)],
) -> Person:
    try:
        pid = people_store.upsert_person(
            full_name=body.full_name,
            role=body.role,
            is_principal=body.is_principal,
            department_slugs=body.department_slugs,
            email=body.email,
            slack_user_id=body.slack_user_id,
            telegram_chat_id=body.telegram_chat_id,
            discord_user_id=body.discord_user_id,
            preferred_channel=body.preferred_channel,  # type: ignore[arg-type]
            response_sla_hours=body.response_sla_hours,
            on_leave_until=body.on_leave_until,
            reports_to_person_id=body.reports_to_person_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if body.authority_scope:
        people_store.set_authority_scope(pid, body.authority_scope)
    if body.availability:
        people_store.set_availability(pid, body.availability)
    people_registry.invalidate()
    person = people_store.get_person(pid)
    if person is None:
        raise HTTPException(status_code=500, detail="Person vanished after insert")
    return person


@router.patch("/people/{person_id}", response_model=Person)
def patch_person(
    person_id: int,
    body: PersonPatch,
    ident: Annotated[bo_identity.Identity, Depends(_admin_ident)],
) -> Person:
    if people_store.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")

    raw = body.model_dump(exclude_unset=True)
    # Explicit-clear contract (CONTROL R12): a JSON null on a nullable
    # field is ambiguous (clear vs. don't-touch) — refused; the clear_* op
    # must be named. A clear_* flag together with a concrete value for the
    # same field is contradictory — refused as well.
    clearable = {
        "email": body.clear_email,
        "slack_user_id": body.clear_slack_user_id,
        "telegram_chat_id": body.clear_telegram_chat_id,
        "discord_user_id": body.clear_discord_user_id,
        "reports_to_person_id": body.clear_reports_to,
    }
    for field, flag in clearable.items():
        if field in raw and raw[field] is None and not flag:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "ambiguous_null",
                    "message": f"{field}: null nu șterge — folosește "
                    f"clear_{field.replace('reports_to_person_id', 'reports_to')}",
                },
            )
        if flag and field in raw and raw[field] is not None:
            raise HTTPException(
                status_code=422,
                detail={
                    "error": "contradictory_clear",
                    "message": f"clear_{field} cu valoare dată — "
                    "ori ștergi explicit, ori scrii o valoare.",
                },
            )
    mutating_keys = set(raw) - {"expected_version"}
    if mutating_keys:
        try:
            people_store.update_person(
                person_id,
                full_name=body.full_name,
                role=body.role,
                email=body.email,
                slack_user_id=body.slack_user_id,
                telegram_chat_id=body.telegram_chat_id,
                discord_user_id=body.discord_user_id,
                preferred_channel=body.preferred_channel,  # type: ignore[arg-type]
                response_sla_hours=body.response_sla_hours,
                on_leave_until=body.on_leave_until,
                clear_on_leave=body.clear_on_leave,
                clear_email=body.clear_email,
                clear_slack_user_id=body.clear_slack_user_id,
                clear_telegram_chat_id=body.clear_telegram_chat_id,
                clear_discord_user_id=body.clear_discord_user_id,
                clear_reports_to=body.clear_reports_to,
                reports_to_person_id=body.reports_to_person_id,
                department_slugs=body.department_slugs,
                department_slugs_remove=body.department_slugs_remove,
                # Scope/availability ride the same CAS-fenced transaction —
                # a scope-only PATCH is still a fenced write (CONTROL R12).
                authority_scope=(
                    body.authority_scope if "authority_scope" in raw else None
                ),
                availability=(
                    body.availability if "availability" in raw else None
                ),
                expected_version=body.expected_version,
            )
        except people_store.PersonConflictError as exc:
            current = people_store.get_person(person_id)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "version_conflict",
                    "message": str(exc),
                    "current_version": current.version if current else None,
                    "current": current.model_dump() if current else None,
                },
            ) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        clears = sorted(f for f, flag in clearable.items() if flag) + (
            ["on_leave_until"] if body.clear_on_leave else []
        )
        _audit.log_event(
            "people_person_updated",
            f"Persoana #{person_id} actualizată",
            actor=ident.actor,
            details={
                "person_id": person_id,
                "fields": sorted(k for k in mutating_keys
                                 if not k.startswith("clear_")),
                "cleared": clears,
            },
        )
    people_registry.invalidate()
    person = people_store.get_person(person_id)
    if person is None:
        raise HTTPException(status_code=500, detail="Person vanished")
    return person


@router.post("/people/{person_id}/archive", status_code=status.HTTP_204_NO_CONTENT)
def archive_person(
    person_id: int,
    ident: Annotated[bo_identity.Identity, Depends(_admin_ident)],
) -> Response:
    if people_store.get_person(person_id) is None:
        raise HTTPException(status_code=404, detail="Person not found")
    people_store.archive_person(person_id)
    people_registry.invalidate()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
