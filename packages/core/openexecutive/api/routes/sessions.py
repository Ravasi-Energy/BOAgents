from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status

from openexecutive.api.models import SessionSummary
from openexecutive.api.routes.chat import _resolve_caller_person_id
from openexecutive.bo.identity import require_http
from openexecutive.memory.session_store import (
    delete_session,
    get_session_metadata,
    list_sessions,
    load_messages,
)


def _require_admin_write(request: Request) -> None:
    require_http(request, "sessions:write")


router = APIRouter()


def _owned_session(request: Request, session_id: str) -> dict:
    """F03: detail/messages/delete serve only the session's owner.

    A session of another person, a legacy NULL-owner row, or an
    unresolvable caller all answer 404 — the same as a session that does
    not exist, so the route leaks no existence oracle.
    """
    meta = get_session_metadata(session_id)
    if meta is None:
        raise HTTPException(status_code=404, detail="Session not found")
    caller = _resolve_caller_person_id(request)
    if caller is None or meta.get("caller_person_id") != caller:
        raise HTTPException(status_code=404, detail="Session not found")
    return meta


@router.get("/sessions", response_model=list[SessionSummary])
def get_sessions(request: Request) -> list[SessionSummary]:
    caller_person_id = _resolve_caller_person_id(request)
    if caller_person_id is None:
        # Either a signed-in user whose email isn't in the roster, or no
        # principal is configured yet (fresh install). Either way they
        # have no chats to see — return empty rather than leaking the
        # legacy NULL-owner rows.
        return []
    return [SessionSummary(**s) for s in list_sessions(caller_person_id)]


@router.get("/sessions/{session_id}", response_model=SessionSummary)
def get_session(request: Request, session_id: str) -> SessionSummary:
    return SessionSummary(**_owned_session(request, session_id))


@router.get("/sessions/{session_id}/messages")
def get_session_messages(request: Request, session_id: str) -> list[dict]:
    _owned_session(request, session_id)
    return load_messages(session_id)


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_require_admin_write)])
def delete_session_route(request: Request, session_id: str) -> Response:
    _owned_session(request, session_id)
    delete_session(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
