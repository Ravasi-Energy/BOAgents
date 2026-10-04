// Who may sign in. The backend People roster is AUTHORITATIVE whenever it is
// readable; the operator's ALLOWED_EMAILS env list is the fallback used only
// when the roster cannot be read at all (BUGHUNT-02 R-1 / C12).
//
// The earlier union rule (#132) honored env unconditionally, which made the
// roster unable to revoke: a Person removed or archived stayed signed in
// forever because their email still sat in ALLOWED_EMAILS, and there was no
// server-side lever to end that session. Under the authoritative rule a
// readable roster decides membership outright — an env-listed email absent
// from the roster is denied, and `decideSessionAction` revokes it.
//
// Consequence, stated plainly: on a fresh install the operator's own email
// must exist in the People roster (onboarding seeds it) — ALLOWED_EMAILS
// alone no longer admits anyone while the backend answers.
//
// No imports — not React, not next-auth — so `npm test` can exercise this
// directly under `node --experimental-strip-types` (see
// scripts/allowlist.test.mjs).
//
// The roster loader lives here rather than in its own module even though the
// decision logic below is pure and the loader is the one piece with a socket,
// a clock and mutable state. Splitting them would need the loader to import
// `normalizeEmail` at runtime, and Node's ESM loader requires an explicit
// `.ts` on that relative import while TypeScript rejects it without
// `allowImportingTsExtensions`. That flag does work (it is compatible with
// this project's `noEmit`), so this is a preference, not a hard block: one
// cohesive 260-line module beat adding a compiler option and the only
// `.ts`-suffixed import in the codebase. Revisit if this file grows again.
//
// No imports — not React, not next-auth — so `npm test` can exercise this
// directly under `node --experimental-strip-types` (see
// scripts/allowlist.test.mjs).
//
// The roster loader lives here rather than in its own module even though the
// decision logic below is pure and the loader is the one piece with a socket,
// a clock and mutable state. Splitting them would need the loader to import
// `normalizeEmail` at runtime, and Node's ESM loader requires an explicit
// `.ts` on that relative import while TypeScript rejects it without
// `allowImportingTsExtensions`. That flag does work (it is compatible with
// this project's `noEmit`), so this is a preference, not a hard block: one
// cohesive 260-line module beat adding a compiler option and the only
// `.ts`-suffixed import in the codebase. Revisit if this file grows again.

/** Where an allow/deny decision came from. Recorded in audit-log `details`. */
export type AllowSource =
  /** Matched ALLOWED_EMAILS — only possible while the roster is unreadable. */
  | "env"
  /** Matched the People roster (the authoritative source when readable). */
  | "roster"
  /** Definite miss: the roster was readable and did not hold the email. */
  | "no_match"
  /** Missed ALLOWED_EMAILS; the roster was unreadable, so membership is unknown. */
  | "env_only_roster_unavailable";

// `readonly` + the frozen ENV_HIT below are load-bearing, not decoration:
// both decision functions return that one shared object by reference, so an
// accidental `decision.allowed = false` in a caller would otherwise poison
// every later env hit in the process — the exact #132 lockout class this
// module exists to prevent. Frozen, the stray write throws instead.
export type AllowDecision = {
  readonly allowed: boolean;
  readonly source: AllowSource;
  /** True only when the answer is "no" AND the roster could not be read. */
  readonly rosterUnknown: boolean;
};

export function normalizeEmail(email: string | null | undefined): string {
  return (email ?? "").trim().toLowerCase();
}

/**
 * Parse a comma-separated ALLOWED_EMAILS value. Trims, lowercases, drops
 * blanks — so whitespace around entries and trailing commas are harmless,
 * as docs/auth.md promises.
 */
export function parseAllowedEmails(
  raw: string | null | undefined,
): ReadonlySet<string> {
  return new Set(
    (raw ?? "")
      .split(",")
      .map(normalizeEmail)
      .filter((e) => e.length > 0),
  );
}

/**
 * The one env-match rule, shared by `decideAllowed` and `resolveAllowed` so the
 * two can never drift. The emptiness guard matters because this is exported
 * surface: a caller can hand in a set built by hand rather than by
 * `parseAllowedEmails`, and `""` must never match `""`.
 */
function matchesEnv(normalized: string, envAllowed: ReadonlySet<string>): boolean {
  return normalized.length > 0 && envAllowed.has(normalized);
}

/** The allow decision for an env hit. One object literal, one place, frozen. */
const ENV_HIT: AllowDecision = Object.freeze({
  allowed: true,
  source: "env",
  rosterUnknown: false,
} as const);

/**
 * The authoritative-roster rule, as a pure function. `roster === null` means
 * the roster could not be read — only then does ALLOWED_EMAILS decide. Any
 * readable roster (including an empty one) is authoritative: absence is a
 * definite deny, so a removed/archived Person is revoked even while their
 * email lingers in the env list.
 *
 * Note that an env hit yields `rosterUnknown: false` even though the roster
 * is unreadable by construction in that branch — the decision never
 * consulted the roster, so there is nothing unknown about it.
 */
