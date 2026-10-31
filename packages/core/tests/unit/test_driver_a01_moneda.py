"""Regresie F8 a01-efect-*: ambele inițializări de mandat ale driverului
A01 declară moneda explicită (AC5/D-07). Fără ea, create_mandate refuză
înainte de scrierea DB — patchul de o linie acoperea numai primul loc."""

import os
import sys
from decimal import Decimal

import pytest

REPO_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(REPO_ROOT, "tools", "val4"))

import driver_a01 as drv  # noqa: E402

from openexecutive.bo import db as bo_db  # noqa: E402
from openexecutive.bo.execution import store  # noqa: E402
from openexecutive.bo.execution.mandate import MandateValidationError  # noqa: E402
from openexecutive.bo.settings import store as settings  # noqa: E402


@pytest.fixture(autouse=True)
def _schema():
    bo_db.initialize_db()


def _permite_exec(tenant: str):
    current = next(
        x for x in settings.list_effective(tenant)
        if x["key"] == "bo.exec.enabled")
    settings.set_value(tenant, "bo.exec.enabled", True,
                       expected_version=current["version"], actor="t")


def test_mandate_are_moneda_declarata():
    m = drv._mandate("tenant-m", "driver", None)
    assert m.budget_currency == "USD"


def test_bound_run_are_moneda_declarata():
    tenant = "tenant-bound"
    _permite_exec(tenant)
    out = drv._bound_run(tenant, None, "bo_agents.db")
    m = store.get_mandate(tenant, out["mandate_id"])
    run = store.get_run(tenant, out["run_id"])
    assert m.budget_currency == "USD"
    assert run["budget_currency"] == "USD"


def test_ac5_ramane_inchis():
    with pytest.raises(MandateValidationError):
        store.create_mandate(
            "tenant-m",
            {"allowed_resources": ["synth.*"],
             "allowed_actions": ["increment"],
             "budget_limit": "10", "concurrency_limit": 2,
             "max_steps": 1, "max_depth": 1,
             "expires_at": drv._expiry()},
            parent=None, principal_ref="t", policy_version=0,
            actor="t", max_depth_cap=3, guardian_ref=None)
