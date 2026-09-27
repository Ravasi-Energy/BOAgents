"""BO-TEL-001: bo.telemetry.v1 schema gate + injectable adapter tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from openexecutive.bo.settings import store as settings_store
from openexecutive.bo.settings.registry import SettingValidationError
from openexecutive.bo.telemetry import schema
from openexecutive.bo.telemetry.adapter import (
    BufferedTransport,
    CredentialUnavailableError,
    HttpTransport,
    NullTransport,
    TelemetryAdapter,
    TelemetryDisabledError,
    set_adapter,
)
from openexecutive.bo.telemetry.schema import TelemetrySchemaError

from .bo_testkit import capture_audit, use_tmp_db

FIXTURES = Path(__file__).resolve().parents[4] / "fixtures/bo/telemetry"


@pytest.fixture(autouse=True)
def _reset_adapter(monkeypatch: pytest.MonkeyPatch) -> None:
    capture_audit(monkeypatch)  # set_value inside these tests must not leak
    set_adapter(None)
    yield
    set_adapter(None)


def _load(path: Path) -> dict:
    return json.loads(path.read_text())


# --------------------------------------------------------------------------- #
# Fixture contract: the schema accepts ONLY the agreed shape
# --------------------------------------------------------------------------- #

def test_all_valid_fixtures_accepted() -> None:
    valid = sorted((FIXTURES / "valid").glob("*.json"))
    assert len(valid) == 5  # one per kind
    for path in valid:
        event = _load(path)
        assert schema.validate_event(event) is event, path.name


def test_all_invalid_fixtures_rejected() -> None:
    invalid = sorted((FIXTURES / "invalid").glob("*.json"))
    assert len(invalid) >= 7
    for path in invalid:
        with pytest.raises(TelemetrySchemaError):
            schema.validate_event(_load(path))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: e.update({"tenantRef": ""}),
        lambda e: e.update({"eventId": "with space"}),
        lambda e: e.update({"configVersion": -1}),  # numeric: contract wants opaque string
        lambda e: e["data"].update({"sequence": -1}),
        lambda e: e["data"].update({"note": "camp extra"}),
        lambda e: e.pop("correlationId"),
    ],
)
def test_schema_mutation_rejected(mutate) -> None:  # noqa: ANN001
    event = _load(FIXTURES / "valid/heartbeat.json")
    mutate(event)
    with pytest.raises(TelemetrySchemaError):
        schema.validate_event(event)


# --------------------------------------------------------------------------- #
# Adapter: disabled by default, injectable transport
# --------------------------------------------------------------------------- #

def test_disabled_adapter_drops_without_io(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    use_tmp_db(tmp_path, monkeypatch)
    transport = BufferedTransport()
    adapter = TelemetryAdapter(enabled=False, transport=transport)
    out = adapter.emit(tenant="tenant-a", kind="Heartbeat",
                       data={"sequence": 1, "status": "HEALTHY",
                             "observedAt": "2026-09-21T10:00:00Z"})
    assert out is None
    assert adapter.dropped == 1
    assert adapter.emitted == 0
    assert transport.events == []  # nothing reached the wire


def test_enabled_adapter_stamps_and_sends(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    use_tmp_db(tmp_path, monkeypatch)
    transport = BufferedTransport()
    adapter = TelemetryAdapter(enabled=True, transport=transport,
                               producer_id="boagents",
                               installation_id="install-test")
    settings_store.set_value("tenant-a", "bo.ui.language", "en",
                             expected_version=0, actor="t")
    event = adapter.emit(tenant="tenant-a", kind="Heartbeat",
                         data={"sequence": 7, "status": "DEGRADED",
                               "observedAt": "2026-09-21T10:00:00Z"})
    assert event is not None
    # Identity is stamped at emission — product/tenant/installation come from
    # the adapter + caller tenant, never from caller-supplied fields.
    assert event["product"] == "BOAgents"
    assert event["tenantRef"] == "tenant-a"
    assert event["installationId"] == "install-test"
    assert event["schemaVersion"] == "bo.telemetry.v1"
    assert event["configVersion"] == "1"  # opaque string on the wire
    schema.validate_event(event)  # the emitted envelope is itself valid
    assert transport.events == [event]
    assert adapter.emitted == 1


def test_adapter_rejects_malformed_emission(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    use_tmp_db(tmp_path, monkeypatch)
    adapter = TelemetryAdapter(enabled=True, transport=BufferedTransport())
    with pytest.raises(TelemetrySchemaError):
        adapter.emit(tenant="tenant-a", kind="Heartbeat",
                     data={"sequence": "NaN", "status": "NOPE",
                           "observedAt": "not-a-time"})
    assert adapter.rejected == 1
    assert adapter.emitted == 0


def test_null_transport_is_silent() -> None:
    adapter = TelemetryAdapter(enabled=True, transport=NullTransport())
    # Emits (validates + sends to null) without error.
    assert adapter.emit(tenant="t", kind="Heartbeat",
                        data={"sequence": 0, "status": "HEALTHY",
                              "observedAt": "2026-09-21T10:00:00Z"}) is not None


# --------------------------------------------------------------------------- #
# S-02 — administered telemetry settings: tenant rows win over bootstrap,
# secrets stay env references, invalid input is refused at save time
# --------------------------------------------------------------------------- #

def test_tenant_override_wins_over_bootstrap(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    use_tmp_db(tmp_path, monkeypatch)
    transport = BufferedTransport()
    adapter = TelemetryAdapter(enabled=False, transport=transport)

    rec = settings_store.set_value(
        "tenant-a", "bo.telemetry.enabled", True,
        expected_version=0, actor="admin@t",
    )
    assert rec["origin"] == "tenant" and rec["version"] == 1
    # CAS preserved on the new keys.
    with pytest.raises(settings_store.ConfigConflictError):
        settings_store.set_value(
            "tenant-a", "bo.telemetry.enabled", False,
            expected_version=0, actor="other@t",
        )

    cfg = adapter.resolve("tenant-a")
    assert cfg.enabled is True and cfg.source["enabled"] == "tenant"
    # A tenant without an override still sees the bootstrap flag.
    cfg_b = adapter.resolve("tenant-b")
    assert cfg_b.enabled is False and cfg_b.source["enabled"] == "bootstrap"

    # The administered enable actually reaches the wire at emit time.
    event = adapter.emit(
        tenant="tenant-a", kind="Heartbeat",
        data={"sequence": 1, "status": "HEALTHY",
              "observedAt": "2026-09-21T10:00:00Z"},
    )
    assert event is not None and transport.events == [event]
    # …and tenant-b remains governed by the disabled bootstrap.
    assert adapter.emit(
        tenant="tenant-b", kind="Heartbeat",
        data={"sequence": 1, "status": "HEALTHY",
              "observedAt": "2026-09-21T10:00:00Z"},
    ) is None


def test_administered_disable_overrides_enabled_bootstrap(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    use_tmp_db(tmp_path, monkeypatch)
    adapter = TelemetryAdapter(enabled=True, transport=BufferedTransport())
    settings_store.set_value(
        "tenant-a", "bo.telemetry.enabled", False,
        expected_version=0, actor="admin@t",
    )
    with pytest.raises(TelemetryDisabledError):
        adapter.deliver_event({"eventId": "evt_1"}, tenant="tenant-a")
    assert adapter.emit(
        tenant="tenant-a", kind="Heartbeat",
        data={"sequence": 1, "status": "HEALTHY",
              "observedAt": "2026-09-21T10:00:00Z"},
    ) is None
    assert adapter.dropped == 2


def test_http_without_endpoint_and_token_is_controlled(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """transport=http saved without endpoint/token: the envelope is refused
    with a clear error and stays pending upstream — never silently dropped,
    never marked delivered, never a raw crash."""
    use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.delenv("BO_TELEMETRY_ENDPOINT", raising=False)
    monkeypatch.delenv("BO_TELEMETRY_TOKEN", raising=False)
    adapter = TelemetryAdapter(enabled=True, transport=BufferedTransport())
    settings_store.set_value(
        "tenant-a", "bo.telemetry.transport", "http",
        expected_version=0, actor="admin@t",
    )
    cfg = adapter.resolve("tenant-a")
    assert cfg.transport_kind == "http"
    assert cfg.transport is None and cfg.token_configured is False
    with pytest.raises(TelemetryDisabledError):
        adapter.deliver_event({"eventId": "evt_1"}, tenant="tenant-a")
    assert adapter.dropped == 1 and adapter.emitted == 0


def test_secret_ref_resolves_env_never_stores_secret(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """The administered token_ref names a PROVISIONED env var; the secret
    value itself is never persisted in bo_settings and never surfaces in
    list_effective."""
    use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setenv("TEL_TOKEN_X", "sekrit-token-material")
    _provision_refs(monkeypatch, "TEL_TOKEN_X")
    settings_store.set_value(
        "tenant-a", "bo.telemetry.transport", "http",
        expected_version=0, actor="admin@t",
    )
    settings_store.set_value(
        "tenant-a", "bo.telemetry.endpoint", "https://guardian.example/v1/telemetry",
        expected_version=0, actor="admin@t",
    )
    settings_store.set_value(
        "tenant-a", "bo.telemetry.token_ref", "TEL_TOKEN_X",
        expected_version=0, actor="admin@t",
    )
    adapter = TelemetryAdapter(enabled=True)
    cfg = adapter.resolve("tenant-a")
    assert isinstance(cfg.transport, HttpTransport)
    assert cfg.transport.endpoint == "https://guardian.example/v1/telemetry"
    assert cfg.transport.token == "sekrit-token-material"
    assert cfg.token_ref == "TEL_TOKEN_X" and cfg.token_configured is True
    items = {i["key"]: i for i in settings_store.list_effective("tenant-a")}
    assert items["bo.telemetry.token_ref"]["value"] == "TEL_TOKEN_X"
    assert "sekrit" not in json.dumps(items)


def test_endpoint_override_gets_fresh_transport(tmp_path, monkeypatch) -> None:  # noqa: ANN001
    """An administered endpoint builds a NEW transport ONLY when an
    administered, provisioned token_ref exists — the bootstrap token is
    never carried to a new destination."""
    use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.setenv("BO_TELEMETRY_TOKEN", "bootstrap-token")
    bootstrap = HttpTransport("https://env.example/t", "bootstrap-token")
    adapter = TelemetryAdapter(enabled=True, transport=bootstrap)

    cfg = adapter.resolve("tenant-a")
    assert cfg.transport is bootstrap  # unchanged config reuses bootstrap

    settings_store.set_value(
        "tenant-a", "bo.telemetry.endpoint", "https://admin.example/t",
        expected_version=0, actor="admin@t",
    )
    # Endpoint override WITHOUT an administered ref: controlled refusal,
    # no transport built, bootstrap token not attached.
    cfg = adapter.resolve("tenant-a")
    assert cfg.transport is None
    assert cfg.credential_state == "endpoint_without_ref"
    assert cfg.source["endpoint"] == "tenant"

    _provision_refs(monkeypatch, "TEL_TOKEN_ADMIN")
    monkeypatch.setenv("TEL_TOKEN_ADMIN", "administered-token")
    settings_store.set_value(
        "tenant-a", "bo.telemetry.token_ref", "TEL_TOKEN_ADMIN",
        expected_version=0, actor="admin@t",
    )
    cfg = adapter.resolve("tenant-a")
    assert isinstance(cfg.transport, HttpTransport)
    assert cfg.transport is not bootstrap
    assert cfg.transport.endpoint == "https://admin.example/t"
    assert cfg.transport.token == "administered-token"  # never the bootstrap one
    assert cfg.source["endpoint"] == "tenant"


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("bo.telemetry.enabled", "yes"),                       # non-bool
        ("bo.telemetry.transport", "carrier-pigeon"),          # outside enum
        ("bo.telemetry.transport", "null"),                    # not administrable
        ("bo.telemetry.endpoint", "ftp://x"),                  # not http(s)
        ("bo.telemetry.endpoint", "has space"),                # invalid URL
        ("bo.telemetry.endpoint",
         "https://user:pass@host.invalid/t"),                  # userinfo creds
        ("bo.telemetry.token_ref", "not a var name!"),         # not env syntax
        ("bo.telemetry.token_ref", "tok-with-D4sh.payload"),   # secret-shaped
        ("bo.telemetry.token_ref", "ARBITRARY_ENV_NAME"),      # unprovisioned
        ("bo.exec.guardian_secret_ref", "ANTHROPIC_API_KEY"),  # unprovisioned
        ("bo.exec.guardian_policy_secret_ref",
         "BACKEND_PROXY_SECRET"),                            # unprovisioned
    ],
)
def test_invalid_telemetry_settings_rejected(tmp_path, monkeypatch, key, value) -> None:  # noqa: ANN001
    use_tmp_db(tmp_path, monkeypatch)
    monkeypatch.delenv("BO_TELEMETRY_SECRET_REFS", raising=False)
    monkeypatch.delenv("BO_GUARDIAN_SECRET_REFS", raising=False)
    with pytest.raises(SettingValidationError):
        settings_store.set_value("tenant-a", key, value,
                                 expected_version=0, actor="admin@t")


# --------------------------------------------------------------------------- #
# PILOT-06 — credential legat de destinație și tenant: referințe provisionate
# server-side, fără fallback la tokenul bootstrap pe destinații administrate
# --------------------------------------------------------------------------- #

def _provision_refs(monkeypatch: pytest.MonkeyPatch, *names: str) -> None:
    """Operator-side provisioning: the only env names an administered
    ``bo.telemetry.token_ref`` may point at (``BO_TELEMETRY_SECRET_REFS``)."""
    monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", ",".join(names))


def _capture_http(monkeypatch: pytest.MonkeyPatch) -> list:
    """Capture the Request objects at the urllib boundary — zero network."""
    import io
    import urllib.request

    calls: list = []

    def _capture(req, **kw):  # noqa: ANN001
        calls.append(req)
        return io.BytesIO(b'{"status":"RECEIVED"}')

    monkeypatch.setattr(urllib.request, "urlopen", _capture)
    return calls


class TestCredentialBoundToDestination:
    def test_administered_endpoint_without_ref_refuses_transport(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """PILOT-06/audit: endpoint override alone must NOT carry the
        bootstrap credential to the new destination — controlled refusal."""
        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-bootstrap-token")
        calls = _capture_http(monkeypatch)
        adapter = TelemetryAdapter(
            enabled=True,
            transport=HttpTransport("https://boot.invalid/v1/telemetry",
                                    "synthetic-bootstrap-token"),
        )
        settings_store.set_value(
            "tenant-a", "bo.telemetry.endpoint",
            "https://administered.invalid/v1/telemetry",
            expected_version=0, actor="admin@t",
        )
        cfg = adapter.resolve("tenant-a")
        assert cfg.transport is None
        assert cfg.credential_state == "endpoint_without_ref"
        with pytest.raises(TelemetryDisabledError):
            adapter.deliver_event({"eventId": "evt_1"}, tenant="tenant-a")
        assert calls == []  # nothing left the process — least of all the bootstrap token

    def test_administered_endpoint_uses_only_the_administered_ref(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-bootstrap-token")
        monkeypatch.setenv("TEL_DEST_B", "synthetic-tenant-token")
        _provision_refs(monkeypatch, "TEL_DEST_B")
        calls = _capture_http(monkeypatch)
        adapter = TelemetryAdapter(
            enabled=True,
            transport=HttpTransport("https://boot.invalid/v1/telemetry",
                                    "synthetic-bootstrap-token"),
        )
        settings_store.set_value(
            "tenant-a", "bo.telemetry.endpoint",
            "https://administered.invalid/v1/telemetry",
            expected_version=0, actor="admin@t",
        )
        settings_store.set_value(
            "tenant-a", "bo.telemetry.token_ref", "TEL_DEST_B",
            expected_version=0, actor="admin@t",
        )
        adapter.deliver_event({"eventId": "evt_1"}, tenant="tenant-a")
        assert len(calls) == 1
        req = calls[0]
        assert req.full_url == "https://administered.invalid/v1/telemetry"
        assert req.get_header("Authorization") == "Bearer synthetic-tenant-token"

    def test_provisioned_ref_missing_env_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A provisioned ref name whose env is absent fails closed — no
        fallback to BO_TELEMETRY_TOKEN on an administered destination."""
        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-bootstrap-token")
        _provision_refs(monkeypatch, "TEL_MISSING")
        monkeypatch.delenv("TEL_MISSING", raising=False)
        calls = _capture_http(monkeypatch)
        adapter = TelemetryAdapter(enabled=True, transport=NullTransport())
        settings_store.set_value(
            "tenant-a", "bo.telemetry.transport", "http",
            expected_version=0, actor="admin@t",
        )
        settings_store.set_value(
            "tenant-a", "bo.telemetry.endpoint",
            "https://administered.invalid/v1/telemetry",
            expected_version=0, actor="admin@t",
        )
        settings_store.set_value(
            "tenant-a", "bo.telemetry.token_ref", "TEL_MISSING",
            expected_version=0, actor="admin@t",
        )
        cfg = adapter.resolve("tenant-a")
        assert cfg.transport is None
        assert cfg.credential_state == "missing"
        assert cfg.credential_ref == "TEL_MISSING"   # sanctioned ref, env absent
        with pytest.raises(TelemetryDisabledError):
            adapter.deliver_event({"eventId": "evt_1"}, tenant="tenant-a")
        assert calls == []

    def test_bootstrap_destination_keeps_bootstrap_credential(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Unchanged bootstrap config keeps its documented behavior: the
        bootstrap token serves the bootstrap endpoint."""
        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-bootstrap-token")
        calls = _capture_http(monkeypatch)
        adapter = TelemetryAdapter(
            enabled=True,
            transport=HttpTransport("https://boot.invalid/v1/telemetry",
                                    "synthetic-bootstrap-token"),
        )
        adapter.deliver_event({"eventId": "evt_1"}, tenant="tenant-a")
        assert len(calls) == 1
        assert calls[0].get_header("Authorization") == (
            "Bearer synthetic-bootstrap-token")
        cfg = adapter.resolve("tenant-a")
        assert cfg.credential_state == "configured"
        assert cfg.credential_ref == "BO_TELEMETRY_TOKEN"


# --------------------------------------------------------------------------- #
# PILOT-06 — precizare coordonator: SecretRef scopat per tenant. O intrare
# ``NAME@tenant`` din allow-list e utilizabilă NUMAI de tenantul ei — un
# tenant nu poate alege referința celuilalt.
# --------------------------------------------------------------------------- #


class TestSecretRefTenantScope:
    def test_scoped_telemetry_ref_rejected_for_other_tenant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "TEL_A@tenant-a")
        monkeypatch.setenv("TEL_A", "synthetic")
        settings_store.set_value(
            "tenant-a", "bo.telemetry.token_ref", "TEL_A",
            expected_version=0, actor="admin@t",
        )
        with pytest.raises(SettingValidationError):
            settings_store.set_value(
                "tenant-b", "bo.telemetry.token_ref", "TEL_A",
                expected_version=0, actor="admin@t",
            )

    def test_bare_ref_usable_by_any_tenant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "SHARED_TEL")
        for tenant in ("tenant-a", "tenant-b"):
            settings_store.set_value(
                tenant, "bo.telemetry.token_ref", "SHARED_TEL",
                expected_version=0, actor="admin@t",
            )

    def test_rescoped_ref_reports_unprovisioned_at_resolve(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Ref bare-provisioned at write, later re-scoped to another tenant:
        resolve reports it unprovisioned — the tenant loses the credential
        the moment the operator re-scopes it."""
        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "TEL_B")
        monkeypatch.setenv("TEL_B", "synthetic-b")
        settings_store.set_value(
            "tenant-b", "bo.telemetry.transport", "http",
            expected_version=0, actor="admin@t",
        )
        settings_store.set_value(
            "tenant-b", "bo.telemetry.endpoint",
            "https://dest-b.invalid/v1/telemetry",
            expected_version=0, actor="admin@t",
        )
        settings_store.set_value(
            "tenant-b", "bo.telemetry.token_ref", "TEL_B",
            expected_version=0, actor="admin@t",
        )
        adapter = TelemetryAdapter(enabled=True, transport=NullTransport())
        assert adapter.resolve("tenant-b").credential_state == "configured"
        monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "TEL_B@tenant-a")
        cfg = adapter.resolve("tenant-b")
        assert cfg.credential_state == "unprovisioned"
        assert cfg.transport is None
        cfg_a = adapter.resolve("tenant-a")
        assert cfg_a.credential_state != "unprovisioned"  # scope is t-a's

    def test_bound_row_ref_rescoped_away_refuses_zero_wire(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The binding persisted on an outbox row is re-checked against the
        CURRENT tenant scope at delivery: re-scoping ``TEL_B`` to tenant-a
        revokes it for tenant-b even though the env var still exists —
        controlled refusal, zero bytes, no credential substitution."""
        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "TEL_B")
        monkeypatch.setenv("TEL_B", "synthetic-b")
        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-bootstrap")
        calls = _capture_http(monkeypatch)
        adapter = TelemetryAdapter(enabled=True, transport=NullTransport())
        bound = ("https://dest-b.invalid/v1/telemetry", "TEL_B")
        monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "TEL_B@tenant-a")
        with pytest.raises(CredentialUnavailableError):
            adapter.deliver_event(
                {"eventId": "evt_b", "tenantRef": "tenant-b"},
                tenant="tenant-b", destination=bound)
        assert calls == []
        adapter.deliver_event(
            {"eventId": "evt_a", "tenantRef": "tenant-a"},
            tenant="tenant-a", destination=bound)
        assert len(calls) == 1
        assert calls[0].get_header("Authorization") == "Bearer synthetic-b"

    def test_bound_row_uses_rotated_env_value(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Binding pins the ref NAME, not the value: while the ref stays
        provisioned for the tenant, the current env value is read — a
        rotated credential reaches the wire on the bound destination."""
        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "TEL_B@tenant-b")
        monkeypatch.setenv("TEL_B", "synthetic-old")
        calls = _capture_http(monkeypatch)
        adapter = TelemetryAdapter(enabled=True, transport=NullTransport())
        bound = ("https://dest-b.invalid/v1/telemetry", "TEL_B")
        monkeypatch.setenv("TEL_B", "synthetic-new")
        adapter.deliver_event(
            {"eventId": "evt_rot", "tenantRef": "tenant-b"},
            tenant="tenant-b", destination=bound)
        assert len(calls) == 1
        assert calls[0].get_header("Authorization") == "Bearer synthetic-new"

    def test_service_bound_row_ref_rescoped_away_refuses(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AA-A01-01: the pilot/service bound path must re-check the ref
        scope exactly like the model path — a re-scoped NAME@tenant means
        zero traffic on the bound destination even though env exists."""
        use_tmp_db(tmp_path, monkeypatch)
        from openexecutive.bo.pilot import delivery as pilot_delivery

        monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "TEL_B")
        monkeypatch.setenv("TEL_B", "synthetic-b")
        monkeypatch.setenv("BO_TELEMETRY_TOKEN", "synthetic-bootstrap")
        opened: list = []

        def _opener(*a, **kw):  # noqa: ANN001
            opened.append(a)
            raise AssertionError("wire must never be reached")

        monkeypatch.setattr(pilot_delivery, "build_opener", _opener)
        envelope = {
            "schemaVersion": "bo.service-observation.v1",
            "eventId": "obs-revoked", "tenantRef": "tenant-b",
        }
        adapter = TelemetryAdapter(enabled=True, transport=NullTransport())
        monkeypatch.setattr(pilot_delivery, "get_adapter", lambda: adapter)
        bound = ("https://dest-b.invalid/v1/telemetry", "TEL_B")
        monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "TEL_B@tenant-a")
        with pytest.raises(CredentialUnavailableError):
            pilot_delivery.deliver(
                "tenant-b", envelope, destination=bound)
        assert opened == []

    def test_scoped_guardian_ref_rejected_for_other_tenant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        use_tmp_db(tmp_path, monkeypatch)
        monkeypatch.setenv("BO_GUARDIAN_SECRET_REFS", "G_A@tenant-a")
        monkeypatch.setenv("G_A", "synthetic-ga")
        settings_store.set_value(
            "tenant-a", "bo.exec.guardian_secret_ref", "G_A",
            expected_version=0, actor="admin@t",
        )
        with pytest.raises(SettingValidationError):
            settings_store.set_value(
                "tenant-b", "bo.exec.guardian_secret_ref", "G_A",
                expected_version=0, actor="admin@t",
            )
        with pytest.raises(SettingValidationError):
            settings_store.set_value(
                "tenant-b", "bo.exec.guardian_policy_secret_ref", "G_A",
                expected_version=0, actor="admin@t",
            )