export function decideAllowed(
  email: string,
  envAllowed: ReadonlySet<string>,
  roster: ReadonlySet<string> | null,
): AllowDecision {
  const normalized = normalizeEmail(email);
  if (roster === null) {
    if (matchesEnv(normalized, envAllowed)) return ENV_HIT;
    return {
      allowed: false,
      source: "env_only_roster_unavailable",
      rosterUnknown: true,
    };
  }
  if (normalized.length > 0 && roster.has(normalized)) {
    return { allowed: true, source: "roster", rosterUnknown: false };
  }
  // Roster readable and it does not hold the email — a definite no. An
  // empty roster lands here too, so it revokes rather than failing open.
  return { allowed: false, source: "no_match", rosterUnknown: false };
}

/**
 * `decideAllowed` with the roster fetched lazily. The roster is ALWAYS
 * consulted first — an env hit can no longer short-circuit, because a
 * readable roster is what carries revocations. The env list only decides
 * when the fetch fails (`roster === null`), which keeps an operator in
 * ALLOWED_EMAILS able to sign in with the backend completely down.
 */
export async function resolveAllowed(
  email: string,
  envAllowed: ReadonlySet<string>,
  loadRoster: () => Promise<ReadonlySet<string> | null>,
): Promise<AllowDecision> {
  const normalized = normalizeEmail(email);
  return decideAllowed(normalized, envAllowed, await loadRoster());
}

/**
 * What the `authorized` callback should do with a decision about an existing
 * session. Split out from auth.ts because auth.ts calls `NextAuth()` at module
 * scope and cannot be imported by the test harness — and this ordering is the
 * security-relevant part: `allowed` is consulted BEFORE `rosterUnknown`, so
 * env and roster members never route through the fail-open branch.
 */
export type SessionAction =
  /** On the allowlist right now. */
  | "allow"
  /** Not on it, but the roster is unreadable — keep the already-vetted session. */
  | "allow_roster_unknown"
  /** Definite miss. Bounce them on this request. */
  | "revoke";

export function decideSessionAction(decision: AllowDecision): SessionAction {
  if (decision.allowed) return "allow";
  if (decision.rosterUnknown) return "allow_roster_unknown";
  return "revoke";
}

/** Human clause for audit summaries. Mirrored in docs/auth.md's Debugging table. */
export function describeDenial(source: AllowSource): string {
  switch (source) {
    case "no_match":
      return "not in ALLOWED_EMAILS or the people roster";
    case "env_only_roster_unavailable":
      return "not in ALLOWED_EMAILS; people roster unavailable";
    case "env":
    case "roster":
      // Unreachable from either callback: these are allow outcomes. The
      // `never` assignment makes adding an AllowSource a build error here
      // rather than a silent fall-through to a generic clause.
      return "not allowed";
    default: {
      const unhandled: never = source;
      return unhandled;
    }
  }
}

// --------------------------------------------------------------------------
// Roster fetching: the one impure part of this module.
// --------------------------------------------------------------------------

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
 * Returns `null` on any failure, which callers read as "roster unknown"
 * (env fallback territory). A failure is NOT retained: the next call
 * retries rather than pinning anyone out after a blip.
 */
export function createRosterLoader(
  opts: RosterLoaderOptions,
): () => Promise<ReadonlySet<string> | null> {
  const { baseUrl, sharedSecret, timeoutMs, fetchImpl } = opts;
  const warn = opts.onWarn ?? (() => {});
  let inFlight: Promise<ReadonlySet<string> | null> | null = null;

  async function fetchRoster(): Promise<ReadonlySet<string> | null> {
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
          `[auth] roster fetch failed (HTTP ${res.status}); ALLOWED_EMAILS still applies, ` +
            `roster-only sign-ins are denied until the backend answers`,
        );
        return null;
      }
      // Parsing stays inside the try: a malformed body degrades to "unknown"
      // rather than throwing into NextAuth's callback.
      const body = (await res.json()) as unknown;
      if (!Array.isArray(body)) {
        warn(
          `[auth] roster fetch returned a non-array body; treating the roster as unavailable`,
        );
        return null;
      }
      const emails: ReadonlySet<string> = new Set(
        body
          // One row with a null email must not null out the whole roster.
          .filter((r): r is { email: string } => typeof r?.email === "string")
          .map((r) => normalizeEmail(r.email))
          .filter((e) => e.length > 0),
      );
      return emails;
    } catch (err) {
      warn(
        `[auth] roster fetch error; ALLOWED_EMAILS still applies, ` +
          `roster-only sign-ins are denied until the backend answers: ${String(err)}`,
      );
      return null;
    }
  }

  return function loadRoster(): Promise<ReadonlySet<string> | null> {
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
