"use client";

// Execuții delegate (VAL4-01/VAL4-03): mandate, rulări, checkpointuri și
// ledgerul de efecte. Distincțiile obligatorii din contractul comun:
// execuție ≠ observație, succes ≠ rezultat necunoscut, reluare ≠
// duplicarea efectului, anularea NU inversează un efect extern deja
// executat. Acțiunile consecvențiale cer confirmare + motiv auditat.
import { useCallback, useEffect, useState } from "react";

import IconBO from "@/components/bo/IconBO";
import { InlineAlert, Pill, StateBlock } from "@/components/bo/ui";
import {
  BoApiError,
  cancelBoRun,
  createBoMandate,
  getBoExecStatus,
  getBoMandates,
  getBoRunAuthority,
  getBoRunDetail,
  listBoExecRuns,
  listBoOutbox,
  pauseBoRun,
  rebindBoOutbox,
  reconcileBoRun,
  resumeBoRun,
  retryBoOutbox,
  revokeBoMandate,
  submitBoRun,
  workBoRuns,
  type BoAuthorityCheck,
  type BoExecRun,
  type BoExecStatus,
  type BoMandate,
  type BoOutboxEntry,
  type BoRunDetail,
} from "@/lib/bo";

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "forbidden" }
  | {
      kind: "data";
      status: BoExecStatus;
      runs: BoExecRun[];
      mandates: BoMandate[];
      outbox: BoOutboxEntry[];
      detail: BoRunDetail | null;
    };

const STATE_LABEL: Record<string, { label: string; kind: "ok" | "warn" | "danger" | "info" | "neutral" }> = {
  PENDING: { label: "în așteptare", kind: "neutral" },
  CLAIMED: { label: "revendicată", kind: "info" },
  RUNNING: { label: "în rulare", kind: "info" },
  PAUSED: { label: "pausată", kind: "warn" },
  SUCCEEDED: { label: "reușită", kind: "ok" },
  FAILED: { label: "eșuată", kind: "danger" },
  UNKNOWN: { label: "necunoscută", kind: "warn" },
  RECONCILIATION_REQUIRED: { label: "reconciliere necesară", kind: "danger" },
  CANCELLED: { label: "anulată", kind: "neutral" },
};

const LEDGER_LABEL: Record<string, { label: string; kind: "ok" | "warn" | "danger" | "info" | "neutral" }> = {
  INTENT: { label: "intenție", kind: "neutral" },
  SUBMITTED: { label: "trimisă", kind: "info" },
  SUCCEEDED: { label: "efect confirmat", kind: "ok" },
  FAILED: { label: "efect eșuat", kind: "danger" },
  UNKNOWN: { label: "efect necunoscut", kind: "warn" },
  RECONCILIATION_REQUIRED: { label: "reconciliere", kind: "danger" },
};

// Motivele de blocare produse de motor/Guardian — explicație pentru
// operator, nu doar codul tehnic.
const BLOCK_HINT: Record<string, string> = {
  guardian_revoked: "Mandatul a fost revocat în Guardian.",
  guardian_expired: "Mandatul Guardian a expirat.",
  guardian_not_found: "Mandatul legat nu există în Guardian.",
  guardian_not_active: "Mandatul Guardian nu e ACTIVE.",
  guardian_unbound: "Mandatul local nu are legătură Guardian (cerută).",
  guardian_forbidden: "Credențialul Guardian a fost refuzat.",
  guardian_policy_revoked: "Politica Guardian curentă e revocată.",
  guardian_policy_missing: "Nu există politică Guardian pentru tenant.",
  guardian_outside_policy: "Pasul iese din drepturile efective curente.",
  guardian_invalid_response: "Autoritatea a răspuns invalid.",
  guardian_unavailable: "Guardian nu poate fi contactat/verificat.",
  mandate_revoked: "Mandatul local a fost revocat.",
  mandate_expired: "Mandatul local a expirat.",
  checkpoint_unavailable: "Checkpointul obligatoriu nu a putut fi scris.",
};

function blockText(reason: string | null): string | null {
  if (!reason) return null;
  const key = reason.split(":")[0].trim();
  const hint = BLOCK_HINT[key];
  return hint ? `${hint} (${reason})` : reason;
}

function statePill(state: string) {
  const s = STATE_LABEL[state] ?? { label: state, kind: "neutral" as const };
  return <Pill kind={s.kind}>{s.label}</Pill>;
}

