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
    MANIFEST_MAX_BYTES,
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


def _manifest_limit_bytes(
    identity: Identity, db_path: Path | None
) -> tuple[int, str | None]:
    """Effective manifest byte cap, from ``bo.packages.manifest_max_kb`` (KiB).

    The setting can only *restrict* the 256 KiB protocol ceiling (spec 0002
    AC8). Reads the stored row explicitly: a historical value outside the
    current 16..256 interval is kept untouched (evidence), reported as
    incompatible, and the effective ceiling applies — never a silent clamp
    and never above the protocol cap.
    """
    stored, is_stored = settings_store.get_stored_value(
        identity.tenant, "bo.packages.manifest_max_kb", db_path=db_path)
    if not is_stored:
        return MANIFEST_MAX_BYTES, None
    try:
        # `int()` accepta si bool/str/float — o valoare memorata printr-un
        # scriitor care a ocolit contractul tipat se aplica restrictiv,
        # dar se si raporteaza; Infinity/NaN trec de json.loads si ar fi
        # crampat toata calea fara OverflowError aici
        if isinstance(stored, bool) or not isinstance(stored, int):
            kb = int(stored)
            return min(kb, 256) * 1024, (
                f"valoare memorata de tip {type(stored).__name__} "
                f"({stored!r}), nu int — plafonul efectiv se aplica "
                "restrictiv pana la corectarea autorizata")
        kb = stored
    except (TypeError, ValueError, OverflowError):
        return MANIFEST_MAX_BYTES, (
            f"valoare memorata invalida {stored!r} — plafonul efectiv "
            "ramane 256 KiB pana la corectarea autorizata")
    diag = None
    if kb > 256:
        diag = (f"valoare memorata {kb}KiB peste plafonul protocolului "
                "256KiB — plafonul efectiv ramane 256 KiB pana la "
                "corectarea autorizata")
    elif kb < 16:
        diag = (f"valoare memorata {kb}KiB sub intervalul administrat "
                "16..256KiB — se aplica valoarea memorata (mai restrictiva) "
                "si se raporteaza incompatibilitatea")
    return min(kb, 256) * 1024, diag


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
    manifest_lim, manifest_diag = _manifest_limit_bytes(identity, db_path)
    if manifest_diag:
        _audit(identity.tenant, identity.actor,
               "bo_setting_manifest_limit_incompatible", manifest_diag,
               {"setting": "bo.packages.manifest_max_kb"})
    # Manifest first: needed for the idempotency check before full verify.
    # An unparseable manifest yields a controlled REJECT verdict — no row is
    # persisted because the package cannot be identified.
    try:
        manifest, _raw = load_manifest_raw(
            source_dir / MANIFEST_NAME, limita_bytes=manifest_lim)
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
            identity, "bo.packages.rollback_requires_approval", db_path)),
        operator_manifest_max_bytes=manifest_lim)

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
    manifest_lim, manifest_diag = _manifest_limit_bytes(identity, db_path)
    if manifest_diag:
        _audit(identity.tenant, identity.actor,
               "bo_setting_manifest_limit_incompatible", manifest_diag,
               {"setting": "bo.packages.manifest_max_kb"})
    m = validate_manifest(
        load_manifest(stored / MANIFEST_NAME, limita_bytes=manifest_lim))
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


BOBOT_ARTIFACT = "bobot.json"


