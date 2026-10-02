from __future__ import annotations

import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

_SAFE_NAME = re.compile(r"^[a-z0-9_-]+$")


def _capability(request: Request, capability: str) -> None:
    """Resolve the caller's BO identity and require ``capability``.

    Same contract as ``api.routes.clients._capability``: these routes swap
    or destroy the live company context, so the shared-secret transport
    gate alone is not enough — the check runs BEFORE any write, LLM call
    or external effect (F-1, REM-AUDIT-18).
    """
    from openexecutive.bo import identity as bo_identity

    bo_identity.require(bo_identity.resolve_identity(request), capability)


class GenerateFixtureRequest(BaseModel):
    description: str


class CreateFixtureRequest(BaseModel):
    """A (possibly user-edited) bundle from the generate step, ready to save."""

    scenario_description: str = ""
    bundle: dict


def _fixture_name_taken(slug: str) -> bool:
    """True if a curated dir OR a generated row already owns ``slug``."""
    from openexecutive.cli.fixture_loader import FIXTURES_ROOT
    from openexecutive.fixtures import store as fixtures_store

    if (FIXTURES_ROOT / slug).exists():
        return True
    return fixtures_store.fixture_name_exists(slug)


@router.get("/fixtures")
async def list_fixtures(request: Request) -> dict:
    _capability(request, "fixtures:read")
    from openexecutive.cli.fixture_loader import list_all_fixtures

    return {"fixtures": list_all_fixtures()}


@router.get("/fixtures/status")
async def fixtures_status(request: Request) -> dict:
    _capability(request, "fixtures:read")
    from openexecutive.cli.fixture_loader import get_fixture_status
    from openexecutive.config import get_settings

    return get_fixture_status(get_settings())


@router.post("/fixtures/snapshot")
async def fixtures_snapshot(request: Request) -> dict:
    """Manual 'save current state as my company' — overwrites the backup.

    Refuses with 409 when a fixture is currently active: the live state is
    fixture data, not the user's company, and overwriting the only backup
    with fixture data would be irreversible data loss.
    """
    _capability(request, "fixtures:write")
    from openexecutive.cli.fixture_loader import (
        FixtureActiveError,
        snapshot_user_state_async,
    )
    from openexecutive.config import get_settings

    settings = get_settings()
    try:
        return await snapshot_user_state_async(settings)
    except FixtureActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/fixtures/reset")
async def fixtures_reset(request: Request) -> dict:
    """Wipe live state AND the snapshot — irreversible factory reset.

    Re-seeds the 8 default specialist departments so the user has a
    sensible starting org instead of a blank departments page. The
    shared ChromaDB store on ``app.state`` is swapped inside the
    destructive-op lock to avoid a race where a concurrent reader could
    hit the deleted-then-recreated collection through the previous
    store instance.
    """
    _capability(request, "fixtures:write")
    from openexecutive.cli.fixture_loader import (
        FixtureActiveError,
        reset_all_state,
    )
    from openexecutive.config import get_settings

    settings = get_settings()
    try:
        return await reset_all_state(settings, app_state=request.app.state)
    except FixtureActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/fixtures/unload")
async def fixtures_unload(request: Request) -> dict:
    """Restore the user's original state from the backup directory."""
    _capability(request, "fixtures:write")
    from openexecutive.cli.fixture_loader import (
        FixtureActiveError,
        FixtureNotFoundError,
        unload_fixture,
    )
    from openexecutive.config import get_settings

    settings = get_settings()
    try:
        result = await unload_fixture(settings)
    except FixtureNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FixtureActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # Mirror the load route: replace the shared store so subsequent requests
    # see the freshly-rebuilt company_docs collection.
    if hasattr(request.app.state, "store"):
        from openexecutive.knowledge.store import ChromaDBStore

        request.app.state.store = ChromaDBStore(
            persist_directory=settings.vector_store_path
        )

    return result