function GuardianBlock({
  guardian,
  onVerify,
  verify,
  busy,
}: {
  guardian: BoRunDetail["guardian"];
  onVerify: () => void;
  verify: BoAuthorityCheck | null;
  busy: boolean;
}) {
  return (
    <div style={{ marginTop: 16 }}>
      <h4 className="bo-card-title">Autoritate efectivă</h4>
      <div className="bo-row" style={{ marginTop: 6, flexWrap: "wrap" }}>
        {guardian.bound_ref ? (
          <Pill kind="info" icon="shield-check">
            autorități Guardian: {(guardian.chain_refs ?? [guardian.bound_ref]).join(", ")}
          </Pill>
        ) : (
          <Pill kind="neutral" icon="info">
            standalone — fără legătură Guardian
          </Pill>
        )}
        <Pill kind={guardian.endpoint_configured ? "ok" : "warn"}>
          endpoint {guardian.endpoint_configured ? "configurat" : "lipsă"}
        </Pill>
        <Pill kind={guardian.credential_configured ? "ok" : "warn"}>
          credențial {guardian.credential_configured ? "prezent" : "lipsă"}
        </Pill>
        {guardian.policy_layer ? (
          <Pill kind="info">citire suplimentară a politicii</Pill>
        ) : null}
      </div>
      {guardian.bound_ref && !guardian.credential_configured ? (
        <div style={{ marginTop: 8 }}>
          <InlineAlert kind="warn">
            Mandatul e legat de Guardian, dar credențialul lipsește din
            mediu — la frontiera de efect rularea se oprește în pauză;
            nu există degradare silențioasă spre control local.
          </InlineAlert>
        </div>
      ) : null}
      <div className="bo-row" style={{ marginTop: 8 }}>
        <button
          type="button"
          className="bo-btn"
          disabled={busy}
          onClick={onVerify}
        >
          Verifică autoritatea acum
        </button>
      </div>
      {verify ? (
        <div style={{ marginTop: 8 }}>
          <InlineAlert
            kind={
              verify.authorized
                ? "info"
                : verify.mode === "unavailable"
                  ? "warn"
                  : "danger"
            }
            icon={verify.authorized ? "shield-check" : "shield-alert"}
          >
            {verify.authorized
              ? `Autorizat ${verify.mode === "guardian" ? "de Guardian" : "local (standalone)"}.`
              : `${blockText(verify.kind) ?? verify.kind}: ${verify.detail ?? ""}`}
          </InlineAlert>
        </div>
      ) : null}
    </div>
  );
}

