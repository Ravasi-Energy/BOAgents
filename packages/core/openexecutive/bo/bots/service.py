"""BoBot service: validation, draft/publish lifecycle, deterministic dry-run.

The simulator is the only executor in Valul 1. It walks the step list against
a caller-supplied *synthetic* context, records every step's outcome, and
writes receipts marked ``NOT_EXECUTED`` — by construction there is no code
path here that can reach a provider, the network, or an external effect.

Determinism (BO-BOT-003): ``plan_hash`` is SHA-256 over the canonical
``{version_hash, input, predicate_result, steps, findings}`` — no ids, no
timestamps. Same input + same version ⇒ same plan hash, always.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from openexecutive.bo.bots import store
from openexecutive.bo.bots.models import (
    SCHEMA_VERSION,
    DefinitionContent,
    validate_step_semantics,
)
from openexecutive.bo.bots.predicates import PredicateError, Tri, _validate, evaluate
from openexecutive.bo.settings import store as settings_store
from openexecutive.bo.telemetry.adapter import opaque_actor_ref


class ValidationFailure(ValueError):
    """Definition content failed validation — carries the error list."""

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__("; ".join(errors))


class SimulationRefused(Exception):
    """The definition cannot be simulated in this slice (e.g. kind=AI)."""


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(content: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical(content).encode("utf-8")).hexdigest()


def validate_content(raw: dict[str, Any]) -> DefinitionContent:
    """Full server-side validation — pydantic shape + semantic + predicate."""
    errors: list[str] = []
    try:
        content = DefinitionContent.model_validate(raw)
    except ValidationError as exc:
        errors.append(
            "; ".join(
                f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}"
                for e in exc.errors()[:20]
            )
        )
        raise ValidationFailure(errors) from exc
    if content.predicates is not None:
        try:
            _validate(content.predicates, depth=1, budget=[64])
        except PredicateError as exc:
            errors.append(f"predicates: {exc}")
    for step in content.steps:
        if step.predicate is not None and step.type != "check":
            errors.append(f"{step.id}: 'predicate' are sens numai la type=check")
    errors.extend(validate_step_semantics(content.steps))
    if errors:
        raise ValidationFailure(errors)
    return content


# --------------------------------------------------------------------- #
# Lifecycle
# --------------------------------------------------------------------- #

def create(tenant: str, actor: str, payload: dict[str, Any],
           db_path: Path | None = None) -> dict[str, Any]:
    name = str(payload.get("name") or "").strip()
    if not (1 <= len(name) <= 120):
        raise ValidationFailure(["name: obligatoriu, max 120 caractere"])
    kind = payload.get("kind", "BOT")
    if kind not in ("BOT", "AI", "MIXED"):
        raise ValidationFailure(["kind: permise BOT, AI, MIXED"])
    description = str(payload.get("description") or "")[:500]
    content = validate_content(payload.get("content") or {})
    canon = _canonical(content.model_dump(mode="json"))
    definition = store.create_definition(
        tenant,
        name=name, description=description, kind=kind, owner=actor,
        draft_content_json=canon, content_hash=content_hash(content.model_dump(mode="json")),
        schema_version=SCHEMA_VERSION, db_path=db_path,
    )
    _audit(tenant, actor, "bo_bot_create", f"BoBot {definition['id']} creat ({kind})",
           {"definition_id": definition["id"], "kind": kind})
    return definition


def update_draft(tenant: str, actor: str, def_id: str, payload: dict[str, Any],
                 db_path: Path | None = None) -> dict[str, Any]:
    expected = payload.get("expected_version")
    if not isinstance(expected, int) or isinstance(expected, bool) or expected < 1:
        raise ValidationFailure(["expected_version: întreg ≥ 1 obligatoriu"])
    canon = None
    hashed = None
    if payload.get("content") is not None:
        content = validate_content(payload["content"])
        dumped = content.model_dump(mode="json")
        canon = _canonical(dumped)
        hashed = content_hash(dumped)
    name = payload.get("name")
    description = payload.get("description")
    if name is not None:
        name = str(name).strip()
        if not (1 <= len(name) <= 120):
            raise ValidationFailure(["name: obligatoriu, max 120 caractere"])
    if description is not None:
        description = str(description)[:500]
    definition = store.update_draft(
        tenant, def_id, expected_version=expected, actor=actor,
        name=name, description=description,
        draft_content_json=canon, content_hash=hashed, db_path=db_path,
    )
    _audit(tenant, actor, "bo_bot_draft_update", f"ciorna {def_id} → v{definition['draft_version']}",
           {"definition_id": def_id, "draft_version": definition["draft_version"]})
    return definition


def publish(tenant: str, actor: str, def_id: str,
            db_path: Path | None = None) -> dict[str, Any]:
    draft = store.get_version(tenant, def_id, status="draft", db_path=db_path)
    validate_content(draft["content"])  # publish-time revalidation
    definition = store.publish(tenant, def_id, actor=actor, db_path=db_path)
    _audit(tenant, actor, "bo_bot_publish",
           f"{def_id} publicat la v{definition['active_version_no']}",
           {"definition_id": def_id, "active_version_no": definition["active_version_no"]})
    return definition


# --------------------------------------------------------------------- #
# Simulation
# --------------------------------------------------------------------- #

_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_.]+)\}")


def _render(template: str, context: dict[str, Any]) -> str:
    """Substitute {a.b} placeholders from context; unknown paths stay literal."""
    def repl(m: re.Match[str]) -> str:
        node: Any = context
        for seg in m.group(1).split("."):
            if isinstance(node, dict) and seg in node:
                node = node[seg]
            else:
                return m.group(0)  # leave {path} visible — never invent a value
        return node if isinstance(node, str) else json.dumps(node, ensure_ascii=False)
    return _PLACEHOLDER_RE.sub(repl, template)


def _sim_receipt(run_tag: str, step_id: str) -> dict[str, Any]:
    return {
        "kind": "simulated_receipt",
        "operation": f"{run_tag}:{step_id}",
        "effect_status": "NOT_EXECUTED",
        "note": "dry-run — niciun efect extern",
    }


def _config_snapshot(tenant: str, db_path: Path | None) -> dict[str, Any]:
    snap: dict[str, Any] = {}
    for item in settings_store.list_effective(tenant, db_path=db_path):
        snap[item["key"]] = item["value"]
    return snap


def simulate(
    tenant: str, actor: str, def_id: str, *,
    input_context: dict[str, Any] | None,
    version_no: int | None = None,
    simulate_draft: bool = False,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Deterministic dry-run. Persists a run row + step timeline + plan hash."""
    definition = store.get_definition(tenant, def_id, db_path=db_path)
    if definition["kind"] != "BOT":
        raise SimulationRefused(
            "doar BoBots deterministici (kind=BOT) pot fi simulați în Valul 1"
        )
    if simulate_draft:
        version = store.get_version(tenant, def_id, status="draft", db_path=db_path)
    elif version_no is not None:
        version = store.get_version(tenant, def_id, version_no=version_no, db_path=db_path)
        if version["status"] != "published":
            raise SimulationRefused("versiunea cerută nu este publicată")
    else:
        if definition["active_version_no"] is None:
            raise SimulationRefused("definiția nu are încă o versiune activă")
        version = store.get_version(
            tenant, def_id, version_no=definition["active_version_no"], db_path=db_path
        )

    if input_context is None:
        input_context = {}
    if not isinstance(input_context, dict):
        raise ValidationFailure(["input: așteptat obiect JSON"])
    if len(_canonical(input_context)) > 64 * 1024:
        raise ValidationFailure(["input: context prea mare (max 64KB)"])

    max_steps = int(settings_store.get_effective_value(
        tenant, "bo.bobot.simulation.max_steps", db_path=db_path))
    retention_days = int(settings_store.get_effective_value(
        tenant, "bo.bobot.simulation.retention_days", db_path=db_path))
    config_version = settings_store.config_version(tenant, db_path=db_path)
    snapshot = _config_snapshot(tenant, db_path)

    # Revalidate the stored version before running it — drafts are validated
    # at save time, but a persisted row could have been edited out-of-band.
    # A corrupt definition is refused (422), never half-simulated.
    content = validate_content(version["content"])
    version_hash = version["hash"]

    # Telemetry: emitted through the injectable adapter — dropped unless an
    # operator configured a transport (default: off). run_id is pre-generated
    # so RunStarted can name the run it announces. The run reference lives in
    # the envelope (runRef), never inside data — VAL1-02 contract.
    run_id = f"run_{uuid.uuid4().hex[:12]}"
    started_at = time.monotonic()
    _emit(tenant, "RunStarted", {
        "trigger": "manual", "definitionRef": def_id,
        "versionNo": version["version_no"], "runKind": "simulation",
    }, run_ref=run_id, agent_ref=def_id, correlation_id=run_id)

    context = dict(input_context)
    predicate_result = "NOT_EVALUATED"
    if content.predicates is not None:
        predicate_result = evaluate(content.predicates, context).value

    step_records: list[dict[str, Any]] = []
    findings: list[dict[str, Any]] = []
    stopped = False
    capped = False

    # A top-level predicate that is not TRUE gates the whole plan — and the
    # plan records WHY. UNKNOWN is named, never silently treated as FALSE or
    # coerced into TRUE.
    gated = predicate_result not in ("NOT_EVALUATED", "TRUE")

    steps = content.steps
    for idx, step in enumerate(steps):
        if gated:
            step_records.append({
                "step_id": step.id, "step_type": step.type, "status": "SKIPPED",
                "detail": {"reason": "predicate_not_true", "result": predicate_result},
                "receipt": None,
            })
            continue
        if idx >= max_steps:
            capped = True
            step_records.append({
                "step_id": step.id, "step_type": step.type, "status": "SKIPPED",
                "detail": {"reason": "step_limit", "limit": max_steps},
                "receipt": None,
            })
            continue
        if stopped:
            step_records.append({
                "step_id": step.id, "step_type": step.type, "status": "SKIPPED",
                "detail": {"reason": "gate_stop"}, "receipt": None,
            })
            continue

        detail: dict[str, Any] = {}
        receipt = _sim_receipt(definition["id"], step.id)
        status = "SIMULATED"

        if step.type == "check":
            tri = evaluate(step.predicate or {}, context)
            detail = {"predicate_result": tri.value}
            if tri is not Tri.TRUE:
                status = "SKIPPED"
                detail["gate"] = "not_true"
                if step.on_false == "stop":
                    stopped = True
                    detail["stopped_remaining"] = True
        elif step.type == "emit_finding":
            finding = {
                "finding_key": step.finding_key,
                "category": step.category,
                "severity": step.severity,
                "message": _render(step.message or "", context),
                "effect_status": "NOT_EXECUTED",
            }
            findings.append(finding)
            detail = {"finding": finding}
        elif step.type == "set_field":
            seg = (step.path or "").split(".")
            node = context
            for part in seg[:-1]:
                nxt = node.get(part)
                if not isinstance(nxt, dict):
                    nxt = {}
                    node[part] = nxt
                node = nxt
            node[seg[-1]] = step.value
            detail = {"set": step.path, "value": step.value}
        elif step.type == "notify":
            detail = {
                "channel": step.channel,
                "message": _render(step.message or "", context),
                "delivered": False,
            }
        elif step.type == "note":
            detail = {"message": _render(step.message or "", context)}

        step_records.append({
            "step_id": step.id, "step_type": step.type, "status": status,
            "detail": detail, "receipt": receipt,
        })

    plan = {
        "version_hash": version_hash,
        "input": input_context,
        "predicate_result": predicate_result,
        "gated_by_predicate": gated,
        "steps": [
            {
                "step_id": s["step_id"], "type": s["step_type"],
                "status": s["status"],
                "note": (s["detail"] or {}).get("predicate_result"),
            }
            for s in step_records
        ],
        "findings": findings,
    }
    plan_hash = hashlib.sha256(_canonical(plan).encode("utf-8")).hexdigest()

    status_run = "PARTIAL" if capped else "SUCCEEDED"
    result = {
        "execution_status": status_run,
        "predicate_result": predicate_result,
        "gated_by_predicate": gated,
        "step_count": len(steps),
        "steps_evaluated": sum(1 for s in step_records if s["status"] != "SKIPPED"),
        "step_limit": max_steps,
        "capped_by_step_limit": capped,
        "findings": findings,
        "simulation": True,
        "external_effects": 0,
    }

    run_id = store.insert_run(
        tenant,
        definition_id=def_id, version_id=version["id"],
        version_no=version["version_no"], version_hash=version_hash,
        kind="simulation", status=status_run,
        input_json=_canonical(input_context), config_version=config_version,
        config_snapshot_json=_canonical(snapshot), plan_hash=plan_hash,
        result_json=_canonical(result), error=None, created_by=actor,
        run_id=run_id, db_path=db_path,
    )
    store.insert_step_runs(run_id, step_records, db_path=db_path)

    _emit(tenant, "RunFinished", {
        "executionStatus": status_run, "verificationStatus": "VERIFIED",
        "planHash": plan_hash,
        "durationMs": round((time.monotonic() - started_at) * 1000, 3),
    }, run_ref=run_id, agent_ref=def_id, correlation_id=run_id)
    for finding in findings:
        _emit(tenant, "VerificationFinding", {
            "findingId": f"{run_id}:{finding['finding_key']}",
            "category": finding["category"], "severity": finding["severity"],
            "effectStatus": "NOT_EXECUTED",
            "ownerRef": opaque_actor_ref(actor),
            "evidenceRefs": [run_id],
        }, run_ref=run_id, agent_ref=def_id, correlation_id=run_id)

    swept = store.sweep_simulation_runs(tenant, retention_days, db_path=db_path)
    if swept:
        _audit(tenant, actor, "bo_bot_sim_sweep",
               f"retenție simulări: {swept} rulări șterse (>{retention_days} zile)",
               {"definition_id": def_id, "swept": swept})

    _audit(tenant, actor, "bo_bot_simulate",
           f"simulare {def_id}@v{version['version_no']} → {status_run}",
           {"definition_id": def_id, "run_id": run_id, "plan_hash": plan_hash,
            "status": status_run})
    return store.get_run(tenant, run_id, db_path=db_path)


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #

def _audit(tenant: str, actor: str, event_type: str, summary: str,
           details: dict[str, Any]) -> None:
    from openexecutive.audit import log_event

    log_event(event_type, summary, actor=actor,
              details={"tenant": tenant, **details})


def _emit(tenant: str, kind: str, data: dict[str, Any], *,
          run_ref: str | None = None, agent_ref: str | None = None,
          correlation_id: str | None = None) -> None:
    """Emit through the configured adapter; telemetry must never break a run."""
    try:
        from openexecutive.bo.telemetry.adapter import get_adapter

        get_adapter().emit(tenant=tenant, kind=kind, data=data,
                           run_ref=run_ref, agent_ref=agent_ref,
                           correlation_id=correlation_id)
    except Exception as exc:  # noqa: BLE001 — telemetry is fire-and-forget
        import logging

        logging.getLogger(__name__).warning("telemetry emit failed (%s)",
                                            type(exc).__name__)
