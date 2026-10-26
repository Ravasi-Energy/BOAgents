import { deniedAuthResponse } from "@/lib/auth-denial";
import NextAuth from "next-auth";
import Google from "next-auth/providers/google";
import {
  createRosterLoader,
  decideSessionAction,
  describeDenial,
  parseAllowedEmails,
  resolveAllowed,
} from "@/lib/allowlist";

// Env bootstrap is admitted only after the backend confirms administered:false.
const ENV_ALLOWED = parseAllowedEmails(process.env.ALLOWED_EMAILS);

const BACKEND_BASE = process.env.BACKEND_BASE_URL ?? "http://localhost:8000";
const BACKEND_SHARED_SECRET = process.env.BACKEND_SHARED_SECRET ?? "";

// Interface compatibility only: completed roster responses are never cached.
const ROSTER_TTL_MS = 5 * 60 * 1000;

// Give up on a roster fetch well before undici's ~300s default. Concurrent
// callers share one in-flight request, so an unbounded hang would pin them all
// and keep denying new roster-only sign-ins after the backend recovered.
const ROSTER_FETCH_TIMEOUT_MS = 3000;

// Both NextAuth callbacks are server-side (Node runtime), so the loader's
// cache is per-server-instance.
const loadRoster = createRosterLoader({
  baseUrl: BACKEND_BASE,
  sharedSecret: BACKEND_SHARED_SECRET,
  ttlMs: ROSTER_TTL_MS,
  timeoutMs: ROSTER_FETCH_TIMEOUT_MS,
  // Wrapped rather than passed bare: `fetch` must keep its own receiver.
  fetchImpl: (input, init) => fetch(input, init),
  onWarn: (message) => console.warn(message),
});

// Both login and existing JWT authorization revalidate the current DTO.
export const checkEmailAllowed = (email: string) =>
  resolveAllowed(email, ENV_ALLOWED, loadRoster);

// Fire-and-forget audit call to the backend. Never awaited — auth must never
// block or expose errors due to audit failures.
function auditAuth(
  event_type: string,
  summary: string,
  actor: string | null,
  details: Record<string, unknown>,
): void {
  const headers: Record<string, string> = { "content-type": "application/json" };
  if (BACKEND_SHARED_SECRET) headers["x-api-key"] = BACKEND_SHARED_SECRET;
  fetch(`${BACKEND_BASE}/audit/log`, {
    method: "POST",
    headers,
    body: JSON.stringify({ event_type, summary, actor, details }),
  }).catch(() => {
    // Intentionally swallowed — audit failures must never surface to users.
  });
}

// C10 logout-replay: the signOut event persists the session's jti into
// the backend revocation table, so a token copied before logout is refused
// on every later request — across processes and restarts (server-side row),
// while other sessions of the same user stay valid. Fire-and-forget like
// auditAuth — a backend hiccup must never break logout itself.
// Awaited + retried: a logout is not "revoked" until the durable row is
// written — the backend commits the row before answering 204. Two
// attempts; a final failure is recorded as an explicit audit event (not
// silently declared a success), so the gap is visible and investigable.
async function revokeSession(sid: unknown, email: string | null): Promise<void> {
  if (typeof sid !== "string" || !sid || !BACKEND_SHARED_SECRET) return;
  const body = JSON.stringify({ email: email ?? "unknown", jti: sid });
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      const resp = await fetch(`${BACKEND_BASE}/sessions/revoke`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": BACKEND_SHARED_SECRET,
        },
        body,
        signal: AbortSignal.timeout(3000),
      });
      if (resp.ok) return;
    } catch {
      // fall through to the retry
    }
  }
  auditAuth("auth_logout_revocation_failed",
    `Revocation write failed after retry: ${email ?? "unknown"}`,
    email, { sid_prefix: sid.slice(0, 8) });
}

