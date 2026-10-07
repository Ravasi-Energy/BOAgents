"""BO-SET-001: settings registry + tenant-scoped store tests."""
from __future__ import annotations

from pathlib import Path

import pytest

from openexecutive.bo import db as bo_db
from openexecutive.bo.settings import store
from openexecutive.bo.settings.registry import REGISTRY, SettingValidationError

from .bo_testkit import capture_audit, use_tmp_db


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return use_tmp_db(tmp_path, monkeypatch)


@pytest.fixture(autouse=True)
def audit(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    return capture_audit(monkeypatch)


# --------------------------------------------------------------------------- #
# Effective values, origin, listing
# --------------------------------------------------------------------------- #

def test_defaults_listed_with_origin_and_version_zero(db: Path) -> None:
    items = store.list_effective("tenant-a")
    assert {i["key"] for i in items} == set(REGISTRY)
    for item in items:
        assert item["origin"] == "default"
        assert item["version"] == 0
        assert item["value"] == REGISTRY[item["key"]].default
        assert item["schema_version"] == "bo.settings.v1"


def test_set_and_reload_effective(db: Path, audit: list[dict]) -> None:
    rec = store.set_value(
        "tenant-a", "bo.ui.language", "en",
        expected_version=0, actor="admin@test",
    )
    assert rec["origin"] == "tenant"
    assert rec["version"] == 1

    # Fresh read (new connection) — persistence, not just in-memory.
    assert store.get_effective_value("tenant-a", "bo.ui.language") == "en"
    items = {i["key"]: i for i in store.list_effective("tenant-a")}
    assert items["bo.ui.language"]["origin"] == "tenant"
    assert items["bo.ui.language"]["updated_by"] == "admin@test"

    # Audit: one change event with old/new values (no secrets in Valul 1).
    changes = [e for e in audit if e["event_type"] == "bo_setting_change"]
    assert len(changes) == 1
    assert changes[0]["details"]["key"] == "bo.ui.language"
    assert changes[0]["details"]["old"] == "ro"
    assert changes[0]["details"]["new"] == "en"


def test_persistence_survives_reinit(db: Path) -> None:
    """Save → 'restart' (re-initialize schema on the same file) → value kept."""
    store.set_value("tenant-a", "bo.ui.display_name", "Fabrica Nord",
                    expected_version=0, actor="admin@test")
    bo_db.initialize_db(db)  # idempotent re-init, simulates a restart
    assert store.get_effective_value("tenant-a", "bo.ui.display_name") == \
        "Fabrica Nord"


def test_tenant_isolation(db: Path) -> None:
    store.set_value("tenant-a", "bo.ui.language", "en",
                    expected_version=0, actor="admin@test")
    assert store.get_effective_value("tenant-b", "bo.ui.language") == "ro"
    assert store.config_version("tenant-b") == 0


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("bo.ui.language", "fr"),                 # outside enum
        ("bo.ui.language", 5),                    # wrong type
        ("bo.ui.timezone", "Mars/Olympus"),       # not IANA
        ("bo.ui.timezone", ""),                   # empty
        ("bo.ui.display_name", ""),               # too short
        ("bo.ui.display_name", "x" * 81),         # too long
        ("bo.ui.display_name", "bad\x07name"),    # control char
        ("bo.bobot.simulation.max_steps", 0),     # below min
        ("bo.bobot.simulation.max_steps", 501),   # above max
        ("bo.bobot.simulation.max_steps", True),  # bool is not int
        ("bo.bobot.simulation.max_steps", "50"),  # string is not int
        ("bo.bobot.simulation.retention_days", 0),
        ("bo.bobot.simulation.retention_days", 3651),
        ("bo.ui.money_display_decimals", 1),      # only 0 or 2 allowed
        ("bo.ui.money_display_decimals", 3),
        ("bo.ui.money_display_decimals", -1),
        ("bo.ui.money_display_decimals", "2"),    # string is not int
        ("bo.ui.money_display_decimals", True),   # bool is not int
    ],
)
def test_invalid_values_rejected(db: Path, key: str, value: object) -> None:
    with pytest.raises(SettingValidationError):
        store.set_value("tenant-a", key, value,
                        expected_version=0, actor="admin@test")


def test_money_display_decimals_0_and_2_persist(db: Path) -> None:
    """D07/F3: setarea de server 0/2 — valorile permise se salvează,
    se citesc la reluare și sunt izolate pe tenant."""
    assert store.get_effective_value(
        "tenant-a", "bo.ui.money_display_decimals") == 2
    for v in (0, 2):
        rec = store.set_value(
            "tenant-a", "bo.ui.money_display_decimals", v,
            expected_version=store.config_version("tenant-a"),
            actor="admin@test",
        )
        assert rec["version"] >= 1
        assert store.get_effective_value(
            "tenant-a", "bo.ui.money_display_decimals") == v
    bo_db.initialize_db(db)  # restart simulat: valoarea rămâne
    assert store.get_effective_value(
        "tenant-a", "bo.ui.money_display_decimals") == 2
    assert store.get_effective_value(
        "tenant-b", "bo.ui.money_display_decimals") == 2


def test_unknown_key_rejected(db: Path) -> None:
    with pytest.raises(store.UnknownSettingError):
        store.set_value("tenant-a", "bo.no.such.key", 1,
                        expected_version=0, actor="admin@test")
    with pytest.raises(store.UnknownSettingError):
        store.get_effective_value("tenant-a", "bo.no.such.key")


# --------------------------------------------------------------------------- #
# Concurrency (CAS)
# --------------------------------------------------------------------------- #

def test_expected_version_conflict(db: Path) -> None:
    store.set_value("tenant-a", "bo.ui.language", "en",
                    expected_version=0, actor="a@test")
    # A second writer holding the stale version 0 is refused deterministically.
    with pytest.raises(store.ConfigConflictError):
        store.set_value("tenant-a", "bo.ui.language", "ro",
                        expected_version=0, actor="b@test")
    # The current version proceeds.
    rec = store.set_value("tenant-a", "bo.ui.language", "ro",
                          expected_version=1, actor="b@test")
    assert rec["version"] == 2
    assert store.config_version("tenant-a") == 2


def test_conflict_does_not_write(db: Path) -> None:
    store.set_value("tenant-a", "bo.bobot.simulation.max_steps", 10,
                    expected_version=0, actor="a@test")
    with pytest.raises(store.ConfigConflictError):
        store.set_value("tenant-a", "bo.bobot.simulation.max_steps", 99,
                        expected_version=0, actor="b@test")
    assert store.get_effective_value(
        "tenant-a", "bo.bobot.simulation.max_steps") == 10
