import { moneyCurrency, requiredExecutionBudgetInput, moneyInputValue, executionBudgetValue } from "./money-format.ts";
export type RunSubmission = { mandate_id: string; steps: Record<string, unknown>[]; budget_amount: string; budget_currency: string|null; correlation_id: string };
/** Keep an uncertain submission's correlation and intent intact. No automatic new run. */
export function replayPayload(previous: RunSubmission | null, next: RunSubmission): RunSubmission {
  requiredExecutionBudgetInput(moneyInputValue(next.budget_amount),next.budget_currency,false);
  const canonical = (value: unknown): unknown => Array.isArray(value) ? value.map(canonical) : value && typeof value === 'object' ? Object.fromEntries(Object.entries(value).sort(([a],[b])=>a.localeCompare(b)).map(([k,v])=>[k,canonical(v)])) : value;
  if (previous && JSON.stringify(canonical(previous)) !== JSON.stringify(canonical(next))) throw new Error('Draftul a fost modificat după o trimitere neconfirmată. Verifică execuția existentă înainte de a începe o operație nouă; nu se retrimite alt payload sub aceeași corelare.');
  return next;
}

/** Uncertain intent survives reload only in this browser tab's authenticated-user scope. */
export function parseRunDraft(raw: string): RunSubmission {
  const value: unknown = JSON.parse(raw);
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('Draftul de execuție păstrat este invalid.');
  const v = value as Partial<RunSubmission>;
  if(v.budget_currency!==null&&(typeof v.budget_currency!=="string"||moneyCurrency(v.budget_currency)!==v.budget_currency))throw new Error("Draftul păstrat nu are valută explicită validă. Verifică execuția existentă; draftul original nu a fost modificat.");
  if (typeof v.mandate_id !== 'string' || !v.mandate_id || typeof v.correlation_id !== 'string' || !v.correlation_id || v.correlation_id.length>80 || typeof v.budget_amount !== 'string' || !/^-?\d+(?:\.\d{1,1000})?$/.test(v.budget_amount) || !Array.isArray(v.steps) || !v.steps.length || !v.steps.every(s=>s && typeof s==='object' && !Array.isArray(s))) throw new Error('Draftul de execuție păstrat este invalid.');
  // Legacy unknown currency is diagnostic only; submit/replay separately requires currency.
  if(v.budget_currency===null)executionBudgetValue(moneyInputValue(v.budget_amount));
  else requiredExecutionBudgetInput(moneyInputValue(v.budget_amount),v.budget_currency,false);
  return {mandate_id:v.mandate_id,correlation_id:v.correlation_id,budget_amount:v.budget_amount,budget_currency:v.budget_currency,steps:v.steps};
}
