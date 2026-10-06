// CONTROL R5/R6: env bootstrap requires explicit unadministered DTO.
// No completed roster is cached; every authorization revalidates revocation.
export type RosterState = {
  readonly administered: boolean;
  readonly emails: ReadonlySet<string>;
};
export type AllowSource = "env" | "roster" | "no_match" | "roster_unavailable";
export type AllowDecision = {
  readonly allowed: boolean;
  readonly source: AllowSource;
  readonly rosterUnknown: boolean;
};
export function normalizeEmail(email: string | null | undefined): string {
  return (email ?? "").trim().toLowerCase();
}
export function parseAllowedEmails(raw: string | null | undefined): ReadonlySet<string> {
  return new Set((raw ?? "").split(",").map(normalizeEmail).filter(Boolean));
}
/** Reject the entire malformed DTO; absence/error never means bootstrap. */
export function parseRosterDto(body: unknown): RosterState | null {
  if (!body || typeof body !== "object" || Array.isArray(body)) return null;
  const dto = body as Record<string, unknown>;
  if (typeof dto.administered !== "boolean" || !Array.isArray(dto.emails)) return null;
  const emails = new Set<string>();
  for (const row of dto.emails) {
    if (!row || typeof row !== "object" || Array.isArray(row)) return null;
    const entry = row as Record<string, unknown>;
    if (typeof entry.email !== "string" || !normalizeEmail(entry.email) ||
        !Number.isSafeInteger(entry.person_id) || (entry.person_id as number) < 1) return null;
    emails.add(normalizeEmail(entry.email));
  }
  return { administered: dto.administered, emails };
}
export function decideAllowed(email: string, envAllowed: ReadonlySet<string>, roster: RosterState | null): AllowDecision {
  const normalized = normalizeEmail(email);
  if (!roster || typeof roster.administered !== "boolean" || !(roster.emails instanceof Set)) return { allowed: false, source: "roster_unavailable", rosterUnknown: true };
  if (!roster.administered) {
    const allowed = !!normalized && envAllowed.has(normalized);
    return { allowed, source: allowed ? "env" : "no_match", rosterUnknown: false };
  }
  const allowed = !!normalized && roster.emails.has(normalized);
  return { allowed, source: allowed ? "roster" : "no_match", rosterUnknown: false };
}
export async function resolveAllowed(email: string, envAllowed: ReadonlySet<string>, loadRoster: () => Promise<RosterState | null>): Promise<AllowDecision> {
  try { return decideAllowed(email, envAllowed, await loadRoster()); }
  catch { return decideAllowed(email, envAllowed, null); }
}
export type SessionAction = "allow" | "revoke";
export function decideSessionAction(decision: AllowDecision): SessionAction {
  return decision.allowed ? "allow" : "revoke";
}
export function describeDenial(source: AllowSource): string {
  switch (source) {
    case "roster_unavailable": return "people roster unavailable; access denied";
    case "no_match": return "not in the authoritative roster or explicit env bootstrap";
    case "env": case "roster": return "not allowed";
    default: { const unhandled: never = source; return unhandled; }
  }
}

export type RosterLoaderOptions = {
  baseUrl: string;
  sharedSecret: string;
  /**
   * Retained for interface compatibility — the loader no longer serves
   * cached rosters at all (BUGHUNT-02 R-1 / C12): a cached hit could keep
   * an archived Person signed in for up to the TTL, so every call
   * revalidates against the backend. Only concurrent in-flight fetches
   * are coalesced.
   */
  ttlMs: number;
  /**
   * Abandon a roster fetch after this long. Required because callers share one
   * in-flight request: without it, undici only gives up at its default
   * ~300s header timeout, so a hung backend would pin every joiner to the same
   * doomed attempt and keep denying new roster-only sign-ins long after the
   * backend recovered — the opposite of "a failure is not cached".
   */
  timeoutMs: number;
  fetchImpl: typeof fetch;
  now?: () => number;
  onWarn?: (message: string) => void;
};

/**
 * Build a loader for GET /auth/allowed-emails. The in-flight promise lives
 * in the returned closure, so there is one per Next.js server instance.
 *
 * This is called from the `authorized` callback, which the middleware runs
 * on essentially every non-asset request — not just at sign-in. Every call
 * revalidates: NO result is ever served from cache, because a cached
 * membership is exactly how a revoked/archived Person would stay admitted
 * for up to a TTL (BUGHUNT-02 R-1 / C12). What remains is coalescing —
 * concurrent callers share a single in-flight fetch rather than each
 * opening their own — and a completed or failed fetch is never reused.
 *
 * Returns `null` on any failure, which denies login and existing sessions. A failure is NOT retained: the next call
 * retries rather than pinning anyone out after a blip.
 */
export function createRosterLoader(
  opts: RosterLoaderOptions,
): () => Promise<RosterState | null> {
  const { baseUrl, sharedSecret, timeoutMs, fetchImpl } = opts;
  const warn = opts.onWarn ?? (() => {});
  let inFlight: Promise<RosterState | null> | null = null;

  async function fetchRoster(): Promise<RosterState | null> {
    try {
      const headers: Record<string, string> = {};
      if (sharedSecret) headers["x-api-key"] = sharedSecret;
      const res = await fetchImpl(`${baseUrl}/auth/allowed-emails`, {
        headers,
        // Don't let a stale Next.js fetch cache gate access.
        cache: "no-store",
        signal: AbortSignal.timeout(timeoutMs),
      });
      if (!res.ok) {
        warn(
          `[auth] roster fetch failed (HTTP ${res.status}); access denied, ` +
            `all access is denied until the backend confirms authorization`,
        );
        return null;
      }
      const body: unknown = await res.json();
      const roster = parseRosterDto(body);
      if (!roster) warn("[auth] invalid roster DTO; access denied");
      return roster;
    } catch (err) {
      warn(
        `[auth] roster fetch error; access denied, ` +
          `all access is denied until the backend confirms authorization: ${String(err)}`,
      );
      return null;
    }
  }

  return function loadRoster(): Promise<RosterState | null> {
    // Coalesce concurrent callers onto one request. This is what keeps a
    // page load — `authorized` runs per gated request — from fanning out
    // into N backend calls, and it is why at most one fetch is ever in
    // flight. Cleared in `finally`, so neither a success nor a failure is
    // retained: the NEXT call revalidates, which is what makes revoking a
    // Person take effect on their very next request rather than after a
    // cache window.
    if (inFlight) return inFlight;
    const pending = fetchRoster().finally(() => {
      if (inFlight === pending) inFlight = null;
    });
    inFlight = pending;
    return pending;
  };
}