function MandatesSection({
  mandates,
  onChanged,
}: {
  mandates: BoMandate[];
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "danger"; text: string } | null>(null);
  const [parentId, setParentId] = useState("");
  const [guardianRef, setGuardianRef] = useState("");
  const [resources, setResources] = useState("synth.*");
  const [actions, setActions] = useState("increment");
  const [budget, setBudget] = useState("10");
  const [maxSteps, setMaxSteps] = useState("10");
  const [ttlHours, setTtlHours] = useState("24");

  async function run(fn: () => Promise<unknown>, okText: string) {
    setBusy(true);
    setNotice(null);
    try {
      await fn();
      setNotice({ kind: "ok", text: okText });
      onChanged();
    } catch (err) {
      const text =
        err instanceof BoApiError
          ? `${err.status} — ${typeof err.detail === "string" ? err.detail : err.code}`
          : "Acțiunea a eșuat.";
      setNotice({ kind: "danger", text });
    } finally {
      setBusy(false);
    }
  }

  async function create() {
    const hours = Number(ttlHours);
    if (!Number.isFinite(hours) || hours <= 0) {
      setNotice({ kind: "danger", text: "Valabilitatea trebuie să fie un număr de ore pozitiv." });
      return;
    }
    const parent = mandates.find((m) => m.mandate_id === parentId);
    const requestedExpiry = Date.now() + hours * 3600_000;
    const expires = new Date(
      parent ? Math.min(requestedExpiry, Date.parse(parent.expires_at)) : requestedExpiry,
    ).toISOString();
    await run(
      () =>
        createBoMandate({
          parent_mandate_id: parentId || undefined,
          guardian_ref: guardianRef || undefined,
          allowed_resources: resources.split(",").map((s) => s.trim()).filter(Boolean),
          allowed_actions: actions.split(",").map((s) => s.trim()).filter(Boolean),
          budget_limit: budget,
          concurrency_limit: 2,
          max_steps: Number(maxSteps) || 10,
          max_depth: 1,
          expires_at: expires,
        }),
      parentId
        ? "Mandat delegat creat — drepturile sunt intersecția cu părintele."
        : "Mandat creat.",
    );
  }

  async function revoke(m: BoMandate) {
    const reason = window.prompt(
      `Revoci mandatul ${m.mandate_id} (și tot subarborele)?\nMotivul e obligatoriu și auditat:`,
    );
    if (reason === null) return;
    if (!reason.trim()) {
      setNotice({ kind: "danger", text: "Revocarea cere un motiv — operație consecvențială." });
      return;
    }
    await run(
      () => revokeBoMandate(m.mandate_id, reason.trim()),
      "Mandat revocat — ia efect la următoarea frontieră de efect.",
    );
  }

  return (
    <div className="bo-card" style={{ marginTop: 12 }}>
      <div className="bo-spread">
        <h3 className="bo-card-title">Mandate ({mandates.length})</h3>
        <button
          type="button"
          className="bo-btn"
          onClick={() => setOpen((v) => !v)}
        >
          {open ? "Închide formularul" : "Mandat nou"}
        </button>
      </div>

      {open ? (
        <div style={{ marginTop: 12 }}>
          <div className="bo-row" style={{ flexWrap: "wrap", gap: 8 }}>
            <select
              className="bo-select"
              value={parentId}
              onChange={(e) => setParentId(e.target.value)}
              aria-label="Mandat părinte (delegare)"
            >
              <option value="">Fără părinte (rădăcină)</option>
              {mandates
                .filter((m) => m.state === "active")
                .map((m) => (
                  <option key={m.mandate_id} value={m.mandate_id}>
                    Delegă din {m.mandate_id.slice(0, 18)}…
                  </option>
                ))}
            </select>
            <input
              className="bo-input"
              placeholder="guardian_ref (opțional — legătură explicită)"
              aria-label="Referință Guardian (opțională)"
              value={guardianRef}
              onChange={(e) => setGuardianRef(e.target.value)}
            />
            <input
              className="bo-input"
              placeholder="resurse (ex. synth.*, tool:x)"
              aria-label="Resurse permise"
              value={resources}
              onChange={(e) => setResources(e.target.value)}
            />
            <input
              className="bo-input"
              placeholder="acțiuni (ex. increment, read)"
              aria-label="Acțiuni permise"
              value={actions}
              onChange={(e) => setActions(e.target.value)}
            />
            <input
              className="bo-input"
              style={{ width: 110 }}
              placeholder="buget"
              aria-label="Buget mandat"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
            />
            <input
              className="bo-input"
              style={{ width: 110 }}
              placeholder="pași max"
              aria-label="Pași maximi ai mandatului"
              value={maxSteps}
              onChange={(e) => setMaxSteps(e.target.value)}
            />
            <input
              className="bo-input"
              style={{ width: 130 }}
              placeholder="valabil (ore)"
              aria-label="Valabilitate în ore"
              value={ttlHours}
              onChange={(e) => setTtlHours(e.target.value)}
            />
            <button
              type="button"
              className="bo-btn bo-btn--primary"
              disabled={busy}
              onClick={() => void create()}
            >
              Creează
            </button>
          </div>
          <p className="bo-hint" style={{ marginTop: 6 }}>
            Delegarea nu amplifică drepturi: copilul primește intersecția
            cu părintele și cu politica curentă. guardian_ref leagă
            mandatul de autoritatea Guardian — stabil și explicit.
            {parentId ? " Valabilitatea copilului este plafonată la expirarea părintelui." : ""}
          </p>
        </div>
      ) : null}

      {mandates.length === 0 ? (
        <p className="bo-hint" style={{ marginTop: 8 }}>
          Niciun mandat — creează unul pentru a putea trimite execuții.
        </p>
      ) : (
        mandates.map((m) => (
          <div key={m.mandate_id} className="bo-row" style={{ marginTop: 8, flexWrap: "wrap" }}>
            <Pill kind={m.state === "active" ? "ok" : "danger"} icon="shield-check">
              {m.state} · d{m.depth}
            </Pill>
            <span className="bo-hint">
              {m.mandate_id} · acțiuni [{m.allowed_actions.join(", ")}] ·
              resurse [{m.allowed_resources.join(", ")}] · buget{" "}
              {m.budget_limit}
              {m.guardian_ref ? ` · Guardian: ${m.guardian_ref}` : ""}
              {m.parent_mandate_id ? " · delegat" : ""}
            </span>
            {m.state === "active" ? (
              <button
                type="button"
                className="bo-btn"
                disabled={busy}
                onClick={() => void revoke(m)}
              >
                Revocă
              </button>
            ) : null}
          </div>
        ))
      )}
      {notice ? (
        <div style={{ marginTop: 8 }}>
          <InlineAlert kind={notice.kind === "ok" ? "info" : notice.kind}>
            {notice.text}
          </InlineAlert>
        </div>
      ) : null}
    </div>
  );
}

