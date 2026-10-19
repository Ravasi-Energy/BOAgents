"""S-CORE — real-path evidence for the 27 transitive dependency alerts
(PyJWT #7–#17/#21/#23, urllib3 #18–#20, pypdf #24–#31, oauthlib #5–#6,
sentence-transformers #22).

Every row's installed pin is already at/above its first patched release
(update commit `2ded6c4`). A version check alone is NOT the proof the
registry requires — these tests pin *why* each advisory can't bite on the
real deployment, and they fail if any of that regresses:

  1. installed versions stay above every affected range (a silent
     downgrade through a future lock regen fails here);
  2. no module under ``openexecutive/`` imports the vulnerable surface
     (jwt, oauthlib, requests_oauthlib, kubernetes, sentence_transformers)
     — the dependency is present only as a transitive passenger or for a
     dormant code path;
  3. the mounted MCP server configures no OAuth/JWT auth — the only code
     in ``mcp`` that touches PyJWT (client_credentials private_key_jwt
     ``jwt.encode``) is never instantiated;
  4. the persisted-schema EF allowlist can never select a
     sentence-transformers embedding function, and the default EF is the
     ONNX one — the local-model-load path of GHSA-jhr6-gm9c-rqjv is
     unreachable.

pypdf's parsing-limit probe lives in test_loader_pdf_limits.py.
"""
from __future__ import annotations

import importlib.metadata
import re
from pathlib import Path

from packaging.version import Version

_OE_ROOT = Path(__file__).resolve().parents[2] / "openexecutive"

# (distribution, minimum safe version, alert numbers it closes)
_VERSION_FLOORS = [
    ("pyjwt", "2.15.0", "PyJWT #7–#17, #21, #23 (13 alerts)"),
    ("urllib3", "2.8.0", "urllib3 #18–#20"),
    ("pypdf", "6.19.0", "pypdf #24–#31"),
    ("oauthlib", "4.0.0", "oauthlib #5–#6"),
    ("sentence-transformers", "5.6.0", "sentence-transformers #22"),
]

# Modules whose presence in openexecutive source would mean a real code
# path into a vulnerable dependency. Scanned as import statements only —
# a bare word in a comment/docstring is not a path.
_FORBIDDEN_IMPORTS = {
    "jwt": "PyJWT decode/JWK paths — we never issue or verify JWTs",
    "oauthlib": "oauthlib endpoints — we run no OAuth provider",
    "requests_oauthlib": "OAuth1/OAuth2 session client — unused",
    "kubernetes": "k8s client — chromadb[standard] passenger only",
    "sentence_transformers": "local model load — GHSA-jhr6-gm9c-rqjv",
}

_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+({})(?:\s|[.]|$)".format(
        "|".join(_FORBIDDEN_IMPORTS)
    )
)


def test_installed_versions_outside_affected_ranges() -> None:
    for dist, floor, alerts in _VERSION_FLOORS:
        installed = Version(importlib.metadata.version(dist))
        assert installed >= Version(floor), (
            f"{dist} {installed} fell below {floor} — reopens {alerts}"
        )


def test_no_openexecutive_module_imports_vulnerable_surface() -> None:
    offenders: list[str] = []
    for py in _OE_ROOT.rglob("*.py"):
        for lineno, line in enumerate(
            py.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if _IMPORT_RE.match(line):
                offenders.append(f"{py.relative_to(_OE_ROOT)}:{lineno}")
    assert not offenders, "vulnerable dependency surfaces imported:\n" + "\n".join(
        offenders
    )


def test_mcp_deployment_configures_no_jwt_auth_path() -> None:
    """The only PyJWT use inside mcp is client_credentials private_key_jwt
    signing, reached only when an OAuth client provider is configured. Our
    /mcp endpoint gates on the shared-secret middleware instead — the
    provider is None, so jwt.* is never called."""
    from openexecutive.mcp_server import server as mcp_server

    assert mcp_server.mcp.settings.auth is None
    assert mcp_server.mcp._auth_server_provider is None


def test_persisted_schema_can_never_select_sentence_transformers() -> None:
    """GHSA-jhr6-gm9c-rqjv: local model loading executing custom Python.
    Chroma's SentenceTransformerEmbeddingFunction is reachable only if code
    or a persisted schema names it — the allowlist rejects every name but
    the ONNX default."""
    from openexecutive.knowledge.store import _ALLOWED_EF_NAMES

    assert sorted(_ALLOWED_EF_NAMES) == ["default", "onnx_mini_lm_l6_v2"]
