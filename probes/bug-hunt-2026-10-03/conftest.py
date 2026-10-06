"""Probe de vânătoare, în afara `packages/core/tests`.

CI rulează doar `pytest tests/unit/` din `packages/core`. Acest director nu
este în `testpaths` și nu este apelat de workflow. Datele sunt fișiere
temporare; nu se atinge nicio bază partajată.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-test-not-used")
os.environ.setdefault("EXEC_EMAIL_ADDRESS", "ceo.test@example.com")
os.environ["BO_TENANT_ID"] = "probe"
os.environ["BACKEND_PROXY_SECRET"] = "proxy-secret-probe"
os.environ["BACKEND_SHARED_SECRET"] = "service-secret-probe"
os.environ["BO_ADMIN_EMAILS"] = "admin@probe.local"
os.environ.pop("OE_PUBLIC_DEPLOYMENT", None)

_COMPANY = Path(tempfile.mkdtemp(prefix="bughunt-company-"))
os.environ["COMPANY_PROFILE_PATH"] = str(_COMPANY / "profile.yaml")


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


def note(label: str, before: object, after: object, expected: object) -> None:
    line = f"{label} inainte={before} dupa={after} asteptat={expected}"
    print(line)
    assert after == expected, line
