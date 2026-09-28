"use client";

// Shared primitives for the BO surfaces. All styling flows from the
// `.bo-scope` token classes in styles/bo.css — no hardcoded values here.
import type { ReactNode } from "react";

import IconBO, { type BoIconName } from "@/components/bo/IconBO";

export function BoPage({
  title,
  sub,
  actions,
  children,
}: {
  title: string;
  sub?: string;
  actions?: ReactNode;
  children: ReactNode;
}) {
  return (
    <main className="bo-scope flex-1 min-h-0 overflow-y-auto">
      <div className="bo-page">
        <div className="bo-page-head">
          <div>
            <h1 className="bo-title">{title}</h1>
            {sub ? <p className="bo-sub">{sub}</p> : null}
          </div>
          {actions ? <div className="bo-row">{actions}</div> : null}
        </div>
        {children}
      </div>
    </main>
  );
}

type PillKind = "ok" | "warn" | "danger" | "info" | "neutral";

export function Pill({
  kind = "neutral",
  icon,
  children,
}: {
  kind?: PillKind;
  icon?: BoIconName;
  children: ReactNode;
}) {
  return (
    <span className={`bo-pill bo-pill--${kind}`}>
      {icon ? <IconBO name={icon} size={13} /> : null}
      {children}
    </span>
  );
}

export function InlineAlert({
  kind = "info",
  icon,
  children,
}: {
  kind?: "info" | "warn" | "danger";
  icon?: BoIconName;
  children: ReactNode;
}) {
  const defaultIcon: BoIconName =
    kind === "danger" ? "alert" : kind === "warn" ? "shield-alert" : "info";
  // danger = eroare acționabilă → alert (asertiv); restul → status (polite).
  return (
    <div
      className={`bo-alert bo-alert--${kind}`}
      role={kind === "danger" ? "alert" : "status"}
    >
      <IconBO name={icon ?? defaultIcon} size={16} />
      <div>{children}</div>
    </div>
  );
}

export function Field({
  label,
  hint,
  htmlFor,
  children,
}: {
  label: string;
  hint?: string;
  htmlFor?: string;
  children: ReactNode;
}) {
  return (
    <div className="bo-field">
      <label className="bo-label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {hint ? <p className="bo-hint">{hint}</p> : null}
    </div>
  );
}

// Required explicit states (DESIGN.md): loading / empty / error / forbidden /
// unconfigured — never a bare spinner and never colour alone.
export function StateBlock({
  state,
  title,
  detail,
  action,
}: {
  state: "loading" | "empty" | "error" | "forbidden" | "unconfigured";
  title: string;
  detail?: string;
  action?: ReactNode;
}) {
  if (state === "loading") {
    return (
      <div className="bo-card" aria-busy="true" aria-label={title}>
        <div className="bo-skeleton" style={{ width: "40%" }} />
        <div className="bo-skeleton" style={{ width: "80%", marginTop: 12 }} />
        <div className="bo-skeleton" style={{ width: "60%", marginTop: 8 }} />
      </div>
    );
  }
  const icon: BoIconName =
    state === "forbidden"
      ? "shield-alert"
      : state === "error"
        ? "alert"
        : state === "unconfigured"
          ? "settings"
          : "info";
  return (
    <div className="bo-empty">
      <div className="bo-row" style={{ justifyContent: "center" }}>
        <IconBO name={icon} size={18} />
        <strong>{title}</strong>
      </div>
      {detail ? <p style={{ marginTop: 8 }}>{detail}</p> : null}
      {action ? <div style={{ marginTop: 16 }}>{action}</div> : null}
    </div>
  );
}
