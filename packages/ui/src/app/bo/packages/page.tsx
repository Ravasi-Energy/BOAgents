"use client";

// Pachete semnate bo.package.v1 (VAL2-01): import → verificare → carantină.
// Activarea pilotului sintetic este separată; aici stările ajung la DRAFT. Verdictul
// verificatorului este afișat întotdeauna, inclusiv pentru importuri respinse.
import { useCallback, useEffect, useState } from "react";

import IconBO from "@/components/bo/IconBO";
import {
  BoPage,
  Field,
  InlineAlert,
  Pill,
  StateBlock,
} from "@/components/bo/ui";
import {
  BoApiError,
  createBoPackageApproval,
  getBoSettings,
  importBoPackage,
  listBoPackageApprovals,
  listBoPackageImports,
  promoteBoPackage,
  revokeBoPackageApproval,
  type BoPackageApproval,
  type BoPackageImport,
} from "@/lib/bo";

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "forbidden" }
  | {
      kind: "data";
      imports: BoPackageImport[];
      approvals: BoPackageApproval[];
      canWrite: boolean;
    };

function statusPill(status: BoPackageImport["status"]) {
  if (status === "QUARANTINED")
    return (
      <Pill kind="warn" icon="clock">
        carantină
      </Pill>
    );
  if (status === "DRAFT")
    return (
      <Pill kind="info" icon="file-json">
        draft
      </Pill>
    );
  return (
    <Pill kind="danger" icon="shield-alert">
      respins
    </Pill>
  );
}

function approvalState(a: BoPackageApproval): {
  label: string;
  kind: "ok" | "warn" | "neutral";
} {
  if (a.status === "revoked") return { label: "revocată", kind: "neutral" };
  if (a.consumed_at) return { label: "consumată", kind: "neutral" };
  if (new Date(a.expires_at).getTime() <= Date.now())
    return { label: "expirată", kind: "warn" };
  return { label: "activă", kind: "ok" };
}

