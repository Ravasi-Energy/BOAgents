// Typed client for the /bo surface (BOAgents Valul 1). Errors carry the HTTP
// status and the machine-readable `error` code so pages can render the
// required states (forbidden, conflict, invalid, not_found) instead of a
// generic failure.

const BO_BASE = "/api/backend/bo";

export class BoApiError extends Error {
  status: number;
  code: string;
  detail?: unknown;

  constructor(status: number, code: string, detail?: unknown) {
    super(`${status} ${code}`);
    this.status = status;
    this.code = code;
    this.detail = detail;
  }
}

export async function req<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${BO_BASE}${path}`, init);
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const code =
      body && typeof body === "object" && "error" in body
        ? String((body as { error: unknown }).error)
        : "http_error";
    throw new BoApiError(res.status, code, (body as { detail?: unknown })?.detail);
  }
  return body as T;
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

export interface BoSetting {
  key: string;
  schema_version: string;
  type: "text" | "enum" | "integer" | "timezone" | "boolean";
  default: unknown;
  apply_mode: "IMMEDIATE" | "NEW_RUN" | "RESTART" | "MIGRATION";
  scope: string;
  page: string;
  tab: string;
  label_ro: string;
  label_en: string;
  help_ro: string;
  edit_role: string;
  sensitivity: string;
  effect_ro: string;
  value: unknown;
  origin: "tenant" | "default";
  version: number;
  updated_by: string | null;
  updated_at: string | null;
}

export interface BoSettingsResponse {
  tenant: string;
  config_version: number;
  settings: BoSetting[];
  role: "admin" | "operator" | "viewer";
}

export function getBoSettings(): Promise<BoSettingsResponse> {
  return req("/settings");
}

export function putBoSetting(
  key: string,
  value: unknown,
  expectedVersion: number,
): Promise<{ result: string; applied: boolean; setting: BoSetting }> {
  return req(`/settings/${encodeURIComponent(key)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ value, expected_version: expectedVersion }),
  });
}

// ---------------------------------------------------------------------------
// BoBots
// ---------------------------------------------------------------------------

