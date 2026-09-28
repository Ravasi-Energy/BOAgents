"use client";

// Modele și rutare (VAL3-01): catalog administrabil + observații produse de
// routerul în mod observare. Pagina afișează explicit «observare» — decizia
// calculată nu schimbă niciodată modelul folosit, iar estimarea de cost nu
// este prezentată ca economie realizată.
import { Fragment, useCallback, useEffect, useState } from "react";

import {
  BoPage,
  Field,
  InlineAlert,
  Pill,
  StateBlock,
} from "@/components/bo/ui";
import {
  BoApiError,
  createBoCatalogEntry,
  flushBoRoutingObservations,
  getBoRoutingCatalog,
  getBoRoutingStatus,
  listBoRouteObservations,
  updateBoCatalogEntry,
  type BoCatalogEntry,
  type BoRouteObservation,
  type BoRoutingStatus,
} from "@/lib/bo";

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "forbidden" }
  | {
      kind: "data";
      status: BoRoutingStatus;
      entries: BoCatalogEntry[];
      observations: BoRouteObservation[];
      canWrite: boolean;
    };

const REASON_LABELS: Record<string, string> = {
  PROVIDER_DENIED: "provider nepermis",
  REGION_DENIED: "regiune respinsă",
  CAPABILITY_MISSING: "capabilități lipsă",
  BUDGET_EXCEEDED: "peste buget",
  COST_DATA_MISSING: "date de cost lipsă",
  MODEL_DISABLED: "model indisponibil",
  EVAL_MISSING: "neevaluat",
  EVAL_TASK_MISMATCH: "evaluare pentru alt task",
  STALE_EVALUATION: "evaluare expirată",
  QUALITY_BAR_UNMET: "sub pragul de calitate",
  CATALOG_EMPTY: "catalog gol",
};

function reasonLabel(r: string): string {
  return REASON_LABELS[r] ?? r;
}

function choiceLabel(
  c: { provider: string; modelId: string; modelVersion: string | null } | null,
): string {
  if (!c) return "—";
  return `${c.provider}/${c.modelId}${c.modelVersion ? ` v${c.modelVersion}` : ""}`;
}

// ------------------------------------------------------------------------- //
// Editor catalog (admin) — formular controlat, CAS pe update
// ------------------------------------------------------------------------- //

