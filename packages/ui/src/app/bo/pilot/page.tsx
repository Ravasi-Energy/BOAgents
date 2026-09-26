"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { BoPage, Field, InlineAlert, Pill, StateBlock } from "@/components/bo/ui";
import { BoApiError, getBoMandates, listBoPackageImports, req, workBoRuns,
  reconcileBoRun, replayBoRunTelemetry, resumeBoRun,
  type BoMandate, type BoPackageImport, type BoRunTelemetry } from "@/lib/bo";

type PilotRun = { run_id: string; state: string; health: string; correlation_id: string;
  block_reason?: string; observation: null | { observedAt: string; service: {
    version: string; queuePending: number; oldestPendingAt: string | null; serviceRef: string } };
  telemetry?: BoRunTelemetry;
  ledger: { receipt_ref: string | null; payload_digest: string; idempotency_key: string; status: string }[] };

function telemetryLabel(t: BoRunTelemetry): string {
  switch (t.status) {
    case "ok": return "Telemetrie livrată";
    case "pending": return "Telemetrie în coadă de livrare";
    case "degraded": return "Telemetrie restantă — plicuri lipsă din outbox";
    case "dead": return "Telemetrie în dead-letter — reluare din registrul outbox";
    case "incident": return "Incident telemetrie istoric — coada refăcută";
    case "unavailable": return "Observație indisponibilă — nereconstruibilă";
    default: return "Fără plicuri de telemetrie";
  }
}
type Pilot = { role: string; config: { enabled: boolean; profile: string; endpoint: string; supervision: string };
  activation: null | { import_id: string; version: number; active: boolean }; runs: PilotRun[] };

