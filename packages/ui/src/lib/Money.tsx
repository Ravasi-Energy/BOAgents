"use client";
import { useState, useSyncExternalStore } from "react";
import { formatMoney } from "./money-format";
const KEY = "bo.money.decimals";
const EVENT = "bo-money-format-change";
function read(): 0 | 2 | "storage-error" {
  try { return window.localStorage.getItem(KEY) === "0" ? 0 : 2; } catch { return "storage-error"; }
}
function subscribe(callback: () => void) {
  window.addEventListener("storage", callback);
  window.addEventListener(EVENT, callback);
  return () => { window.removeEventListener("storage", callback); window.removeEventListener(EVENT, callback); };
}
function usePreference() { return useSyncExternalStore(subscribe, read, () => 2 as const); }
function useDecimals() { const value = usePreference(); return value === "storage-error" ? 2 : value; }
export function Money({ amount, currency }: { amount: unknown; currency: unknown }) {
  const decimals = useDecimals();
  try { return <>{currency == null || currency === "" ? <>{formatMoney(amount, "XXX", decimals).replace(/ XXX$/, "")} — valută neconfigurată</> : formatMoney(amount, currency, decimals)}</>; }
  catch (error) { return <span role="status">{error instanceof Error ? error.message : "Sumă invalidă"}</span>; }
}
export function UnconfiguredMoney({ amount }: { amount: number }) {
  const decimals = useDecimals();
  return <span>{formatMoney(amount, "XXX", decimals).replace(/ XXX$/, "")} — valută neconfigurată</span>;
}
export function MoneySettings() {
  const preference = usePreference();
  const decimals = preference === "storage-error" ? 2 : preference;
  const [error, setError] = useState("");
  return <section aria-label="Afișarea sumelor">
    <h2>Afișarea sumelor</h2>
    <label>Zecimale <select value={decimals} onChange={event => {
      try {
        window.localStorage.setItem(KEY, event.target.value);
        window.dispatchEvent(new Event(EVENT));
        setError("");
      } catch { setError("Preferința nu a putut fi salvată în browser."); }
    }}><option value="0">0 — 6.000 lei</option><option value="2">2 — 6.000,00 lei</option></select></label>
    <p>Preferință pentru acest browser, aplicată imediat tuturor sumelor. Valuta provine din date; nu se face conversie valutară.</p>
    {(error || preference === "storage-error") && <p role="alert">{error || "Preferința nu a putut fi citită din browser. Se afișează 2 zecimale."}</p>}
  </section>;
}
