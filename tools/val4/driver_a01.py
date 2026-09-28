"""VAL4-01 REM-01 — driverul real BOAgents (A01) pentru gate-ul comun.

Rulează ÎN worktree-ul A01 fixat la SHA, cu venv-ul A01
(packages/core/.venv). CLI compatibil cu contractul gate-exec.sh:

    python tools/val4/driver_a01.py flow --wt <worktree> --db <sqlite> \
        --tenant tenant-alpha [--steps N] [--execute] [--correlation C] \
        [--guardian-ref MND]
    python tools/val4/driver_a01.py work --wt <worktree> --db <sqlite>
    python tools/val4/driver_a01.py deliver --wt <worktree> --db <sqlite>
    python tools/val4/driver_a01.py stare --wt <worktree> --db <sqlite>

Calea reală, fără niciun eveniment construit manual:
settings → (opțional) mandat Guardian → create_mandate(guardian_ref) →
submit_execution → work_once cu SyntheticCounterProvider (contor
persistent) → plicurile bo.execution-control.event.v1 ies prin
bo_telemetry_outbox → deliver_pending le postează byte-identic la
receptorul /v1/execution-events. La fiecare frontieră de efect motorul
reverifică GET /v1/mandates/{ref}/status în Guardian.

Mediu (setat înainte de import): BOAGENTS_DB_PATH, BO_TENANT_ID,
BO_TELEMETRY_ENABLED=1, BO_TELEMETRY_ENDPOINT (URL complet
/v1/execution-events), BO_TELEMETRY_TOKEN (execobs:write),
BO_TELEMETRY_PRODUCER_ID, BO_INSTALLATION_ID.

Legătura Guardian — EXPLICITĂ și stabilă (VAL4-03):
  BO_GUARDIAN_URL, apoi baza derivată din BO_TELEMETRY_ENDPOINT
  (endpointul pe care produsul îl „vede" — gate-ul îl poate redirecționa
  spre un stub de autoritate), apoi GATE_BASE ca fallback → endpointul
  de autorizare; BO_GUARDIAN_ADMIN_TOKEN (implicit „exadmin",
  credențialul sintetic execpolicy al gate-ului) → emiterea mandatului
  și stratul de drepturi efective în probe.
  Rezolvarea guardian_ref: --guardian-ref → BO_GUARDIAN_MANDATE_REF →
  „mnd-gate-val402" (ID fix al sondei, doar dacă e ACTIVE) → mandat
  emis proaspăt „mnd-a01-*" (ID-ul întors de Guardian). NICIODATĂ
  selecție automată a altui mandat ACTIVE când ref-ul cerut e
  revocat/inaccesibil. Fără legătură configurată, execuția rămâne
  locală (mod standalone, documentat).

Probe `probe` (VAL4-03, autoritate): policy-revoke · rights-reduced ·
authority-invalid · wrong-ref · tenant-mismatch · no-token ·
authority-down · restart-bound. Fiecare măsoară contorul sintetic —
un efect după blocare = eșec vizibil în JSON-ul de ieșire. Fiecare
scenariu care emite mandate își restabilește întâi politica canonică
(probele nu se contaminează între ele).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
import uuid
from decimal import Decimal


def _setup(wt: str, db: str) -> None:
    os.environ["BOAGENTS_DB_PATH"] = db
    sys.path.insert(0, os.path.join(wt, "packages", "core"))
    from openexecutive.bo.db import initialize_db
    initialize_db()


def _set(tenant: str, key: str, value, actor: str = "driver-gate") -> None:
    """Setare reală prin magazinul de setări (CAS cu versiunea curentă)."""
    from openexecutive.bo.db import get_conn
    from openexecutive.bo.settings import store as settings_store
    with get_conn() as conn:
        row = conn.execute(
            "SELECT version FROM bo_settings WHERE tenant = ? AND key = ?",
            (tenant, key),
        ).fetchone()
    settings_store.set_value(
        tenant, key, value,
        expected_version=0 if row is None else int(row["version"]),
        actor=actor,
    )


def _guardian_base() -> str:
    """URL-ul de bază Guardian: endpointul explicit BO_GUARDIAN_URL,
    apoi baza derivată din BO_TELEMETRY_ENDPOINT (endpointul de livrare
    al produsului — gate-ul îl redirectionează spre stub-uri de
    autoritate pentru probele de răspuns invalid), apoi GATE_BASE
    (knob intern al instrumentului, doar fallback)."""
    if os.environ.get("BO_GUARDIAN_URL"):
        return os.environ["BO_GUARDIAN_URL"].rstrip("/")
    ep = os.environ.get("BO_TELEMETRY_ENDPOINT", "").rstrip("/")
    if ep:
        return ep.split("/v1/")[0]
    return os.environ.get("GATE_BASE", "").rstrip("/")


def _admin_token() -> str:
    # Credențialul sintetic execpolicy al gate-ului — public în
    # gate-exec.sh; valid doar contra unui Guardian de dev cu fixture.
    return os.environ.get("BO_GUARDIAN_ADMIN_TOKEN", "exadmin")


def _http(method: str, url: str, token: str, body=None,
          timeout: float = 10.0):
    req = urllib.request.Request(
        url, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"{}")
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode()[:300]
        except Exception:
            detail = ""
        return exc.code, detail
    except Exception as exc:  # noqa: BLE001 — Guardian jos/net
        return 0, str(exc)[:200]


def _mandate_status(base: str, tenant: str, ref: str, token: str):
    s, d = _http("GET", f"{base}/v1/mandates/{ref}/status?tenant={tenant}",
                 token)
    return d if s == 200 and isinstance(d, dict) else None


def _resolve_guardian_ref(base: str, tenant: str, args) -> str | None:
    """Legare EXPLICITĂ, stabilă — niciodată „cel mai nou ACTIVE":
    --guardian-ref → BO_GUARDIAN_MANDATE_REF → mnd-gate-val402 (ID-ul
    fix al sondei gate) → mandat emis proaspăt de driver (ID-ul întors
    de Guardian). Dacă ref-ul cerut e revocat/inaccesibil, legătura nu
    se repară prin alegerea altui mandat — rămâne neligată și se raportează."""
    explicit = (
        getattr(args, "guardian_ref", None)
        or os.environ.get("BO_GUARDIAN_MANDATE_REF")
    )
    if explicit:
        return explicit
    if not base:
        return None
    admin = _admin_token()
    st = _mandate_status(base, tenant, "mnd-gate-val402", admin)
    if st and st.get("status") == "ACTIVE":
        return "mnd-gate-val402"
    # Nimic explicit disponibil — emitem un mandat proaspăt și legăm
    # chiar ID-ul întors de Guardian (explicit, stabil, verificabil).
    mid = f"mnd-a01-{uuid.uuid4().hex[:10]}"
    from datetime import UTC, datetime, timedelta
    now = datetime.now(UTC)
    doc = {
        "schemaVersion": "bo.execution-control.mandate.v1",
        "mandateId": mid, "mandateVersion": 1, "status": "ACTIVE",
        "tenantRef": tenant, "principalRef": "driver-gate",
        "product": "BOAgents",
        "installationId": os.environ.get(
            "BO_INSTALLATION_ID", "inst-a01-alpha"),
        "parentRef": None,
        "issuedAt": now.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"),
        "expiresAt": (now + timedelta(hours=1)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
        "policyVersion": "pol_0", "correlationId": f"corr-{mid}",
        "allowed": {"resources": ["synth.*", "tool:counter.increment",
                                  "tool:catalog.read"],
                    "actions": ["read", "checkpoint", "effect.intent",
                                "increment"]},
        "limits": {"maxSteps": 20, "maxDepth": 1, "maxConcurrency": 2,
                   "budget": {"amount": "50.00", "currency": "USD"}},
        "checkpoint": {"required": True, "everySteps": 5},
    }
    s, d = _http("POST", f"{base}/v1/mandates?tenant={tenant}", admin,
                 {"mandate": doc, "reason": "emis de driverul A01"})
    return mid if s in (200, 201) else None


def _configure_guardian(
    tenant: str, base: str, bound: bool, *, policy_check: bool = False
) -> None:
    """Setările bo.exec.guardian.* prin magazinul real — persistate,
    auditate, vizibile în UI. `policy_check` provisionează stratul de
    drepturi efective cu credențialul admin al sondei (execpolicy:read)
    — în producție se provisionează un credențial dedicat."""
    if not base:
        return
    _set(tenant, "bo.exec.guardian_endpoint", base)
    _set(tenant, "bo.exec.guardian_secret_ref", "BO_TELEMETRY_TOKEN")
    _set(tenant, "bo.exec.guardian_auth_required", bool(bound))
    if policy_check:
        _set(tenant, "bo.exec.guardian_policy_secret_ref",
             "BO_GUARDIAN_ADMIN_TOKEN")


def _mandate(tenant: str, actor: str, guardian_ref: str | None):
    from datetime import UTC, datetime, timedelta

    from openexecutive.bo.execution import store
    expiry = (datetime.now(UTC) + timedelta(hours=1)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    return store.create_mandate(
        tenant,
        {
            "allowed_resources": ["synth.*"],
            "allowed_actions": ["increment"],
            "budget_limit": "10",
            "concurrency_limit": 2,
            "max_steps": 10,
            "max_depth": 1,
            "expires_at": expiry,
        },
        parent=None, principal_ref=actor,
        policy_version=0, actor=actor, max_depth_cap=3,
        guardian_ref=guardian_ref,
    )


def _outbox_stats(tenant: str) -> dict:
    from openexecutive.bo.db import get_conn
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT delivered, COUNT(*) n FROM bo_telemetry_outbox "
            "WHERE tenant = ? GROUP BY delivered", (tenant,),
        ).fetchall()
    return {str(r["delivered"]): r["n"] for r in rows}


def cmd_flow(args) -> dict:
    """Mandat (+legătură Guardian) + execuție reală: pași sintetici,
    contor persistent, outbox."""
    _setup(args.wt, args.db)
    from openexecutive.bo.execution import engine, store
    from openexecutive.bo.execution.synth import SyntheticCounterProvider
    from openexecutive.bo.routing import delivery

    tenant = args.tenant
    _set(tenant, "bo.exec.enabled", True)
    base = _guardian_base()
    gref = _resolve_guardian_ref(base, tenant, args)
    _configure_guardian(tenant, base, bound=bool(gref))
    provider = SyntheticCounterProvider(idempotent=True)
    mandate = _mandate(tenant, actor="driver-gate", guardian_ref=gref)
    steps = [
        {"action": "increment", "resource": "synth.counter",
         "payload": {"amount": 1}}
        for _ in range(args.steps)
    ]
    run = engine.submit_execution(
        tenant, mandate.mandate_id, steps,
        budget_amount=Decimal("5"), correlation_id=args.correlation,
        actor="driver-gate",
    )
    outcome = None
    if args.execute:
        outcome = engine.work_once(
            tenant, provider=provider, worker_id=args.worker)
    delivered = delivery.deliver_pending(tenant)
    return {
        "tenant": tenant, "mandate_id": mandate.mandate_id,
        "guardian_ref": gref, "guardian_base": base or None,
        "run_id": run["run_id"], "work": outcome,
        "final_state": store.get_run(tenant, run["run_id"])["state"],
        "efecte_furnizor": provider.total(tenant),
        "deliver": delivered, "outbox": _outbox_stats(tenant),
    }


def cmd_work(args) -> dict:
    """Un ciclu de lucru pe run-urile existente — faza după revocare."""
    _setup(args.wt, args.db)
    from openexecutive.bo.execution import engine
    from openexecutive.bo.execution.synth import SyntheticCounterProvider
    tenant = args.tenant
    provider = SyntheticCounterProvider(idempotent=True)
    outcome = engine.work_once(
        tenant, provider=provider, worker_id=args.worker)
    return {"tenant": tenant, "work": outcome,
            "efecte_furnizor": provider.total(tenant),
            "outbox": _outbox_stats(tenant)}


def cmd_deliver(args) -> dict:
    """Doar ciclul de livrare — retry după repornirea receptorului."""
    _setup(args.wt, args.db)
    from openexecutive.bo.routing import delivery
    tenant = args.tenant
    return {"tenant": tenant, "deliver": delivery.deliver_pending(tenant),
            "outbox": _outbox_stats(tenant)}


def cmd_stare(args) -> dict:
    _setup(args.wt, args.db)
    from openexecutive.bo.execution.synth import SyntheticCounterProvider
    tenant = args.tenant
    return {"tenant": tenant,
            "efecte_furnizor": SyntheticCounterProvider(
                idempotent=True).total(tenant),
            "outbox": _outbox_stats(tenant)}


# --------------------------------------------------------------------- #
# probe — scenarii de autoritate VAL4-03 (contorul sintetic e dovada)
# --------------------------------------------------------------------- #

_PROBE_STEP = {"action": "effect.intent",
               "resource": "tool:counter.increment",
               "payload": {"amount": 1}}


def _policy_get(base: str, tenant: str, token: str):
    s, d = _http("GET", f"{base}/v1/exec-policies?tenant={tenant}", token)
    return d if s == 200 and isinstance(d, dict) else None


_GATE_POLICY = {
    "schemaVersion": "bo.exec-policy.v1",
    "tenantRef": "tenant-alpha", "maxSteps": 100, "maxDepth": 3,
    "maxConcurrency": 4, "maxMandateTtlMinutes": 480,
    "budgetCap": {"amount": "50.00", "currency": "USD"},
    "allowedResources": ["tool:catalog.read", "tool:counter.increment",
                         "synth.*", "oblio.write"],
    "allowedActions": ["read", "checkpoint", "effect.intent",
                       "increment", "oblio.write"],
    "checkpoint": {"required": True, "everySteps": 5},
    "delegationAllowed": True,
}


def _ensure_policy(base: str, tenant: str, admin: str,
                   policy: dict | None = None) -> bool:
    """Readuce politica tenantului la forma canonică a sondei — fiecare
    scenariu pornește dintr-o stare de autoritate cunoscută, indiferent
    ce a lăsat proba anterioară (revocare / restrângere)."""
    import copy
    cur = _policy_get(base, tenant, admin)
    if cur is None:
        return False
    doc = copy.deepcopy(policy or _GATE_POLICY)
    doc["tenantRef"] = tenant
    s, _d = _http(
        "PUT", f"{base}/v1/exec-policies?tenant={tenant}", admin,
        {"policy": doc, "expectedRev": cur["rev"],
         "expectedSha256": cur["contentSha256"],
         "reason": "probă VAL4-03: stare de autoritate cunoscută"})
    return s in (200, 201)


def _policy_revoke(base: str, tenant: str, token: str,
                   reason: str) -> tuple[int, object]:
    cur = _policy_get(base, tenant, token)
    if not cur or not cur.get("exists"):
        return 0, {"error": "politica lipsește"}
    return _http(
        "POST", f"{base}/v1/exec-policies/revoke?tenant={tenant}", token,
        {"expectedRev": cur["rev"],
         "expectedSha256": cur["contentSha256"], "reason": reason})


def _issue_gate_mandate(base: str, tenant: str, admin: str) -> str | None:
    """Emite un mandat Guardian al cărui `allowed` stă în politica
    curentă — legătura e chiar ID-ul întors (explicit, stabil)."""
    from datetime import UTC, datetime, timedelta
    mid = f"mnd-a01-{uuid.uuid4().hex[:10]}"
    now = datetime.now(UTC)
    doc = {
        "schemaVersion": "bo.execution-control.mandate.v1",
        "mandateId": mid, "mandateVersion": 1, "status": "ACTIVE",
        "tenantRef": tenant, "principalRef": "driver-gate",
        "product": "BOAgents",
        "installationId": os.environ.get(
            "BO_INSTALLATION_ID", "inst-a01-alpha"),
        "parentRef": None,
        "issuedAt": now.isoformat(timespec="milliseconds").replace(
            "+00:00", "Z"),
        "expiresAt": (now + timedelta(hours=1)).isoformat(
            timespec="milliseconds").replace("+00:00", "Z"),
        "policyVersion": "pol_0", "correlationId": f"corr-{mid}",
        "allowed": {"resources": ["tool:counter.increment"],
                    "actions": ["effect.intent"]},
        "limits": {"maxSteps": 20, "maxDepth": 1, "maxConcurrency": 2,
                   "budget": {"amount": "50.00", "currency": "USD"}},
        "checkpoint": {"required": True, "everySteps": 5},
    }
    s, _d = _http("POST", f"{base}/v1/mandates?tenant={tenant}", admin,
                  {"mandate": doc, "reason": "emis de driverul A01"})
    return mid if s in (200, 201) else None


def _bound_run(tenant: str, guardian_ref: str | None,
               db: str) -> dict:
    """Mandat local legat + run PENDING cu un singur pas de efect."""
    from openexecutive.bo.execution import engine, store
    mandate = store.create_mandate(
        tenant,
        {"allowed_resources": ["tool:counter.increment"],
         "allowed_actions": ["effect.intent"],
         "budget_limit": "10", "concurrency_limit": 2,
         "max_steps": 10, "max_depth": 1,
         "expires_at": _expiry()},
        parent=None, principal_ref="driver-gate",
        policy_version=0, actor="driver-gate", max_depth_cap=3,
        guardian_ref=guardian_ref,
    )
    run = engine.submit_execution(
        tenant, mandate.mandate_id, [dict(_PROBE_STEP)],
        budget_amount=Decimal("5"), correlation_id=None,
        actor="driver-gate",
    )
    return {"mandate_id": mandate.mandate_id, "run_id": run["run_id"],
            "guardian_ref": guardian_ref}


def _expiry() -> str:
    from datetime import UTC, datetime, timedelta
    return (datetime.now(UTC) + timedelta(hours=1)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _work_result(tenant: str) -> dict:
    from openexecutive.bo.execution import engine
    from openexecutive.bo.execution.synth import SyntheticCounterProvider
    prov = SyntheticCounterProvider(idempotent=True)
    out = engine.work_once(
        tenant, provider=prov, worker_id="probe")
    return {"work": out, "efecte_furnizor": prov.total(tenant)}


def cmd_probe(args) -> dict:
    """Scenarii de autoritate — fiecare termină cu contorul sintetic;
    efect după blocare = PASS fals."""
    _setup(args.wt, args.db)
    from openexecutive.bo.execution import guardian  # noqa: F401

    tenant = args.tenant
    _set(tenant, "bo.exec.enabled", True)
    base = _guardian_base()
    admin = _admin_token()
    scenario = args.scenario

    if scenario == "policy-revoke":
        # Mandat ACTIVE legat + strat de drepturi efective pornit;
        # politica revocată DUPĂ aprobarea mandatului → efectul e
        # blocat (policy_revoked), nu permis de eticheta ACTIVE.
        if not _ensure_policy(base, tenant, admin):
            return {"scenario": scenario, "error": "politica nu poate "
                    "fi restaurată — Guardian indisponibil"}
        ref = _issue_gate_mandate(base, tenant, admin)
        if not ref:
            return {"scenario": scenario,
                    "error": "mandatul Guardian nu a fost emis"}
        _configure_guardian(tenant, base, bound=True, policy_check=True)
        r = _bound_run(tenant, ref, args.db)
        revoked = _policy_revoke(
            base, tenant, admin, "probă VAL4-03: revocare politică")
        return {"scenario": scenario, **r, "policy_revoke": revoked,
                **_work_result(tenant)}

    if scenario == "rights-reduced":
        # Politica restrânsă după emiterea mandatului: acțiunea pasului
        # iese din allowedActions → efectul e blocat (outside_policy).
        import copy
        if not _ensure_policy(base, tenant, admin):
            return {"scenario": scenario, "error": "politica nu poate "
                    "fi restaurată — Guardian indisponibil"}
        ref = _issue_gate_mandate(base, tenant, admin)
        if not ref:
            return {"scenario": scenario,
                    "error": "mandatul Guardian nu a fost emis"}
        _configure_guardian(tenant, base, bound=True, policy_check=True)
        r = _bound_run(tenant, ref, args.db)
        cur = _policy_get(base, tenant, admin)
        pol = copy.deepcopy(cur["policy"])
        pol["allowedActions"] = [
            a for a in pol["allowedActions"] if a != "effect.intent"]
        s, d = _http(
            "PUT", f"{base}/v1/exec-policies?tenant={tenant}", admin,
            {"policy": pol, "expectedRev": cur["rev"],
             "expectedSha256": cur["contentSha256"],
             "reason": "probă VAL4-03: drepturi reduse"})
        return {"scenario": scenario, **r, "policy_put": [s, d],
                **_work_result(tenant)}

    if scenario == "authority-invalid":
        # Autoritatea răspunde 200 dar cu un corp invalid (fără status)
        # → refuzat, nu tratat ca permisiv.
        import http.server
        import threading

        class _BadAuthority(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                body = b'{"unexpected": true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):  # noqa: A003
                pass

        srv = http.server.HTTPServer(
            ("127.0.0.1", 0), _BadAuthority)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        bad_base = f"http://127.0.0.1:{srv.server_address[1]}"
        _configure_guardian(tenant, bad_base, bound=True)
        r = _bound_run(tenant, "mnd-gate-val402", args.db)
        out = {"scenario": scenario, **r, **_work_result(tenant)}
        srv.shutdown()
        return out

    if scenario == "wrong-ref":
        # Mandat legat la un ref care nu există în Guardian →
        # not_found → deny, contor nemișcat.
        _configure_guardian(tenant, base, bound=True)
        r = _bound_run(tenant, "mnd-inexistent-probe", args.db)
        return {"scenario": scenario, **r, **_work_result(tenant)}

    if scenario == "restart-bound":
        # Legătura persistă peste restart de PROCES: se creează
        # mandatul legat + run-ul aici, iar `work` rulează într-un
        # subproces real (aceeași DB, proces nou). Revocăm între ele →
        # workerul nou trebuie să blocheze efectul.
        import subprocess
        if not _ensure_policy(base, tenant, admin):
            return {"scenario": scenario, "error": "politica nu poate "
                    "fi restaurată — Guardian indisponibil"}
        ref = _issue_gate_mandate(base, tenant, admin)
        if not ref:
            return {"scenario": scenario,
                    "error": "mandatul Guardian nu a fost emis"}
        _configure_guardian(tenant, base, bound=True)
        r = _bound_run(tenant, ref, args.db)
        s, _d = _http(
            "POST",
            f"{base}/v1/mandates/{ref}/revoke?tenant={tenant}", admin,
            {"reason": "probă VAL4-03: restart cu legare păstrată"})
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__), "work",
             "--wt", args.wt, "--db", args.db, "--tenant", tenant],
            capture_output=True, text=True, env=dict(os.environ),
            timeout=60)
        last = {}
        for line in proc.stdout.strip().splitlines()[::-1]:
            try:
                last = json.loads(line)
                break
            except ValueError:
                continue
        return {"scenario": scenario, **r, "revoke_http": s,
                "subprocess": last}

    if scenario == "tenant-mismatch":
        # Mandatul există în Guardian dar sub ALT tenant (tenant-beta):
        # din tenant-alpha, statusul nu e găsit → deny, Δ=0. Nimic nu
        # „repară" legătura spre un mandat al altui tenant.
        if not _ensure_policy(base, "tenant-beta", admin):
            return {"scenario": scenario, "error": "politica tenant-beta "
                    "nu poate fi pregătită — Guardian indisponibil"}
        other_ref = _issue_gate_mandate(base, "tenant-beta", admin)
        if not other_ref:
            return {"scenario": scenario,
                    "error": "mandatul tenant-beta nu a fost emis"}
        _configure_guardian(tenant, base, bound=True)
        r = _bound_run(tenant, other_ref, args.db)
        return {"scenario": scenario, **r, "alt_tenant": "tenant-beta",
                **_work_result(tenant)}

    if scenario == "no-token":
        # Legătura e configurată, mandatul e legat, dar credențialul
        # lipsește din mediu → verificarea nu poate rula → PAUZĂ
        # (guardian_unavailable), nu degradare silențioasă spre local.
        if not _ensure_policy(base, tenant, admin):
            return {"scenario": scenario, "error": "politica nu poate "
                    "fi restaurată — Guardian indisponibil"}
        ref = _issue_gate_mandate(base, tenant, admin)
        if not ref:
            return {"scenario": scenario,
                    "error": "mandatul Guardian nu a fost emis"}
        _configure_guardian(tenant, base, bound=True)
        _set(tenant, "bo.exec.guardian_secret_ref",
             "BO_TOKEN_INEXISTENT_PROBE")
        saved = os.environ.pop("BO_TELEMETRY_TOKEN", None)
        try:
            r = _bound_run(tenant, ref, args.db)
            return {"scenario": scenario, **r, **_work_result(tenant)}
        finally:
            if saved is not None:
                os.environ["BO_TELEMETRY_TOKEN"] = saved

    if scenario == "authority-down":
        # Endpoint setat dar autoritatea nu răspunde (port mort) →
        # PAUZĂ (guardian_unavailable): recuperabil, reluabil; contorul
        # nu se mișcă.
        ref = _issue_gate_mandate(base, tenant, admin)
        if not ref:
            return {"scenario": scenario,
                    "error": "mandatul Guardian nu a fost emis"}
        _configure_guardian(tenant, "http://127.0.0.1:1", bound=True)
        r = _bound_run(tenant, ref, args.db)
        return {"scenario": scenario, **r, **_work_result(tenant)}

    return {"scenario": scenario, "error": "scenariu necunoscut"}


def cmd_soak(args) -> dict:
    """Soak FINIT și configurabil (--iterations, --steps): N cicluri
    submit→work→deliver pe același mandat legat. Raportează contoarele
    dublei/pierderi, latența măsurată și creșterea cozii — fără afirmații
    de capacitate dintr-o proba locală finită."""
    import time
    _setup(args.wt, args.db)
    from openexecutive.bo.execution import engine, store
    from openexecutive.bo.execution.synth import SyntheticCounterProvider
    from openexecutive.bo.routing import delivery

    tenant = args.tenant
    _set(tenant, "bo.exec.enabled", True)
    base = _guardian_base()
    gref = _resolve_guardian_ref(base, tenant, args)
    _configure_guardian(tenant, base, bound=bool(gref))
    provider = SyntheticCounterProvider(idempotent=True)
    mandate = _mandate(tenant, actor="driver-soak", guardian_ref=gref)

    durations_ms = []
    failed_runs = 0
    for _i in range(int(args.iterations)):
        t0 = time.monotonic()
        engine.submit_execution(
            tenant, mandate.mandate_id,
            [{"action": "increment", "resource": "synth.counter",
              "payload": {"amount": 1}} for _ in range(args.steps)],
            budget_amount=Decimal("1"), correlation_id=None,
            actor="driver-soak")
        outcome = engine.work_once(
            tenant, provider=provider, worker_id="soak")
        st = outcome["outcomes"][0]["state"] if outcome.get(
            "outcomes") else None
        if st != store.RUN_SUCCEEDED:
            failed_runs += 1
        delivery.deliver_pending(tenant)
        durations_ms.append(round((time.monotonic() - t0) * 1000, 1))

    stats = _outbox_stats(tenant)
    return {
        "iterations": int(args.iterations), "steps_per_run": args.steps,
        "guardian_ref": gref,
        "efecte_furnizor": provider.total(tenant),
        "efecte_asteptate": int(args.iterations) * args.steps,
        "runs_failed": failed_runs,
        "outbox": stats,
        "latenta_ms": {
            "min": min(durations_ms), "max": max(durations_ms),
            "avg": round(sum(durations_ms) / len(durations_ms), 1),
        },
        "nota": "probă locală finită — nu măsoară capacitatea de "
                "producție; demonstrează doar lipsa dubleurilor/pierderilor",
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["flow", "work", "deliver", "stare",
                                    "probe", "soak"],
                    help="work = ciclu de lucru pe run-urile existente")
    ap.add_argument("scenario", nargs="?", default=None,
                    help="numele probei (cmd=probe)")
    ap.add_argument("--wt", required=True)
    ap.add_argument("--db", required=True)
    ap.add_argument("--tenant", default="tenant-alpha")
    ap.add_argument("--worker", default="gate-w1")
    ap.add_argument("--steps", type=int, default=2)
    ap.add_argument("--correlation", default=None)
    ap.add_argument("--guardian-ref", default=None)
    ap.add_argument("--iterations", type=int, default=10)
    ap.add_argument("--execute", action="store_true")
    args = ap.parse_args()
    out = {
        "flow": cmd_flow, "work": cmd_work,
        "deliver": cmd_deliver, "stare": cmd_stare,
        "probe": cmd_probe, "soak": cmd_soak,
    }[args.cmd](args)
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