function EntryEditor({
  entry,
  onSaved,
  onCancel,
}: {
  entry: BoCatalogEntry | null;
  onSaved: () => void;
  onCancel: () => void;
}) {
  const [provider, setProvider] = useState(entry?.provider ?? "");
  const [modelId, setModelId] = useState(entry?.model_id ?? "");
  const [modelVersion, setModelVersion] = useState(entry?.model_version ?? "");
  const [state, setState] = useState(entry?.state ?? "ACTIVE");
  const [capabilities, setCapabilities] = useState(
    (entry?.capabilities ?? []).join(","),
  );
  const [regions, setRegions] = useState((entry?.regions ?? []).join(","));
  const [purpose, setPurpose] = useState(entry?.purpose ?? "");
  const [source, setSource] = useState(entry?.source ?? "admin");
  const [costIn, setCostIn] = useState(entry?.cost.input_per_million ?? "");
  const [costOut, setCostOut] = useState(entry?.cost.output_per_million ?? "");
  const [currency, setCurrency] = useState(entry?.cost.currency ?? "");
  const [validUntil, setValidUntil] = useState(entry?.cost.valid_until ?? "");
  const [score, setScore] = useState(
    entry?.quality?.score != null ? String(entry.quality.score) : "",
  );
  const [taskKind, setTaskKind] = useState(entry?.quality?.task_kind ?? "");
  const [evalRef, setEvalRef] = useState(entry?.quality?.eval_set_ref ?? "");
  const [evalVersion, setEvalVersion] = useState(
    entry?.quality?.eval_set_version ?? "",
  );
  const [observedAt, setObservedAt] = useState(entry?.quality?.observed_at ?? "");
  const [sampleCount, setSampleCount] = useState(
    entry?.quality ? String(entry.quality.sample_count) : "0",
  );
  const [methodology, setMethodology] = useState(
    entry?.quality?.methodology ?? "",
  );
  const [err, setErr] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);

  const csv = (v: string) =>
    v
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);

  async function submit() {
    setErr(null);
    setConflict(false);
    setBusy(true);
    try {
      const quality =
        score.trim() || taskKind.trim()
          ? {
              score: score.trim() ? Number(score) : null,
              methodology: methodology.trim() || "manual",
              task_kind: taskKind.trim() || "generic",
              eval_set_ref: evalRef.trim() || "manual",
              eval_set_version: evalVersion.trim() || "v1",
              observed_at:
                observedAt.trim() ||
                new Date().toISOString().replace(/\.\d+Z$/, "Z"),
              sample_count: Number(sampleCount) || 0,
            }
          : null;
      const payload = {
        provider: provider.trim(),
        model_id: modelId.trim(),
        model_version: modelVersion.trim() || null,
        state,
        capabilities: csv(capabilities),
        regions: csv(regions),
        cost: {
          input_per_million: costIn.trim() || null,
          output_per_million: costOut.trim() || null,
          currency: currency.trim() || null,
          valid_until: validUntil.trim() || null,
        },
        quality,
        purpose: purpose.trim() || "general",
        source: source.trim() || "admin",
      };
      if (entry) {
        await updateBoCatalogEntry(entry.entry_id, {
          ...payload,
          expected_version: entry.version,
        });
      } else {
        await createBoCatalogEntry(payload);
      }
      onSaved();
    } catch (e) {
      if (e instanceof BoApiError && e.status === 409) {
        setConflict(true);
        setErr(
          "Altă sesiune a modificat intrarea între timp — reîncarcă și reia editarea.",
        );
      } else if (e instanceof BoApiError) {
        setErr(`${e.code}${e.detail ? ` — ${String(e.detail)}` : ""}`);
      } else {
        setErr("Eroare neașteptată la salvare.");
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="bo-card">
      <h3 className="bo-card-title">
        {entry ? `Editează ${entry.provider}/${entry.model_id}` : "Intrare nouă în catalog"}
      </h3>
      {err ? (
        <InlineAlert kind={conflict ? "warn" : "danger"}>{err}</InlineAlert>
      ) : null}
      <div className="bo-grid bo-grid-3" style={{ marginTop: 12 }}>
        <Field label="Provider" htmlFor="cat-provider">
          <input
            id="cat-provider"
            className="bo-input"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
          />
        </Field>
        <Field label="Model" htmlFor="cat-model">
          <input
            id="cat-model"
            className="bo-input"
            value={modelId}
            onChange={(e) => setModelId(e.target.value)}
          />
        </Field>
        <Field label="Versiune (gol = necunoscută)" htmlFor="cat-ver">
          <input
            id="cat-ver"
            className="bo-input"
            value={modelVersion}
            onChange={(e) => setModelVersion(e.target.value)}
          />
        </Field>
        <Field label="Stare" htmlFor="cat-state">
          <select
            id="cat-state"
            className="bo-select"
            value={state}
            onChange={(e) => setState(e.target.value as BoCatalogEntry["state"])}
          >
            <option value="ACTIVE">ACTIVE</option>
            <option value="DISABLED">DISABLED</option>
            <option value="DEPRECATED">DEPRECATED</option>
          </select>
        </Field>
        <Field label="Capabilități (CSV)" htmlFor="cat-caps">
          <input
            id="cat-caps"
            className="bo-input"
            value={capabilities}
            onChange={(e) => setCapabilities(e.target.value)}
            placeholder="analysis,json"
          />
        </Field>
        <Field label="Regiuni (CSV, gol = orice)" htmlFor="cat-regions">
          <input
            id="cat-regions"
            className="bo-input"
            value={regions}
            onChange={(e) => setRegions(e.target.value)}
            placeholder="eu,us"
          />
        </Field>
        <Field label="Scop" htmlFor="cat-purpose">
          <input
            id="cat-purpose"
            className="bo-input"
            value={purpose}
            onChange={(e) => setPurpose(e.target.value)}
          />
        </Field>
        <Field label="Sursă" htmlFor="cat-source">
          <input
            id="cat-source"
            className="bo-input"
            value={source}
            onChange={(e) => setSource(e.target.value)}
          />
        </Field>
      </div>
      <h4 style={{ margin: "16px 0 8px" }}>Cost (pe 1M tokeni, opțional)</h4>
      <div className="bo-grid bo-grid-3">
        <Field label="Intrare" htmlFor="cat-cin">
          <input
            id="cat-cin"
            className="bo-input"
            value={costIn}
            onChange={(e) => setCostIn(e.target.value)}
            placeholder="3.00"
          />
        </Field>
        <Field label="Ieșire" htmlFor="cat-cout">
          <input
            id="cat-cout"
            className="bo-input"
            value={costOut}
            onChange={(e) => setCostOut(e.target.value)}
            placeholder="15.00"
          />
        </Field>
        <Field label="Monedă" htmlFor="cat-ccy">
          <input
            id="cat-ccy"
            className="bo-input"
            value={currency}
            onChange={(e) => setCurrency(e.target.value)}
            placeholder="USD"
            maxLength={3}
          />
        </Field>
        <Field label="Valabil până la" htmlFor="cat-cvu">
          <input
            id="cat-cvu"
            className="bo-input"
            value={validUntil}
            onChange={(e) => setValidUntil(e.target.value)}
            placeholder="2027-12-31"
          />
        </Field>
      </div>
      <h4 style={{ margin: "16px 0 8px" }}>Evaluare (opțional)</h4>
      <div className="bo-grid bo-grid-3">
        <Field label="Scor 0–1 (gol = neevaluat)" htmlFor="cat-score">
          <input
            id="cat-score"
            className="bo-input"
            value={score}
            onChange={(e) => setScore(e.target.value)}
            placeholder="0.85"
          />
        </Field>
        <Field label="Task evaluat" htmlFor="cat-tk">
          <input
            id="cat-tk"
            className="bo-input"
            value={taskKind}
            onChange={(e) => setTaskKind(e.target.value)}
            placeholder="specialist"
          />
        </Field>
        <Field label="Metodologie" htmlFor="cat-meth">
          <input
            id="cat-meth"
            className="bo-input"
            value={methodology}
            onChange={(e) => setMethodology(e.target.value)}
          />
        </Field>
        <Field label="Set de evaluare" htmlFor="cat-esr">
          <input
            id="cat-esr"
            className="bo-input"
            value={evalRef}
            onChange={(e) => setEvalRef(e.target.value)}
          />
        </Field>
        <Field label="Versiune set" htmlFor="cat-esv">
          <input
            id="cat-esv"
            className="bo-input"
            value={evalVersion}
            onChange={(e) => setEvalVersion(e.target.value)}
          />
        </Field>
        <Field label="Observată la (UTC)" htmlFor="cat-obs">
          <input
            id="cat-obs"
            className="bo-input"
            value={observedAt}
            onChange={(e) => setObservedAt(e.target.value)}
            placeholder="2026-09-24T00:00:00Z"
          />
        </Field>
        <Field label="Exemple" htmlFor="cat-samples">
          <input
            id="cat-samples"
            className="bo-input"
            value={sampleCount}
            onChange={(e) => setSampleCount(e.target.value)}
          />
        </Field>
      </div>
      <div className="bo-row" style={{ marginTop: 16 }}>
        <button
          className="bo-btn bo-btn--primary"
          onClick={submit}
          disabled={busy}
        >
          {busy ? "Se salvează…" : entry ? "Salvează" : "Adaugă"}
        </button>
        <button className="bo-btn" onClick={onCancel} disabled={busy}>
          Renunță
        </button>
      </div>
    </div>
  );
}

// ------------------------------------------------------------------------- //
// Observație expandată — recomandat vs folosit + motivele refuzului
// ------------------------------------------------------------------------- //

function ObservationDetail({ obs }: { obs: BoRouteObservation }) {
  return (
    <div style={{ padding: "8px 4px" }}>
      <div className="bo-grid bo-grid-3">
        <div>
          <strong>Recomandat</strong>
          <div>{choiceLabel(obs.recommendation)}</div>
        </div>
        <div>
          <strong>Folosit efectiv</strong>
          <div>{choiceLabel(obs.actual_route)}</div>
        </div>
        <div>
          <strong>Estimare cost</strong>
          <div>
            {obs.cost_estimate
              ? `${obs.cost_estimate.amount} ${obs.cost_estimate.currency} (estimare, nu economie realizată)`
              : "necunoscut"}
          </div>
        </div>
        <div>
          <strong>Cost facturat</strong>
          <div>
            {obs.billed
              ? `${String(obs.billed.amount)} ${String(obs.billed.currency)}`
              : "nedisponibil"}
          </div>
        </div>
      </div>
      {obs.reasons.length ? (
        <p style={{ marginTop: 8 }}>
          Motive: {obs.reasons.map(reasonLabel).join("; ")}
        </p>
      ) : null}
      {obs.detail?.candidates?.length ? (
        <table className="bo-table" style={{ marginTop: 8 }}>
          <thead>
            <tr>
              <th>Candidat</th>
              <th>Eligibil</th>
              <th>Motiv</th>
              <th>Scor</th>
              <th>Cost est.</th>
            </tr>
          </thead>
          <tbody>
            {obs.detail.candidates.map((c, i) => (
              <tr key={i}>
                <td>{choiceLabel(c.ref)}</td>
                <td>{c.eligible ? "da" : "nu"}</td>
                <td>{c.reason ? reasonLabel(c.reason) : "—"}</td>
                <td>{c.score != null ? c.score.toFixed(2) : "necunoscut"}</td>
                <td>{c.estimated_cost ?? "necunoscut"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      ) : null}
      <p style={{ marginTop: 8, fontSize: 12, opacity: 0.7 }}>
        policy {obs.policy_version} · catalog {obs.catalog_version} · corelare{" "}
        {obs.correlation_id} · livrare{" "}
        {obs.delivered === 1
          ? "efectuată"
          : obs.delivered === 2
            ? `eșuată definitiv${obs.delivery_error ? ` (${obs.delivery_error})` : ""}`
            : `în așteptare${obs.delivery_error ? ` (${obs.delivery_error})` : ""}`}
        {obs.event_id ? ` · ${obs.event_id}` : ""}
      </p>
    </div>
  );
}

// ------------------------------------------------------------------------- //
// Pagina
// ------------------------------------------------------------------------- //

export default function BoRoutingPage() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [editing, setEditing] = useState<BoCatalogEntry | null>(null);
  const [creating, setCreating] = useState(false);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [filterDecision, setFilterDecision] = useState("");
  const [flushMsg, setFlushMsg] = useState<string | null>(null);

  const load = useCallback(async (decision?: string) => {
    setState({ kind: "loading" });
    try {
      const [status, catalog, obs] = await Promise.all([
        getBoRoutingStatus(),
        getBoRoutingCatalog(),
        listBoRouteObservations(
          decision ? { decision, limit: 100 } : { limit: 100 },
        ),
      ]);
      setState({
        kind: "data",
        status,
        entries: catalog.entries,
        observations: obs.observations,
        canWrite: catalog.role === "admin",
      });
    } catch (e) {
      if (e instanceof BoApiError && e.status === 403) {
        setState({ kind: "forbidden" });
      } else {
        setState({
          kind: "error",
          message:
            e instanceof BoApiError
              ? `${e.code} (${e.status})`
              : "Eroare la încărcare.",
        });
      }
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function applyFilter(decision: string) {
    setFilterDecision(decision);
    if (state.kind !== "data") return;
    try {
      const obs = await listBoRouteObservations(
        decision ? { decision, limit: 100 } : { limit: 100 },
      );
      setState({ ...state, observations: obs.observations });
    } catch {
      /* păstrează lista curentă */
    }
  }

  async function flush() {
    setFlushMsg(null);
    try {
      const res = await flushBoRoutingObservations();
      setFlushMsg(
        `Relivrate: ${res.sent}; eșuate: ${res.failed}.`,
      );
      await load(filterDecision || undefined);
    } catch (e) {
      setFlushMsg(
        e instanceof BoApiError ? `Eșec flush: ${e.code}` : "Eșec flush.",
      );
    }
  }

  if (state.kind === "loading") {
    return (
      <BoPage title="Modele și rutare">
        <StateBlock state="loading" title="Se încarcă catalogul și observațiile…" />
      </BoPage>
    );
  }
  if (state.kind === "forbidden") {
    return (
      <BoPage title="Modele și rutare">
        <StateBlock
          state="forbidden"
          title="Acces interzis"
          detail="Ai nevoie de cel puțin rol viewer pentru această pagină."
        />
      </BoPage>
    );
  }
  if (state.kind === "error") {
    return (
      <BoPage title="Modele și rutare">
        <StateBlock state="error" title="Eroare" detail={state.message} />
      </BoPage>
    );
  }

  const { status, entries, observations, canWrite } = state;

  return (
    <BoPage
      title="Modele și rutare"
      sub="Catalogul administrat și observațiile routerului — mod strict de observare."
      actions={
        <Pill kind="info" icon="info">
          observare — ruta reală nu se schimbă
        </Pill>
      }
    >
      <div className="bo-card">
        <div className="bo-row" style={{ justifyContent: "space-between" }}>
          <div>
            <strong>Stare observare:</strong>{" "}
            {status.observe_enabled ? (
              <Pill kind="ok" icon="check">pornită</Pill>
            ) : (
              <Pill kind="neutral" icon="clock">oprită</Pill>
            )}{" "}
            · catalog {status.catalog_version} · {status.total} observații
            {status.pending_delivery
              ? ` · ${status.pending_delivery} nelivrate`
              : ""}
            {status.dead_delivery
              ? ` · ${status.dead_delivery} eșuate definitiv`
              : ""}
          </div>
          {canWrite ? (
            <div className="bo-row">
              <button
                className="bo-btn bo-btn--primary"
                onClick={() => {
                  setCreating(true);
                  setEditing(null);
                }}
              >
                + Intrare catalog
              </button>
              {status.pending_delivery ? (
                <button className="bo-btn" onClick={flush}>
                  Relivează nelivratele
                </button>
              ) : null}
            </div>
          ) : null}
        </div>
        {flushMsg ? (
          <p style={{ marginTop: 8, fontSize: 13 }}>{flushMsg}</p>
        ) : null}
        {!status.observe_enabled ? (
          <InlineAlert kind="info">
            Observarea este oprită — activează „Observare rutare“ în
            Setări → Rutare pentru a înregistra recomandări. Ruta reală nu se
            schimbă în niciun caz.
          </InlineAlert>
        ) : null}
      </div>

      {creating || editing ? (
        <EntryEditor
          entry={editing}
          onSaved={() => {
            setCreating(false);
            setEditing(null);
            load(filterDecision || undefined);
          }}
          onCancel={() => {
            setCreating(false);
            setEditing(null);
          }}
        />
      ) : null}

      <div className="bo-card">
        <h3 className="bo-card-title">Catalog</h3>
        {entries.length === 0 ? (
          <p style={{ marginTop: 8 }}>
            Catalogul este gol — adaugă intrări pentru ca routerul să poată
            recomanda.
          </p>
        ) : (
          <div className="bo-table-wrap">
            <table className="bo-table">
              <thead>
                <tr>
                  <th>Model</th>
                  <th>Stare</th>
                  <th>Capabilități</th>
                  <th>Regiuni</th>
                  <th>Scor</th>
                  <th>Evaluat la</th>
                  {canWrite ? <th /> : null}
                </tr>
              </thead>
              <tbody>
                {entries.map((e) => {
                  const stale =
                    e.quality &&
                    Date.now() - new Date(e.quality.observed_at).getTime() >
                      90 * 86400_000;
                  return (
                    <tr key={e.entry_id}>
                      <td>
                        {e.provider}/{e.model_id}
                        {e.model_version ? ` v${e.model_version}` : ""}
                      </td>
                      <td>
                        <Pill
                          kind={
                            e.state === "ACTIVE"
                              ? "ok"
                              : e.state === "DISABLED"
                                ? "danger"
                                : "warn"
                          }
                        >
                          {e.state.toLowerCase()}
                        </Pill>
                      </td>
                      <td>{e.capabilities.join(", ") || "—"}</td>
                      <td>{e.regions.join(", ") || "orice"}</td>
                      <td>
                        {e.quality?.score != null
                          ? e.quality.score.toFixed(2)
                          : "neevaluat"}
                        {stale ? (
                          <Pill kind="warn" icon="clock">
                            stale
                          </Pill>
                        ) : null}
                      </td>
                      <td>{e.quality?.observed_at?.slice(0, 10) ?? "—"}</td>
                      {canWrite ? (
                        <td>
                          <button
                            className="bo-btn"
                            onClick={() => {
                              setEditing(e);
                              setCreating(false);
                            }}
                          >
                            Editează
                          </button>
                        </td>
                      ) : null}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="bo-card">
        <div className="bo-row" style={{ justifyContent: "space-between" }}>
          <h3 className="bo-card-title">Observații</h3>
          <select
            className="bo-select"
            value={filterDecision}
            onChange={(e) => applyFilter(e.target.value)}
            aria-label="Filtru decizie"
          >
            <option value="">toate</option>
            <option value="ROUTE">ROUTE</option>
            <option value="REFUSE">REFUSE</option>
          </select>
        </div>
        {observations.length === 0 ? (
          <StateBlock
            state="empty"
            title="Nicio observație"
            detail={
              status.observe_enabled
                ? "Observațiile apar după următorul apel de model real."
                : "Pornește observarea din Setări pentru a înregistra recomandări."
            }
          />
        ) : (
          <div className="bo-table-wrap">
            <table className="bo-table">
              <thead>
                <tr>
                  <th>Când</th>
                  <th>Task</th>
                  <th>Recomandat</th>
                  <th>Folosit</th>
                  <th>Decizie</th>
                  <th>Livrare</th>
                  <th />
                </tr>
              </thead>
              <tbody>
                {observations.map((o) => (
                  <Fragment key={o.obs_id}>
                    <tr>
                      <td>{o.occurred_at.slice(0, 19).replace("T", " ")}</td>
                      <td>{o.task_kind}</td>
                      <td>{choiceLabel(o.recommendation)}</td>
                      <td>{choiceLabel(o.actual_route)}</td>
                      <td>
                        <Pill
                          kind={o.decision === "ROUTE" ? "ok" : "warn"}
                          icon={o.decision === "ROUTE" ? "check" : "alert"}
                        >
                          {o.decision === "ROUTE"
                            ? "recomandat"
                            : "refuz observat"}
                        </Pill>
                      </td>
                      <td>
                        {o.delivered === 1 ? (
                          <Pill kind="ok">livrat</Pill>
                        ) : o.delivered === 2 ? (
                          <Pill kind="danger" icon="alert">eșuat</Pill>
                        ) : (
                          <Pill kind="warn" icon="clock">în așteptare</Pill>
                        )}
                      </td>
                      <td>
                        <button
                          className="bo-btn bo-btn--icon"
                          aria-label="Detalii observație"
                          onClick={() =>
                            setExpanded(
                              expanded === o.obs_id ? null : o.obs_id,
                            )
                          }
                        >
                          {expanded === o.obs_id ? "−" : "+"}
                        </button>
                      </td>
                    </tr>
                    {expanded === o.obs_id ? (
                      <tr>
                        <td colSpan={7}>
                          <ObservationDetail obs={o} />
                        </td>
                      </tr>
                    ) : null}
                  </Fragment>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </BoPage>
  );
}
