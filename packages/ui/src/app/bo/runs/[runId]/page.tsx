"use client";

// Run detail — provenance + step timeline. Every receipt is NOT_EXECUTED:
// this page is the audit surface proving a simulation touched nothing.
import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import IconBO from "@/components/bo/IconBO";
import BoTime from "@/components/bo/BoTime";
import { BoPage, Pill, StateBlock } from "@/components/bo/ui";
import { BoApiError, getBoRun, type BoRun } from "@/lib/bo";

type State =
  | { kind: "loading" }
  | { kind: "error" }
  | { kind: "forbidden" }
  | { kind: "notfound" }
  | { kind: "data"; run: BoRun };

function pillFor(status: string): "ok" | "warn" | "danger" | "neutral" {
  if (status === "SUCCEEDED" || status === "SIMULATED") return "ok";
  if (status === "PARTIAL" || status === "SKIPPED") return "warn";
  if (status === "FAILED") return "danger";
  return "neutral";
}

export default function BoRunPage() {
  const { runId } = useParams<{ runId: string }>();
  const [state, setState] = useState<State>({ kind: "loading" });

  const load = useCallback(async () => {
    try {
      const res = await getBoRun(runId);
      setState({ kind: "data", run: res.run });
    } catch (err) {
      if (err instanceof BoApiError && err.status === 404) setState({ kind: "notfound" });
      else if (err instanceof BoApiError && err.status === 403) setState({ kind: "forbidden" });
      else setState({ kind: "error" });
    }
  }, [runId]);

  useEffect(() => {
    void load();
  }, [load]);

  if (state.kind !== "data") {
    return (
      <BoPage
        title="Rulare simulare"
        actions={
          <Link href="/bo/bots" className="bo-btn">
            <IconBO name="arrow-left" size={15} /> BoBots
          </Link>
        }
      >
        {state.kind === "loading" ? (
          <StateBlock state="loading" title="Se încarcă rularea" />
        ) : state.kind === "notfound" ? (
          <StateBlock state="empty" title="Rularea nu există" />
        ) : state.kind === "forbidden" ? (
          <StateBlock state="forbidden" title="Acces interzis" />
        ) : (
          <StateBlock state="error" title="Eroare la încărcare" />
        )}
      </BoPage>
    );
  }

  const { run } = state;
  const result = run.result;

  return (
    <BoPage
      title={`Rulare ${run.id}`}
      sub="Simulare deterministă — niciun efect extern. Proveniența completă e mai jos."
      actions={
        <Link href={`/bo/bots/${run.definition_id}`} className="bo-btn">
          <IconBO name="arrow-left" size={15} /> Înapoi la BoBot
        </Link>
      }
    >
      <div className="bo-row" style={{ marginBottom: 16 }}>
        <Pill kind={pillFor(run.status)}>{run.status}</Pill>
        <Pill kind="neutral" icon="bot">v{run.version_no}</Pill>
        <Pill kind={result?.predicate_result === "UNKNOWN" ? "warn" : "neutral"}>
          predicat: {result?.predicate_result ?? "—"}
        </Pill>
        <Pill kind="ok" icon="shield-check">efecte externe: {result?.external_effects ?? 0}</Pill>
      </div>

      <div className="bo-card">
        <h2 className="bo-card-title">Proveniență</h2>
        <dl className="bo-kv" style={{ marginTop: 10 }}>
          <dt>Definiție</dt><dd className="bo-mono">{run.definition_id}</dd>
          <dt>Versiune</dt><dd className="bo-mono">v{run.version_no} · {run.version_hash.slice(0, 16)}…</dd>
          <dt>Plan (deterministic)</dt><dd className="bo-mono">{run.plan_hash}</dd>
          <dt>ConfigVersion</dt><dd className="bo-mono">cv{run.config_version}</dd>
          <dt>Pornit de</dt><dd>{run.created_by}</dd>
          <dt>Pornit la</dt><dd><BoTime iso={run.started_at} /></dd>
          <dt>Limită pași</dt><dd>{result?.step_limit ?? "—"} {result?.capped_by_step_limit ? "(atingă — PARTIAL)" : ""}</dd>
        </dl>
      </div>

      {result && result.findings.length > 0 ? (
        <div className="bo-card" style={{ marginTop: 16 }}>
          <h2 className="bo-card-title">Constatări ({result.findings.length})</h2>
          <div style={{ display: "grid", gap: 10, marginTop: 10 }}>
            {result.findings.map((f, i) => (
              <div key={i} className="bo-alert bo-alert--warn">
                <IconBO name="alert" size={16} />
                <div>
                  <div>
                    <strong>{f.finding_key}</strong> · {f.category} · {f.severity}
                  </div>
                  <div style={{ marginTop: 4 }}>{f.message}</div>
                  <div className="bo-hint" style={{ marginTop: 4 }}>
                    efect: {f.effect_status}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>
      ) : null}

      <div className="bo-card" style={{ marginTop: 16 }}>
        <h2 className="bo-card-title">Timeline pași</h2>
        <div className="bo-table-wrap" style={{ marginTop: 10 }}>
          <table className="bo-table">
            <thead>
              <tr><th>#</th><th>Pas</th><th>Tip</th><th>Stare</th><th>Detaliu</th><th>Chitanță</th></tr>
            </thead>
            <tbody>
              {(run.timeline ?? []).map((s) => (
                <tr key={s.idx}>
                  <td className="bo-mono">{s.idx}</td>
                  <td className="bo-mono">{s.step_id}</td>
                  <td>{s.step_type}</td>
                  <td><Pill kind={pillFor(s.status)}>{s.status}</Pill></td>
                  <td className="bo-muted">
                    <code className="bo-mono" style={{ whiteSpace: "pre-wrap" }}>
                      {s.detail ? JSON.stringify(s.detail) : "—"}
                    </code>
                  </td>
                  <td className="bo-muted">
                    {s.receipt?.effect_status ?? "—"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
    </BoPage>
  );
}
