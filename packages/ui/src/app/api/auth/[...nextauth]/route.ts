import { NextRequest } from "next/server";
import { auditAuth, auth, handlers, revocationAuthority } from "@/auth";
import { mutationOriginRefusal, ownOrigins } from "@/lib/request-origin";
import {
  confirmSessionRevocation,
  logoutUnconfirmedResponse,
  sessionIdOf,
} from "@/lib/session-revocation";

export const { GET } = handlers;

// C10: every sign-out (UserBadge, Auth.js's own /api/auth/signout page) is a
// POST here. The session is revoked server-side first and Auth.js may clear
// the cookie only after the backend's contractual 204 — otherwise a copied
// cookie would outlive a logout the user believes succeeded. Without the ACK
// the cookie stays, the answer is 503 logout_unconfirmed and the UI offers a
// retry. A session without sid is a legacy token: the jwt callback already
// refuses it everywhere, so there is nothing left to revoke.
export async function POST(req: NextRequest): Promise<Response> {
  if (!req.nextUrl.pathname.endsWith("/signout")) return handlers.POST(req);
  if (mutationOriginRefusal(req.method, req.headers, ownOrigins(req.nextUrl.origin, process.env.AUTH_URL))) {
    return Response.json({ error: "csrf_origin_refused" }, { status: 403, headers: { "cache-control": "private, no-store" } });
  }
  const session = await auth();
  const sid = sessionIdOf(session);
  if (sid) {
    const email = session?.user?.email?.toLowerCase() ?? null;
    const confirmation = await confirmSessionRevocation(sid, email, revocationAuthority);
    if (!confirmation.confirmed) {
      auditAuth("auth_logout_revocation_failed",
        `Logout not confirmed (session kept, user told to retry): ${email ?? "unknown"}`,
        email, { attempts: confirmation.attempts, last: confirmation.last });
      return logoutUnconfirmedResponse(confirmation);
    }
  }
  return handlers.POST(req);
}
