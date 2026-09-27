"""Import → verify → quarantine/draft service for bo.package.v1.

No external effects: the source is a server-local directory, the verified
content is copied under its artifact-set digest into a local quarantine
store, and lifecycle state lives in the tenant SQLite DB. Activation and
execution are out of scope for VAL2-01.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openexecutive.bo.identity import Identity, require
from openexecutive.bo.packages import store
from openexecutive.bo.packages.canon import signed_payload
from openexecutive.bo.packages.contract import (
    MANIFEST_NAME,
    load_manifest,
    load_manifest_raw,
    validate_manifest,
)
from openexecutive.bo.packages.errors import PackageReject
from openexecutive.bo.packages.registry import TrustRegistry
from openexecutive.bo.packages.verify import (
    Approval,
    Verdict,
    artifact_set_digest,
    verify_package,
)
from openexecutive.bo.settings import store as settings_store

BO_PRODUCT_VERSION = "1.0.0"  # runtime host version checked against compatibility
_CHUNK = 64 * 1024


def _quarantine_root() -> Path:
    return Path(os.environ.get("BO_PACKAGES_DIR", "./bo_packages")).resolve()


def _audit(tenant: str, actor: str, event_type: str, summary: str,
           details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    log_event(event_type, summary, actor=actor,
              details={"tenant": tenant, **details})


def _setting(identity: Identity, key: str, db_path: Path | None) -> Any:
    return settings_store.get_effective_value(identity.tenant, key, db_path=db_path)


def _trust_registry(identity: Identity, db_path: Path | None) -> TrustRegistry:
    raw = _setting(identity, "bo.packages.trust_store_json", db_path)
    try:
        doc = json.loads(raw) if raw else None
    except json.JSONDecodeError as e:
        raise PackageReject("INVALID_REGISTRY", "trust store JSON invalid") from e
    if doc is None:
        raise PackageReject("INVALID_REGISTRY", "trust store gol — înrolați un emitent")
    return TrustRegistry.from_dict(doc)


def _approvals(identity: Identity, package_id: str,
               db_path: Path | None) -> tuple[list[Approval], set[str]]:
    out = []
    consumed: set[str] = set()
    for r in store.list_approvals(identity.tenant, package_id, db_path=db_path):
        try:
            exp = datetime.fromisoformat(r["expires_at"])
        except (TypeError, ValueError):
            continue
        if r["consumed_at"] is not None:
            consumed.add(r["id"])
        out.append(Approval(
            approval_id=r["id"], package_id=r["package_id"],
            to_version=r["to_version"], tenant_ref=r["tenant"],
            from_version=r["from_version"],
            artifact_set_digest=r["artifact_set_digest"], expires_at=exp,
            consumed=r["consumed_at"] is not None, status=r["status"]))
    return out, consumed


def _copy_verified(src: Path, dest_root: Path,
                   digests: dict[str, str]) -> Path:
    """Copy declared artifacts into the quarantine store, re-hashing while
    copying — a file changed between verify and import is ARTIFACT_DRIFT."""
    dest_root.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    try:
        for rel, declared in digests.items():
            s = src / rel
            d = dest_root / rel
            d.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            with open(s, "rb") as fi, open(d, "wb") as fo:
                while chunk := fi.read(_CHUNK):
                    fo.write(chunk)
                    h.update(chunk)
            if f"sha256:{h.hexdigest()}" != declared:
                raise PackageReject(
                    "ARTIFACT_DRIFT", f"{rel} changed between verify and import")
            written.append(d)
        # manifest itself is copied for audit, outside the signed set
        shutil.copy2(src / MANIFEST_NAME, dest_root / MANIFEST_NAME)
    except BaseException:
        for p in written:
            p.unlink(missing_ok=True)
        raise
    return dest_root


def import_package(
    identity: Identity,
    source_dir: Path,
    *,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Verify a local package dir and quarantine it. Returns the import row +
    verdict. Idempotent for identical (package, version, artifact set)."""
    require(identity, "packages:write")
    if not _setting(identity, "bo.packages.enabled", db_path):
        raise PackageReject("PACKAGES_DISABLED", "importul de pachete este oprit")

    source_dir = Path(source_dir)
    # Manifest first: needed for the idempotency check before full verify.
    # An unparseable manifest yields a controlled REJECT verdict — no row is
    # persisted because the package cannot be identified.
    try:
        manifest, _raw = load_manifest_raw(source_dir / MANIFEST_NAME)
        manifest = validate_manifest(manifest)
    except PackageReject as e:
        v = Verdict(False, e.code, e.detail, tenant_ref=identity.tenant)
        _audit(identity.tenant, identity.actor, "bo_package_import_rejected",
               f"manifest invalid: {e.code}", v.to_dict())
        return {"verdict": v.to_dict(), "idempotent": False}
    asd = artifact_set_digest(manifest["artifactDigests"])
    # manifestDigest = sha256 over the canonical signed payload — the same
    # definition the verifier binds into verdicts (contract §5)
    m_digest = f"sha256:{hashlib.sha256(signed_payload(manifest)).hexdigest()}"
    pkg_id, version = manifest["packageId"], manifest["version"]

    existing = store.find_import(identity.tenant, pkg_id, version, db_path=db_path)
    if existing is not None:
        # Idempotent only when the *manifest* is identical — same artifact set
        # under a different manifest (e.g. changed capabilities) must be
        # verified, not skipped.
        if existing["manifest_digest"] == m_digest:
            return {**existing, "idempotent": True}
        verdict = Verdict(
            False, "VERSION_CONFLICT",
            f"{version} exists with manifest {existing['manifest_digest']}",
            package_id=pkg_id, version=version, artifact_set_digest=asd,
            tenant_ref=identity.tenant)
        _audit(identity.tenant, identity.actor, "bo_package_import_rejected",
               f"conflict {pkg_id}@{version}", verdict.to_dict())
        return {"verdict": verdict.to_dict(), "idempotent": False}

    registry = _trust_registry(identity, db_path)
    installed = store.list_installed(identity.tenant, db_path=db_path)
    approvals, consumed = _approvals(identity, pkg_id, db_path)

    verdict = verify_package(
        source_dir, registry,
        tenant_ref=identity.tenant, installed=installed,
        approvals=approvals, consumed_approvals=consumed,
        host_version=BO_PRODUCT_VERSION,
        operator_max_bytes=int(
            _setting(identity, "bo.packages.max_package_bytes", db_path)),
        operator_rollback_requires_approval=bool(_setting(
            identity, "bo.packages.rollback_requires_approval", db_path)))

    if not verdict.accepted:
        row_id = store.insert_import(
            {"tenant": identity.tenant, "package_id": pkg_id, "version": version,
             "kind": manifest["kind"], "publisher_id": manifest["publisherId"],
             "key_id": manifest["keyId"],
             "manifest_digest": verdict.manifest_digest or "",
             "artifact_set_digest": asd, "status": "REJECTED",
             "verdict_json": json.dumps(verdict.to_dict()),
             "source_path": str(source_dir), "actor": identity.actor},
            db_path=db_path)
        _audit(identity.tenant, identity.actor, "bo_package_import_rejected",
               f"respins {pkg_id}@{version}: {verdict.code}", verdict.to_dict())
        return {**store.get_import(identity.tenant, row_id, db_path=db_path),
                "idempotent": False}

    # Accepted → drift-checked copy into quarantine, then persist.
    try:
        stored = _copy_verified(
            source_dir,
            _quarantine_root() / identity.tenant / asd.replace(":", "_"),
            manifest["artifactDigests"])
    except PackageReject as e:
        return {"verdict": Verdict(False, e.code, e.detail).to_dict(),
                "idempotent": False}

    try:
        row_id = store.insert_import(
            {"tenant": identity.tenant, "package_id": pkg_id, "version": version,
             "kind": manifest["kind"], "publisher_id": manifest["publisherId"],
             "key_id": manifest["keyId"],
             "manifest_digest": verdict.manifest_digest or "",
             "artifact_set_digest": asd, "status": "QUARANTINED",
             "verdict_json": json.dumps(verdict.to_dict()),
             "source_path": str(source_dir), "stored_path": str(stored),
             "approval_id": verdict.approval_id, "actor": identity.actor},
            db_path=db_path)
    except sqlite3.IntegrityError:
        # Race: a live row for (tenant, pkg, version, asd) appeared between the
        # pre-check and the insert. The stored copy is redundant — drop it and
        # report the existing row instead of a 500.
        shutil.rmtree(stored, ignore_errors=True)
        raced = store.find_import(identity.tenant, pkg_id, version,
                                  db_path=db_path)
        if raced is not None and raced["manifest_digest"] == m_digest:
            return {**raced, "idempotent": True}
        v = Verdict(False, "VERSION_CONFLICT",
                    f"{version} already exists", package_id=pkg_id,
                    version=version, artifact_set_digest=asd,
                    tenant_ref=identity.tenant)
        return {"verdict": v.to_dict(), "idempotent": False}
    if verdict.approval_id:
        store.consume_approval(identity.tenant, verdict.approval_id, row_id,
                               db_path=db_path)
    _audit(identity.tenant, identity.actor, "bo_package_imported",
           f"carantină {pkg_id}@{version}",
           {"import_id": row_id, "artifact_set_digest": asd})
    return {**store.get_import(identity.tenant, row_id, db_path=db_path),
            "idempotent": False}


