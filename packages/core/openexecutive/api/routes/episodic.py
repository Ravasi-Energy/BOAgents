from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel

from openexecutive.bo.identity import require_http
from openexecutive.memory.episodic import (
    Advice,
    Decision,
    Initiative,
    delete_advice,
    delete_decision,
    delete_initiative,
    get_advice,
    get_decision,
    get_initiative,
    count_advice,
    count_decisions,
    count_initiatives,
    list_advice,
    list_decisions,
    list_initiatives,
    update_advice,
    update_decision,
    update_initiative,
)


def _require_admin_write(request: Request) -> None:
    require_http(request, "episodic:write")


router = APIRouter()


class DecisionUpdate(BaseModel):
    domain: str | None = None
    summary: str | None = None
    rationale: str | None = None
    outcome: str | None = None
    tags: str | None = None


class InitiativeUpdate(BaseModel):
    title: str | None = None
    status: str | None = None
    summary: str | None = None


class AdviceUpdate(BaseModel):
    domain: str | None = None
    query_summary: str | None = None
    advice_summary: str | None = None


# --- Decisions ---


def _antete_paginare(response: Response, items: list, total: int,
                     ts_attr: str) -> None:
    """Continuare keyset: Link rel=next cu cursorul ultimului rand +
    numarator onest — pagina nu deriva la mutatii precum OFFSET."""
    response.headers["X-Total-Count"] = str(total)
    if len(items) == 0:
        return
    last = items[-1]
    if total <= len(items):
        return
    ts = getattr(last, ts_attr)
    nxt = f"{ts}|{last.id}"
    response.headers["Link"] = f'<?cursor={nxt}>; rel="next"'
    response.headers["X-Next-Cursor"] = nxt


@router.get("/memories/decisions", response_model=list[Decision])
def get_decisions(
    response: Response,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None),
) -> list[Decision]:
    items = list_decisions(limit=limit, offset=offset, cursor=cursor)
    _antete_paginare(response, items, count_decisions(), "timestamp")
    return items


@router.patch("/memories/decisions/{decision_id}", response_model=Decision, dependencies=[Depends(_require_admin_write)])
def patch_decision(decision_id: int, body: DecisionUpdate) -> Decision:
    if not update_decision(decision_id, **body.model_dump(exclude_unset=True)):
        raise HTTPException(status_code=404, detail="Decision not found")
    updated = get_decision(decision_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Decision not found")
    return updated


@router.delete("/memories/decisions/{decision_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_require_admin_write)])
def remove_decision(decision_id: int) -> Response:
    if not delete_decision(decision_id):
        raise HTTPException(status_code=404, detail="Decision not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Initiatives ---


@router.get("/memories/initiatives", response_model=list[Initiative])
def get_initiatives(
    response: Response,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None),
) -> list[Initiative]:
    items = list_initiatives(limit=limit, offset=offset, cursor=cursor)
    _antete_paginare(response, items, count_initiatives(), "updated_at")
    return items


@router.patch("/memories/initiatives/{initiative_id}", response_model=Initiative, dependencies=[Depends(_require_admin_write)])
def patch_initiative(initiative_id: int, body: InitiativeUpdate) -> Initiative:
    if not update_initiative(initiative_id, **body.model_dump(exclude_unset=True)):
        raise HTTPException(status_code=404, detail="Initiative not found")
    updated = get_initiative(initiative_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Initiative not found")
    return updated


@router.delete("/memories/initiatives/{initiative_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_require_admin_write)])
def remove_initiative(initiative_id: int) -> Response:
    if not delete_initiative(initiative_id):
        raise HTTPException(status_code=404, detail="Initiative not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- Advice ---


@router.get("/memories/advice", response_model=list[Advice])
def get_advice_list(
    response: Response,
    limit: int = Query(default=500, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None),
) -> list[Advice]:
    items = list_advice(limit=limit, offset=offset, cursor=cursor)
    _antete_paginare(response, items, count_advice(), "timestamp")
    return items


@router.patch("/memories/advice/{advice_id}", response_model=Advice, dependencies=[Depends(_require_admin_write)])
def patch_advice(advice_id: int, body: AdviceUpdate) -> Advice:
    if not update_advice(advice_id, **body.model_dump(exclude_unset=True)):
        raise HTTPException(status_code=404, detail="Advice not found")
    updated = get_advice(advice_id)
    if updated is None:
        raise HTTPException(status_code=404, detail="Advice not found")
    return updated


@router.delete("/memories/advice/{advice_id}", status_code=status.HTTP_204_NO_CONTENT, dependencies=[Depends(_require_admin_write)])
def remove_advice(advice_id: int) -> Response:
    if not delete_advice(advice_id):
        raise HTTPException(status_code=404, detail="Advice not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
