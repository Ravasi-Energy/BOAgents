"""bo.package.v1 verification — contract §4 order, fail-fast, drift-aware.

Limits are enforced before and *during* reads: per-file size is checked from
stat before opening, and the running total aborts mid-stream the moment the
package budget is exceeded. A nested `manifest.json` is an ordinary artifact —
only the root manifest is exempt from the unsigned-file check. No symlink is
permitted anywhere in the package (root, manifest, path components, on-disk
inventory).

`verify_package` performs no writes and no external effects. It returns a
`Verdict` serialized as `bo.package.verdict.v1` — the shared verdict shape of
the contract decision (A02's independent verifier emits the same document).
"""
from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from openexecutive.bo.packages.canon import signed_payload
from openexecutive.bo.packages.contract import (
    MANIFEST_NAME,
    load_manifest_raw,
    parse_semver,
    semver_cmp,
    validate_manifest,
)
from openexecutive.bo.packages.errors import PackageReject
from openexecutive.bo.packages.registry import TrustRegistry
from openexecutive.bo.packages.signing import public_from_b64, verify

_CHUNK = 64 * 1024
_SCAN_MAX_DEPTH = 64
VERDICT_TTL = timedelta(minutes=15)
VERDICT_SCHEMA = "bo.package.verdict.v1"


@dataclass(frozen=True)
class Approval:
    """Administered downgrade approval. An arbitrary approvalRef is NOT
    authority — every binding is mandatory and must match
    (tenant/package/from/to/digest), unexpired and unconsumed. In BOAgents
    approvals are tenant DB rows (store.py); the shared context form is the
    snake_case dict parsed strictly by `parse_approvals`."""
    approval_id: str
    tenant_ref: str
    package_id: str
    from_version: str
    to_version: str
    artifact_set_digest: str
    expires_at: datetime
    consumed: bool = False
    status: str = "active"  # active | revoked

    def matches(self, *, tenant_ref: str, package_id: str, from_version: str,
                to_version: str, artifact_set_digest: str,
                now: datetime) -> bool:
        """All bindings are mandatory (contract §8) — an approval missing a
        binding cannot be constructed, and a non-matching one never
        authorizes."""
        return (
            self.status == "active" and not self.consumed
            and self.tenant_ref == tenant_ref
            and self.package_id == package_id
            and self.from_version == from_version
            and self.to_version == to_version
            and self.artifact_set_digest == artifact_set_digest
            and now.astimezone(UTC) <= self.expires_at.astimezone(UTC))


@dataclass
class Verdict:
    """Verifier output — `bo.package.verdict.v1`. Fields left empty ("") when
    not determinable (e.g. a manifest that never parsed). A verdict is not an
    execution approval and carries no signature: authenticity comes from the
    verified service channel that delivers it."""
    accepted: bool
    code: str = "ACCEPT"
    detail: str = ""
    checks: list[str] = field(default_factory=list)
    package_id: str | None = None
    version: str | None = None
    manifest_digest: str | None = None
    artifact_set_digest: str | None = None
    tenant_ref: str | None = None
    publisher_id: str | None = None
    key_id: str | None = None
    policy_version: str | None = None
    trust_version: str | None = None
    checked_at: str | None = None
    expires_at: str | None = None
    # set when a matching downgrade approval was found; the importer consumes it
    approval_id: str | None = None
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        if self.accepted:
            reasons = []
        else:
            # common form: one entry, "<CODE>: <detail>" (detail optional)
            reasons = [f"{self.code}: {self.detail}"
                       if self.detail else self.code]
        doc: dict[str, Any] = {
            "schemaVersion": VERDICT_SCHEMA,
            "verdict": "ACCEPT" if self.accepted else "REJECT",
            "reasons": reasons,
            "packageId": self.package_id or "",
            "version": self.version or "",
            "manifestDigest": self.manifest_digest or "",
            "artifactSetDigest": self.artifact_set_digest or "",
            "tenantRef": self.tenant_ref or "",
            "publisherId": self.publisher_id or "",
            "keyId": self.key_id or "",
            "policyVersion": self.policy_version or "",
            "trustVersion": self.trust_version or "",
            "checkedAt": self.checked_at or "",
            "expiresAt": self.expires_at or "",
            "idempotent": self.idempotent,
        }
        if self.approval_id:
            doc["approvalRef"] = self.approval_id
        return doc