def promote_to_draft(identity: Identity, import_id: str,
                     *, db_path: Path | None = None) -> dict[str, Any]:
    """QUARANTINED → DRAFT after re-verifying the stored copy (drift check)."""
    require(identity, "packages:write")
    row = store.get_import(identity.tenant, import_id, db_path=db_path)
    if row["status"] != "QUARANTINED":
        raise store.StateError(f"status {row['status']} nu permite promovarea")

    stored = Path(row["stored_path"] or "")
    m = validate_manifest(load_manifest(stored / MANIFEST_NAME))
    if artifact_set_digest(m["artifactDigests"]) != row["artifact_set_digest"]:
        raise PackageReject("ARTIFACT_DRIFT", "manifestul stocat diferă")
    for rel, declared in m["artifactDigests"].items():
        h = hashlib.sha256((stored / rel).read_bytes()).hexdigest()
        if f"sha256:{h}" != declared:
            raise PackageReject(
                "ARTIFACT_DRIFT", f"{rel} modificat în carantină")

    store.set_status(identity.tenant, import_id, "DRAFT", db_path=db_path)
    _audit(identity.tenant, identity.actor, "bo_package_promoted",
           f"draft {row['package_id']}@{row['version']}", {"import_id": import_id})
    return store.get_import(identity.tenant, import_id, db_path=db_path)


