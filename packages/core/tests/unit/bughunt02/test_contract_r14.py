"""CONTROL R14 — PROFILE-CAS-RACE: multi-process profile write fence.

The coordinator probe proved two processes could both read profile.yaml v1,
both pass expected_version=1, and both write v2 — one edit silently lost.
These vendored regressions pin the fix:

- the exact adversarial schedule (two real processes, real reads
  synchronized before either writes) — one 200/v2, one 409, zero stale
  effects, winner's edit readable on disk;
- a real multi-worker HTTP server (uvicorn --workers 2) with the real
  role gates — one admin PATCH wins, the other is refused 409, a viewer
  is refused 403;
- bounded acquisition — a wedged holder yields ProfileLockTimeout / 503,
  never a hang;
- crash release — SIGKILL on the holder frees the kernel lock;
- monotone version — a non-CAS writer can never stamp a version at or
  below the one already persisted, so stale pins cannot collide.
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from openexecutive.memory.company_profile import (
    CompanyProfile,
    ProfileLockTimeout,
    profile_lock,
)

_WORKER = Path(__file__).resolve().parent / "_r14_race_writer.py"
_CORE = Path(__file__).resolve().parents[3]

ADMIN = {
    "x-api-key": "service-secret-probe",
    "x-caller-proxy-secret": "proxy-secret-probe",
    "x-caller-email": "admin@probe.local",
}
VIEWER = {
    "x-api-key": "service-secret-probe",
    "x-caller-proxy-secret": "proxy-secret-probe",
    "x-caller-email": "viewer@probe.local",
}


def _subprocess_env(profile: Path, **extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["COMPANY_PROFILE_PATH"] = str(profile)
    env["PYTHONPATH"] = str(_CORE) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra)
    return env


def _wait_http_ready(port: int, proc: subprocess.Popen, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError("uvicorn exited during boot")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("uvicorn workers did not come up")


# ── exact coordinator schedule: two processes, synchronized real reads ───────


def test_r14_two_process_cas_race(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    CompanyProfile(name="Race SRL", industry="base").save_to_yaml(profile)
    ready = tmp_path / "ready"
    ready.mkdir()
    procs: list[subprocess.Popen] = []
    try:
        for i in range(2):
            name = f"writer-{i}"
            env = _subprocess_env(
                profile,
                RACE_WRITER_NAME=name,
                RACE_READY_DIR=str(ready),
                RACE_WRITERS="2",
            )
            out = tmp_path / f"{name}.json"
            procs.append(
                subprocess.Popen(
                    [sys.executable, str(_WORKER), "race", str(out)],
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
            )
        for p in procs:
            assert p.wait(timeout=60) == 0, p.stderr.read() if p.stderr else ""
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()

    rows = [
        json.loads((tmp_path / f"writer-{i}.json").read_text()) for i in range(2)
    ]
    statuses = sorted(r["status"] for r in rows)
    assert statuses == [200, 409], rows
    winner = next(r for r in rows if r["status"] == 200)
    loser = next(r for r in rows if r["status"] == 409)

    after = CompanyProfile.load_from_yaml(profile)
    assert after.version == 2
    assert after.industry == winner["writer"]
    # The stale writer has zero effects: its industry is absent and the
    # profile still validates against the schema.
    assert after.industry != loser["writer"]
    assert winner["version"] == 2


# ── real multi-worker HTTP: two uvicorn workers, real role gates ─────────────


def test_r14_http_multiworker_cas(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    CompanyProfile(name="Race SRL", industry="base").save_to_yaml(profile)

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    env = _subprocess_env(
        profile,
        EPISODIC_DB_PATH=str(tmp_path / "episodic.db"),
        BOAGENTS_DB_PATH=str(tmp_path / "bo.db"),
        VECTOR_STORE_PATH=str(tmp_path / "vectors"),
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "openexecutive.api.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--workers",
            "2",
            "--lifespan",
            "off",
        ],
        env=env,
        cwd=tmp_path,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _wait_http_ready(port, proc)
        import httpx

        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, timeout=15) as client:
            # The real role gate runs: viewer cannot even reach the write.
            refused = client.patch(
                "/company-profile",
                json={"industry": "viewer-edit", "expected_version": 1},
                headers=VIEWER,
            )
            assert refused.status_code == 403, refused.text
            anonymous = client.patch(
                "/company-profile",
                json={"industry": "anon", "expected_version": 1},
                headers={"x-api-key": "service-secret-probe"},
            )
            assert anonymous.status_code in (401, 403)

            # Two concurrent admin PATCHes pinned at the same version.
            results: list[dict] = []
            barrier = threading.Barrier(3)

            def fire(name: str) -> None:
                barrier.wait(timeout=30)
                resp = client.patch(
                    "/company-profile",
                    json={"industry": name, "expected_version": 1},
                    headers=ADMIN,
                )
                results.append({"writer": name, "status": resp.status_code})

            threads = [
                threading.Thread(target=fire, args=(f"http-writer-{i}",))
                for i in range(2)
            ]
            for t in threads:
                t.start()
            barrier.wait(timeout=30)
            for t in threads:
                t.join(timeout=30)

        statuses = sorted(r["status"] for r in results)
        assert statuses == [200, 409], results
        winner = next(r["writer"] for r in results if r["status"] == 200)
        after = CompanyProfile.load_from_yaml(profile)
        assert after.version == 2
        assert after.industry == winner
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


# ── bounded acquisition: wedged holder → refusal, never a hang ───────────────


def test_r14_lock_timeout_refuses(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    CompanyProfile(name="Wedged", industry="base").save_to_yaml(profile)
    ready = tmp_path / "ready"
    ready.mkdir()
    env = _subprocess_env(profile, RACE_READY_DIR=str(ready))
    holder = subprocess.Popen(
        [sys.executable, str(_WORKER), "hold"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not (ready / "holder.ready").exists():
            if time.monotonic() > deadline:
                raise AssertionError("holder never acquired the lock")
            if holder.poll() is not None:
                raise AssertionError("holder exited early")
            time.sleep(0.05)

        with (
            pytest.raises(ProfileLockTimeout),
            profile_lock(profile, timeout_s=0.3),
        ):
            pass

        # The HTTP surface maps the timeout to 503, not a hang or a write.
        from openexecutive.api.models import CompanyProfileUpdateRequest
        from openexecutive.api.routes import company_profile as route

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("COMPANY_PROFILE_PATH", str(profile))
            mp.setattr(
                "openexecutive.memory.company_profile.PROFILE_LOCK_TIMEOUT_S", 0.3
            )
            with pytest.raises(Exception) as exc_info:
                asyncio.run(
                    route.update_company_profile(
                        CompanyProfileUpdateRequest(
                            industry="blocked", expected_version=1
                        )
                    )
                )
        assert getattr(exc_info.value, "status_code", None) == 503
        # Zero effects from the refused write.
        assert CompanyProfile.load_from_yaml(profile).industry == "base"
    finally:
        holder.kill()
        holder.wait(timeout=10)


# ── crash release: SIGKILL frees the kernel lock ─────────────────────────────


def test_r14_crash_releases_lock(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    CompanyProfile(name="Crash", industry="base").save_to_yaml(profile)
    ready = tmp_path / "ready"
    ready.mkdir()
    env = _subprocess_env(profile, RACE_READY_DIR=str(ready))
    holder = subprocess.Popen(
        [sys.executable, str(_WORKER), "hold"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 30
    while not (ready / "holder.ready").exists():
        if time.monotonic() > deadline:
            holder.kill()
            raise AssertionError("holder never acquired the lock")
        time.sleep(0.05)
    holder.kill()  # SIGKILL — no cleanup path can run
    holder.wait(timeout=10)

    # The kernel released the flock when the fd died with the process:
    # acquisition succeeds well under the timeout — no stale-lock deadlock.
    start = time.monotonic()
    with profile_lock(profile, timeout_s=5):
        pass
    assert time.monotonic() - start < 5


# ── monotone version: non-CAS writers cannot regress the counter ─────────────


def test_r14_version_monotone_across_writers(tmp_path: Path) -> None:
    profile = tmp_path / "profile.yaml"
    CompanyProfile(name="v5", industry="base", version=5).save_to_yaml(profile)
    assert CompanyProfile.load_from_yaml(profile).version == 5
    # A non-CAS writer (onboarding, fixture load, slot restore) carrying a
    # stale/default version bumps off the persisted counter, never below it —
    # a client pin on v5 dies with this overwrite instead of colliding.
    CompanyProfile(name="restored", industry="other").save_to_yaml(profile)
    after = CompanyProfile.load_from_yaml(profile)
    assert after.name == "restored"
    assert after.version == 6