def artifact_set_digest(digests: dict[str, str]) -> str:
    """sha256 over the compact JSON of the sorted `[path, digest]` pair list
    (contract §2) — identical bytes in every implementation."""
    pairs = [[path, digests[path]] for path in sorted(digests)]
    blob = json.dumps(pairs, separators=(",", ":"),
                      ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(blob).hexdigest()


def _reject(code: str, detail: str, checks: list[str], **kw: Any) -> Verdict:
    return Verdict(False, code, detail, checks, **kw)


def _hash_file_limited(path: Path, budget: int) -> tuple[str, int]:
    """Stream-hash `path`; abort mid-read if `budget` bytes are exceeded."""
    h = hashlib.sha256()
    total = 0
    with open(path, "rb") as f:
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > budget:
                raise PackageReject(
                    "PACKAGE_TOO_LARGE", f"artifact {path.name} exceeds budget")
            h.update(chunk)
    return f"sha256:{h.hexdigest()}", total


def _check_components(pkg_root: Path, rel: str) -> None:
    """Every path component checked with is_symlink() BEFORE resolve — any
    symlink (file, directory, dangling) is TRAVERSAL."""
    cur = pkg_root
    for seg in rel.split("/"):
        cur = cur / seg
        if cur.is_symlink():
            raise PackageReject("TRAVERSAL", f"symlink component: {rel!r}")


def _iter_on_disk(pkg_root: Path) -> tuple[set[str], set[str], bool]:
    """Iterative on-disk inventory (no symlink following): returns
    (relative files, symlink paths, inventory_errors). An unreadable dir or a
    non-regular file is an error — we cannot prove the absence of unsigned
    artifacts (fail-closed)."""
    files: set[str] = set()
    links: set[str] = set()
    errors = False
    stack: list[tuple[Path, str, int]] = [(pkg_root, "", 0)]
    while stack:
        d, prefix, depth = stack.pop()
        if depth > _SCAN_MAX_DEPTH:
            errors = True
            continue
        try:
            entries = sorted(os.scandir(d), key=lambda e: e.name)
        except OSError:
            errors = True
            continue
        for e in entries:
            rel = f"{prefix}{e.name}"
            if e.is_symlink():
                links.add(rel)
            elif e.is_dir(follow_symlinks=False):
                stack.append((Path(e.path), rel + "/", depth + 1))
            elif e.is_file(follow_symlinks=False):
                files.add(rel)
            else:
                errors = True  # fifo/socket/device — not a signed artifact
    return files, links, errors


def _normalize_installed(installed: dict[str, Any] | None) -> dict[str, dict]:
    """Accepts both conventions: {pkg: "1.2.0"} (digest unknown) or
    {pkg: {"version": ..., "artifactSetDigest": ...}}. Malformed state is
    fail-closed — a downgrade must not hide behind 'no known version'."""
    out: dict[str, dict] = {}
    for pkg, val in (installed or {}).items():
        if isinstance(val, str):
            out[pkg] = {"version": val, "artifactSetDigest": None}
        elif isinstance(val, dict) and isinstance(val.get("version"), str):
            out[pkg] = {"version": val["version"],
                        "artifactSetDigest": val.get("artifactSetDigest")}
        else:
            raise PackageReject(
                "REGISTRY_UNAVAILABLE", f"installed[{pkg}] malformed")
    return out


def parse_approvals(raw: Any) -> list[Approval]:
    """Administered approval list from context/state — the canonical
    snake_case dict form with ALL bindings required. Malformed entries are
    fail-closed (REGISTRY_UNAVAILABLE), never silently skipped."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise PackageReject("REGISTRY_UNAVAILABLE", "approvals is not a list")
    out = []
    required = {"approval_id", "tenant_ref", "package_id", "from_version",
                "to_version", "artifact_set_digest", "expires_at"}
    for doc in raw:
        if not isinstance(doc, dict) or set(doc) != required:
            raise PackageReject("REGISTRY_UNAVAILABLE", "approval malformed")
        for f in required - {"expires_at"}:
            if not isinstance(doc[f], str) or not doc[f] or len(doc[f]) > 512:
                raise PackageReject(
                    "REGISTRY_UNAVAILABLE", f"approval.{f} invalid")
        try:
            parse_semver(doc["from_version"])
            parse_semver(doc["to_version"])
        except PackageReject:
            raise PackageReject(
                "REGISTRY_UNAVAILABLE", "approval versions invalid") from None
        try:
            exp = datetime.fromisoformat(str(doc["expires_at"]))
        except (TypeError, ValueError):
            raise PackageReject(
                "REGISTRY_UNAVAILABLE", "approval.expires_at invalid") from None
        if exp.tzinfo is None or exp.utcoffset() is None:
            raise PackageReject(
                "REGISTRY_UNAVAILABLE", "approval.expires_at lacks timezone")
        out.append(Approval(
            approval_id=doc["approval_id"], tenant_ref=doc["tenant_ref"],
            package_id=doc["package_id"], from_version=doc["from_version"],
            to_version=doc["to_version"],
            artifact_set_digest=doc["artifact_set_digest"], expires_at=exp))
    return out


def verify_package(
    pkg_dir: Path,
    registry: TrustRegistry,
    *,
    tenant_ref: str,
    installed: dict[str, Any] | None = None,
    approvals: Iterable[Approval] = (),
    consumed_approvals: Iterable[str] = (),
    host_version: str = "1.0.0",
    now: datetime | None = None,
    verdict_ttl: timedelta = VERDICT_TTL,
    operator_max_bytes: int | None = None,
    operator_rollback_requires_approval: bool | None = None,
) -> Verdict:
    """Contract §4 order — short-circuit at the first rejection."""
    now = now or datetime.now(UTC)
    installed_norm = _normalize_installed(installed)
    consumed = set(consumed_approvals)
    candidates = list(approvals)
    checks: list[str] = []
    base: dict[str, Any] = {
        "tenant_ref": tenant_ref,
        "policy_version": registry.policy.get("policyVersion", ""),
        "trust_version": registry.version,
        "checked_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "expires_at": (now + verdict_ttl).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    # 0. the package root itself cannot be a symlink — checked on the
    #    original path BEFORE resolve()
    if pkg_dir.is_symlink():
        return _reject("TRAVERSAL", "package root is a symlink", checks, **base)
    try:
        pkg_root = pkg_dir.resolve(strict=True)
    except OSError as e:
        return _reject("ARTIFACT_MISSING", f"inaccessible package: {e}",
                       checks, **base)
    if not pkg_root.is_dir():
        return _reject("ARTIFACT_MISSING", "package path is not a directory",
                       checks, **base)

    # 1. manifest parse (size cap before+read-time, dup keys, constants) +
    #    schema. manifestDigest = sha256 over the canonical signed payload —
    #    the exact bytes the signature binds (contract §5).
    mpath = pkg_root / MANIFEST_NAME
    if mpath.is_symlink():
        return _reject("TRAVERSAL", "manifest.json is a symlink", checks, **base)
    try:
        manifest, _raw = load_manifest_raw(mpath)
        manifest = validate_manifest(manifest)
        m_digest = f"sha256:{hashlib.sha256(signed_payload(manifest)).hexdigest()}"
    except PackageReject as e:
        return _reject(e.code, e.detail, checks, **base)
    base.update(
        package_id=manifest["packageId"], version=manifest["version"],
        publisher_id=manifest["publisherId"], key_id=manifest["keyId"],
        manifest_digest=m_digest,
        artifact_set_digest=artifact_set_digest(manifest["artifactDigests"]),
    )
    checks.append("manifest")

    # 2. publisher — from the administered trust store, never the package
    pub = registry.publisher(manifest["publisherId"])
    if pub is None:
        return _reject("PUBLISHER_UNKNOWN", manifest["publisherId"], checks, **base)
    if pub["status"] != "active":
        return _reject("PUBLISHER_SUSPENDED", manifest["publisherId"], checks, **base)
    checks.append("publisher")

    # 3. key — top-level in the registry, bound to publisher.keyIds
    key = registry.key(manifest["keyId"])
    if key is None or manifest["keyId"] not in pub["keyIds"]:
        return _reject("KEY_UNKNOWN", manifest["keyId"], checks, **base)
    if key["status"] == "revoked":
        return _reject("KEY_REVOKED", manifest["keyId"], checks, **base)
    if not registry.key_window_ok(key, now):
        return _reject("KEY_EXPIRED", manifest["keyId"], checks, **base)
    checks.append("key")

    # 4. Ed25519 signature over the canonicalized payload without `signature`
    try:
        pk = public_from_b64(key["publicKey"])
    except PackageReject as e:
        return _reject(e.code, e.detail, checks, **base)
    if not verify(pk, signed_payload(manifest), manifest["signature"]["value"]):
        return _reject("BAD_SIGNATURE", "signature does not verify", checks, **base)
    checks.append("signature")

    # 5. artifacts — every path component symlink-checked before resolve;
    #    size budget at stat and incrementally during the read; then the
    #    on-disk inventory: any symlink → TRAVERSAL, unproven inventory →
    #    UNSIGNED_ARTIFACT, undeclared file → UNSIGNED_ARTIFACT. Only the
    #    ROOT manifest.json is exempt.
    # Operator cap (bo.packages.max_package_bytes) tightens the trust-store
    # policy — never loosens it.
    max_bytes = registry.policy["maxPackageBytes"]
    if operator_max_bytes is not None:
        max_bytes = min(max_bytes, operator_max_bytes)
    signed_paths = set(manifest["artifactDigests"])
    total = 0
    for rel, declared in manifest["artifactDigests"].items():
        try:
            _check_components(pkg_root, rel)
        except PackageReject as e:
            return _reject(e.code, e.detail, checks, **base)
        target = pkg_root / rel
        try:
            st = target.stat()
            resolved = target.resolve(strict=True)
        except (OSError, RuntimeError):
            return _reject("ARTIFACT_MISSING", rel, checks, **base)
        if not resolved.is_file():
            return _reject("ARTIFACT_MISSING", rel, checks, **base)
        if not resolved.is_relative_to(pkg_root):
            return _reject("TRAVERSAL", rel, checks, **base)
        if st.st_size > max_bytes - total:
            return _reject(
                "PACKAGE_TOO_LARGE", f"{rel}: {st.st_size}B over remaining budget",
                checks, **base)
        try:
            actual, n = _hash_file_limited(resolved, max_bytes - total)
        except PackageReject as e:
            return _reject(e.code, e.detail, checks, **base)
        total += n
        if actual != declared:
            return _reject("ARTIFACT_MODIFIED", rel, checks, **base)
    on_disk, links, walk_errors = _iter_on_disk(pkg_root)
    if links:
        return _reject(
            "TRAVERSAL", f"symlink in package: {sorted(links)[0]!r}",
            checks, **base)
    if walk_errors:
        return _reject(
            "UNSIGNED_ARTIFACT",
            "incomplete inventory — cannot prove no unsigned files",
            checks, **base)
    extra = on_disk - signed_paths - {MANIFEST_NAME}
    if extra:
        return _reject("UNSIGNED_ARTIFACT", sorted(extra)[0], checks, **base)
    checks.append("artifacts")

    # 6. host compatibility — against the consuming product's runtime version
    compat = manifest["compatibility"]
    host = parse_semver(host_version)
    if compat["minHost"] and semver_cmp(host, parse_semver(compat["minHost"])) < 0:
        return _reject(
            "INCOMPATIBLE", f"host {host_version} < {compat['minHost']}",
            checks, **base)
    if compat["maxHost"] and semver_cmp(host, parse_semver(compat["maxHost"])) > 0:
        return _reject(
            "INCOMPATIBLE", f"host {host_version} > {compat['maxHost']}",
            checks, **base)
    checks.append("compatibility")

    # 7. capabilities + kind — catalog ⊆ requested, per-kind limit, and the
    #    kind must be allowed by policy AND by the publisher
    catalog = set(registry.policy["capabilityCatalog"])
    for cap in manifest["requestedCapabilities"]:
        if cap not in catalog:
            return _reject("CAPABILITY_UNKNOWN", cap, checks, **base)
    cap_limit = registry.max_caps_for(manifest["kind"])
    if cap_limit is not None and len(manifest["requestedCapabilities"]) > cap_limit:
        return _reject(
            "CAPABILITY_EXCESSIVE",
            f"{len(manifest['requestedCapabilities'])} > {cap_limit} for kind={manifest['kind']}",
            checks, **base)
    if manifest["kind"] not in set(registry.policy["allowedKinds"]) or \
            manifest["kind"] not in set(pub["allowedKinds"]):
        return _reject("KIND_NOT_ALLOWED", manifest["kind"], checks, **base)
    checks.append("capabilities")

    # 8. version semantics — same version + same artifactSetDigest is an
    #    idempotent reimport (no new effect); same version + different or
    #    unknown digest is VERSION_CONFLICT; lower version needs a bound,
    #    unexpired, unconsumed administered approval.
    asd = base["artifact_set_digest"]
    prior = installed_norm.get(manifest["packageId"])
    if prior is not None:
        cmp_ = semver_cmp(parse_semver(manifest["version"]),
                          parse_semver(prior["version"]))
        if cmp_ == 0:
            if prior["artifactSetDigest"] is not None and \
                    prior["artifactSetDigest"] == asd:
                return Verdict(True, checks=checks, idempotent=True, **base)
            return _reject(
                "VERSION_CONFLICT",
                f"{manifest['version']} already present with a different or unknown digest",
                checks, **base)
        if cmp_ < 0:
            # Two distinct gates: trust-store ``rollbackRequiresApproval``
            # False means downgrades are switched OFF entirely (a tenant
            # setting never re-enables what the enrolled registry forbids);
            # when the registry allows them, the operator setting
            # ``bo.packages.rollback_requires_approval`` decides whether a
            # bound approval is demanded (True/default) or the downgrade is
            # permitted freely (False — the admin owns both documents).
            if not registry.policy["rollbackRequiresApproval"]:
                return _reject(
                    "ROLLBACK_UNAUTHORIZED",
                    "rollback not enabled in policy", checks, **base)
            if operator_rollback_requires_approval is False:
                checks.append("versioning")
                return Verdict(True, checks=checks, **base)
            match = next(
                (a for a in candidates
                 if a.approval_id not in consumed and a.matches(
                     tenant_ref=tenant_ref, package_id=manifest["packageId"],
                     from_version=prior["version"],
                     to_version=manifest["version"],
                     artifact_set_digest=asd, now=now)),
                None)
            if match is None:
                return _reject(
                    "ROLLBACK_UNAUTHORIZED",
                    f"{manifest['version']} < installed {prior['version']} "
                    "without a bound, unexpired, unconsumed approval",
                    checks, **base)
            base["approval_id"] = match.approval_id
    checks.append("versioning")

    return Verdict(True, checks=checks, **base)
