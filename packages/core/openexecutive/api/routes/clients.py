"""FastAPI routes for client-company slots (fractional / multi-client mode).

Mirrors the shape of ``api.routes.fixtures`` — thin handlers over
``openexecutive.clients.slots``, which owns validation, the shared
destructive-op lock, and the save/restore machinery. Single-company installs
simply see an empty list here; nothing in the default experience changes
until a slot is explicitly created.
"""
from __future__ import annotations

from pathlib import Path
from typing import Literal

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from openexecutive.api.intake_uploads import (
    _INTAKE_GEN_CHARS_PER_FILE,
    _INTAKE_MAX_DOC_CHARS,
    _gather_intake_attachments,
)

router = APIRouter()


def _capability(request: Request, capability: str) -> None:
    """Resolve the caller's BO identity and require ``capability``.

    These routes swap or destroy the entire live company context, so the
    shared-secret transport gate alone is not enough: an authenticated
    viewer (proxied session) or operator (x-api-key without caller email)
    must be refused BEFORE any slot mutation runs. Identity resolution is
    server-side only — the proxy's caller headers are trusted only with a
    valid BACKEND_PROXY_SECRET, and the dev fallback stays admin as
    documented in bo.identity.
    """
    from openexecutive.bo import identity as bo_identity

    bo_identity.require(bo_identity.resolve_identity(request), capability)


class CreateClientRequest(BaseModel):
    display_name: str = Field(..., max_length=200)
    slug: str | None = Field(default=None, max_length=64)
    # "current" captures the live company into the new slot (and activates it);
    # "blank" creates an empty client to onboard fresh; "generated" writes a
    # seed slot from an engagement-intake bundle (see POST /clients/generate).
    source: str = "current"
    bundle: dict | None = None
    intake_description: str = Field(default="", max_length=20000)


class ClientMetaPatch(BaseModel):
    """Engagement metadata patch — practice-level fields on a slot's meta.json.

    All optional; only provided fields are written. The slot module enforces
    the allowlist and status enum (so direct callers get the same gate).
    """

    role: str | None = Field(default=None, max_length=200)
    status: str | None = Field(default=None, max_length=32)
    engagement_start: str | None = Field(default=None, max_length=32)
    renewal_date: str | None = Field(default=None, max_length=32)
    retainer: str | None = Field(default=None, max_length=200)
    hours_per_week: float | None = None
    primary_contact: str | None = Field(default=None, max_length=200)
    notes: str | None = Field(default=None, max_length=5000)