@router.post("/fixtures/{name}/load")
async def load_fixture(name: str, request: Request) -> dict:
    _capability(request, "fixtures:write")
    from openexecutive.cli.fixture_loader import (
        FixtureActiveError,
        FixtureNotFoundError,
        load_fixture_any,
    )
    from openexecutive.config import get_settings

    # Allowlist — fixture names are directory names; only lowercase alphanumeric,
    # hyphens, and underscores are valid. This prevents any path traversal variant.
    if not _SAFE_NAME.match(name):
        raise HTTPException(status_code=400, detail="Invalid fixture name")

    settings = get_settings()
    try:
        result = await load_fixture_any(name, settings)
    except FixtureNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FixtureActiveError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    # If the app has a shared store in state, replace it with a fresh instance
    # so the new company_docs collection is visible to subsequent requests.
    if hasattr(request.app.state, "store"):
        from openexecutive.knowledge.store import ChromaDBStore

        request.app.state.store = ChromaDBStore(
            persist_directory=settings.vector_store_path
        )

    return result


@router.post("/fixtures/generate")
async def generate_fixture(req: GenerateFixtureRequest, request: Request) -> dict:
    """Generate a DRAFT fixture bundle from a scenario description.

    The bundle is validated but NOT persisted — the UI shows it for review and
    posts it back to ``POST /fixtures`` to save. Returns the bundle plus a
    suggested unique slug.
    """
    _capability(request, "fixtures:write")
    from openexecutive.config import get_settings
    from openexecutive.fixtures.generator import (
        GenerationError,
        derive_slug,
        generate_fixture_bundle,
    )

    description = (req.description or "").strip()
    if not description:
        raise HTTPException(status_code=400, detail="description is required")

    try:
        bundle = await generate_fixture_bundle(description, get_settings())
    except GenerationError as exc:
        # 422 — the model produced something unusable; surface it to the UI.
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    suggested = derive_slug(bundle.profile.name, _fixture_name_taken)
    return {
        "suggested_name": suggested,
        "display_name": bundle.profile.name,
        "bundle": bundle.model_dump(),
    }


@router.post("/fixtures")
async def create_fixture(req: CreateFixtureRequest, request: Request) -> dict:
    """Validate a (reviewed) bundle and persist it as a generated fixture."""
    _capability(request, "fixtures:write")
    from openexecutive.fixtures import store as fixtures_store
    from openexecutive.fixtures.generator import (
        FixtureBundle,
        bundle_to_serialized,
        derive_slug,
        validate_bundle,
    )

    try:
        bundle = FixtureBundle.model_validate(req.bundle)
    except Exception as exc:  # pydantic ValidationError
        raise HTTPException(status_code=422, detail=f"Invalid bundle: {exc}") from exc

    errors = validate_bundle(bundle)
    if errors:
        raise HTTPException(status_code=422, detail="; ".join(errors))

    fixtures_store.initialize_db()
    slug = derive_slug(bundle.profile.name, _fixture_name_taken)
    serialized = bundle_to_serialized(bundle, req.scenario_description)

    try:
        fixtures_store.insert_fixture(name=slug, **serialized)
    except Exception as exc:
        # UNIQUE violation despite the slug check → racing create; ask to retry.
        raise HTTPException(
            status_code=409, detail=f"Could not save fixture: {exc}"
        ) from exc

    return {
        "name": slug,
        "display_name": serialized["display_name"],
        "source": "generated",
        "doc_count": serialized["doc_count"],
    }


@router.delete("/fixtures/{name}")
async def delete_fixture(name: str, request: Request) -> dict:
    """Soft-delete a GENERATED fixture. Refuses curated and active fixtures."""
    _capability(request, "fixtures:write")
    from openexecutive.cli.fixture_loader import FIXTURES_ROOT, get_fixture_status
    from openexecutive.config import get_settings
    from openexecutive.fixtures import store as fixtures_store

    if not _SAFE_NAME.match(name):
        raise HTTPException(status_code=400, detail="Invalid fixture name")

    if (FIXTURES_ROOT / name).exists():
        raise HTTPException(
            status_code=400, detail="Curated fixtures cannot be deleted"
        )

    # Refuse deleting the active fixture — otherwise /fixtures/status would keep
    # advertising an active fixture that can no longer be loaded. Unload first.
    if get_fixture_status(get_settings()).get("active_fixture") == name:
        raise HTTPException(
            status_code=409,
            detail="This fixture is currently active — unload it before deleting.",
        )

    if not fixtures_store.delete_fixture(name):
        raise HTTPException(status_code=404, detail="Generated fixture not found")

    return {"deleted": True, "name": name}