function SubmitRunForm({
  mandates,
  onChanged,
}: {
  mandates: BoMandate[];
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "danger"; text: string } | null>(null);
  const [mandateId, setMandateId] = useState("");
  const [steps, setSteps] = useState(
    '[{"action":"increment","resource":"synth.counter","payload":{"amount":1}}]',
  );
  const [budget, setBudget] = useState("5");

  async function submit() {
    let parsed: unknown;
    try {
      parsed = JSON.parse(steps);
    } catch {
      setNotice({ kind: "danger", text: "Pașii trebuie să fie JSON valid." });
      return;
    }
    if (!Array.isArray(parsed) || parsed.length === 0) {
      setNotice({ kind: "danger", text: "Lista de pași e goală." });
      return;
    }
    if (!mandateId) {
      setNotice({ kind: "danger", text: "Alege un mandat." });
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      await submitBoRun({
        mandate_id: mandateId,
        steps: parsed as Record<string, unknown>[],
        budget_amount: budget,
      });
      setNotice({ kind: "ok", text: "Execuție trimisă — în așteptare pentru un ciclu de lucru." });
      setOpen(false);
      onChanged();
    } catch (err) {
      const text =
        err instanceof BoApiError
          ? `${err.status} — ${typeof err.detail === "string" ? err.detail : err.code}`
          : "Trimiterea a eșuat.";
      setNotice({ kind: "danger", text });
    } finally {
      setBusy(false);
    }
  }

  const active = mandates.filter((m) => m.state === "active");
  return (
    <div className="bo-card" style={{ marginTop: 12 }}>
      <div className="bo-spread">
        <h3 className="bo-card-title">Trimitere controlată</h3>
        <button type="button" className="bo-btn" onClick={() => setOpen((v) => !v)}>
          {open ? "Închide" : "Execuție nouă"}
        </button>
      </div>
      {open ? (
        <div style={{ marginTop: 12 }}>
          <div className="bo-row" style={{ flexWrap: "wrap", gap: 8 }}>
            <select
              className="bo-select"
              value={mandateId}
              onChange={(e) => setMandateId(e.target.value)}
              aria-label="Mandat"
            >
              <option value="">Alege mandatul activ…</option>
              {active.map((m) => (
                <option key={m.mandate_id} value={m.mandate_id}>
                  {m.mandate_id.slice(0, 18)}…
                  {m.guardian_ref ? " (Guardian)" : " (standalone)"}
                </option>
              ))}
            </select>
            <input
              className="bo-input"
              style={{ width: 110 }}
              placeholder="buget"
              aria-label="Buget execuție"
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
            />
            <button
              type="button"
              className="bo-btn bo-btn--primary"
              disabled={busy}
              onClick={() => void submit()}
            >
              Trimite
            </button>
          </div>
          <textarea
            className="bo-input"
            style={{ width: "100%", minHeight: 72, marginTop: 8, fontFamily: "monospace" }}
            value={steps}
            onChange={(e) => setSteps(e.target.value)}
            aria-label="Pași (JSON)"
          />
          <p className="bo-hint" style={{ marginTop: 6 }}>
            Trimiterea doar înregistrează intenția — execuția pornește la
            un ciclu de lucru, cu re-autorizare la fiecare efect.
          </p>
        </div>
      ) : null}
      {notice ? (
        <div style={{ marginTop: 8 }}>
          <InlineAlert kind={notice.kind === "ok" ? "info" : notice.kind}>
            {notice.text}
          </InlineAlert>
        </div>
      ) : null}
    </div>
  );
}

const OUTBOX_LABEL: Record<number, { label: string; kind: "ok" | "warn" | "danger" | "info" | "neutral" }> = {
  0: { label: "în așteptare", kind: "warn" },
  1: { label: "livrat", kind: "ok" },
  2: { label: "dead-letter", kind: "danger" },
};