export default function BoPackagesPage() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [selected, setSelected] = useState<string | null>(null);
  const [notice, setNotice] = useState<{
    kind: "ok" | "warn" | "danger";
    text: string;
  } | null>(null);
  const [sourceDir, setSourceDir] = useState("");
  const [busy, setBusy] = useState(false);
  const [approvalForm, setApprovalForm] = useState({
    package_id: "",
    from_version: "",
    to_version: "",
    artifact_set_digest: "",
    expires_at: "",
  });

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const [imports, approvals, settings] = await Promise.all([
        listBoPackageImports(),
        listBoPackageApprovals(),
        getBoSettings(),
      ]);
      setState({
        kind: "data",
        imports: imports.imports,
        approvals: approvals.approvals,
        canWrite: settings.role === "admin",
      });
    } catch (err) {
      if (err instanceof BoApiError && err.status === 403) {
        setState({ kind: "forbidden" });
      } else {
        setState({
          kind: "error",
          message: "Lista de pachete nu a putut fi încărcată.",
        });
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function describeError(err: unknown, fallback: string): string {
    if (err instanceof BoApiError) {
      if (err.status === 403) return "Acțiunea cere rol de administrator.";
      if (err.code === "package_rejected" && err.detail) {
        const d = err.detail as { code?: string; detail?: string };
        return `Pachet respins: ${d.code ?? ""} ${d.detail ?? ""}`.trim();
      }
      if (typeof err.detail === "string") return err.detail;
    }
    return fallback;
  }

  async function doImport() {
    setBusy(true);
    setNotice(null);
    try {
      const res = await importBoPackage(sourceDir.trim());
      if (res.verdict.verdict === "ACCEPT") {
        setNotice({
          kind: "ok",
          text: `${res.package_id}@${res.version} verificat și pus în carantină${res.idempotent ? " (import idempotent — aceiași octeți)" : ""}.`,
        });
        setSourceDir("");
      } else {
        setNotice({
          kind: "warn",
          text: `Respins: ${res.verdict.reasons.join("; ")}`,
        });
      }
      await load();
    } catch (err) {
      setNotice({
        kind: "danger",
        text: describeError(err, "Importul a eșuat. Încearcă din nou."),
      });
    } finally {
      setBusy(false);
    }
  }

  async function doPromote(id: string) {
    setBusy(true);
    setNotice(null);
    try {
      await promoteBoPackage(id);
      setNotice({ kind: "ok", text: "Pachet promovat la DRAFT." });
      await load();
    } catch (err) {
      setNotice({
        kind: "danger",
        text: describeError(err, "Promovarea a eșuat."),
      });
    } finally {
      setBusy(false);
    }
  }

  async function doCreateApproval() {
    setBusy(true);
    setNotice(null);
    try {
      await createBoPackageApproval({
        package_id: approvalForm.package_id.trim(),
        from_version: approvalForm.from_version.trim(),
        to_version: approvalForm.to_version.trim(),
        artifact_set_digest: approvalForm.artifact_set_digest.trim(),
        expires_at: new Date(approvalForm.expires_at).toISOString(),
      });
      setNotice({ kind: "ok", text: "Aprobare de downgrade creată." });
      setApprovalForm({
        package_id: "",
        from_version: "",
        to_version: "",
        artifact_set_digest: "",
        expires_at: "",
      });
      await load();
    } catch (err) {
      setNotice({
        kind: "danger",
        text: describeError(err, "Aprobarea nu a putut fi creată."),
      });
    } finally {
      setBusy(false);
    }
  }

  async function doRevoke(id: string) {
    setBusy(true);
    try {
      await revokeBoPackageApproval(id);
      await load();
    } catch (err) {
      setNotice({
        kind: "danger",
        text: describeError(err, "Revocarea a eșuat."),
      });
    } finally {
      setBusy(false);
    }
  }

  const body =
    state.kind === "loading" ? (
      <StateBlock state="loading" title="Se încarcă pachetele" />
    ) : state.kind === "forbidden" ? (
      <StateBlock
        state="forbidden"
        title="Acces interzis"
        detail="Contul tău nu are drept de citire pe pachetele BOAgents."
      />
    ) : state.kind === "error" ? (
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
    ) : (
      <>
        {state.imports.length === 0 ? (
          <StateBlock
            state="empty"
            title="Niciun pachet importat"
            detail="Pachetele semnate bo.package.v1 sunt verificate criptografic și păstrate în carantină — nimic nu se activează automat."
          />
        ) : (
          <div className="bo-table-wrap">
            <table className="bo-table">
              <thead>
                <tr>
                  <th>Pachet</th>
                  <th>Versiune</th>
                  <th>Emitent</th>
                  <th>Stare</th>
                  <th>Digest artefacte</th>
                  <th>Importat</th>
                  <th aria-label="Acțiuni" />
                </tr>
              </thead>
              <tbody>
                {state.imports.map((p) => (
                  <tr key={p.id}>
                    <td>
                      <div className="bo-mono" style={{ fontWeight: 600 }}>
                        {p.package_id}
                      </div>
                      <div className="bo-muted" style={{ fontSize: 12 }}>
                        {p.kind} · cheie {p.key_id}
                      </div>
                    </td>
                    <td className="bo-mono">{p.version}</td>
                    <td className="bo-mono">{p.publisher_id}</td>
                    <td>{statusPill(p.status)}</td>
                    <td
                      className="bo-mono bo-muted"
                      style={{ fontSize: 12 }}
                      title={p.artifact_set_digest}
                    >
                      {p.artifact_set_digest.slice(0, 20)}…
                    </td>
                    <td className="bo-muted">
                      {new Date(p.created_at).toLocaleString("ro-RO")}
                    </td>
                    <td>
                      <div className="bo-row">
                        <button
                          type="button"
                          className="bo-btn"
                          onClick={() =>
                            setSelected(selected === p.id ? null : p.id)
                          }
                          aria-expanded={selected === p.id}
                          aria-label={`Verdict pentru ${p.package_id}`}
                        >
                          Verdict
                        </button>
                        {p.status === "QUARANTINED" && state.canWrite ? (
                          <button
                            type="button"
                            className="bo-btn"
                            onClick={() => doPromote(p.id)}
                            disabled={busy}
                            title="Re-verifică copia din carantină și promovează la DRAFT"
                          >
                            <IconBO name="check" size={14} /> Promovează
                          </button>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {selected
          ? (() => {
              const p = state.imports.find((x) => x.id === selected);
              if (!p) return null;
              return (
                <div className="bo-card" style={{ marginTop: 16 }}>
                  <div className="bo-spread">
                    <h3 className="bo-card-title">
                      Verdict — {p.package_id}@{p.version}
                    </h3>
                    <Pill
                      kind={p.verdict.verdict === "ACCEPT" ? "ok" : "danger"}
                      icon={
                        p.verdict.verdict === "ACCEPT"
                          ? "shield-check"
                          : "shield-alert"
                      }
                    >
                      {p.verdict.verdict === "ACCEPT" ? "acceptat" : "respins"}
                    </Pill>
                  </div>
                  {p.verdict.reasons.length > 0 ? (
                    <ul className="bo-hint" style={{ marginTop: 8 }}>
                      {p.verdict.reasons.map((r) => (
                        <li key={r} className="bo-mono">
                          {r}
                        </li>
                      ))}
                    </ul>
                  ) : null}
                  <dl
                    className="bo-hint"
                    style={{
                      marginTop: 10,
                      display: "grid",
                      gridTemplateColumns: "max-content 1fr",
                      gap: "4px 16px",
                    }}
                  >
                    <dt>manifestDigest</dt>
                    <dd className="bo-mono">{p.verdict.manifestDigest || "—"}</dd>
                    <dt>verificat la</dt>
                    <dd>
                      {p.verdict.checkedAt
                        ? new Date(p.verdict.checkedAt).toLocaleString("ro-RO")
                        : "—"}
                    </dd>
                    <dt>politica</dt>
                    <dd className="bo-mono">{p.verdict.policyVersion || "—"}</dd>
                    <dt>sursă</dt>
                    <dd className="bo-mono">{p.source_path}</dd>
                    {p.approval_id ? (
                      <>
                        <dt>aprobare</dt>
                        <dd className="bo-mono">{p.approval_id}</dd>
                      </>
                    ) : null}
                  </dl>
                </div>
              );
            })()
          : null}

        {state.canWrite ? (
          <div className="bo-card" style={{ marginTop: 16 }}>
            <h3 className="bo-card-title">Importă un pachet</h3>
            <p className="bo-hint" style={{ marginTop: 4 }}>
              Calea unui director de pachet pe server (manifest.json +
              artefacte). Verificarea rulează înainte de orice copiere.
            </p>
            <div
              className="bo-row"
              style={{ marginTop: 10, alignItems: "flex-end" }}
            >
              <div style={{ flex: 1, minWidth: 240 }}>
                <Field label="Director sursă" htmlFor="pkg-src">
                  <input
                    id="pkg-src"
                    className="bo-input"
                    style={{ width: "100%" }}
                    value={sourceDir}
                    onChange={(e) => setSourceDir(e.target.value)}
                    placeholder="/cale/catre/pachet"
                    disabled={busy}
                  />
                </Field>
              </div>
              <button
                type="button"
                className="bo-btn bo-btn--primary"
                onClick={doImport}
                disabled={busy || !sourceDir.trim()}
              >
                <IconBO name="package" size={15} /> Importă în carantină
              </button>
            </div>
          </div>
        ) : null}

        <div className="bo-card" style={{ marginTop: 16 }}>
          <h3 className="bo-card-title">Aprobări de downgrade</h3>
          <p className="bo-hint" style={{ marginTop: 4 }}>
            Reinstalarea unei versiuni mai vechi cere o aprobare legată de
            tenant, digest al setului de artefacte și interval de versiuni —
            de unică folosință, cu expirare.
          </p>
          {state.approvals.length === 0 ? (
            <p className="bo-hint" style={{ marginTop: 8 }}>
              Nicio aprobare înregistrată.
            </p>
          ) : (
            <div className="bo-table-wrap" style={{ marginTop: 10 }}>
              <table className="bo-table">
                <thead>
                  <tr>
                    <th>Pachet</th>
                    <th>Versiuni</th>
                    <th>Digest țintă</th>
                    <th>Expiră</th>
                    <th>Stare</th>
                    <th aria-label="Acțiuni" />
                  </tr>
                </thead>
                <tbody>
                  {state.approvals.map((a) => {
                    const s = approvalState(a);
                    return (
                      <tr key={a.id}>
                        <td className="bo-mono">{a.package_id}</td>
                        <td className="bo-mono">
                          {a.from_version} → {a.to_version}
                        </td>
                        <td
                          className="bo-mono bo-muted"
                          style={{ fontSize: 12 }}
                          title={a.artifact_set_digest}
                        >
                          {a.artifact_set_digest.slice(0, 20)}…
                        </td>
                        <td className="bo-muted">
                          {new Date(a.expires_at).toLocaleString("ro-RO")}
                        </td>
                        <td>
                          <Pill kind={s.kind}>{s.label}</Pill>
                        </td>
                        <td>
                          {s.label === "activă" && state.canWrite ? (
                            <button
                              type="button"
                              className="bo-btn"
                              onClick={() => doRevoke(a.id)}
                              disabled={busy}
                            >
                              Revocă
                            </button>
                          ) : null}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}

          {state.canWrite ? (
            <div style={{ marginTop: 14 }}>
              <h4 className="bo-card-title" style={{ fontSize: 14 }}>
                Aprobare nouă
              </h4>
              <div
                className="bo-grid"
                style={{ marginTop: 8, gap: 10 }}
              >
                <Field label="Pachet" htmlFor="ap-pkg">
                  <input
                    id="ap-pkg"
                    className="bo-input"
                    value={approvalForm.package_id}
                    onChange={(e) =>
                      setApprovalForm((f) => ({
                        ...f,
                        package_id: e.target.value,
                      }))
                    }
                    placeholder="pkg.demo"
                  />
                </Field>
                <Field label="De la versiunea" htmlFor="ap-from">
                  <input
                    id="ap-from"
                    className="bo-input"
                    value={approvalForm.from_version}
                    onChange={(e) =>
                      setApprovalForm((f) => ({
                        ...f,
                        from_version: e.target.value,
                      }))
                    }
                    placeholder="2.0.0"
                  />
                </Field>
                <Field label="La versiunea" htmlFor="ap-to">
                  <input
                    id="ap-to"
                    className="bo-input"
                    value={approvalForm.to_version}
                    onChange={(e) =>
                      setApprovalForm((f) => ({
                        ...f,
                        to_version: e.target.value,
                      }))
                    }
                    placeholder="1.0.0"
                  />
                </Field>
                <Field label="Digest set artefacte" htmlFor="ap-digest">
                  <input
                    id="ap-digest"
                    className="bo-input"
                    value={approvalForm.artifact_set_digest}
                    onChange={(e) =>
                      setApprovalForm((f) => ({
                        ...f,
                        artifact_set_digest: e.target.value,
                      }))
                    }
                    placeholder="sha256:…"
                  />
                </Field>
                <Field label="Expiră la" htmlFor="ap-exp">
                  <input
                    id="ap-exp"
                    className="bo-input"
                    type="datetime-local"
                    value={approvalForm.expires_at}
                    onChange={(e) =>
                      setApprovalForm((f) => ({
                        ...f,
                        expires_at: e.target.value,
                      }))
                    }
                  />
                </Field>
              </div>
              <button
                type="button"
                className="bo-btn"
                style={{ marginTop: 10 }}
                onClick={doCreateApproval}
                disabled={
                  busy ||
                  !approvalForm.package_id.trim() ||
                  !approvalForm.from_version.trim() ||
                  !approvalForm.to_version.trim() ||
                  !approvalForm.artifact_set_digest.trim() ||
                  !approvalForm.expires_at
                }
              >
                <IconBO name="plus" size={15} /> Creează aprobarea
              </button>
            </div>
          ) : null}
        </div>
      </>
    );

  return (
    <BoPage
      title="Pachete semnate"
      sub="Import bo.package.v1 cu verificare Ed25519 și trust store separat. După carantină și DRAFT, pachetul ERP sintetic cere aprobare explicită în Pilot sintetic."
      actions={null}
    >
      {notice ? (
        <div style={{ marginBottom: 12 }}>
          <InlineAlert
            kind={notice.kind === "ok" ? "info" : notice.kind}
          >
            {notice.text}
          </InlineAlert>
        </div>
      ) : null}
      {body}
    </BoPage>
  );
}
