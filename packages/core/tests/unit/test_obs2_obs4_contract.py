"""OBS-2 policy-refusal code + OBS-4 telemetry endpoint convention."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def test_policy_refused_distinct_code(tmp_path: Path, monkeypatch) -> None:
    """pkg_store.StateError maps to 409 policy_refused — never confused
    with a CAS version_conflict."""
    from openexecutive.api.routes import bo as bo_routes
    from openexecutive.bo.packages import store as pkg_store

    app = FastAPI()
    bo_routes.register_error_handlers(app)

    @app.get("/boom")
    def _boom() -> None:
        raise pkg_store.StateError("Verificare curentă refuzată: X")

    resp = TestClient(app).get("/boom")
    assert resp.status_code == 409
    assert resp.json()["error"] == "policy_refused"


def test_telemetry_endpoint_convention(tmp_path: Path) -> None:
    """OBS-4: base + derived paths — the general adapter and the pilot
    delivery resolve the SAME canonical route for a telemetry envelope."""
    from openexecutive.bo.pilot.delivery import _kind_url
    from openexecutive.bo.telemetry.adapter import HttpTransport

    base = "https://receiver.example/"
    t = HttpTransport(endpoint=base, token="t")
    sent: dict[str, str] = {}
    import urllib.request

    class _Resp:
        def read(self) -> bytes:
            return b'{"status": "RECEIVED"}'

    def _fake_urlopen(req, timeout):  # noqa: ANN001, ANN202
        sent["url"] = req.full_url
        return _Resp()

    import unittest.mock as mock

    with mock.patch.object(urllib.request, "urlopen", _fake_urlopen):
        t.send({"kind": "ConfigApplied", "data": {}})

    assert sent["url"] == "https://receiver.example/v1/telemetry"
    # Same convention as the pilot path — telemetry kind lands identically;
    # observation kind lands on its own canonical route.
    assert _kind_url(base, service=False) == sent["url"]
    assert _kind_url(base, service=True) == (
        "https://receiver.example/v1/observations"
    )