function OutboxSection({
  entries,
  onChanged,
}: {
  entries: BoOutboxEntry[];
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ kind: "ok" | "danger"; text: string } | null>(null);

  async function retry(e: BoOutboxEntry) {
    const reason = window.prompt(
      `Reiei plicul ${e.event_id}? Octeții persistați se retrimit byte-identic — receptorul deduplică după eventId.\nMotiv (obligatoriu, auditat):`,
    );
    if (reason === null) return;
    if (!reason.trim()) {
      setNotice({ kind: "danger", text: "Reluarea cere un motiv — operație auditată." });
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      await retryBoOutbox(e.event_id, reason.trim());
      setNotice({ kind: "ok", text: "Plicul a fost repus în coadă." });
      onChanged();
    } catch (err) {
      setNotice({
        kind: "danger",
        text: err instanceof BoApiError
          ? `${err.status} — ${typeof err.detail === "string" ? err.detail : err.code}`
          : "Reluarea a eșuat.",
      });
    } finally {
      setBusy(false);
    }
  }

  async function rebind(e: BoOutboxEntry | null) {
    const reason = window.prompt(
      e
        ? `Reasociez plicul ${e.event_id} la destinația curent efectivă pentru kind-ul ${e.kind}? Singura cale autorizată de a muta backlog sau rânduri legacy; octeții plicului nu se rescriu.\nMotiv (obligatoriu, auditat):`
        : "Reasociez TOATE plicurile nelivrate la destinația curent efectivă pentru kind-ul lor? Singura cale autorizată de a muta backlog sau rânduri legacy; octeții plicurilor nu se rescriu.\nMotiv (obligatoriu, auditat):",
    );
    if (reason === null) return;
    if (!reason.trim()) {
      setNotice({ kind: "danger", text: "Reasocierea cere un motiv — operație auditată." });
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const out = await rebindBoOutbox(
        reason.trim(), e ? [e.event_id] : undefined,
      );
      setNotice({
        kind: "ok",
        text: out.skipped_leased
          ? `${out.rebound} plic(uri) reasociate; ${out.skipped_leased} sărit(e) — lease activ.`
          : `${out.rebound} plic(uri) reasociate destinației curente.`,
      });
      onChanged();
    } catch (err) {
      setNotice({
        kind: "danger",
        text: err instanceof BoApiError
          ? `${err.status} — ${typeof err.detail === "string" ? err.detail : err.code}`
          : "Reasocierea a eșuat.",
      });
    } finally {
      setBusy(false);
    }
  }

  const dead = entries.filter((e) => e.delivered === 2).length;
  const pending = entries.filter((e) => e.delivered === 0).length;
  return (
    <div className="bo-card" style={{ marginTop: 12 }}>
      <div className="bo-spread">
        <h3 className="bo-card-title">Coadă de livrare</h3>
        <div className="bo-row">
          <Pill kind="warn">{pending} în așteptare</Pill>
          <Pill kind={dead ? "danger" : "neutral"}>{dead} dead-letter</Pill>
          {pending + dead > 0 ? (
            <button
              type="button"
              className="bo-btn"
              disabled={busy}
              onClick={() => void rebind(null)}
            >
              Reasociază toate
            </button>
          ) : null}
        </div>
      </div>
      <p className="bo-hint" style={{ marginTop: 4 }}>
        Plicurile persistate înainte de prima trimitere; retry-urile
        retrimit aceiași octeți. Eșecurile permanente (401/403/409/422)
        ajung în dead-letter — vizibile, inspectabile, niciodată pierdute
        tăcut. Fiecare plic păstrează destinația legată la enqueue —
        rândurile legacy fără legătură refuză până la o reasociere
        explicită, auditată.
      </p>
      {entries.length === 0 ? (
        <p className="bo-hint" style={{ marginTop: 8 }}>Coada e goală.</p>
      ) : (
        entries.map((e) => {
          const l = OUTBOX_LABEL[e.delivered] ?? OUTBOX_LABEL[0];
          return (
            <div key={e.event_id} className="bo-row" style={{ marginTop: 8, flexWrap: "wrap" }}>
              <Pill kind={l.kind}>{l.label}</Pill>
              {e.delivered !== 1 && !e.dest_bound ? (
                <Pill kind="warn">fără destinație</Pill>
              ) : null}
              <span className="bo-hint">
                {e.event_id.slice(0, 18)}… · {e.kind}/{e.event_type ?? "?"} ·
                tentative {e.series_attempts} în seria curentă · {e.attempts} total
                {e.dest_endpoint ? ` → ${e.dest_endpoint.slice(0, 80)}` : ""}
                {e.dest_ref ? ` · ref ${e.dest_ref.slice(0, 40)}` : ""}
                {e.last_error ? ` · ${e.last_error.slice(0, 90)}` : ""}
              </span>
              <button
                type="button"
                className="bo-btn bo-btn--icon"
                onClick={() => setExpanded(expanded === e.event_id ? null : e.event_id)}
                aria-label="Plic persistat"
              >
                <IconBO name="file-json" size={14} />
              </button>
              {e.delivered === 2 ? (
                <button
                  type="button"
                  className="bo-btn"
                  disabled={busy}
                  onClick={() => void retry(e)}
                >
                  Reia autorizat
                </button>
              ) : null}
              {e.delivered !== 1 ? (
                <button
                  type="button"
                  className="bo-btn"
                  disabled={busy}
                  onClick={() => void rebind(e)}
                >
                  Reasociază destinația
                </button>
              ) : null}
              {expanded === e.event_id ? (
                <pre
                  className="bo-hint"
                  style={{
                    width: "100%", marginTop: 6, fontSize: 11,
                    whiteSpace: "pre-wrap", wordBreak: "break-all",
                    maxHeight: 160, overflow: "auto",
                  }}
                >
                  {JSON.stringify({ envelope: e.envelope, retry_history: e.retry_history }, null, 2)}
                </pre>
              ) : null}
            </div>
          );
        })
      )}
      {notice ? (
        <div style={{ marginTop: 8 }}>
          <InlineAlert kind={notice.kind === "ok" ? "info" : notice.kind}>
            {notice.text}
          </InlineAlert>
        </div>
      ) : null}
    </div>
  );
}

