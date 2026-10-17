"""Deterministic rejection codes for bo.package.v1 (contract §4 + VAL2-01)."""
from __future__ import annotations


class PackageReject(Exception):
    """Controlled refusal — every invalid input maps to a stable code."""

    CODES = {
        # manifest / schema
        "INVALID_MANIFEST",
        "MANIFEST_TOO_LARGE",
        "DUPLICATE_KEY",
        # trust
        "PUBLISHER_UNKNOWN",
        "PUBLISHER_SUSPENDED",
        "KEY_UNKNOWN",
        "KEY_REVOKED",
        "KEY_EXPIRED",
        "BAD_SIGNATURE",
        # artifacts
        "ARTIFACT_MISSING",
        "ARTIFACT_MODIFIED",
        "UNSIGNED_ARTIFACT",
        "TRAVERSAL",
        "PACKAGE_TOO_LARGE",
        "ARTIFACT_DRIFT",
        # policy
        "INCOMPATIBLE",
        "CAPABILITY_UNKNOWN",
        "CAPABILITY_EXCESSIVE",
        "ROLLBACK_UNAUTHORIZED",
        "VERSION_CONFLICT",
        "APPROVAL_INVALID",
        "KIND_NOT_ALLOWED",
        "LIMIT_EXCEEDED",
        # store
        "INVALID_REGISTRY",
        "REGISTRY_UNAVAILABLE",
        "PACKAGES_DISABLED",
    }

    def __init__(self, code: str, detail: str = "") -> None:
        if code not in self.CODES:
            raise ValueError(f"unknown reject code: {code}")
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)
