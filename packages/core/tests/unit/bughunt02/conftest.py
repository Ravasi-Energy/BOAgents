"""BUGHUNT-02 coordinator probes vendored into the CI-covered unit suite.

Origin: `coordonare/rapoarte/coordonator/BUGHUNT-BOAGENTS-20261003/reproduced/`
(same assertions; the only adaptation is env setup — moved from import-time
assignments to per-test monkeypatch fixtures so the suite cannot leak the
probe environment into neighbouring test modules).

The two Node probes (`session_allow.mjs`, `money_roundtrip.mjs`) live in
`packages/ui/scripts/bughunt02_*.test.mjs` instead of python subprocess
wrappers, so they execute under the UI job's Node 22 runner.
"""
from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _probe_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Per-test probe environment (secrets present → dev fallback disabled)."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test-not-used")
    monkeypatch.setenv("EXEC_EMAIL_ADDRESS", "ceo.test@example.com")
    monkeypatch.setenv("BO_TENANT_ID", "probe")
    monkeypatch.setenv("BACKEND_PROXY_SECRET", "proxy-secret-probe")
    monkeypatch.setenv("BACKEND_SHARED_SECRET", "service-secret-probe")
    monkeypatch.setenv("BO_ADMIN_EMAILS", "admin@probe.local")
    monkeypatch.delenv("OE_PUBLIC_DEPLOYMENT", raising=False)
    monkeypatch.setenv(
        "COMPANY_PROFILE_PATH", str(tmp_path / "company" / "profile.yaml")
    )


@pytest.fixture(autouse=True)
def _silence_audit(monkeypatch: pytest.MonkeyPatch) -> None:
    import openexecutive.audit as audit

    monkeypatch.setattr(audit, "log_event", lambda *a, **k: None)


@pytest.fixture()
def bo_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from openexecutive.bo import db as bo_db

    path = tmp_path / "bo_agents.db"
    monkeypatch.setattr(bo_db, "DB_PATH", path)
    bo_db.initialize_db(path)
    return path
