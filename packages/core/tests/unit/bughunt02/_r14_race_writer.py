"""Subprocess worker for test_contract_r14 — NOT a test module.

Mirrors the coordinator's CONTROL-R14 probe exactly: each process runs the
REAL ``load_or_create_profile()`` (the barrier only schedules — it forces
both YAML v1 reads to complete before either writer proceeds), then invokes
the real ``update_company_profile`` with ``expected_version=1``.

Modes:
  race — synchronized read then PATCH-equivalent call; result JSON → argv[1]
  hold — acquire ``profile_lock`` then sleep; used by the crash-release test
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path


def _race(out: Path) -> None:
    name = os.environ["RACE_WRITER_NAME"]
    ready = Path(os.environ["RACE_READY_DIR"])
    writers = int(os.environ["RACE_WRITERS"])
    from openexecutive.api.models import CompanyProfileUpdateRequest
    from openexecutive.api.routes import company_profile as route

    original = route.load_or_create_profile

    def synchronized_read():
        profile = original()  # the real YAML v1 read
        (ready / f"{name}.ready").touch()
        deadline = time.monotonic() + 30
        while len(list(ready.glob("*.ready"))) < writers:
            if time.monotonic() > deadline:
                raise TimeoutError("rendezvous")
            time.sleep(0.01)
        return profile

    route.load_or_create_profile = synchronized_read
    try:
        result = asyncio.run(
            route.update_company_profile(
                CompanyProfileUpdateRequest(industry=name, expected_version=1)
            )
        )
        rec = {"writer": name, "status": 200, "version": result.version}
    except Exception as e:  # noqa: BLE001 - report any refusal verbatim
        rec = {
            "writer": name,
            "status": getattr(e, "status_code", None),
            "exception": str(e),
        }
    out.write_text(json.dumps(rec))


def _hold() -> None:
    """Acquire the fence and sleep holding it — the crash-release fixture."""
    profile = Path(os.environ["COMPANY_PROFILE_PATH"])
    ready = Path(os.environ["RACE_READY_DIR"])
    from openexecutive.memory.company_profile import profile_lock

    with profile_lock(profile):
        (ready / "holder.ready").touch()
        time.sleep(120)


def main() -> None:
    mode = sys.argv[1]
    if mode == "race":
        _race(Path(sys.argv[2]))
    elif mode == "hold":
        _hold()
    else:
        raise SystemExit(f"unknown mode {mode}")


if __name__ == "__main__":
    main()
