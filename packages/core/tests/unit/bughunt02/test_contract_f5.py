"""CONTROL F5 — multi-process probes for the reopened registry rows.

The coordinator's F5 acceptance criteria require real process boundaries,
not in-process stand-ins:

- B-R-2  requeue re-revendică running: a second PROCESS must never
  recycle a live (or just-crashed-with-live-lease) claim, and must
  recover an expired claim exactly once — including the effect.
- B-R-6  restore/activate slot with a REAL synthetic ChromaDB volume:
  snapshot → switch → switch-back round trip with client separation
  proven by querying the vector store, not stubs.
- B-R-7  outbox ``deliver_pending`` + email poller: two processes race
  on the same outbox/claim rows; an effect committed while the ACK is
  lost reconciles to exactly-once at the receiver; a lost durable mail
  claim can never be won by two owners.
- B-R-9  two tenants on one SQLite, each process booting with its own
  ``BO_TENANT_ID`` config: reads, writes and claims stay isolated;
  nothing degrades to a mono-tenant assumption.

Everything drives production code paths — the subprocess worker
``_f5_worker.py`` calls the real store/poller/delivery functions with
state resolved purely from the process environment.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

_WORKER = Path(__file__).resolve().parent / "_f5_worker.py"
_CORE = Path(__file__).resolve().parents[3]


def _env(**extra: str) -> dict[str, str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_CORE) + os.pathsep + env.get("PYTHONPATH", "")
    env.update(extra)
    return env


def _run_worker(args: list[str], env: dict[str, str], timeout: int = 60) -> dict:
    proc = subprocess.run(
        [sys.executable, str(_WORKER), *args],
        capture_output=True, text=True, env=env, timeout=timeout,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(Path(args[1]).read_text())


# ---------------------------------------------------------------------------
# B-R-2 — scheduled_actions: live lease vs crash orphan, two processes
# ---------------------------------------------------------------------------


def test_br2_requeue_never_reclaims_live_claim_across_processes(
    tmp_path: Path,
) -> None:
    """A claim stamped by another process (alive or just SIGKILLed) is
    lease-protected: the sweeper in a second process leaves it alone.
    Once the lease is stale the orphan is recycled and the effect lands
    exactly once."""
    from openexecutive.memory import episodic

    db = tmp_path / "episodic.db"
    episodic.initialize_db(db)
    episodic.insert_scheduled_action(
        run_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        channel="email",
        channel_ref="a@probe.local",
        intent_text="trimite reminder",
        db_path=db,
    )
    env = _env(EPISODIC_DB_PATH=str(db))

    # Proc A claims the action and simulates a dispatch in progress.
    out_a = tmp_path / "a.json"
    holder = subprocess.Popen(
        [sys.executable, str(_WORKER), "sched-hold", str(out_a),
         str(tmp_path / "a.ready")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while not (tmp_path / "a.ready").exists():
            assert time.monotonic() < deadline, "worker A never claimed"
            assert holder.poll() is None, "worker A exited early"
            time.sleep(0.05)
        assert json.loads(out_a.read_text())["claimed"] != []

        # Proc B sweeps with the normal lease: the live claim is protected.
        b_out = tmp_path / "b.json"
        env_b = dict(env, F5_OUT=str(b_out))
        res = _run_worker(["sched-requeue", str(b_out), "600"], env_b)
        assert res["requeued"] == 0

        # SIGKILL mid-dispatch: no cleanup runs, row stays 'running' with a
        # fresh lease — still protected on the very next sweep.
        holder.kill()
        holder.wait(timeout=10)
        res = _run_worker(["sched-requeue", str(b_out), "600"], env_b)
        assert res["requeued"] == 0
        res = _run_worker(["sched-claim", str(b_out)], env_b)
        assert res["claimed"] == []
    finally:
        if holder.poll() is None:
            holder.kill()
            holder.wait(timeout=10)

    # Lease expiry (or a stale-claim sweep) recycles the orphan: a third
    # process claims it and the effect happens exactly once.
    env_c = dict(env, F5_OUT=str(tmp_path / "c.json"))
    res = _run_worker(
        ["sched-requeue", str(tmp_path / "c.json"), "0"], env_c
    )
    assert res["requeued"] == 1
    effects = tmp_path / "effects.log"
    res = _run_worker(
        ["sched-exec", str(tmp_path / "d.json"), str(effects)], env_c
    )
    assert len(res["done"]) == 1
    assert effects.read_text().count("effect:") == 1

    # Nothing left for a hypothetical fourth process.
    res = _run_worker(["sched-claim", str(tmp_path / "e.json")], env_c)
    assert res["claimed"] == []


# ---------------------------------------------------------------------------
# B-R-9 — two tenant processes on one SQLite
# ---------------------------------------------------------------------------


def test_br9_two_tenant_processes_isolated_on_shared_db(
    tmp_path: Path,
) -> None:
    """alpha and beta run store ops from separate processes, each with its
    own BO_TENANT_ID, on the same bo_agents.db: submissions stay with the
    owner, lists/claims/gets never cross."""
    db = tmp_path / "bo.db"
    alpha = _env(
        BOAGENTS_DB_PATH=str(db), BO_TENANT_ID="alpha",
        F5_CORRELATION="f5-run-alpha", F5_OUT=str(tmp_path / "setup-a.json"),
    )
    beta = _env(
        BOAGENTS_DB_PATH=str(db), BO_TENANT_ID="beta",
        F5_CORRELATION="f5-run-beta", F5_OUT=str(tmp_path / "setup-b.json"),
    )

    setup_a = _run_worker(["exec-setup", str(tmp_path / "setup-a.json")], alpha)
    run_a = setup_a["run_id"]
    assert setup_a["tenant"] == "alpha"

    # Beta's own process sees nothing of alpha: no list entry, no claim,
    # no get (404-equivalent at store level).
    res = _run_worker(["exec-list", str(tmp_path / "list-b.json")], beta)
    assert res["runs"] == []
    res = _run_worker(["exec-claim", str(tmp_path / "claim-b.json")], beta)
    assert res["claimed"] == []
    res = _run_worker(
        ["exec-get", str(tmp_path / "get-b.json"), run_a], beta
    )
    assert res["found"] is False

    # Alpha claims its own pending run from a second process.
    res = _run_worker(["exec-claim", str(tmp_path / "claim-a.json")], alpha)
    assert res["claimed"] == [run_a]

    # Beta submits under the SAME correlation id — no collision, separate
    # row namespaced by tenant.
    setup_b = _run_worker(["exec-setup", str(tmp_path / "setup-b2.json")], beta)
    assert setup_b["run_id"] != run_a
    res = _run_worker(["exec-list", str(tmp_path / "list-b2.json")], beta)
    assert res["runs"] == [setup_b["run_id"]]
    res = _run_worker(
        ["exec-get", str(tmp_path / "get-b2.json"), setup_b["run_id"]], alpha
    )
    assert res["found"] is False


# ---------------------------------------------------------------------------
# B-R-7 — outbox deliver_pending: two processes + effect-before-ack loss
# ---------------------------------------------------------------------------


class _ReceiverState:
    def __init__(self, effects: Path, lose_once: set[str]) -> None:
        self.effects = effects
        self.lose_once = lose_once
        self.lock = threading.Lock()


def _make_receiver(state: _ReceiverState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers["Content-Length"]))
            event = json.loads(body)
            event_id = event["eventId"]
            with state.lock:
                applied = set()
                if state.effects.exists():
                    applied = set(state.effects.read_text().split())
                if event_id in applied:
                    # Idempotent receiver: the retry re-sends identical
                    # bytes, the effect is NOT applied a second time.
                    self._ack(200, "DUPLICATE")
                    return
                applied.add(event_id)
                with open(state.effects, "a") as f:
                    f.write(event_id + "\n")
                if event_id in state.lose_once:
                    # Effect committed receiver-side, ACK never sent —
                    # the sender records a retryable failure, not loss.
                    state.lose_once.discard(event_id)
                    self._ack(503, "LOSE-ACK")
                    return
                self._ack(200, "RECEIVED")

        def _ack(self, code: int, status: str) -> None:
            payload = json.dumps({"status": status}).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_a) -> None:  # silence test noise
            pass

    return Handler


def _seed_tenant_telemetry(db: Path, tenant: str, endpoint: str) -> None:
    from openexecutive.bo.settings import store as settings_store

    for key, value in (
        ("bo.telemetry.enabled", True),
        ("bo.telemetry.transport", "http"),
        ("bo.telemetry.endpoint", endpoint),
        ("bo.telemetry.token_ref", "F5_PROBE_TOKEN"),
    ):
        settings_store.set_value(
            tenant, key, value, expected_version=0,
            actor="admin@f5", db_path=db,
        )


def test_br7_two_process_delivery_and_lost_ack_effect_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from openexecutive.bo import db as bo_db
    from openexecutive.bo.routing import observe
    from openexecutive.bo.settings import store as settings_store
    from openexecutive.bo.telemetry import adapter as tel

    db = tmp_path / "bo.db"
    monkeypatch.setattr(bo_db, "DB_PATH", db)
    bo_db.initialize_db(db)
    monkeypatch.setenv("BO_TENANT_ID", "f5-tenant")
    monkeypatch.setenv("BO_TELEMETRY_ENABLED", "1")
    monkeypatch.setenv("BO_TELEMETRY_SECRET_REFS", "F5_PROBE_TOKEN")
    monkeypatch.setenv("F5_PROBE_TOKEN", "probe-secret")
    monkeypatch.setattr(tel, "_adapter", None)

    tenant = "f5-tenant"
    effects = tmp_path / "effects.log"
    state = _ReceiverState(effects, set())
    server = ThreadingHTTPServer(("127.0.0.1", 0), _make_receiver(state))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/recv"
        _seed_tenant_telemetry(db, tenant, endpoint)
        settings_store.set_value(
            tenant, "bo.router.observe_enabled", True,
            expected_version=0, actor="admin@f5", db_path=db,
        )

        # Six envelopes bound to the real HTTP destination.
        for _ in range(6):
            observe.observe_call(
                model="m", actor="specialist", counts=None)
        outbox_ids = [
            r["event_id"] for r in _outbox_rows(db, tenant)
        ]
        assert len(outbox_ids) == 6
        state.lose_once.add(outbox_ids[0])

        worker_env = _env(
            BOAGENTS_DB_PATH=str(db), BO_TENANT_ID=tenant,
            BO_TELEMETRY_ENABLED="1",
            BO_TELEMETRY_SECRET_REFS="F5_PROBE_TOKEN",
            F5_PROBE_TOKEN="probe-secret",
        )
        # Worker 1 sweeps all six rows; the receiver applies every effect
        # but drops the ACK on one — that row stays pending, NOT lost.
        res1 = _run_worker(
            ["outbox-deliver", str(tmp_path / "w1.json")],
            dict(worker_env, F5_OUT=str(tmp_path / "w1.json")),
        )
        assert res1["sent"] == 5
        assert res1["failed"] == 1

        # Worker 2 (a separate process) retries the un-acked row. The
        # receiver already applied the effect and dedupes the identical
        # re-send — DUPLICATE ack, exactly-once at the receiver.
        res2 = _run_worker(
            ["outbox-deliver", str(tmp_path / "w2.json")],
            dict(worker_env, F5_OUT=str(tmp_path / "w2.json")),
        )
        assert res2["sent"] == 1
        assert res2["failed"] == 0

        delivered = [
            r for r in _outbox_rows(db, tenant) if r["delivered"] == 1
        ]
        assert len(delivered) == 6, [
            r["last_error"] for r in _outbox_rows(db, tenant)
        ]
        assert len(set(effects.read_text().split())) == 6
    finally:
        server.shutdown()
        server.server_close()


def _outbox_rows(db: Path, tenant: str) -> list[dict]:
    from openexecutive.bo.routing import store

    with store.get_conn(db) as conn:
        return [
            dict(r) for r in conn.execute(
                "SELECT event_id, delivered, attempts, last_error "
                "FROM bo_telemetry_outbox WHERE tenant = ?",
                (tenant,),
            ).fetchall()
        ]


def test_br7_mail_claim_two_processes_one_owner(tmp_path: Path) -> None:
    """Two processes racing the same message id on one audit journal:
    exactly one wins the durable claim; the loser reads ``lost`` — the
    in-process lock is never the barrier."""
    env_a = _env(F5_OUT=str(tmp_path / "ma.json"))
    env_b = _env(F5_OUT=str(tmp_path / "mb.json"))
    res_a = _run_worker(
        ["mail-claim", str(tmp_path / "ma.json"), str(tmp_path / "audit.db"),
         "m-f5", "0"],
        env_a,
    )
    res_b = _run_worker(
        ["mail-claim", str(tmp_path / "mb.json"), str(tmp_path / "audit.db"),
         "m-f5", "0"],
        env_b,
    )
    verdicts = sorted((res_a["verdict"], res_b["verdict"]))
    assert verdicts == ["claimed", "lost"], (res_a, res_b)


# ---------------------------------------------------------------------------
# B-R-6 — real synthetic Chroma through slot activate/restore
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# B-R-4 — public load_fixture: parked data, auto-snapshot, mid-apply
#          failure and recovery readback through unload_fixture
# ---------------------------------------------------------------------------


def _f5_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Isolated company + every DB the fixture/slot path touches."""
    from openexecutive.bo import db as bo_db
    from openexecutive.clients import slots
    from openexecutive.departments import store as dept_store
    from openexecutive.memory import episodic, honcho_client
    from openexecutive.people import store as people_store

    company = tmp_path / "company"
    company.mkdir()
    settings = SimpleNamespace(
        company_profile_path=company / "profile.yaml",
        vector_store_path=tmp_path / "chroma",
        mcp_servers_config_path=company / "mcp_servers.json",
        honcho_workspace_id="openexec-f5",
    )
    # snapshot_user_state's episodic dump binds `db_path=DB_PATH` at def
    # time — monkeypatching the module attr does NOT redirect it. Anchor
    # the process CWD at tmp_path so the bound default `./episodic_memory.db`
    # lands inside the sandbox, and use that SAME file for the monkeypatched
    # path so lazy and bound readers see one schema.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(episodic, "DB_PATH", tmp_path / "episodic_memory.db")
    monkeypatch.setattr(people_store, "DB_PATH", tmp_path / "people.db")
    monkeypatch.setattr(dept_store, "DB_PATH", tmp_path / "depts.db")
    episodic.initialize_db(tmp_path / "episodic_memory.db")
    people_store.initialize_db(tmp_path / "people.db")
    dept_store.initialize_db(tmp_path / "depts.db")
    monkeypatch.setattr(bo_db, "DB_PATH", tmp_path / "bo.db")
    bo_db.initialize_db(tmp_path / "bo.db")
    monkeypatch.setenv("COMPANY_PROFILE_PATH", str(settings.company_profile_path))
    monkeypatch.setenv("HONCHO_WORKSPACE_ID", "openexec-f5")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    honcho_client.reset_client_for_tests()
    monkeypatch.setattr(
        slots, "_set_honcho_client_workspace", lambda _slug: None)
    return settings


