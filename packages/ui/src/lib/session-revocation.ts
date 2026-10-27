// C10 per-session revocation, UI side. The backend (A01) owns the durable
// table; this module decides what the UI does with its answers.
//
// - sid: OUR stable session id, minted only at sign-in and carried verbatim
//   through every re-encode (the JWT `jti` is re-minted by @auth/core on
//   every encode, so it cannot be the revocation key).
// - A token without sid predates the contract (legacy). Minting one on decode
//   would hand every replay of a copied cookie a fresh, never-revoked sid, so
//   it is refused instead: the jwt callback returns null, Auth.js clears the
//   cookie and the user signs in again.
// - Only an explicit `200 {"revoked": false}` admits a session. 404 (no
//   contract), any other status, a malformed body, a timeout, a missing sid or
//   a missing service credential all deny.
// - Logout is confirmed only by the contractual 204 (the row is durable before
//   the backend answers). Anything else leaves the session in place and the
//   caller shows an explicit "not confirmed" state with a retry.

export type FetchLike = (input: string, init?: RequestInit) => Promise<Response>;

export interface RevocationAuthority {
  baseUrl: string;
  sharedSecret: string;
  timeoutMs: number;
  fetchImpl: FetchLike;
}

export type RevocationCheck = "active" | "revoked" | "unverifiable";

export interface RevocationConfirmation {
  confirmed: boolean;
  attempts: number;
  // Last upstream status, or "error" for a network failure/timeout, or
  // "not_configured" when no request could be made.
  last: number | "error" | "not_configured";
}

type TokenLike = Record<string, unknown> & { sid?: unknown };

/** jwt callback core: a sid exists only when minted at sign-in. */
export function withSessionId<T extends TokenLike>(
  token: T,
  trigger: string | undefined,
  mint: () => string = () => crypto.randomUUID(),
): T | null {
  if (typeof token.sid === "string" && token.sid) return token;
  if (trigger === "signIn") return { ...token, sid: mint() };
  return null;
}

export function sessionIdOf(session: unknown): string | null {
  const sid = (session as { sid?: unknown } | null)?.sid;
  return typeof sid === "string" && sid ? sid : null;
}

export async function checkSessionRevocation(
  sid: unknown,
  authority: RevocationAuthority,
): Promise<RevocationCheck> {
  if (typeof sid !== "string" || !sid || !authority.sharedSecret) return "unverifiable";
  try {
    const resp = await authority.fetchImpl(
      `${authority.baseUrl}/sessions/revoked/${encodeURIComponent(sid)}`,
      {
        headers: { "x-api-key": authority.sharedSecret },
        signal: AbortSignal.timeout(authority.timeoutMs),
        cache: "no-store",
      },
    );
    if (resp.status !== 200) return "unverifiable";
    const body: unknown = await resp.json();
    const revoked = (body as { revoked?: unknown } | null)?.revoked;
    if (typeof revoked !== "boolean") return "unverifiable";
    return revoked ? "revoked" : "active";
  } catch {
    return "unverifiable";
  }
}

export async function confirmSessionRevocation(
  sid: string,
  email: string | null,
  authority: RevocationAuthority,
  attempts = 2,
): Promise<RevocationConfirmation> {
  if (!authority.sharedSecret) return { confirmed: false, attempts: 0, last: "not_configured" };
  const body = JSON.stringify({ email: email ?? "unknown", jti: sid });
  let last: RevocationConfirmation["last"] = "error";
  for (let attempt = 1; attempt <= attempts; attempt++) {
    try {
      const resp = await authority.fetchImpl(`${authority.baseUrl}/sessions/revoke`, {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-api-key": authority.sharedSecret,
        },
        body,
        signal: AbortSignal.timeout(authority.timeoutMs),
        cache: "no-store",
      });
      last = resp.status;
      // Drain so the connection is reusable; the 204 has no body anyway.
      await resp.arrayBuffer().catch(() => undefined);
      if (resp.status === 204) return { confirmed: true, attempts: attempt, last };
    } catch {
      last = "error";
    }
  }
  return { confirmed: false, attempts, last };
}

const PRIVATE = "private, no-store";
const EXPIRED = "; Path=/; HttpOnly; SameSite=Lax; Expires=Thu, 01 Jan 1970 00:00:00 GMT";

function clearSessionCookies(headers: Headers): void {
  headers.append("set-cookie", "authjs.session-token=" + EXPIRED);
  headers.append("set-cookie", "__Secure-authjs.session-token=" + EXPIRED + "; Secure");
}

// A revoked (or unverifiable) session CANNOT be a 302 to /signin: Auth.js
// rolls a fresh copy of the still-decodable cookie onto the response after
// ours, so /signin would bounce straight back to a gated page (observed:
// ERR_TOO_MANY_REDIRECTS). The refusal is a terminal 401; the expired-cookie
// headers let a client that honours them reach a clean anonymous sign-in.
export function sessionRefusedResponse(request: Request, reason: "revoked" | "unverifiable"): Response {
  const url = new URL(request.url);
  const headers = new Headers({ "cache-control": PRIVATE });
  clearSessionCookies(headers);
  const error = reason === "revoked" ? "session_revoked" : "session_unverifiable";
  if (url.pathname.startsWith("/api/")) {
    return Response.json({ error }, { status: 401, headers });
  }
  headers.set("content-type", "text/html; charset=utf-8");
  const title = reason === "revoked"
    ? "Sesiunea a fost revocată."
    : "Sesiunea nu a putut fi verificată.";
  return new Response(
    `<!doctype html><html lang="ro"><body><h1>${title}</h1>` +
      '<p><a href="/signin">Autentificați-vă din nou</a></p></body></html>',
    { status: 401, headers },
  );
}

// Logout not acknowledged: the session cookie is deliberately kept so the
// user can retry; nothing claims the session ended.
export function logoutUnconfirmedResponse(confirmation: RevocationConfirmation): Response {
  return Response.json(
    { error: "logout_unconfirmed", attempts: confirmation.attempts, last: confirmation.last },
    { status: 503, headers: { "cache-control": PRIVATE } },
  );
}

/** Client side: did the sign-out POST end the session? */
export function signOutOutcome(data: unknown): "signed_out" | "unconfirmed" {
  const d = data as { url?: unknown; error?: unknown } | null | undefined;
  return d && typeof d.url === "string" && d.error === undefined ? "signed_out" : "unconfirmed";
}