export default function PilotPage() {
  const [data, setData] = useState<Pilot | null>(null);
  const [packages, setPackages] = useState<BoPackageImport[]>([]);
  const [mandates, setMandates] = useState<BoMandate[]>([]);
  const [importId, setImportId] = useState("");
  const [mandateId, setMandateId] = useState("");
  const [reason, setReason] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const [pilot, pkgs, grants] = await Promise.all([req<Pilot>("/pilot"), listBoPackageImports(), getBoMandates()]);
      setData(pilot); setPackages(pkgs.imports); setMandates(grants.mandates);
    } catch (err) {
      setError(err instanceof BoApiError && err.status === 403 ? "Acces refuzat pentru identitatea curentă." : "Pilotul nu poate fi încărcat. Starea serviciului este necunoscută.");
    } finally { setLoading(false); }
  }, []);
  useEffect(() => { void load(); }, [load]);
  const admin = data?.role === "admin";
  const operator = admin || data?.role === "operator";
  async function action(fn: () => Promise<unknown>, message: string) {
    setBusy(true); setNotice("");
    try { await fn(); await load(); setNotice(message); }
    catch (err) {
      setNotice(err instanceof BoApiError ? (err.status === 409 ? "Conflict CAS/stare: reîncarcă datele înainte de reluare. " : "Acțiune refuzată: ") +
        (typeof err.detail === "string" ? err.detail : err.code) : "Cererea nu a fost confirmată; verifică dovezile înainte de repetare.");
    } finally { setBusy(false); }
  }
  return <BoPage title="Pilot ERP sintetic" sub="BoBot determinist, fără LLM. Diagnostic local, fără conexiune la ERP real."
    actions={<button className="bo-btn" disabled={busy} onClick={() => void load()}>Reîncarcă</button>}>
    <div className="space-y-4">
    <div className="bo-row"><Link className="bo-btn" href="/bo/packages">Import și verificare pachet</Link>
      <Link className="bo-btn" href="/settings/bo">Configurare pilot</Link>
      <Link className="bo-btn" href="/bo/executions">Mandate, autoritate și recuperare</Link></div>
    {notice && <InlineAlert>{notice}</InlineAlert>}
    {loading ? <StateBlock state="loading" title="Se încarcă pilotul" /> : error ?
      <StateBlock state="error" title="Pilot indisponibil" detail={error} /> : data && <>
      <InlineAlert kind="warn">Importul și DRAFT nu activează execuția. Observabilitatea nu acordă autoritate.
        Modul {data.config.supervision === "required" ? "Guardian obligatoriu" : "standalone"} păstrează verificarea tuturor mandatelor legate.</InlineAlert>
      <section className="bo-card">
        <h2 className="bo-card-title">Aprobare și activare</h2>
        <p><Pill kind={data.activation?.active ? "ok" : "neutral"}>{data.activation?.active ? "Activat" : "Inactiv"}</Pill> ·
          versiune CAS {data.activation?.version ?? 0} · {data.config.enabled ? "Pilot pornit" : "Pilot oprit"}</p>
        <p>Destinație: {data.config.endpoint || "Neconfigurat"} · Profil: {data.config.profile}</p>
        {!admin && <InlineAlert>Activarea și configurarea cer administrator. Identitate curentă: {data.role}.</InlineAlert>}
        <Field label="Pachet sintetic verificat" htmlFor="pilot-package"><select id="pilot-package" className="bo-select" value={importId}
          disabled={!admin || busy} onChange={e => setImportId(e.target.value)}><option value="">Selectează DRAFT</option>
          {packages.filter(p => p.package_id === "pkg.synthetic-erp" && p.status === "DRAFT").map(p => <option key={p.id} value={p.id}>{p.package_id}@{p.version} · {p.id}</option>)}
        </select></Field>
        <Field label="Motiv aprobare/dezactivare" htmlFor="pilot-reason"><input className="bo-input" id="pilot-reason" value={reason}
          disabled={!admin || busy} maxLength={300} onChange={e => setReason(e.target.value)} /></Field>
        <p>Aprobarea permite numai diagnosticul semnat al serviciului sintetic configurat. Fiecare rulare consumă un pas și buget 1 din mandat.</p>
        <div className="bo-row"><button className="bo-btn bo-btn--primary" disabled={!admin || busy || !importId || reason.trim().length < 3}
          onClick={() => void action(() => req("/pilot/activation", { method: "PUT", headers: {"Content-Type":"application/json"}, body: JSON.stringify({import_id: importId, active: true, expected_version: data.activation?.version ?? 0, reason}) }), "Pachet aprobat și activat; nicio rulare nu a pornit automat.")}>Aprobă și activează</button>
          <button className="bo-btn" disabled={!admin || busy || !data.activation?.active || reason.trim().length < 3}
          onClick={() => void action(() => req("/pilot/activation", { method: "PUT", headers: {"Content-Type":"application/json"}, body: JSON.stringify({import_id: data.activation?.import_id, active: false, expected_version: data.activation?.version, reason}) }), "Dezactivat. Istoricul, bugetele și dovezile sunt păstrate.")}>Dezactivează, păstrează istoricul</button></div>
      </section>
      <section className="bo-card">
        <h2 className="bo-card-title">Diagnostic limitat</h2>
        <Field label="Mandat diagnostic" htmlFor="pilot-mandate"><select id="pilot-mandate" className="bo-select" value={mandateId}
          disabled={!admin || busy} onChange={e => setMandateId(e.target.value)}><option value="">Selectează mandat</option>
          {mandates.filter(m => m.allowed_actions.includes("diagnose")).map(m => <option key={m.mandate_id} value={m.mandate_id}>{m.mandate_id}</option>)}
        </select></Field>
        <p>Acțiune diagnose pe synth.erp, o singură probă HTTP. Backendul reverifică autoritatea la frontieră. Fără notificări sau remediere automată.</p>
        <div className="bo-row"><button className="bo-btn bo-btn--primary" disabled={!admin || busy || !mandateId || !data.activation?.active || !data.config.enabled}
          onClick={() => void action(() => req("/pilot/runs", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({mandate_id:mandateId})}), "Plan trimis PENDING; efectul cere un ciclu local autorizat.")}>Trimite diagnostic (1 pas)</button>
          <button className="bo-btn" disabled={!operator || busy} onClick={() => void action(() => workBoRuns(), "Ciclu local încheiat. Verifică starea și receiptul fiecărei rulări.")}>Procesează ciclul local</button></div>
      </section>
      <section className="bo-card"><h2 className="bo-card-title">Dovezi și stare</h2>
        {!data.runs.length && <StateBlock state="empty" title="Nemăsurat" detail="Nicio probă efectuată. Lipsa datelor nu înseamnă sănătos." />}
        {data.runs.map(run => <article className="bo-card" key={run.run_id}>
          <h3 className="bo-card-title">{run.run_id}</h3>
          <p><Pill kind={run.health === "HEALTHY" ? "ok" : "warn"}>{run.health}</Pill> · Execuție {run.state}</p>
          {run.telemetry && run.telemetry.status !== "none" && <p><Pill kind={
            run.telemetry.status === "ok" ? "ok" : run.telemetry.status === "pending" ? "info" :
            run.telemetry.status === "incident" ? "neutral" : "warn"}>{telemetryLabel(run.telemetry)}</Pill>
            {run.telemetry.expected > 0 && <span className="bo-mono"> · {run.telemetry.queued}/{run.telemetry.expected} în coadă
              {run.telemetry.delivered > 0 ? `, ${run.telemetry.delivered} livrate` : ""}{run.telemetry.dead > 0 ? `, ${run.telemetry.dead} dead-letter` : ""}</span>}</p>}
          {run.telemetry?.marker && <p className="bo-muted">Efectul rămâne confirmat de receipt ({run.telemetry.marker}
            {run.telemetry.error === "invalid" ? " — plic respins la validare" : run.telemetry.error === "persistence" ? " — persistare eșuată" : ""});
            degradarea privește numai livrarea observației, nu rezultatul execuției.</p>}
          {run.block_reason && run.state !== "SUCCEEDED" && <InlineAlert kind="warn">{run.block_reason}</InlineAlert>}
          <p>Versiune: {run.observation?.service.version ?? "UNKNOWN"} · Coadă: {run.observation?.service.queuePending ?? "UNKNOWN"}</p>
          <p>Observat: {run.observation?.observedAt ?? "Nemăsurat"} · Cel mai vechi în coadă: {run.observation?.service.oldestPendingAt ?? "Nicio dovadă de întârziere"}</p>
          <p className="bo-mono">Corelație: {run.correlation_id}</p>
          {run.ledger.map(entry => <details key={entry.idempotency_key}><summary>Receipt: {entry.receipt_ref ?? "UNKNOWN — dovadă absentă"}</summary>
            <p className="bo-mono">Cheie: {entry.idempotency_key}</p><p className="bo-mono">Digest: {entry.payload_digest}</p><p>Ledger: {entry.status}</p></details>)}
          {["UNKNOWN", "RECONCILIATION_REQUIRED"].includes(run.state) && <button className="bo-btn" disabled={!admin || busy}
            onClick={() => void action(() => reconcileBoRun(run.run_id, "receipt"), "Readback terminat. Reia numai după dovadă corelată; nu se retrimite efectul.")}>Caută receipt (fără reexecutare)</button>}
          {["UNKNOWN", "RECONCILIATION_REQUIRED", "PAUSED"].includes(run.state) && <button className="bo-btn" disabled={!admin || busy}
            onClick={() => void action(() => resumeBoRun(run.run_id), "Reluare cerută; autoritatea și rezervările se reverifică.")}>Reia explicit</button>}
          {run.telemetry?.replayable && <button className="bo-btn" disabled={!admin || busy}
            onClick={() => void action(() => replayBoRunTelemetry(run.run_id), "Telemetrie reemisă din dovada păstrată; efectul nu a fost repetat.")}>Reemite telemetria (fără efect nou)</button>}
        </article>)}
      </section>
    </>}
    </div>
  </BoPage>;
}
