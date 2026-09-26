"use client";

// Setări BOAgents — BO-SET-001. Toate valorile vin din /bo/settings; fiecare
// salvare poartă expected_version (CAS) și un conflict 409 cere reîncărcare,
// nu suprascriere oarbă.
import { useCallback, useEffect, useState } from "react";

import IconBO from "@/components/bo/IconBO";
import { Field, InlineAlert, Pill, StateBlock } from "@/components/bo/ui";
import {
  BoApiError,
  getBoSettings,
  getBoTelemetryStatus,
  putBoSetting,
  type BoSetting,
  type BoSettingsResponse,
  type BoTelemetryStatus,
} from "@/lib/bo";

type LoadState =
  | { kind: "loading" }
  | { kind: "error"; message: string }
  | { kind: "forbidden" }
  | { kind: "data"; data: BoSettingsResponse; telemetry: BoTelemetryStatus | null };

// Gruparea pe taburi — registrul marchează fiecare parametru; tabul
// „exec" e suprafața de execuție/recuperare cerută de VAL4-03.
const TAB_ORDER = ["general", "routing", "exec", "pilot"] as const;
const TAB_LABEL: Record<string, string> = {
  general: "General",
  routing: "Rutare modele",
  exec: "Execuție și recuperare",
  pilot: "Pilot ERP sintetic",
};

function SettingEditor({
  setting,
  canEdit,
  onSaved,
}: {
  setting: BoSetting;
  canEdit: boolean;
  onSaved: (s: BoSetting) => void;
}) {
  const [draft, setDraft] = useState(String(setting.value ?? ""));
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState<
    { kind: "ok" | "warn" | "danger"; text: string } | null
  >(null);

  const dirty = draft !== String(setting.value ?? "");

  async function save() {
    setSaving(true);
    setNotice(null);
    let value: unknown = draft;
    if (setting.type === "integer") {
      const n = Number(draft);
      if (!Number.isInteger(n)) {
        setNotice({ kind: "danger", text: "Introdu un număr întreg." });
        setSaving(false);
        return;
      }
      value = n;
    } else if (setting.type === "boolean") {
      value = draft === "true";
    }
    try {
      const res = await putBoSetting(setting.key, value, setting.version);
      onSaved(res.setting);
      setNotice({
        kind: "ok",
        text: res.applied
          ? "Salvat și aplicat imediat."
          : `Salvat. ${setting.effect_ro}`,
      });
    } catch (err) {
      if (err instanceof BoApiError && err.status === 409) {
        setNotice({
          kind: "warn",
          text: "Altă modificare s-a salvat între timp. Reîncarcă pagina pentru valoarea curentă.",
        });
      } else if (err instanceof BoApiError && err.status === 422) {
        const detail =
          typeof err.detail === "string" ? err.detail : "Valoare invalidă.";
        setNotice({ kind: "danger", text: detail });
      } else if (err instanceof BoApiError && err.status === 403) {
        setNotice({ kind: "danger", text: "Nu ai dreptul să modifici această setare." });
      } else {
        setNotice({ kind: "danger", text: "Salvarea a eșuat. Încearcă din nou." });
      }
    } finally {
      setSaving(false);
    }
  }

  const inputId = `set-${setting.key}`;
  const disabled = !canEdit || saving;

  return (
    <div className="bo-card">
      <div className="bo-spread">
        <div>
          <h3 className="bo-card-title">{setting.label_ro}</h3>
          <p className="bo-hint" style={{ marginTop: 4 }}>{setting.help_ro}</p>
        </div>
        <div className="bo-row">
          <Pill kind={setting.origin === "tenant" ? "info" : "neutral"}>
            {setting.origin === "tenant" ? "personalizat" : "implicit"}
          </Pill>
          <Pill kind="neutral" icon="history">
            v{setting.version}
          </Pill>
        </div>
      </div>

      <div className="bo-row" style={{ marginTop: 12, alignItems: "flex-end" }}>
        <div style={{ flex: 1, minWidth: 200 }}>
          <Field label="" htmlFor={inputId}>
            {setting.type === "enum" || setting.type === "boolean" ? (
              <select
                id={inputId}
                className="bo-select"
                style={{ width: "100%" }}
                value={draft}
                disabled={disabled}
                onChange={(e) => setDraft(e.target.value)}
                aria-label={setting.label_ro}
              >
                {setting.type === "boolean" ? (
                  <>
                    <option value="false">Oprit</option>
                    <option value="true">Pornit</option>
                  </>
                ) : setting.key === "bo.pilot.profile" ? (
                  <><option value="disabled">Dezactivat</option><option value="synthetic-loopback">Sintetic loopback</option></>
                ) : setting.key === "bo.pilot.supervision" ? (
                  <><option value="standalone">Standalone</option><option value="required">Guardian obligatoriu</option></>
                ) : (
                  <>
                    <option value="ro">Română</option>
                    <option value="en">Engleză</option>
                  </>
                )}
              </select>
            ) : (
              <input
                id={inputId}
                className="bo-input"
                style={{ width: "100%" }}
                type={setting.type === "integer" ? "number" : "text"}
                inputMode={setting.type === "integer" ? "numeric" : undefined}
                value={draft}
                disabled={disabled}
                onChange={(e) => setDraft(e.target.value)}
                aria-label={setting.label_ro}
              />
            )}
          </Field>
        </div>
        <button
          type="button"
          className="bo-btn bo-btn--primary"
          onClick={save}
          disabled={disabled || !dirty}
          title={
            !canEdit
              ? "Necesită rol de administrator"
              : !dirty
                ? "Nicio modificare de salvat"
                : undefined
          }
        >
          {saving ? "Se salvează…" : "Salvează"}
        </button>
      </div>

      <p className="bo-hint" style={{ marginTop: 8 }}>
        {setting.effect_ro}
        {setting.updated_by
          ? ` Ultima modificare: ${setting.updated_by}.`
          : ""}
      </p>
      {notice ? (
        <div style={{ marginTop: 10 }}>
          <InlineAlert kind={notice.kind === "ok" ? "info" : notice.kind}>
            {notice.text}
          </InlineAlert>
        </div>
      ) : null}
    </div>
  );
}