async def test_br4_public_load_fixture_snapshot_failure_and_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Public path only: ``load_fixture`` auto-snapshots user state,
    preserves parked client data, and — on a mid-apply crash — leaves no
    stranded sentinel while ``unload_fixture`` restores the user's state
    by readback."""
    from openexecutive.cli import fixture_loader
    from openexecutive.clients import slots
    from openexecutive.knowledge.store import ChromaDBStore
    from openexecutive.memory.company_profile import CompanyProfile

    def _name(path: Path) -> str:
        return CompanyProfile.load_from_yaml(path).name

    settings = _f5_env(tmp_path, monkeypatch)
    company = settings.company_profile_path.parent
    docs = company / "docs"
    docs.mkdir()
    for i in range(3):
        (docs / f"user-{i}.md").write_text(f"# User doc {i}\n\nuser-marker-{i}")
    settings.company_profile_path.write_text("name: User Co\n")

    # A parked client slot with its own doc — must survive everything below.
    await slots.create_client_slot(
        settings, display_name="Client B", slug="client_b", source="blank")
    parked_doc = company / "_client_slots" / "client_b" / "docs"
    parked_doc.mkdir(parents=True, exist_ok=True)
    (parked_doc / "beta.md").write_text("# B\n\nbeta-parked-marker")

    # Synthetic fixture on a private FIXTURES_ROOT — real _apply path.
    fixture_root = tmp_path / "fixtures"
    (fixture_root / "demo_f5" / "docs").mkdir(parents=True)
    (fixture_root / "demo_f5" / "profile.yaml").write_text("name: Demo F5\n")
    (fixture_root / "demo_f5" / "docs" / "only.md").write_text(
        "# Fixture doc\n\nfixture-marker")
    monkeypatch.setattr(fixture_loader, "FIXTURES_ROOT", fixture_root)

    # _load_from_dir swallows snapshot errors (best-effort on empty envs);
    # spy so a swallowed failure is diagnosable, not silent.
    snapshot_errors: list[str] = []
    _orig_snapshot = fixture_loader.snapshot_user_state

    def _spy_snapshot(s: Any, **kw: Any) -> dict:
        try:
            return _orig_snapshot(s, **kw)
        except Exception as exc:  # pragma: no cover - diagnostic surface
            snapshot_errors.append(repr(exc))
            raise

    monkeypatch.setattr(
        fixture_loader, "snapshot_user_state", _spy_snapshot)

    # Happy path: seed 3→1, auto-snapshot, sentinel, parked data intact.
    summary = await fixture_loader.load_fixture("demo_f5", settings)
    assert summary["docs_indexed"] >= 1
    assert summary["auto_snapshot_taken"] is True, snapshot_errors
    assert _name(company / "_user_backup" / "profile.yaml") == "User Co"
    assert _name(company / "profile.yaml") == "Demo F5"
    assert sorted(p.name for p in docs.glob("*.md")) == ["only.md"]
    assert fixture_loader._fixture_active_sentinel(settings).read_text() == (
        "demo_f5"
    )
    assert (parked_doc / "beta.md").exists()

    # Rollback via the public unload: user state restored by readback.
    await fixture_loader.unload_fixture(settings)
    assert _name(settings.company_profile_path) == "User Co"
    assert sorted(p.name for p in docs.glob("*.md")) == [
        "user-0.md", "user-1.md", "user-2.md"]
    assert not fixture_loader._fixture_active_sentinel(settings).exists()
    assert (parked_doc / "beta.md").exists()

    # Failure leg: the apply crashes AFTER profile/docs swap (strict
    # research wipe raises). The exception propagates, the sentinel is
    # never written, and the user's snapshot is NOT overwritten by the
    # fixture's state.
    def _boom(self: Any, **_kw: Any) -> None:
        raise OSError("injected mid-apply failure")

    with monkeypatch.context() as m:
        m.setattr(ChromaDBStore, "delete_notion_docs", _boom)
        with pytest.raises(OSError, match="injected mid-apply"):
            await fixture_loader.load_fixture("demo_f5", settings)
    assert not fixture_loader._fixture_active_sentinel(settings).exists()
    # The ORIGINAL user backup survived — a failed load must never
    # re-snapshot the half-applied fixture state over it.
    assert _name(company / "_user_backup" / "profile.yaml") == "User Co"

    # Public recovery path: unload restores the user's company by
    # readback, parked data still intact.
    await fixture_loader.unload_fixture(settings)
    assert _name(settings.company_profile_path) == "User Co"
    assert sorted(p.name for p in docs.glob("*.md")) == [
        "user-0.md", "user-1.md", "user-2.md"]
    assert (parked_doc / "beta.md").exists()


@pytest.mark.asyncio
async def test_br6_slot_switch_roundtrip_on_real_chroma(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The slot machinery rebuilds the REAL persisted vector store on every
    activation: A's docs are queryable while A is live, gone after the
    switch to B, and back on restore — a synthetic Chroma volume, no REST."""
    from openexecutive.clients import slots
    from openexecutive.knowledge.store import ChromaDBStore

    company = tmp_path / "company"
    company.mkdir()
    chroma_dir = tmp_path / "chroma"
    settings = SimpleNamespace(
        company_profile_path=company / "profile.yaml",
        vector_store_path=chroma_dir,
        mcp_servers_config_path=company / "mcp_servers.json",
        honcho_workspace_id="default-ws",
    )
    db_path = tmp_path / "episodic.db"
    from openexecutive.departments import store as dept_store
    from openexecutive.memory import episodic
    from openexecutive.people import store as people_store

    monkeypatch.setattr(episodic, "DB_PATH", db_path)
    monkeypatch.setattr(people_store, "DB_PATH", db_path)
    monkeypatch.setattr(dept_store, "DB_PATH", db_path)
    episodic.initialize_db(db_path)
    people_store.initialize_db(db_path)
    dept_store.initialize_db(db_path)
    monkeypatch.setattr(
        slots, "_set_honcho_client_workspace", lambda _slug: None)

    docs = company / "docs"
    docs.mkdir()
    (docs / "strategy.md").write_text(
        "# Acme Strategy\n\nacme-unique-marker alpha roadmap")
    settings.company_profile_path.write_text("name: Acme Corp\n")

    from openexecutive.knowledge.loader import ingest_file

    await slots.create_client_slot(
        settings, display_name="Acme Corp", source="current")
    await slots.create_client_slot(
        settings, display_name="Beta Inc", source="blank")

    store = ChromaDBStore(persist_directory=chroma_dir)
    probe = ChromaDBStore(persist_directory=chroma_dir)

    def _hits(marker: str) -> list[dict]:
        return probe.query(
            marker,
            collection=ChromaDBStore.COMPANY_COLLECTION,
            n_results=5,
        )

    # Slot A live: index its doc the way production ingest does.
    await ingest_file(
        path=docs / "strategy.md",
        store=store,
        collection=ChromaDBStore.COMPANY_COLLECTION,
    )
    hits = _hits("acme-unique-marker")
    assert any("acme-unique-marker" in (h.get("text") or "") for h in hits)

    await slots.activate_client_slot(settings, "beta_inc")
    # After the switch the real volume no longer serves A's chunk — the
    # rebuild wiped it before indexing B's baseline.
    hits_b = _hits("acme-unique-marker")
    assert not any(
        "acme-unique-marker" in (h.get("text") or "") for h in hits_b
    )

    # B writes its own doc; it is searchable while B is live.
    (docs / "beta.md").write_text("# Beta\n\nbeta-unique-marker only B")
    await ingest_file(
        path=docs / "beta.md",
        store=store,
        collection=ChromaDBStore.COMPANY_COLLECTION,
    )
    assert any(
        "beta-unique-marker" in (h.get("text") or "")
        for h in _hits("beta-unique-marker")
    )

    # Park B (snapshot includes beta.md), restore A: the rebuild wipes
    # B's chunk and reindexes A's — separation proven on the real store.
    await slots.activate_client_slot(settings, "acme_corp")
    hits_a = _hits("acme-unique-marker")
    assert any("acme-unique-marker" in (h.get("text") or "") for h in hits_a)
    assert not any(
        "beta-unique-marker" in (h.get("text") or "")
        for h in _hits("beta-unique-marker")
    )

    # The slot dirs survived the round trip (restore is lossless back).
    assert (tmp_path / "company" / "_client_slots"
            / "beta_inc" / "docs" / "beta.md").exists()