// Fail-closed revocation check used by the `authorized` callback: the row
// lives in the backend's durable store, so a copied token stays dead
// through both processes' restarts.
async function isSessionRevoked(sid: unknown): Promise<boolean> {
  if (typeof sid !== "string" || !sid || !BACKEND_SHARED_SECRET) return false;
  try {
    const resp = await fetch(`${BACKEND_BASE}/sessions/revoked/${encodeURIComponent(sid)}`, {
      headers: { "x-api-key": BACKEND_SHARED_SECRET },
      signal: AbortSignal.timeout(3000),
    });
    // 404 = the backend predates the revocation contract — the feature is
    // absent there, so there is nothing to enforce (neutral, not a deny).
    // A live backend that errors is different: fail closed.
    if (resp.status === 404) return false;
    if (!resp.ok) return true;
    return (await resp.json())?.revoked === true;
  } catch {
    return true; // fail closed — an unverifiable session is a denied session
  }
}

// A revoked session CANNOT be a 302 to /signin: Auth.js rolls a fresh
// copy of the session cookie onto the response AFTER ours, so the
// still-decodable session makes /signin bounce right back to a gated
// page — an infinite redirect loop (observed: ERR_TOO_MANY_REDIRECTS).
// The refusal is a terminal 401 instead: no redirect, no loop; the
// expired-cookie headers are a best-effort belt-and-suspenders so a
// client that does drop it reaches a clean anonymous sign-in.
function revokedAuthResponse(request: Request): Response {
  const url = new URL(request.url);
  const headers = new Headers({ "cache-control": "private, no-store" });
  const expired =
    "; Path=/; HttpOnly; Expires=Thu, 01 Jan 1970 00:00:00 GMT";
  headers.append("set-cookie", "authjs.session-token=" + expired);
  headers.append("set-cookie", "__Secure-authjs.session-token=" + expired);
  if (url.pathname.startsWith("/api/")) {
    headers.set("content-type", "application/json");
    return Response.json(
      { error: "session_revoked" }, { status: 401, headers });
  }
  headers.set("content-type", "text/html; charset=utf-8");
  return new Response(
    "<!doctype html><html><body><h1>Sesiunea a fost revocată.</h1>" +
      '<p><a href="/signin">Autentificați-vă din nou</a></p></body></html>',
    { status: 401, headers });
}

