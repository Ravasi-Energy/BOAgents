"""Minimal identity/tenant adapter for the BOAgents slice (Valul 1).

The host app has no tenant or role system of its own: sign-in happens in the
Next.js layer (NextAuth), which stamps the verified caller email onto the
upstream request as ``x-caller-email`` after stripping any client-supplied
``x-caller-*`` headers (see ``app/api/backend/[...path]/route.ts``). This
adapter authenticates the separate BACKEND_PROXY_SECRET before resolving
that header — plus server-side configuration — into an
``Identity`` the BO routes can authorize against.

What this deliberately is NOT:
- a tenant picker. The tenant id comes from ``BO_TENANT_ID`` (server config),
  never from the request body or a UI selector. A caller that *presents* a
  different tenant (``x-bo-tenant`` header or ``?tenant=``) is refused with
  403 and learns nothing about it.
- a role picker. ``admin`` is derived from ``BO_ADMIN_EMAILS`` and the People
  roster's ``is_principal`` flag; ``operator`` is a service identity
  (shared-secret without a user email); everyone else is a ``viewer``.

Fail-closed: on a public deployment (``OE_PUBLIC_DEPLOYMENT``) an
unauthenticated request is refused rather than silently promoted to the dev
identity.
"""
from __future__ import annotations

import hmac
import os
import re
from dataclasses import dataclass
from typing import Literal

from fastapi import Request

Role = Literal["admin", "operator", "viewer"]

_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_DEV_ACTOR = "local-dev"

_ROLE_RANK: dict[Role, int] = {"viewer": 0, "operator": 1, "admin": 2}

# Capability → minimum role. Write paths are admin-only; simulation runs are
# read-shaped (they persist simulation rows but produce no external effect),
# so operators may run them. Everything readable is available to viewers.
_CAP_MIN_ROLE: dict[str, Role] = {
    "settings:read": "viewer",
    "settings:write": "admin",
    "bots:read": "viewer",
    "bots:write": "admin",
    "bots:simulate": "operator",
    "telemetry:read": "viewer",
    "packages:read": "viewer",
    "packages:write": "admin",
    "routing:read": "viewer",
    "routing:write": "admin",
    "execution:read": "viewer",
    "execution:write": "admin",
    "execution:operate": "operator",
}


class IdentityError(Exception):
    """Base for identity resolution failures; carries an HTTP status."""

    status_code = 403
    error = "forbidden"


class UnauthenticatedError(IdentityError):
    status_code = 401
    error = "unauthenticated"


class TenantMismatchError(IdentityError):
    status_code = 403
    error = "tenant_mismatch"


class ForbiddenError(IdentityError):
    status_code = 403
    error = "forbidden"


@dataclass(frozen=True, slots=True)
class Identity:
    actor: str
    tenant: str
    role: Role
    is_service: bool
    auth_source: Literal["proxy_email", "shared_secret", "dev"]


def _is_public_deployment() -> bool:
    falsey = {"", "0", "false", "no", "off"}
    return os.environ.get("OE_PUBLIC_DEPLOYMENT", "").strip().lower() not in falsey


def configured_tenant() -> str:
    """The installation's tenant id. Validated so it stays an opaque slug."""
    tenant = os.environ.get("BO_TENANT_ID", "local").strip().lower()
    if not _TENANT_RE.match(tenant):
        # A malformed tenant must never widen access — fail closed.
        raise UnauthenticatedError("BO_TENANT_ID is not a valid tenant slug")
    return tenant


def _admin_emails() -> frozenset[str]:
    raw = os.environ.get("BO_ADMIN_EMAILS", "")
    return frozenset(
        e.strip().lower() for e in raw.split(",") if e.strip()
    )


def _is_principal_email(email: str) -> bool:
    """True when the email belongs to a non-archived principal in the roster."""
    try:
        from openexecutive.people import store as people_store

        for person in people_store.list_people():
            if (
                person.email
                and person.email.lower() == email
                and person.is_principal
                and not person.archived
            ):
                return True
    except Exception:
        # The roster is an authorization *source of convenience*, not a
        # correctness dependency — if it cannot be read we simply do not
        # grant admin through it.
        return False
    return False


def resolve_identity(request: Request) -> Identity:
    """Resolve request → Identity. Raises IdentityError on any mismatch."""
    tenant = configured_tenant()

    # A caller may *assert* a tenant, but never select one: any hint that
    # disagrees with the configured installation tenant is a refusal, and the
    # error must not reveal the configured value.
    hinted = request.headers.get("x-bo-tenant") or request.query_params.get("tenant")
    if hinted and hinted.strip().lower() != tenant:
        raise TenantMismatchError("tenant does not match this installation")

    email = (request.headers.get("x-caller-email") or "").strip().lower()
    if email:
        # A service key never authenticates a delegated user. The session
        # proxy must also hold a distinct, server-only delegation credential.
        proxy_secret = os.environ.get("BACKEND_PROXY_SECRET", "").strip()
        supplied = request.headers.get("x-caller-proxy-secret", "")
        service_secret = os.environ.get("BACKEND_SHARED_SECRET", "").strip()
        if (not proxy_secret or proxy_secret == service_secret
                or not hmac.compare_digest(supplied, proxy_secret)):
            raise UnauthenticatedError("untrusted delegated identity")
        role: Role = (
            "admin" if email in _admin_emails() or _is_principal_email(email) else "viewer"
        )
        return Identity(
            actor=email, tenant=tenant, role=role,
            is_service=False, auth_source="proxy_email",
        )

    if request.headers.get("x-api-key"):
        # Service identity: passed the shared-secret gate with no user. Can
        # read and run simulations, cannot change configuration or bots.
        return Identity(
            actor="service:shared-secret", tenant=tenant, role="operator",
            is_service=True, auth_source="shared_secret",
        )

    if _is_public_deployment():
        raise UnauthenticatedError("no caller identity")

    # Local dev fallback — only reachable when the shared-secret gate is off
    # AND the deployment is not public. Documented in .env.example; an
    # operator who wants admin on a locked-down install uses BO_ADMIN_EMAILS.
    return Identity(
        actor=_DEV_ACTOR, tenant=tenant, role="admin",
        is_service=False, auth_source="dev",
    )


def require(identity: Identity, capability: str) -> None:
    """Raise ForbiddenError unless the identity's role covers the capability."""
    minimum = _CAP_MIN_ROLE[capability]
    if _ROLE_RANK[identity.role] < _ROLE_RANK[minimum]:
        raise ForbiddenError(f"{capability} requires role {minimum}")