export default function BoSettingsPage() {
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const data = await getBoSettings();
      let telemetry: BoTelemetryStatus | null = null;
      try {
        telemetry = await getBoTelemetryStatus();
      } catch {
        telemetry = null; // status card is informational; absence ≠ success
      }
      setState({ kind: "data", data, telemetry });
    } catch (err) {
      if (err instanceof BoApiError && err.status === 403) {
        setState({ kind: "forbidden" });
      } else {
        setState({
          kind: "error",
          message: "Nu am putut încărca setările. Verifică backend-ul.",
        });
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  if (state.kind === "loading") {
    return <StateBlock state="loading" title="Se încarcă setările" />;
  }
  if (state.kind === "forbidden") {
    return (
      <StateBlock
        state="forbidden"
        title="Acces interzis"
        detail="Contul tău nu are drept de citire pe setările BOAgents."
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

  const { data, telemetry } = state;
  const canEdit = data.role === "admin";
  const grouped: Record<string, BoSetting[]> = {};
  for (const s of data.settings) {
    const t = s.tab || "general";
    (grouped[t] ??= []).push(s);
  }

  return (
    <div className="bo-scope" style={{ marginTop: 20 }}>
      <div className="bo-row" style={{ marginBottom: 16 }}>
        <Pill kind="info" icon="shield-check">
          tenant: {data.tenant}
        </Pill>
        <Pill kind="neutral" icon="list">
          rol: {data.role}
        </Pill>
        <Pill kind="neutral" icon="history">
          configVersion: {data.config_version}
        </Pill>
      </div>

      {!canEdit ? (
        <div style={{ marginBottom: 16 }}>
          <InlineAlert kind="warn" icon="shield-alert">
            Vizualizare doar-citire — modificarea setărilor cere rol de
            administrator.
          </InlineAlert>
        </div>
      ) : null}

      {TAB_ORDER.filter((t) => grouped[t]?.length).map((tab) => (
        <section key={tab} style={{ marginTop: 20 }}>
          <h3 className="bo-card-title" style={{ marginBottom: 4 }}>
            {TAB_LABEL[tab] ?? tab}
          </h3>
          {tab === "exec" ? (
            <p className="bo-hint" style={{ marginBottom: 8 }}>
              Parametrii de execuție și recuperare. Controalele
              obligatorii (checkpoint, autorizarea Guardian pentru
              mandate legate) nu pot fi dezactivate de agent — oprirea
              lor nu poate produce efecte nedeclarate.
            </p>
          ) : null}
          <div className="bo-grid">
            {grouped[tab].map((s) => (
              <SettingEditor
                key={s.key}
                setting={s}
                canEdit={canEdit}
                onSaved={(updated) =>
                  setState((prev) =>
                    prev.kind === "data"
                      ? {
                          kind: "data",
                          telemetry: prev.telemetry,
                          data: {
                            ...prev.data,
                            config_version: Math.max(
                              prev.data.config_version,
                              updated.version,
                            ),
                            settings: prev.data.settings.map((x) =>
                              x.key === updated.key ? { ...x, ...updated } : x,
                            ),
                          },
                        }
                      : prev,
                  )
                }
              />
            ))}
          </div>
        </section>
      ))}

      <div className="bo-card" style={{ marginTop: 16 }}>
        <div className="bo-spread">
          <h3 className="bo-card-title">Telemetrie produs</h3>
          {telemetry ? (
            <Pill kind={telemetry.enabled ? "warn" : "ok"} icon="activity">
              {telemetry.enabled ? "activă" : "oprită (implicit)"}
            </Pill>
          ) : (
            <Pill kind="neutral">nemăsurat</Pill>
          )}
        </div>
        <p className="bo-hint" style={{ marginTop: 8 }}>
          {telemetry
            ? `${telemetry.transport} · emise: ${telemetry.emitted} · respinse: ${telemetry.rejected}. ${telemetry.note}`
            : "Starea adaptorului nu a putut fi citită."}
        </p>
      </div>
    </div>
  );
}