def create_approval(
    identity: Identity,
    *,
    package_id: str,
    from_version: str,
    to_version: str,
    artifact_set_digest_value: str,
    expires_at: str,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Tenant-bound downgrade approval — bound to digest, expiring, single-use."""
    require(identity, "packages:write")
    try:
        exp = datetime.fromisoformat(expires_at)
    except (TypeError, ValueError):
        raise PackageReject("APPROVAL_INVALID", "expires_at nu e RFC3339") from None
    if exp.tzinfo is None or exp <= datetime.now(UTC):
        raise PackageReject("APPROVAL_INVALID", "expires_at trebuie să fie în viitor")
    row = {
        "tenant": identity.tenant, "package_id": package_id,
        "from_version": from_version, "to_version": to_version,
        "artifact_set_digest": artifact_set_digest_value,
        "expires_at": exp.isoformat(), "created_by": identity.actor,
    }
    row_id = store.insert_approval(row, db_path=db_path)
    _audit(identity.tenant, identity.actor, "bo_package_approval",
           f"aprobare downgrade {package_id} {from_version}→{to_version}",
           {"approval_id": row_id})
    return {"id": row_id, **row}


def list_imports(identity: Identity, *, db_path: Path | None = None) -> list[dict[str, Any]]:
    require(identity, "packages:read")
    return store.list_imports(identity.tenant, db_path=db_path)


def get_import(identity: Identity, import_id: str,
               *, db_path: Path | None = None) -> dict[str, Any]:
    require(identity, "packages:read")
    return store.get_import(identity.tenant, import_id, db_path=db_path)


def list_approvals(identity: Identity,
                   *, db_path: Path | None = None) -> list[dict[str, Any]]:
    require(identity, "packages:read")
    return store.list_approvals(identity.tenant, db_path=db_path)


def revoke_approval(identity: Identity, approval_id: str,
                    *, db_path: Path | None = None) -> None:
    require(identity, "packages:write")
    store.revoke_approval(identity.tenant, approval_id, db_path=db_path)
    _audit(identity.tenant, identity.actor, "bo_package_approval_revoked",
           "aprobare revocată", {"approval_id": approval_id})