def _raise_for(exc: Exception) -> None:
    from openexecutive.clients.slots import (
        ClientSlotConflictError,
        ClientSlotNotFoundError,
    )

    if isinstance(exc, ClientSlotNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ClientSlotConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/clients")
async def list_clients(request: Request) -> dict:
    _capability(request, "clients:read")
    from openexecutive.cli.fixture_loader import get_fixture_status
    from openexecutive.clients.rotation import rotation_in_progress
    from openexecutive.clients.slots import get_active_client, list_client_slots
    from openexecutive.config import get_settings

    settings = get_settings()
    status = get_fixture_status(settings)
    return {
        "active": get_active_client(settings),
        "fixture_active": status.get("active_fixture"),
        "rotation_in_progress": rotation_in_progress(settings),
        "clients": list_client_slots(settings),
    }


@router.get("/clients/turn-blockers")
async def list_turn_blockers(request: Request) -> dict:
    """Open/uncertain turn leases blocking client switches (RA13-A02-01).

    The admin surface for the durable turn/switch barrier: each entry
    carries the turn id, kind, per-client ref, owner, status and reason —
    exactly what a refused activate/restore reports. ``switch_in_progress``
    tells the UI a switch currently holds the barrier (new turns deferred).
    The ``owner`` field (pid:nonce) is withheld from non-admin viewers.
    """
    from openexecutive.bo import identity as bo_identity
    from openexecutive.bo import turn_barrier

    ident = bo_identity.resolve_identity(request)
    bo_identity.require(ident, "clients:read")
    try:
        bo_identity.require(ident, "clients:write")
        can_write = True
    except bo_identity.ForbiddenError:
        can_write = False
    blockers = turn_barrier.blockers()
    if not can_write:
        blockers = [
            {k: v for k, v in b.items() if k != "owner"} for b in blockers
        ]
    return {
        "switch_in_progress": turn_barrier.switch_in_progress(),
        "blockers": blockers,
    }


@router.get("/clients/turn-history")
async def list_turn_history(
    request: Request, limit: int = 50
) -> dict:
    """Closed/reconciled turn leases, newest first (F-2, REM-AUDIT-18).

    The durable trail that survives switches and restarts — what the
    operator reviews after a refusal, or to confirm a reconcile landed.
    ``owner`` is withheld from non-admin viewers, same as turn-blockers.
    """
    from openexecutive.bo import identity as bo_identity
    from openexecutive.bo import turn_barrier

    ident = bo_identity.resolve_identity(request)
    bo_identity.require(ident, "clients:read")
    try:
        bo_identity.require(ident, "clients:write")
        can_write = True
    except bo_identity.ForbiddenError:
        can_write = False
    rows = turn_barrier.recent_turns(limit)
    if not can_write:
        rows = [
            {k: v for k, v in r.items() if k != "owner"} for r in rows
        ]
    return {"turns": rows}


@router.get("/clients/control-audit")
async def list_control_audit(
    request: Request, turn_id: str | None = None
) -> dict:
    """Durable control-plane audit rows (F-2, REM-AUDIT-18).

    ``bo_control_audit`` is written in the same transaction as the state
    change it records (e.g. ``turn_reconcile``), so this is the
    authoritative operator-action trail — it cannot be lost by a journal
    swap or an I/O error on the episodic audit log. ``resolution``
    distinguishes provider-verified evidence (``verified``) from human
    attestation (``attested``). ``actor`` is withheld from non-admins.
    """
    from openexecutive.bo import identity as bo_identity
    from openexecutive.bo import turn_barrier

    ident = bo_identity.resolve_identity(request)
    bo_identity.require(ident, "clients:read")
    try:
        bo_identity.require(ident, "clients:write")
        can_write = True
    except bo_identity.ForbiddenError:
        can_write = False
    rows = turn_barrier.control_audit(turn_id)
    if not can_write:
        rows = [
            {k: v for k, v in r.items() if k != "actor"} for r in rows
        ]
    return {"audit": rows}


class ReconcileTurnRequest(BaseModel):
    resolution: Literal["verified", "attested"]


@router.post("/clients/turn-blockers/{turn_id}/reconcile")
async def reconcile_turn_blocker(
    turn_id: str, req: ReconcileTurnRequest, request: Request
) -> dict:
    """Operator reconciliation of a blocking turn lease (RA13-A02-01).

    ``verified`` — the caller checked the journal/provider evidence for the
    turn's outcome. ``attested`` — the operator explicitly accepts the
    decision. Either way the lease closes with the resolution recorded;
    the row is never deleted, so the trail survives. An ``active`` lease
    refuses (409): its owner may still complete it — reconcile once it
    has expired to ``uncertain``."""
    from openexecutive.bo import identity as bo_identity
    from openexecutive.bo import turn_barrier

    ident = bo_identity.resolve_identity(request)
    bo_identity.require(ident, "clients:write")
    try:
        return turn_barrier.reconcile_turn(
            turn_id, resolution=req.resolution, actor=ident.actor
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except turn_barrier.ReconcileRefusedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/clients")
async def create_client(req: CreateClientRequest, request: Request) -> dict:
    _capability(request, "clients:write")
    from openexecutive.clients.slots import ClientSlotError, create_client_slot
    from openexecutive.config import get_settings

    try:
        return await create_client_slot(
            get_settings(),
            display_name=req.display_name,
            slug=req.slug,
            source=req.source,
            bundle=req.bundle,
            intake_description=req.intake_description,
        )
    except ClientSlotError as exc:
        _raise_for(exc)
        raise  # unreachable — _raise_for always raises


@router.post("/clients/generate")
async def generate_client(
    request: Request,
    description: str = Form(""),
    files: list[UploadFile] = File(  # noqa: B008 — FastAPI multipart marker, mirrors chat.py
        default=[]
    ),
) -> dict:
    """Draft a client company from real intake notes and/or attached files.

    Accepts pasted intake material as ``description`` plus optional attachments
    (PDF/Word/Excel/CSV/Markdown/text). Each attachment's extracted text both
    (a) feeds the grounded engagement-intake draft and (b) is appended to the
    returned bundle's ``docs`` as ``source_<name>.md`` so it persists into the
    client slot and is indexed into the ``company_docs`` collection on
    activation. Nothing is persisted here — the UI posts the (possibly edited)
    bundle back to ``POST /clients`` with ``source="generated"``.
    """
    _capability(request, "clients:write")
    from openexecutive.clients.slots import derive_client_slug
    from openexecutive.config import get_settings
    from openexecutive.fixtures.generator import (
        DocSpec,
        GenerationError,
        _safe_doc_filename,
        generate_engagement_bundle,
    )

    notes = (description or "").strip()
    extracted = await _gather_intake_attachments(files or [])
    if not notes and not extracted:
        raise HTTPException(
            status_code=400,
            detail="description or at least one readable file is required",
        )

    # Material fed to the LLM: pasted notes + a capped block per attachment.
    parts = [notes] if notes else []
    parts += [
        f"=== Attached: {safe} ===\n{text[:_INTAKE_GEN_CHARS_PER_FILE]}"
        for safe, text in extracted
    ]
    combined = "\n\n".join(parts)

    settings = get_settings()
    try:
        bundle = await generate_engagement_bundle(combined, settings)
    except GenerationError as exc:
        # 422 — the model produced something unusable; surface it to the UI.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # Persist each attachment's text as a company doc in the bundle so it flows
    # into the slot and is indexed on activation. Names are sanitized with the
    # same helper the persistence path uses and de-duplicated here (against the
    # LLM-written docs and each other) so the reviewed draft and the saved slot
    # show identical filenames — two files with the same stem don't collide.
    seen = {d.filename for d in bundle.docs}
    for i, (safe, text) in enumerate(extracted):
        stem = Path(safe).stem or "attachment"
        fn = _safe_doc_filename(f"source_{stem}", i)
        base = fn[:-3]  # strip the guaranteed ".md" suffix
        n = 2
        while fn in seen:
            fn = f"{base}_{n}.md"
            n += 1
        seen.add(fn)
        bundle.docs.append(DocSpec(filename=fn, content=text[:_INTAKE_MAX_DOC_CHARS]))

    return {
        "suggested_name": derive_client_slug(bundle.profile.name, settings),
        "display_name": bundle.profile.name,
        "bundle": bundle.model_dump(),
    }


@router.post("/clients/save")
async def save_client(request: Request) -> dict:
    """Checkpoint the active client's live state into its slot."""
    _capability(request, "clients:write")
    from openexecutive.clients.slots import ClientSlotError, save_active_client
    from openexecutive.config import get_settings

    try:
        return await save_active_client(get_settings())
    except ClientSlotError as exc:
        _raise_for(exc)
        raise


@router.post("/clients/{slug}/activate")
async def activate_client(slug: str, request: Request) -> dict:
    """Switch the live company context to this client (saving the current one)."""
    _capability(request, "clients:write")
    from openexecutive.clients.slots import ClientSlotError, activate_client_slot
    from openexecutive.config import get_settings

    try:
        return await activate_client_slot(
            get_settings(), slug, app_state=request.app.state
        )
    except ClientSlotError as exc:
        _raise_for(exc)
        raise


@router.get("/clients/cockpit")
async def clients_cockpit(request: Request) -> dict:
    """Practice-wide board: one rollup card per client (active first).

    Read-only across live DB + parked slot snapshots; one broken slot
    degrades to an error-flagged card rather than failing the board.
    """
    _capability(request, "clients:read")
    from datetime import UTC, datetime

    from openexecutive.clients.cockpit import practice_overview
    from openexecutive.config import get_settings

    cards = practice_overview(get_settings())
    return {
        "clients": [c.model_dump() for c in cards],
        "generated_at": datetime.now(UTC).isoformat(),
    }


@router.patch("/clients/{slug}")
async def patch_client_meta(slug: str, req: ClientMetaPatch, request: Request) -> dict:
    """Update a slot's engagement metadata (role, status, renewal, …)."""
    _capability(request, "clients:write")
    from openexecutive.clients.slots import ClientSlotError, update_client_meta
    from openexecutive.config import get_settings

    patch = req.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="empty metadata patch")
    try:
        return await update_client_meta(get_settings(), slug, patch)
    except ClientSlotError as exc:
        _raise_for(exc)
        raise


@router.delete("/clients/{slug}")
async def delete_client(slug: str, request: Request) -> dict:
    _capability(request, "clients:write")
    from openexecutive.clients.slots import ClientSlotError, delete_client_slot
    from openexecutive.config import get_settings

    try:
        return await delete_client_slot(get_settings(), slug)
    except ClientSlotError as exc:
        _raise_for(exc)
        raise