function RunDetail({
  detail,
  canOperate,
  onChanged,
}: {
  detail: BoRunDetail;
  canOperate: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ kind: "ok" | "warn" | "danger"; text: string } | null>(null);
  const [verify, setVerify] = useState<BoAuthorityCheck | null>(null);

  // A verdict is a point-in-time check, never authority for a changed run.
  useEffect(() => setVerify(null), [detail.run.updated_at]);

  async function act(fn: () => Promise<unknown>, okText: string) {
    setBusy(true);
    setNotice(null);
    try {
      await fn();
      setNotice({ kind: "ok", text: okText });
      onChanged();
    } catch (err) {
      const text =
        err instanceof BoApiError
          ? `${err.status} — ${typeof err.detail === "string" ? err.detail : err.code}`
          : "Acțiunea a eșuat.";
      setNotice({ kind: "danger", text });
    } finally {
      setBusy(false);
    }
  }

  async function checkAuthority() {
    setBusy(true);
    try {
      setVerify(await getBoRunAuthority(run.run_id));
    } catch {
      setNotice({ kind: "danger", text: "Verificarea autorității a eșuat." });
    } finally {
      setBusy(false);
    }
  }

  const run = detail.run;
  const ambiguous = detail.ledger.filter(
    (e) => e.status === "UNKNOWN" || e.status === "RECONCILIATION_REQUIRED",
  );
  const active = !["SUCCEEDED", "FAILED", "CANCELLED"].includes(run.state);
  const canResume = ["PAUSED", "UNKNOWN", "RECONCILIATION_REQUIRED"].includes(
    run.state,
  );

  function confirmCancel() {
    if (!window.confirm(
      "Anulezi rularea? Pașii viitori se opresc la următoarea frontieră — efectele externe deja executate NU se inversează.",
    )) {
      return;
    }
    const reason = window.prompt("Motivul anulării (obligatoriu, auditat):");
    if (reason === null) return;
    if (!reason.trim()) {
      setNotice({ kind: "danger", text: "Anularea cere un motiv." });
      return;
    }
    void act(() => cancelBoRun(run.run_id, reason.trim()), "Anulare cerută.");
  }

  function confirmMarkFailed() {
    if (!window.confirm(
      "Declari că efectul NU s-a produs extern? Operatorul își asumă starea externă — după aceasta intenția poate fi reîncercată.",
    )) {
      return;
    }
    void act(
      () => reconcileBoRun(run.run_id, "mark_failed"),
      "Marcat ca neexecutat — decizia operatorului e auditată.",
    );
  }

  function confirmPause() {
    const reason = window.prompt("Motivul pauzei (opțional, auditat):");
    if (reason === null) return;
    void act(
      () => pauseBoRun(run.run_id, reason.trim() || undefined),
      "Pauză cerută — se aplică la următorul pas.",
    );
  }

  return (
    <div className="bo-card" style={{ marginTop: 12 }}>
      <div className="bo-spread">
        <h3 className="bo-card-title">Rulare {run.run_id}</h3>
        {statePill(run.state)}
      </div>
      <p className="bo-hint" style={{ marginTop: 4 }}>
        Pas {run.current_step}/{run.steps.length} · politică v
        {run.policy_version} · corelare {run.correlation_id}
        {run.lease_owner ? ` · lease ${run.lease_owner}` : ""}
      </p>
      {run.block_reason ? (
        <div style={{ marginTop: 8 }}>
          <InlineAlert kind="warn">Blocată: {blockText(run.block_reason)}</InlineAlert>
        </div>
      ) : null}

      {/* Lanțul de delegare */}
      <h4 className="bo-card-title" style={{ marginTop: 16 }}>Lanț de delegare</h4>
      {detail.chain.map((m) => (
        <div key={m.mandate_id} className="bo-row" style={{ marginTop: 6 }}>
          <Pill kind={m.state === "active" ? "ok" : "danger"} icon="shield-check">
            adâncime {m.depth} · {m.state}
          </Pill>
          <span className="bo-hint">
            {m.mandate_id} · acțiuni [{m.allowed_actions.join(", ")}] ·
            resurse [{m.allowed_resources.join(", ")}] · buget{" "}
            {m.budget_limit} · expiră {m.expires_at.slice(0, 19)}Z
            {m.guardian_ref ? ` · Guardian: ${m.guardian_ref}` : ""}
          </span>
        </div>
      ))}
      {detail.mandate.revoked_reason ? (
        <p className="bo-hint" style={{ marginTop: 4 }}>
          Revocat: {detail.mandate.revoked_reason}
        </p>
      ) : null}

      {/* Rezervare */}
      {detail.reservation ? (
        <p className="bo-hint" style={{ marginTop: 12 }}>
          Rezervare: {detail.reservation.amount} ·{" "}
          {detail.reservation.slots} sloturi ·{" "}
          {detail.reservation.state}
        </p>
      ) : null}

      {/* Autoritatea efectivă */}
      <GuardianBlock
        guardian={detail.guardian}
        onVerify={() => void checkAuthority()}
        verify={verify}
        busy={busy}
      />

      {/* Ledger */}
      <h4 className="bo-card-title" style={{ marginTop: 16 }}>Ledger de efecte</h4>
      {detail.ledger.length === 0 ? (
        <p className="bo-hint">Nicio intrare — niciun efect nu a fost încercat.</p>
      ) : (
        detail.ledger.map((e) => {
          const l = LEDGER_LABEL[e.status] ?? {
            label: e.status,
            kind: "neutral" as const,
          };
          return (
            <div key={e.entry_id} className="bo-row" style={{ marginTop: 6 }}>
              <Pill kind={l.kind}>{l.label}</Pill>
              <span className="bo-hint">
                pas {e.step} · {e.provider} · cheie {e.idempotency_key} ·
                digest {e.payload_digest.slice(0, 12)}… · încercări{" "}
                {e.attempts}
                {e.receipt_ref ? ` · chitanță ${e.receipt_ref}` : " · fără chitanță"}
              </span>
            </div>
          );
        })
      )}
      {ambiguous.length > 0 ? (
        <div style={{ marginTop: 8 }}>
          <InlineAlert kind="warn" icon="shield-alert">
            {ambiguous.length} efect(e) cu stare externă necunoscută — un
            timeout după trimitere NU dovedește eșecul. Reconcilierea
            verifică chitanța providerului înainte de orice reexecutare;
            exactly-once nu e garantat pentru provideri fără idempotență.
          </InlineAlert>
        </div>
      ) : null}

      {/* Checkpointuri */}
      <h4 className="bo-card-title" style={{ marginTop: 16 }}>Checkpointuri</h4>
      {detail.checkpoints.length === 0 ? (
        <p className="bo-hint">Niciun checkpoint — rularea nu a pornit.</p>
      ) : (
        detail.checkpoints.map((c) => (
          <div key={`${c.step}-${c.checkpoint_version}`} className="bo-row" style={{ marginTop: 4 }}>
            <Pill kind="neutral" icon="history">
              pas {c.step} · v{c.checkpoint_version}
            </Pill>
            <span className="bo-hint">
              {String(c.state.phase)} · {c.created_at.slice(11, 19)}Z ·
              stare de execuție (nu dovadă de efect)
            </span>
          </div>
        ))
      )}

      {/* Controale — consecvențialele cer confirmare + motiv */}
      {canOperate && active ? (
        <div className="bo-row" style={{ marginTop: 16, flexWrap: "wrap" }}>
          {!run.pause_requested && (
            <button
              type="button"
              className="bo-btn"
              disabled={busy}
              onClick={confirmPause}
            >
              Pauzează
            </button>
          )}
          {canResume && (
            <button
              type="button"
              className="bo-btn bo-btn--primary"
              disabled={busy}
              onClick={() =>
                act(() => resumeBoRun(run.run_id), "Reluare programată — aceeași identitate și chei.")
              }
            >
              Reia
            </button>
          )}
          {ambiguous.length > 0 && (
            <>
              <button
                type="button"
                className="bo-btn"
                disabled={busy}
                onClick={() =>
                  act(
                    () => reconcileBoRun(run.run_id, "receipt"),
                    "Reconciliere prin chitanțe efectuată.",
                  )
                }
              >
                Reconciliază (chitanțe)
              </button>
              <button
                type="button"
                className="bo-btn"
                disabled={busy}
                onClick={confirmMarkFailed}
              >
                Marchează neexecutat
              </button>
            </>
          )}
          {!run.cancel_requested && (
            <button
              type="button"
              className="bo-btn"
              disabled={busy}
              onClick={confirmCancel}
            >
              Anulează
            </button>
          )}
        </div>
      ) : null}
      <p className="bo-hint" style={{ marginTop: 10 }}>
        Anularea oprește pașii viitori la următoarea frontieră — NU
        inversează un efect extern deja executat.
      </p>
      {notice ? (
        <div style={{ marginTop: 8 }}>
          <InlineAlert kind={notice.kind === "ok" ? "info" : notice.kind}>
            {notice.text}
          </InlineAlert>
        </div>
      ) : null}
    </div>
  );
}

