from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from openexecutive.api.models import CompanyProfileResponse, CompanyProfileUpdateRequest
from openexecutive.bo import identity as bo_identity
from openexecutive.config import get_settings
from openexecutive.memory.company_profile import (
    CompanyProfile,
    ProfileLockTimeout,
    profile_lock,
)
from openexecutive.onboarding.profile_builder import load_or_create_profile

router = APIRouter()


def _admin_ident(request: Request) -> bo_identity.Identity:
    return bo_identity.require_http(request, "profile:write")


def _deep_merge(existing: dict, patch: dict) -> dict:
    """Recursive field-level merge: keys absent from ``patch`` keep the
    stored value; provided nested dicts merge the same way (BUGHUNT-02
    P0-6 — updating only ``financials.runway`` must not delete burn rate,
    currency, or the metrics that were never mentioned)."""
    merged = dict(existing)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


@router.get("/company-profile", response_model=CompanyProfileResponse)
async def get_company_profile() -> CompanyProfileResponse:
    profile = load_or_create_profile()
    if profile.is_empty():
        raise HTTPException(status_code=404, detail="No company profile found. Complete onboarding first.")
    return CompanyProfileResponse(**profile.model_dump())


@router.patch("/company-profile", response_model=CompanyProfileResponse)
async def update_company_profile(
    body: CompanyProfileUpdateRequest,
    ident: Annotated[bo_identity.Identity | None, Depends(_admin_ident)] = None,
) -> CompanyProfileResponse:
    # In-process callers (tests, CLI, other services) pass no identity —
    # trusted local code. Over HTTP the dependency above enforces
    # profile:write=admin before we get here (BUGHUNT-02 P0-8/10).
    settings = get_settings()
    profile_path = settings.company_profile_path
    # Fast-path 404 outside the fence — this read stays injectable (the R14
    # probe's barrier synchronizes both writers here). It is advisory only:
    # nothing below trusts it, the CAS read inside the lock is authoritative.
    if load_or_create_profile().is_empty():
        raise HTTPException(status_code=404, detail="No company profile found. Complete onboarding first.")

    # Durable CAS (CONTROL R12): the writer must declare the version it
    # actually read; a stale write is refused before any effect — the
    # client reloads and rebases explicitly.
    if body.expected_version is None:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "expected_version_required",
                "message": "PATCH /company-profile requires expected_version "
                "pinned to the version the client read.",
            },
        )

    # The fence is a kernel lock on <profile>.lock, shared across processes
    # (CONTROL R14 — PROFILE-CAS-RACE). EVERYTHING CAS-dependent happens
    # inside: the persisted version is re-read from disk under the lock
    # (never trusted from the pre-lock profile — that object can be stale
    # by the time we get here), the merge is applied onto that same fresh
    # read, and save_to_yaml writes inside the same critical section
    # (profile_lock is re-entrant per thread).
    try:
        with profile_lock(profile_path):
            stored = CompanyProfile.load_from_yaml(profile_path)
            if stored.is_empty():
                # TOCTOU guard: emptied between the pre-check and the lock.
                raise HTTPException(status_code=404, detail="No company profile found. Complete onboarding first.")
            if body.expected_version != stored.version:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "version_conflict",
                        "message": f"expected_version={body.expected_version} dar "
                        f"versiunea curentă este {stored.version}",
                        "current_version": stored.version,
                    },
                )

            update_data = body.model_dump(exclude_unset=True)
            update_data.pop("expected_version", None)
            # Convert nested Pydantic models to dicts so the merge sees
            # plain data
            update_data = {
                k: v.model_dump(exclude_unset=True) if hasattr(v, "model_dump") else v
                for k, v in update_data.items()
            }

            merged = stored.model_dump()
            for key, value in update_data.items():
                if key in ("vendors", "tickers", "vendors_remove", "tickers_remove"):
                    continue
                if isinstance(value, dict) and isinstance(merged.get(key), dict):
                    merged[key] = _deep_merge(merged[key], value)
                else:
                    merged[key] = value

            # List fields are DELTAS: provided entries union-add onto the stored
            # list, `[]` is a no-op, and removal is only via the explicit *_remove
            # fields (BUGHUNT-02 P0-5 — a partial PATCH must not wipe vendors).
            for list_key, remove_key in (("vendors", "vendors_remove"), ("tickers", "tickers_remove")):
                if list_key in update_data or remove_key in update_data:
                    current = list(merged.get(list_key) or [])
                    for item in update_data.get(list_key) or []:
                        if item not in current:
                            current.append(item)
                    for item in update_data.get(remove_key) or []:
                        if item in current:
                            current.remove(item)
                    merged[list_key] = current

            merged["version"] = stored.version + 1
            validated = CompanyProfile.model_validate(merged)
            validated.save_to_yaml(profile_path)
    except ProfileLockTimeout as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "profile_lock_timeout",
                "message": str(exc),
            },
        ) from exc

    return CompanyProfileResponse(**validated.model_dump())
