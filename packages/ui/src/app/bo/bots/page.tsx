"use client";

// BoBots — lista definițiilor (BO-BOT-001). Draft/active, owner, versiuni.
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";

import IconBO from "@/components/bo/IconBO";
import BoTime from "@/components/bo/BoTime";
import { BoPage, InlineAlert, Pill, StateBlock } from "@/components/bo/ui";
import { BoApiError, createExampleBoBot, listBoBots, type BoBot } from "@/lib/bo";

function kindLabel(kind: BoBot["kind"]) {
  return kind === "BOT" ? "BoBot" : kind === "AI" ? "Agent AI" : "Mixt";
}

export default function BoBotsPage() {
  const router = useRouter();
  const [state, setState] = useState<
    | { kind: "loading" }
    | { kind: "error"; message: string }
    | { kind: "forbidden" }
    | { kind: "data"; bots: BoBot[] }
  >({ kind: "loading" });
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState({ kind: "loading" });
    try {
      const res = await listBoBots();
      setState({ kind: "data", bots: res.bots });
    } catch (err) {
      if (err instanceof BoApiError && err.status === 403) {
        setState({ kind: "forbidden" });
      } else {
        setState({ kind: "error", message: "Lista de BoBots nu a putut fi încărcată." });
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function installExample() {
    setBusy(true);
    setNotice(null);
    try {
      const res = await createExampleBoBot();
      router.push(`/bo/bots/${res.bot.id}`);
    } catch (err) {
      if (err instanceof BoApiError && err.status === 403) {
        setNotice("Crearea cere rol de administrator.");
      } else {
        setNotice("Exemplul nu a putut fi creat. Încearcă din nou.");
      }
      setBusy(false);
    }
  }

  const body =
    state.kind === "loading" ? (
      <StateBlock state="loading" title="Se încarcă lista de BoBots" />
    ) : state.kind === "forbidden" ? (
      <StateBlock
        state="forbidden"
        title="Acces interzis"
        detail="Contul tău nu are drept de citire pe BoBots."
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
    ) : state.bots.length === 0 ? (
      <StateBlock
        state="empty"
        title="Niciun BoBot încă"
        detail="BoBots sunt automatizări deterministe: eveniment → condiții → plan → chitanță. În Valul 1 rulează numai simulări, fără efecte externe."
        action={
          <button
            type="button"
            className="bo-btn bo-btn--primary"
            onClick={installExample}
            disabled={busy}
          >
            <IconBO name="plus" size={15} /> Instalează exemplul heartbeat
          </button>
        }
      />
    ) : (
      <div className="bo-table-wrap">
        <table className="bo-table">
          <thead>
            <tr>
              <th>Nume</th>
              <th>Tip</th>
              <th>Stare</th>
              <th>Versiune activă</th>
              <th>Ciornă</th>
              <th>Actualizat</th>
              <th aria-label="Acțiuni" />
            </tr>
          </thead>
          <tbody>
            {state.bots.map((b) => (
              <tr key={b.id}>
                <td>
                  <div style={{ fontWeight: 600 }}>{b.name}</div>
                  {b.description ? (
                    <div className="bo-muted" style={{ fontSize: 12 }}>
                      {b.description}
                    </div>
                  ) : null}
                </td>
                <td>
                  <Pill kind="neutral" icon="bot">{kindLabel(b.kind)}</Pill>
                </td>
                <td>
                  {b.status === "active" ? (
                    <Pill kind="ok" icon="check">activ</Pill>
                  ) : (
                    <Pill kind="neutral" icon="clock">ciornă</Pill>
                  )}
                </td>
                <td className="bo-mono">
                  {b.active_version_no !== null ? `v${b.active_version_no}` : "—"}
                </td>
                <td className="bo-mono">d{b.draft_version}</td>
                <td className="bo-muted">
                  <BoTime iso={b.updated_at} />
                </td>
                <td>
                  <Link
                    href={`/bo/bots/${b.id}`}
                    className="bo-btn"
                    aria-label={`Deschide ${b.name}`}
                  >
                    Deschide <IconBO name="chevron-right" size={14} />
                  </Link>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    );

  return (
    <BoPage
      title="BoBots"
      sub="Automatizări deterministe ale produsului BOAgents. În Valul 1 se pot doar simula — niciun efect extern, niciun provider AI."
      actions={
        <>
          <button
            type="button"
            className="bo-btn"
            onClick={installExample}
            disabled={busy}
            title="Creează BoBotul sintetic heartbeat→finding"
          >
            <IconBO name="bot" size={15} /> Exemplu heartbeat
          </button>
          <Link href="/bo/bots/nou" className="bo-btn bo-btn--primary">
            <IconBO name="plus" size={15} /> BoBot nou
          </Link>
        </>
      }
    >
      {notice ? (
        <div style={{ marginBottom: 12 }}>
          <InlineAlert kind="warn">{notice}</InlineAlert>
        </div>
      ) : null}
      {body}
    </BoPage>
  );
}