export default function BoExecutionsPage() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [selected, setSelected] = useState<string | null>(null);
  const [filter, setFilter] = useState<string>("");
  const [workNotice, setWorkNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const [status, runsRes, mandatesRes, outboxRes] = await Promise.all([
        getBoExecStatus(),
        listBoExecRuns(filter || undefined),
        getBoMandates(),
        listBoOutbox(),
      ]);
      let detail: BoRunDetail | null = null;
      if (selected) {
        try {
          detail = await getBoRunDetail(selected);
        } catch {
          detail = null;
        }
      }
      setState({
        kind: "data",
        status,
        runs: runsRes.runs,
        mandates: mandatesRes.mandates,
        outbox: outboxRes.entries,
        detail,
      });
    } catch (err) {
      if (err instanceof BoApiError && err.status === 403) {
        setState({ kind: "forbidden" });
      } else {
        setState({
          kind: "error",
          message: "Nu am putut încărca execuțiile. Verifică backend-ul.",
        });
      }
    }
  }, [filter, selected]);

  useEffect(() => {
    void load();
  }, [load]);

  async function runWorkCycle() {
    setBusy(true);
    setWorkNotice(null);
    try {
      const out = await workBoRuns();
      const states = out.outcomes
        .map((o) => `${o.run_id.slice(0, 12)}… → ${o.state}`)
        .join(", ");
      setWorkNotice(
        `Ciclu de lucru: ${out.claimed} revendicate${states ? ` — ${states}` : ""}`,
      );
      void load();
    } catch (err) {
      setWorkNotice(
        err instanceof BoApiError
          ? `Ciclul de lucru a eșuat: ${err.status}`
          : "Ciclul de lucru a eșuat.",
      );
    } finally {
      setBusy(false);
    }
  }

  async function open(runId: string) {
    setSelected(runId === selected ? null : runId);
  }

  if (state.kind === "loading") {
    return <StateBlock state="loading" title="Se încarcă execuțiile" />;
  }
  if (state.kind === "forbidden") {
    return (
      <StateBlock
        state="forbidden"
        title="Acces interzis"
        detail="Contul tău nu are drept de citire pe execuțiile BOAgents."
      />
    );
  }
  if (state.kind === "error") {
    return (
      <StateBlock
        state="error"
        title="Eroare la încărcare"
        detail={state.message}
        action={
          <button type="button" className="bo-btn" onClick={load}>
            <IconBO name="refresh" size={15} /> Reîncearcă
          </button>
        }
      />
    );
  }

  const { status, runs, mandates, outbox, detail } = state;
  const canOperate = true; // server enforcește RBAC; controalele cer 403 explicit

  return (
    <div className="bo-scope" style={{ marginTop: 20 }}>
      <div className="bo-row" style={{ marginBottom: 16, flexWrap: "wrap" }}>
        <Pill kind={status.enabled ? "ok" : "warn"} icon="shield-check">
          execuție {status.enabled ? "pornită" : "oprită"}
        </Pill>
        <Pill kind="neutral" icon="list">
          {status.runs_total} rulări
        </Pill>
        <Pill kind="neutral" icon="activity">
          efecte sintetice: {status.synthetic_effect_total}
        </Pill>
        <Pill
          kind={status.guardian.endpoint_configured ? "info" : "neutral"}
          icon="shield-check"
        >
          Guardian {status.guardian.endpoint_configured ? "configurat" : "neconfigurat"}
        </Pill>
        <button
          type="button"
          className="bo-btn"
          disabled={busy}
          onClick={() => void runWorkCycle()}
        >
          Rulează un ciclu de lucru
        </button>
      </div>
      {workNotice ? (
        <p className="bo-hint" style={{ marginBottom: 8 }}>{workNotice}</p>
      ) : null}

      <InlineAlert kind="info" icon="info">
        Aceasta este o suprafață de <strong>execuție</strong>: mandatele
        autorizează, checkpointurile salvează stare de execuție, iar
        ledgerul dovedește efectele. Este diferită de observațiile
        routerului (care nu produc efecte). Efectele sunt sintetice și
        locale în această etapă.
      </InlineAlert>
      <p className="bo-hint" style={{ marginTop: 8 }}>{status.note}</p>

      <MandatesSection mandates={mandates} onChanged={() => void load()} />
      <SubmitRunForm mandates={mandates} onChanged={() => void load()} />
      <OutboxSection entries={outbox} onChanged={() => void load()} />

      {/* Filtru stare */}
      <div className="bo-row" style={{ marginTop: 16 }}>
        <select
          className="bo-select"
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          aria-label="Filtru stare"
        >
          <option value="">Toate stările</option>
          {Object.keys(STATE_LABEL).map((s) => (
            <option key={s} value={s}>
              {STATE_LABEL[s].label}
            </option>
          ))}
        </select>
        <button type="button" className="bo-btn" onClick={load}>
          <IconBO name="refresh" size={15} /> Reîmprospătează
        </button>
      </div>

      {/* Lista rulărilor */}
      {runs.length === 0 ? (
        <StateBlock
          state="empty"
          title="Nicio rulare"
          detail="Execuțiile delegate apar aici după trimitere."
        />
      ) : (
        <div style={{ marginTop: 12 }}>
          {runs.map((r) => (
            <button
              key={r.run_id}
              type="button"
              className="bo-card"
              style={{
                width: "100%",
                textAlign: "left",
                marginTop: 8,
                cursor: "pointer",
              }}
              onClick={() => void open(r.run_id)}
            >
              <div className="bo-spread">
                <div>
                  <strong>{r.run_id}</strong>
                  <span className="bo-hint" style={{ marginLeft: 8 }}>
                    mandat {r.mandate_id.slice(0, 14)}… · pas{" "}
                    {r.current_step}/{r.steps.length}
                    {r.parent_run_id ? " · copil" : ""}
                  </span>
                </div>
                {statePill(r.state)}
              </div>
              {r.block_reason ? (
                <p className="bo-hint" style={{ marginTop: 4 }}>
                  {blockText(r.block_reason)}
                </p>
              ) : null}
            </button>
          ))}
        </div>
      )}

      {detail ? (
        <RunDetail
          key={detail.run.run_id}
          detail={detail}
          canOperate={canOperate}
          onChanged={() => void load()}
        />
      ) : null}
    </div>
  );
}