export interface BoBot {
  id: string;
  tenant: string;
  name: string;
  description: string;
  kind: "BOT" | "AI" | "MIXED";
  owner: string;
  scope: string;
  status: "draft" | "active";
  draft_version: number;
  active_version_no: number | null;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface BoBotVersion {
  id: string;
  definition_id: string;
  version_no: number;
  schema_version: string;
  hash: string;
  status: "draft" | "published";
  created_by: string;
  created_at: string;
  content?: Record<string, unknown>;
}

export interface BoBotDetail {
  bot: BoBot;
  versions: BoBotVersion[];
  draft: BoBotVersion;
  active: BoBotVersion | null;
}

export interface BoStepRun {
  idx: number;
  step_id: string;
  step_type: string;
  status: "SIMULATED" | "SKIPPED" | string;
  ts: string;
  detail: Record<string, unknown> | null;
  receipt: { effect_status?: string; note?: string } | null;
}

export interface BoRunResult {
  execution_status: "SUCCEEDED" | "PARTIAL" | "FAILED" | string;
  predicate_result: "TRUE" | "FALSE" | "UNKNOWN" | "NOT_EVALUATED";
  gated_by_predicate: boolean;
  step_count: number;
  steps_evaluated: number;
  step_limit: number;
  capped_by_step_limit: boolean;
  findings: {
    finding_key: string;
    category: string;
    severity: string;
    message: string;
    effect_status: string;
  }[];
  simulation: boolean;
  external_effects: number;
}

export interface BoRun {
  id: string;
  tenant: string;
  definition_id: string;
  version_id: string;
  version_no: number;
  version_hash: string;
  kind: string;
  status: string;
  input: Record<string, unknown> | null;
  config_version: number;
  config_snapshot: Record<string, unknown> | null;
  plan_hash: string;
  result: BoRunResult | null;
  error: string | null;
  created_by: string;
  started_at: string;
  finished_at: string | null;
  timeline?: BoStepRun[];
}

export function listBoBots(): Promise<{ bots: BoBot[] }> {
  return req("/bots");
}

export function getBoBot(id: string): Promise<BoBotDetail> {
  return req(`/bots/${id}`);
}

export function createBoBot(payload: {
  name: string;
  kind?: string;
  description?: string;
  content: Record<string, unknown>;
}): Promise<{ bot: BoBot }> {
  return req("/bots", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function createExampleBoBot(): Promise<{ bot: BoBot }> {
  return req("/bots/example", { method: "POST" });
}

export function patchBoBot(
  id: string,
  body: {
    expected_version: number;
    name?: string;
    description?: string;
    content?: Record<string, unknown>;
  },
): Promise<{ bot: BoBot }> {
  return req(`/bots/${id}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function publishBoBot(
  id: string,
): Promise<{ bot: BoBot; result: string; active_version_no: number }> {
  return req(`/bots/${id}/publish`, { method: "POST" });
}

export function simulateBoBot(
  id: string,
  body: { input?: Record<string, unknown>; version_no?: number; draft?: boolean },
): Promise<{ run: BoRun }> {
  return req(`/bots/${id}/simulate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export function listBoRuns(id: string): Promise<{ runs: BoRun[] }> {
  return req(`/bots/${id}/runs`);
}

export function getBoRun(runId: string): Promise<{ run: BoRun }> {
  return req(`/runs/${runId}`);
}

export interface BoTelemetryStatus {
  enabled: boolean;
  transport: string;
  emitted: number;
  dropped: number;
  rejected: number;
  schema_version: string;
  note: string;
}

export function getBoTelemetryStatus(): Promise<BoTelemetryStatus> {
  return req("/telemetry/status");
}

// ---------------------------------------------------------------------------
// Signed packages (bo.package.v1) — VAL2-01. Import is disabled by default and
// every response carries the verifier verdict; rejected imports stay auditable.
// ---------------------------------------------------------------------------

export interface BoPackageVerdict {
  schemaVersion: "bo.package.verdict.v1";
  verdict: "ACCEPT" | "REJECT";
  reasons: string[];
  packageId: string;
  version: string;
  manifestDigest: string;
  artifactSetDigest: string;
  tenantRef: string;
  publisherId: string;
  keyId: string;
  policyVersion: string;
  trustVersion: string;
  checkedAt: string;
  expiresAt: string;
  idempotent: boolean;
  approvalRef?: string;
}

export interface BoPackageImport {
  id: string;
  tenant: string;
  package_id: string;
  version: string;
  kind: string;
  publisher_id: string;
  key_id: string;
  manifest_digest: string;
  artifact_set_digest: string;
  status: "QUARANTINED" | "DRAFT" | "REJECTED";
  verdict: BoPackageVerdict;
  source_path: string;
  stored_path: string | null;
  approval_id: string | null;
  actor: string;
  created_at: string;
  updated_at: string;
}

export interface BoPackageApproval {
  id: string;
  tenant: string;
  package_id: string;
  from_version: string;
  to_version: string;
  artifact_set_digest: string;
  status: "active" | "revoked";
  expires_at: string;
  consumed_at: string | null;
  consumed_by_import: string | null;
  created_by: string;
  created_at: string;
}

export function listBoPackageImports(): Promise<{ imports: BoPackageImport[] }> {
  return req("/packages");
}

export function getBoPackageImport(
  id: string,
): Promise<BoPackageImport & { idempotent?: boolean }> {
  return req(`/packages/${id}`);
}

export function importBoPackage(
  sourceDir: string,
): Promise<BoPackageImport & { verdict: BoPackageVerdict; idempotent: boolean }> {
  return req("/packages/import", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ source_dir: sourceDir }),
  });
}

export function promoteBoPackage(id: string): Promise<BoPackageImport> {
  return req(`/packages/${id}/promote`, { method: "POST" });
}

export function listBoPackageApprovals(): Promise<{
  approvals: BoPackageApproval[];
}> {
  return req("/packages-approvals");
}

export function createBoPackageApproval(payload: {
  package_id: string;
  from_version: string;
  to_version: string;
  artifact_set_digest: string;
  expires_at: string;
}): Promise<BoPackageApproval> {
  return req("/packages-approvals", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function revokeBoPackageApproval(id: string): Promise<{ ok: boolean }> {
  return req(`/packages-approvals/${id}/revoke`, { method: "POST" });
}

// ---------------------------------------------------------------------------
// Routing (VAL3-01) — administered catalog + observe-mode observations
// ---------------------------------------------------------------------------

export interface BoModelCost {
  input_per_million: string | null;
  output_per_million: string | null;
  currency: string | null;
  valid_until: string | null;
}

export interface BoModelQuality {
  score: number | null;
  methodology: string;
  task_kind: string;
  eval_set_ref: string;
  eval_set_version: string;
  observed_at: string;
  sample_count: number;
}

export interface BoCatalogEntry {
  entry_id: string;
  provider: string;
  model_id: string;
  model_version: string | null;
  state: "ACTIVE" | "DISABLED" | "DEPRECATED";
  capabilities: string[];
  regions: string[];
  cost: BoModelCost;
  quality: BoModelQuality | null;
  purpose: string;
  source: string;
  version: number;
  updated_by: string | null;
  updated_at: string | null;
}

export interface BoRouteChoice {
  provider: string;
  modelId: string;
  modelVersion: string | null;
}

export interface BoRouteObservation {
  obs_id: string;
  occurred_at: string;
  correlation_id: string;
  task_kind: string;
  actor_ref: string;
  policy_version: string;
  catalog_version: string;
  decision: "ROUTE" | "REFUSE";
  met_bar: boolean;
  reasons: string[];
  recommendation: BoRouteChoice | null;
  actual_route: BoRouteChoice | null;
  cost_estimate: { amount: string; currency: string; validUntil: string } | null;
  measured: Record<string, number> | null;
  billed: Record<string, unknown> | null;
  detail: {
    candidates: {
      ref: BoRouteChoice;
      eligible: boolean;
      reason: string | null;
      estimated_cost: string | null;
      score: number | null;
    }[];
  };
  /** 0 = în așteptare, 1 = livrat, 2 = eșuat definitiv (cap de tentative). */
  delivered: number;
  delivery_error: string | null;
  event_id: string | null;
}

export interface BoRoutingStatus {
  observe_enabled: boolean;
  mode: string;
  catalog_version: string;
  total: number;
  pending_delivery: number;
  dead_delivery: number;
  met_bar: number;
  last_at: string | null;
  outbox_total: number;
  outbox_pending: number;
  outbox_dead: number;
  outbox_attempts: number;
  outbox_last_error: string | null;
  delivery: {
    interval_s: number;
    batch_size: number;
    max_attempts: number;
  };
  note: string;
}

export function getBoRoutingCatalog(): Promise<{
  catalog_version: string;
  entries: BoCatalogEntry[];
  role: string;
}> {
  return req("/routing/catalog");
}

export function createBoCatalogEntry(payload: {
  provider: string;
  model_id: string;
  model_version?: string | null;
  state?: string;
  capabilities?: string[];
  regions?: string[];
  cost?: Partial<BoModelCost>;
  quality?: Partial<BoModelQuality> | null;
  purpose: string;
  source: string;
}): Promise<{ entry: BoCatalogEntry }> {
  return req("/routing/catalog", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function updateBoCatalogEntry(
  id: string,
  payload: {
    provider: string;
    model_id: string;
    model_version?: string | null;
    state?: string;
    capabilities?: string[];
    regions?: string[];
    cost?: Partial<BoModelCost>;
    quality?: Partial<BoModelQuality> | null;
    purpose: string;
    source: string;
    expected_version: number;
  },
): Promise<{ entry: BoCatalogEntry }> {
  return req(`/routing/catalog/${id}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function listBoRouteObservations(params?: {
  decision?: string;
  task_kind?: string;
  met_bar?: boolean;
  limit?: number;
}): Promise<{ observations: BoRouteObservation[] }> {
  const q = new URLSearchParams();
  if (params?.decision) q.set("decision", params.decision);
  if (params?.task_kind) q.set("task_kind", params.task_kind);
  if (params?.met_bar !== undefined) q.set("met_bar", String(params.met_bar));
  if (params?.limit) q.set("limit", String(params.limit));
  const qs = q.toString();
  return req(`/routing/observations${qs ? `?${qs}` : ""}`);
}

export function getBoRoutingStatus(): Promise<BoRoutingStatus> {
  return req("/routing/status");
}

export function flushBoRoutingObservations(): Promise<{
  sent: number;
  failed: number;
}> {
  return req("/routing/flush", { method: "POST" });
}

// ---------------------------------------------------------------------------
// Delegated execution (VAL4-01)
// ---------------------------------------------------------------------------

export interface BoMandate {
  mandate_id: string;
  parent_mandate_id: string | null;
  principal_ref: string;
  depth: number;
  allowed_resources: string[];
  allowed_actions: string[];
  budget_limit: string;
  concurrency_limit: number;
  max_steps: number;
  max_depth: number;
  expires_at: string;
  policy_version: number;
  state: string;
  revoked_at: string | null;
  revoked_reason: string | null;
  guardian_ref: string | null;
  created_by: string;
  created_at: string;
}

export interface BoGuardianInfo {
  bound_ref: string | null;
  chain_refs: string[];
  endpoint_configured: boolean;
  credential_configured: boolean;
  auth_required: boolean;
  policy_layer: boolean;
}

export interface BoAuthorityCheck {
  authorized: boolean;
  mode: "guardian" | "standalone" | "denied" | "unavailable";
  kind: string | null;
  detail: string | null;
  guardian: BoGuardianInfo;
}

export interface BoExecRun {
  run_id: string;
  mandate_id: string;
  parent_run_id: string | null;
  state: string;
  steps: { action: string; resource: string; payload?: Record<string, unknown> }[];
  current_step: number;
  budget_reserved: string;
  concurrency_slots: number;
  policy_version: number;
  correlation_id: string;
  lease_owner: string | null;
  lease_until: string | null;
  pause_requested: boolean;
  cancel_requested: boolean;
  block_reason: string | null;
  created_by: string;
  created_at: string;
  updated_at: string;
  finished_at: string | null;
}

export interface BoCheckpoint {
  step: number;
  checkpoint_version: number;
  state: Record<string, unknown>;
  payload_digest: string;
  created_at: string;
}

export interface BoLedgerEntry {
  entry_id: string;
  run_id: string;
  step: number;
  intent_ref: string;
  idempotency_key: string;
  payload_digest: string;
  provider: string;
  action: string;
  resource: string;
  status: string;
  receipt_ref: string | null;
  receipt: Record<string, unknown> | null;
  fence_version: number;
  attempts: number;
  policy_version: number;
  correlation_id: string;
  submitted_at: string | null;
  finalized_at: string | null;
  created_at: string;
}

export interface BoRunDetail {
  run: BoExecRun;
  kind: "execution";
  mandate: BoMandate;
  chain: BoMandate[];
  children: BoExecRun[];
  checkpoints: BoCheckpoint[];
  ledger: BoLedgerEntry[];
  reservation: {
    amount: string;
    slots: number;
    state: string;
  } | null;
  guardian: BoGuardianInfo;
  limits_note: string;
}

export interface BoExecStatus {
  enabled: boolean;
  runs_total: number;
  by_state: Record<string, number>;
  synthetic_effect_total: number;
  guardian: BoGuardianInfo;
  limits: Record<string, unknown>;
  note: string;
}

export function getBoMandates(): Promise<{ mandates: BoMandate[] }> {
  return req("/execution/mandates");
}

export function createBoMandate(payload: {
  parent_mandate_id?: string;
  guardian_ref?: string;
  allowed_resources: string[];
  allowed_actions: string[];
  budget_limit: string;
  concurrency_limit: number;
  max_steps: number;
  max_depth: number;
  expires_at: string;
}): Promise<{ mandate: BoMandate }> {
  return req("/execution/mandates", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function revokeBoMandate(
  id: string,
  reason: string,
): Promise<{ mandate: BoMandate }> {
  return req(`/execution/mandates/${id}/revoke`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ reason }),
  });
}

export function listBoExecRuns(state?: string): Promise<{ runs: BoExecRun[] }> {
  return req(`/execution/runs${state ? `?state=${state}` : ""}`);
}

export function submitBoRun(payload: {
  mandate_id: string;
  steps: Record<string, unknown>[];
  budget_amount: string;
  parent_run_id?: string;
}): Promise<{ run: BoExecRun }> {
  return req("/execution/runs", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
}

export function getBoRunDetail(runId: string): Promise<BoRunDetail> {
  return req(`/execution/runs/${runId}`);
}

export function pauseBoRun(
  runId: string,
  reason?: string,
): Promise<{ run: BoExecRun }> {
  return req(`/execution/runs/${runId}/pause`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(reason ? { reason } : {}),
  });
}

export function cancelBoRun(
  runId: string,
  reason: string,
): Promise<{ run: BoExecRun }> {
  return req(`/execution/runs/${runId}/cancel`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ reason }),
  });
}

export function getBoRunAuthority(runId: string): Promise<BoAuthorityCheck> {
  return req(`/execution/runs/${runId}/authority`);
}

export function resumeBoRun(runId: string): Promise<{ run: BoExecRun }> {
  return req(`/execution/runs/${runId}/resume`, { method: "POST" });
}

export function reconcileBoRun(
  runId: string,
  resolution: "receipt" | "mark_failed",
): Promise<{ run: BoExecRun; resolved: number; pending: number }> {
  return req(`/execution/runs/${runId}/reconcile`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ resolution }),
  });
}

export function workBoRuns(workerId?: string): Promise<{
  worker_id: string;
  claimed: number;
  outcomes: { run_id: string; state: string; block_reason?: string }[];
}> {
  return req("/execution/work", {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(workerId ? { worker_id: workerId } : {}),
  });
}

// Postura telemetriei unei rulări pilot, raportată de GET /bo/pilot. Nu este
// starea execuției: receiptul rămâne dovada efectului; aici urmărim doar
// dacă observația persistată a ajuns în outboxul durabil.
export interface BoRunTelemetry {
  status: "ok" | "pending" | "degraded" | "dead" | "incident" | "unavailable" | "none";
  marker: string | null;
  error: string | null;
  expected: number;
  queued: number;
  delivered: number;
  dead: number;
  missing: number;
  replayable: boolean;
}

export function replayBoRunTelemetry(runId: string): Promise<{
  run_id: string;
  enqueued: number;
  existing: number;
  telemetry: BoRunTelemetry;
}> {
  return req(`/pilot/runs/${encodeURIComponent(runId)}/telemetry/replay`, {
    method: "POST",
  });
}

export interface BoOutboxEntry {
  event_id: string;
  kind: string;
  ref_id: string | null;
  event_type: string | null;
  schema_version: string | null;
  envelope: Record<string, unknown>;
  created_at: string;
  attempts: number;
  series_attempts: number;
  retry_history: { series: number; attempts_before: number; last_error: string | null;
    reason: string; actor: string; created_at: string }[];
  delivered: number; // 0 pending · 1 livrat · 2 dead-letter
  last_error: string | null;
  lease_owner: string | null;
  lease_until: string | null;
}

export function listBoOutbox(
  delivered?: number,
): Promise<{ entries: BoOutboxEntry[]; stats: Record<string, number> }> {
  return req(
    `/execution/outbox${delivered !== undefined ? `?delivered=${delivered}` : ""}`,
  );
}

export function retryBoOutbox(
  eventId: string,
  reason: string,
): Promise<{ event_id: string; requeued: boolean }> {
  return req(`/execution/outbox/${encodeURIComponent(eventId)}/retry`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ reason }),
  });
}

export function getBoExecStatus(): Promise<BoExecStatus> {
  return req("/execution/status");
}