export const { handlers, auth, signIn, signOut } = NextAuth({
  // @auth/core auto-detects trustHost via `AUTH_URL ?? AUTH_TRUST_HOST ??
  // VERCEL ?? CF_PAGES ?? NODE_ENV !== "production"` — a chain of `??`
  // (nullish coalescing). This repo's own local-dev default sets AUTH_URL
  // to an EMPTY STRING, which is present-but-not-nullish, so it
  // short-circuits that chain to `false` *before* AUTH_TRUST_HOST or the
  // NODE_ENV fallback are ever consulted — exactly the documented local-dev
  // config (AUTH_TRUST_HOST=true, AUTH_URL blank) breaks sign-in.
  //
  // Reimplemented below with an emptiness test instead of `??`, so a blank
  // AUTH_URL can no longer mask AUTH_TRUST_HOST. Deliberately NOT a
  // hardcoded `true`: that would trust the host on any real deployment
  // that leaves AUTH_URL blank, letting a spoofed X-Forwarded-Host drive
  // the OAuth callback/redirect origin. VERCEL/CF_PAGES/NODE_ENV are also
  // deliberately dropped, not just reordered: this app doesn't target
  // those platforms, and a literal `process.env.NODE_ENV` check gets
  // folded to a build-time constant by Next.js's bundler (verified against
  // the compiled output — even reading it off an intermediate variable
  // didn't survive Turbopack's dead-code elimination), so it can't
  // actually reflect the container's runtime NODE_ENV the way @auth/core's
  // own dynamic property access does. This repo's documented local-dev
  // setup already sets AUTH_TRUST_HOST=true explicitly and never relied on
  // that fallback anyway. A deployment that sets neither AUTH_URL nor
  // AUTH_TRUST_HOST gets `false` here — fail-closed, matching intent.
  trustHost:
    Boolean(process.env.AUTH_URL?.trim()) ||
    process.env.AUTH_TRUST_HOST?.trim().toLowerCase() === "true",
  providers: [Google],
  // 24h JWT TTL. Defence in depth alongside the `authorized` re-check
  // below — a session that somehow drifts out of sync with the roster
  // is corrected on next access, but also naturally expires within a
  // day so stale JWTs never coast forever.
  session: { strategy: "jwt", maxAge: 24 * 60 * 60 },
  pages: {
    signIn: "/signin",
    error: "/signin",
  },
  callbacks: {
    // Strict initial gate. Requires `email_verified === true` explicitly: a
    // missing / non-boolean value fails closed. Google always returns true
    // for real accounts.
    signIn: async ({ profile }) => {
      const email = profile?.email?.toLowerCase();
      if (!email) {
        auditAuth("auth_login", "Login denied: no email", null, { denied: true, reason: "no_email" });
        return false;
      }
      if (profile?.email_verified !== true) {
        auditAuth("auth_login", `Login denied: ${email} (email not verified)`, email, { denied: true, reason: "email_not_verified" });
        return false;
      }
      const { allowed, source, rosterUnknown } = await checkEmailAllowed(email);
      if (!allowed) {
        auditAuth(
          "auth_login",
          `Login denied: ${email} (${describeDenial(source)})`,
          email,
          {
            denied: true,
            reason: "not_in_allowlist",
            source,
            roster_unavailable: rosterUnknown,
          },
        );
        return false;
      }
      return true;
    },
    // Every JWT gets a stable per-session id — the revocation key. Assigned
    // once at sign-in, it survives refresh so logout can name exactly this
    // session (not the whole account).
    // sid — OUR session id, distinct from the JWT ``jti`` claim (which
    // @auth/core re-mints on every encode). Assigned once and then carried
    // verbatim through every re-encode/refresh, so revocation names all
    // generations of one session. A token decoded without sid is a legacy
    // session: it migrates explicitly — a sid is minted here once, encoded
    // into the rolled cookie, and enforced from then on (no permanent
    // sid-less bypass).
    jwt: async ({ token }) => {
      if (!token.sid) token.sid = crypto.randomUUID();
      return token;
    },
    session: async ({ session, token }) => {
      // The proxy route stamps this as x-caller-session-sid for the
      // backend's revocation check; it is never exposed to the page.
      if (typeof token.sid === "string") {
        (session as { sid?: string }).sid = token.sid;
      }
      return session;
    },
    // Re-runs on every request gated by the middleware (see middleware.ts).
    // Without this, a user removed from the roster mid-session — or one
    // whose JWT predates the roster being installed — would keep coasting
    // until their JWT expires.
    //
    // Unknown/error also revokes. A previous valid JWT does not bypass this gate.
    authorized: async ({ auth, request }) => {
      if (!auth?.user?.email) return deniedAuthResponse(request);
      const email = auth.user.email.toLowerCase();
      const decision = await checkEmailAllowed(email);
      // Exhaustive on purpose: a `!== "revoke"` test would admit any future
      // SessionAction, i.e. fail open. This way adding one is a build error.
      // C10: a revoked session loses even before it reaches an endpoint —
      // a copied token must not render pages either.
      if (await isSessionRevoked((auth as { sid?: string }).sid)) return revokedAuthResponse(request);
      switch (decideSessionAction(decision)) {
        case "allow":
          return true;
        case "revoke":
          // Fire-and-forget audit so a mid-session eviction leaves a
          // trail even if the user never re-attempts sign-in.
          auditAuth(
            "auth_logout",
            `Session revoked: ${email} (${describeDenial(decision.source)})`,
            email,
            {
              revoked: true,
              reason: "not_in_allowlist",
              source: decision.source,
              // Distinguish an explicit revocation from an unavailable roster.
              roster_unavailable: decision.rosterUnknown,
            },
          );
          return deniedAuthResponse(request);
      }
    },
  },
  events: {
    signIn: ({ user }) => {
      const email = user.email?.toLowerCase() ?? null;
      auditAuth("auth_login", `Login: ${email ?? "unknown"}`, email, { provider: "google" });
    },
    signOut: async (message) => {
      // JWT strategy sends { token }, session strategy sends { session }.
      const token = "token" in message ? message.token : undefined;
      const email = typeof token?.email === "string" ? token.email.toLowerCase() : null;
      auditAuth("auth_logout", `Logout: ${email ?? "unknown"}`, email, {});
      // Revoke exactly this session server-side (C10 logout-replay):
    // awaited, retried, audited on failure — not fire-and-forget.
      await revokeSession(token?.sid, email);
    },
  },
});
