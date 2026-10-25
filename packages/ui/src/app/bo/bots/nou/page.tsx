"use client";

// BoBot nou — definire prin conținut JSON validat strict pe server.
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";

import IconBO from "@/components/bo/IconBO";
import { BoPage, Field, InlineAlert } from "@/components/bo/ui";
import { BoApiError, boErrorDetail, createBoBot } from "@/lib/bo";

const TEMPLATE = `{
  "schema_version": "bo.bobot.v1",
  "trigger": { "type": "manual" },
  "predicates": null,
  "steps": [
    { "id": "note", "type": "note", "message": "primul pas — simulat" }
  ],
  "capability_refs": [],
  "policy_refs": []
}`;

export default function BoBotNewPage() {
  const router = useRouter();
  const [name, setName] = useState("");
  const [kind, setKind] = useState<"BOT" | "AI" | "MIXED">("BOT");
  const [description, setDescription] = useState("");
  const [content, setContent] = useState(TEMPLATE);
  const [busy, setBusy] = useState(false);
  const [errors, setErrors] = useState<string[]>([]);

  async function submit() {
    setBusy(true);
    setErrors([]);
    let parsed: unknown;
    try {
      parsed = JSON.parse(content);
    } catch {
      setErrors(["Conținutul nu este JSON valid."]);
      setBusy(false);
      return;
    }
    try {
      const res = await createBoBot({
        name,
        kind,
        description,
        content: parsed as Record<string, unknown>,
      });
      router.push(`/bo/bots/${res.bot.id}`);
    } catch (err) {
      if (err instanceof BoApiError && err.status === 422) {
        const detail = err.detail;
        setErrors(
          Array.isArray(detail)
            ? detail.map((d) => boErrorDetail([d]))
            : [typeof detail === "string" ? detail : "Definiție invalidă."],
        );
      } else if (err instanceof BoApiError && err.status === 403) {
        setErrors(["Crearea cere rol de administrator."]);
      } else {
        setErrors(["Crearea a eșuat. Încearcă din nou."]);
      }
      setBusy(false);
    }
  }

  return (
    <BoPage
      title="BoBot nou"
      sub="Definiția se validează strict pe server (gramatică închisă — fără eval, SQL sau shell). Salvarea creează o ciornă; nimic nu se activează automat."
      actions={
        <Link href="/bo/bots" className="bo-btn">
          <IconBO name="arrow-left" size={15} /> Înapoi la listă
        </Link>
      }
    >
      <div className="bo-card" style={{ maxWidth: 760 }}>
        <div className="bo-grid bo-grid-2">
          <Field label="Nume" hint="Obligatoriu, max 120 caractere." htmlFor="bb-name">
            <input
              id="bb-name"
              className="bo-input"
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={120}
            />
          </Field>
          <Field label="Tip" hint="În Valul 1 doar BoBot (determinist) poate fi simulat." htmlFor="bb-kind">
            <select
              id="bb-kind"
              className="bo-select"
              value={kind}
              onChange={(e) => setKind(e.target.value as typeof kind)}
            >
              <option value="BOT">BoBot — determinist</option>
              <option value="AI">Agent AI (nesimulabil în Valul 1)</option>
              <option value="MIXED">Mixt (nesimulabil în Valul 1)</option>
            </select>
          </Field>
        </div>

        <div style={{ marginTop: 14 }}>
          <Field label="Descriere" htmlFor="bb-desc">
            <input
              id="bb-desc"
              className="bo-input"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              maxLength={500}
            />
          </Field>
        </div>

        <div style={{ marginTop: 14 }}>
          <Field
            label="Conținut (JSON)"
            hint="schema_version bo.bobot.v1 · trigger · predicates (logică în 3 valori) · steps tipizați"
            htmlFor="bb-content"
          >
            <textarea
              id="bb-content"
              className="bo-textarea"
              style={{ width: "100%", minHeight: 260 }}
              value={content}
              onChange={(e) => setContent(e.target.value)}
              spellCheck={false}
            />
          </Field>
        </div>

        {errors.length > 0 ? (
          <div style={{ marginTop: 14 }}>
            <InlineAlert kind="danger">
              <ul style={{ margin: 0, paddingLeft: 18 }}>
                {errors.map((e, i) => (
                  <li key={i}>{e}</li>
                ))}
              </ul>
            </InlineAlert>
          </div>
        ) : null}

        <div className="bo-row" style={{ marginTop: 16 }}>
          <button
            type="button"
            className="bo-btn bo-btn--primary"
            onClick={submit}
            disabled={busy || name.trim().length === 0}
            title={name.trim().length === 0 ? "Completează numele" : undefined}
          >
            <IconBO name="check" size={15} /> {busy ? "Se creează…" : "Creează ciorna"}
          </button>
        </div>
      </div>
    </BoPage>
  );
}
