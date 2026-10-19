"""bo.package.v1 manifest validation — closed schema, hard limits, semver.

VAL2-01 remediations vs the VAL2-00 prototype:
- duplicate JSON object keys rejected at parse (`load_manifest`);
- NaN/Infinity constants rejected;
- `manifest.json` may appear as a signed artifact only when nested — the
  top-level name is reserved (decision: only the root manifest is exempt);
- all limits are enforced before/during reads, never after.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from openexecutive.bo.packages.errors import PackageReject

SCHEMA_VERSION = "bo.package.v1"
KINDS = {"bobot", "agent", "workflow"}
MANIFEST_NAME = "manifest.json"

OPAQUE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
SEMVER = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)(-[a-z0-9.-]{1,32})?$")
VERSION_RANGE = re.compile(
    r"^(>=)?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)"
    r"(-(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*))?$"
)
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
PATH_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")
CAPABILITY = re.compile(r"^[a-z][a-z0-9:_-]{1,63}$")
B64 = re.compile(r"^[A-Za-z0-9+/]+={0,2}$")

MANIFEST_MAX_BYTES = 256 * 1024
MAX_ARTIFACTS = 256
MAX_DEPS = 16
MAX_CAPS = 32
MAX_STRING = 512

_FIELDS = {
    "schemaVersion", "packageId", "version", "kind", "publisherId", "keyId",
    "createdAt", "artifactDigests", "dependencies", "compatibility",
    "requestedCapabilities", "settingsSchemaRef", "signature",
}
_REQUIRED = _FIELDS - {"settingsSchemaRef"}


def parse_semver(text: Any) -> tuple[int, int, int, str]:
    if not isinstance(text, str) or isinstance(text, bool):
        raise PackageReject("INVALID_MANIFEST", f"semver not a string: {text!r}")
    m = SEMVER.match(text)
    if not m:
        raise PackageReject("INVALID_MANIFEST", f"invalid semver: {text!r}")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), m.group(4) or "")


def semver_cmp(a: tuple[int, int, int, str], b: tuple[int, int, int, str]) -> int:
    ka, kb = a[:3], b[:3]
    if ka != kb:
        return -1 if ka < kb else 1
    if a[3] == b[3]:
        return 0
    if a[3] == "":
        return 1
    if b[3] == "":
        return -1
    return -1 if a[3] < b[3] else 1


def _id(value: Any, field: str) -> None:
    if not isinstance(value, str) or not OPAQUE_ID.match(value):
        raise PackageReject("INVALID_MANIFEST", f"{field}: invalid identifier")


def check_artifact_path(path: str) -> None:
    """Path legality — traversal rejected here, before any I/O."""
    if path == MANIFEST_NAME:
        raise PackageReject(
            "INVALID_MANIFEST", "root manifest.json cannot be a signed artifact")
    if len(path) > 256 or path.startswith("/"):
        raise PackageReject("TRAVERSAL", f"forbidden path: {path!r}")
    if "\x00" in path or "\\" in path:
        raise PackageReject("TRAVERSAL", f"forbidden character in path: {path!r}")
    for seg in path.split("/"):
        if seg in ("", ".", "..") or not PATH_SEGMENT.match(seg):
            raise PackageReject("TRAVERSAL", f"forbidden segment in {path!r}")


def _no_dupes(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in pairs:
        if k in out:
            raise PackageReject("DUPLICATE_KEY", f"duplicate JSON key: {k!r}")
        out[k] = v
    return out


def _no_const(name: str) -> None:
    raise PackageReject("INVALID_MANIFEST", f"forbidden JSON constant: {name}")


def load_manifest_raw(
    path: Path, *, limita_bytes: int | None = None
) -> tuple[dict[str, Any], bytes]:
    """Read + parse manifest.json with the size cap applied *before* parse and
    duplicate keys rejected. Returns (parsed object, raw bytes) — the raw
    bytes are kept for callers that need the on-disk form; `manifestDigest`
    binds the canonical signed payload (BO-C14N-v1 minus `signature`).

    `limita_bytes` is the administered ceiling (AC8, spec 0002): it can only
    *restrict* the 256 KiB protocol cap — the effective cap is
    ``min(limita_bytes, MANIFEST_MAX_BYTES)``. A caller that omits it stays
    under the protocol cap; none can exceed it. The check runs on the raw
    bytes BEFORE parse, schema or signature, and a refusal persists nothing.
    """
    cap = MANIFEST_MAX_BYTES if limita_bytes is None \
        else min(limita_bytes, MANIFEST_MAX_BYTES)
    try:
        size = path.stat().st_size
    except OSError:
        raise PackageReject("INVALID_MANIFEST", "manifest.json missing") from None
    if size > cap:
        raise PackageReject("MANIFEST_TOO_LARGE", f"{size} bytes")
    try:
        raw = path.read_bytes()
    except OSError:
        raise PackageReject("INVALID_MANIFEST", "manifest.json unreadable") from None
    if len(raw) > cap:
        raise PackageReject("MANIFEST_TOO_LARGE", f"{len(raw)} bytes read")
    try:
        doc: Any = json.loads(
            raw, object_pairs_hook=_no_dupes, parse_constant=_no_const)
    except PackageReject:
        raise
    except (ValueError, json.JSONDecodeError):
        raise PackageReject("INVALID_MANIFEST", "invalid JSON") from None
    if not isinstance(doc, dict):
        raise PackageReject("INVALID_MANIFEST", "manifest is not an object")
    return doc, raw


def load_manifest(
    path: Path, *, limita_bytes: int | None = None
) -> dict[str, Any]:
    """Parsed manifest object (not yet validated). See `load_manifest_raw`."""
    return load_manifest_raw(path, limita_bytes=limita_bytes)[0]


def validate_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    unknown = set(manifest) - _FIELDS
    if unknown:
        raise PackageReject("INVALID_MANIFEST", f"unknown fields: {sorted(unknown)}")
    missing = _REQUIRED - set(manifest)
    if missing:
        raise PackageReject("INVALID_MANIFEST", f"missing fields: {sorted(missing)}")

    if manifest["schemaVersion"] != SCHEMA_VERSION:
        raise PackageReject(
            "INVALID_MANIFEST", f"schemaVersion={manifest['schemaVersion']!r}")

    _id(manifest["packageId"], "packageId")
    _id(manifest["publisherId"], "publisherId")
    _id(manifest["keyId"], "keyId")
    parse_semver(manifest["version"])

    if manifest["kind"] not in KINDS:
        raise PackageReject("INVALID_MANIFEST", f"invalid kind: {manifest['kind']!r}")

    created = manifest["createdAt"]
    try:
        dt = datetime.fromisoformat(str(created))
    except (TypeError, ValueError):
        raise PackageReject("INVALID_MANIFEST", "createdAt is not RFC3339") from None
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise PackageReject("INVALID_MANIFEST", "createdAt lacks explicit timezone")

    digests = manifest["artifactDigests"]
    if not isinstance(digests, dict) or not 1 <= len(digests) <= MAX_ARTIFACTS:
        raise PackageReject("INVALID_MANIFEST", "artifactDigests: invalid size")
    for path, dg in digests.items():
        if not isinstance(path, str):
            raise PackageReject("INVALID_MANIFEST", "non-string path")
        check_artifact_path(path)
        if not isinstance(dg, str) or not DIGEST.match(dg):
            raise PackageReject("INVALID_MANIFEST", f"invalid digest for {path!r}")

    deps = manifest["dependencies"]
    if not isinstance(deps, list) or len(deps) > MAX_DEPS:
        raise PackageReject("INVALID_MANIFEST", "dependencies: invalid size")
    for dep in deps:
        if not isinstance(dep, dict) or set(dep) != {"packageId", "versionRange"}:
            raise PackageReject("INVALID_MANIFEST", "malformed dependency")
        _id(dep["packageId"], "dependencies.packageId")
        if not isinstance(dep["versionRange"], str) or \
                not VERSION_RANGE.match(dep["versionRange"]):
            raise PackageReject(
                "INVALID_MANIFEST", f"invalid versionRange: {dep['versionRange']!r}")

    compat = manifest["compatibility"]
    if not isinstance(compat, dict) or set(compat) != {"minHost", "maxHost"}:
        raise PackageReject("INVALID_MANIFEST", "malformed compatibility")
    for bound in ("minHost", "maxHost"):
        if compat[bound] is not None:
            parse_semver(compat[bound])

    caps = manifest["requestedCapabilities"]
    if not isinstance(caps, list) or len(caps) > MAX_CAPS:
        raise PackageReject(
            "INVALID_MANIFEST", "requestedCapabilities: invalid size")
    if len(set(caps)) != len(caps):
        raise PackageReject("INVALID_MANIFEST", "duplicate capabilities")
    for cap in caps:
        if not isinstance(cap, str) or not CAPABILITY.match(cap):
            raise PackageReject("INVALID_MANIFEST", f"malformed capability: {cap!r}")

    ref = manifest.get("settingsSchemaRef")
    if ref is not None and (not isinstance(ref, str) or len(ref) > 256):
        raise PackageReject("INVALID_MANIFEST", "invalid settingsSchemaRef")

    sig = manifest["signature"]
    if not isinstance(sig, dict) or set(sig) != {"algorithm", "value"}:
        raise PackageReject("INVALID_MANIFEST", "malformed signature")
    if sig["algorithm"] != "ed25519":
        raise PackageReject(
            "INVALID_MANIFEST", f"unsupported algorithm: {sig['algorithm']!r}")
    if not isinstance(sig["value"], str) or len(sig["value"]) > 128 \
            or not B64.match(sig["value"]):
        raise PackageReject("INVALID_MANIFEST", "invalid signature.value")

    for field_name in ("packageId", "publisherId", "keyId", "version"):
        if len(manifest[field_name]) > MAX_STRING:
            raise PackageReject("INVALID_MANIFEST", f"{field_name} too long")

    return manifest