def activate_bobot(identity: Identity, import_id: str,
                   *, db_path: Path | None = None) -> dict[str, Any]:
    """DRAFT + kind=bobot → explicit materialization into a tenant draft
    BoBot definition (AC4 / spec 0002).

    Re-runs the full verify pipeline on the stored copy — signature, trust
    store, revocation, digests, compatibility, capabilities — so a trust
    revoked or an artifact drifted *after* the original import is refused
    here, not at some later implicit step. ``bobot.json`` (the contract
    convention at the artifact root) is validated against the closed-world
    ``bo.bobot.v1`` grammar; the result is a DRAFT definition bound to
    ``(import_id, artifact_set_digest)``. Publishing the bot stays a
    separate CAS action; nothing here executes effects or arbitrary code.
    Idempotent: repeating the same action on the same digest returns the
    already-materialized draft instead of duplicating it.
    """
    require(identity, "packages:write")
    from openexecutive.bo.bots import service as bots_service
    from openexecutive.bo.bots import store as bots_store

    row = store.get_import(identity.tenant, import_id, db_path=db_path)
    if row["kind"] != "bobot":
        raise PackageReject(
            "KIND_NOT_ALLOWED",
            "activarea generică materializează doar pachete kind=bobot")
    if row["status"] != "DRAFT":
        raise store.StateError(
            f"status {row['status']} nu permite activarea — cere DRAFT")

    stored = Path(row["stored_path"] or "")
    manifest_lim, manifest_diag = _manifest_limit_bytes(identity, db_path)
    if manifest_diag:
        _audit(identity.tenant, identity.actor,
               "bo_setting_manifest_limit_incompatible", manifest_diag,
               {"setting": "bo.packages.manifest_max_kb"})
    m = validate_manifest(
        load_manifest(stored / MANIFEST_NAME, limita_bytes=manifest_lim))
    if artifact_set_digest(m["artifactDigests"]) != row["artifact_set_digest"]:
        raise PackageReject("ARTIFACT_DRIFT", "manifestul stocat diferă")

    registry = _trust_registry(identity, db_path)
    verdict = verify_package(
        stored, registry,
        tenant_ref=identity.tenant,
        installed=store.list_installed(identity.tenant, db_path=db_path),
        approvals=[], consumed_approvals=set(),
        host_version=BO_PRODUCT_VERSION,
        operator_max_bytes=int(_setting(
            identity, "bo.packages.max_package_bytes", db_path)),
        operator_manifest_max_bytes=manifest_lim,
        operator_rollback_requires_approval=bool(_setting(
            identity, "bo.packages.rollback_requires_approval", db_path)))
    if not verdict.accepted:
        _audit(identity.tenant, identity.actor,
               "bo_package_activate_rejected",
               f"activare refuzată {row['package_id']}@{row['version']}: "
               f"{verdict.code}", verdict.to_dict())
        raise PackageReject(verdict.code, verdict.detail or verdict.code)

    # Artifact drift: every declared file is re-hashed before parsing —
    # a file changed inside quarantine refuses as ARTIFACT_DRIFT.
    for rel, declared in m["artifactDigests"].items():
        h = hashlib.sha256((stored / rel).read_bytes()).hexdigest()
        if f"sha256:{h}" != declared:
            raise PackageReject(
                "ARTIFACT_DRIFT", f"{rel} modificat în carantină")
    if BOBOT_ARTIFACT not in m["artifactDigests"]:
        raise PackageReject(
            "INVALID_MANIFEST",
            f"pachetul kind=bobot nu declară {BOBOT_ARTIFACT} în "
            "artifactDigests")
    try:
        raw = json.loads((stored / BOBOT_ARTIFACT).read_bytes())
    except json.JSONDecodeError as exc:
        raise PackageReject(
            "INVALID_MANIFEST", f"{BOBOT_ARTIFACT} nu e JSON valid") from exc
    try:
        content = bots_service.validate_content(raw)
    except bots_service.ValidationFailure as exc:
        raise PackageReject(
            "INVALID_MANIFEST",
            f"{BOBOT_ARTIFACT} nu respectă bo.bobot.v1: {exc}") from exc

    # Idempotent materialization: the same import + same digest returns the
    # existing draft; a different digest under the same import is drift.
    asd = row["artifact_set_digest"]
    existing = bots_store.find_definition_by_source(
        identity.tenant, import_id, db_path=db_path)
    if existing is not None:
        if existing["source_artifact_set_digest"] == asd:
            return {"import": row, "definition": existing,
                    "idempotent": True, "package_state": row["status"],
                    "definition_state": existing["status"]}
        raise PackageReject(
            "VERSION_CONFLICT",
            "definiția materializată aparține altui artifactSetDigest")

    canon = bots_service._canonical(content.model_dump(mode="json"))
    max_defs = bots_store.effective_limit(
        int(settings_store.get_effective_value(
            identity.tenant, "bo.bobot.max_definitions", db_path=db_path)),
        bots_store.MAX_DEFINITIONS_HARD_CAP)
    try:
        definition = bots_store.create_definition(
            identity.tenant,
            name=str(m["packageId"])[:120],
            description=(f"Materializat din pachet {m['packageId']}@"
                         f"{m['version']} (import {import_id})"),
            kind="BOT",
            owner=identity.actor,
            draft_content_json=canon,
            content_hash=bots_service.content_hash(
                content.model_dump(mode="json")),
            schema_version=bots_service.SCHEMA_VERSION,
            max_definitions=max_defs,
            source_import_id=import_id,
            source_artifact_set_digest=asd,
            db_path=db_path,
        )
    except bots_store.LimitExceededError as exc:
        raise PackageReject("LIMIT_EXCEEDED", str(exc)) from exc
    except sqlite3.IntegrityError:
        # Race: two activations of the same import — exactly one wins the
        # unique (tenant, source_import_id) slot; report the winner's draft.
        raced = bots_store.find_definition_by_source(
            identity.tenant, import_id, db_path=db_path)
        if raced is not None and raced["source_artifact_set_digest"] == asd:
            return {"import": row, "definition": raced, "idempotent": True,
                    "package_state": row["status"],
                    "definition_state": raced["status"]}
        raise

    _audit(identity.tenant, identity.actor, "bo_package_activated",
           f"materializat {m['packageId']}@{m['version']} → "
           f"{definition['id']} (draft)",
           {"import_id": import_id, "artifact_set_digest": asd,
            "definition_id": definition["id"]})
    return {"import": store.get_import(identity.tenant, import_id,
                                       db_path=db_path),
            "definition": definition, "idempotent": False,
            "package_state": row["status"], "definition_state": "draft"}


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
