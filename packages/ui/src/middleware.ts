import { NextResponse } from "next/server";
import { auth } from "@/auth";

// Gate every page + non-auth API route. `auth` from NextAuth v5 wraps a
// handler that injects req.auth; here we use it directly as middleware, which
// makes unauthenticated requests redirect to the configured sign-in page.
// A path segment with an invalid percent-escape (e.g. "%E0%A4%A") makes Next's
// dynamic-param decoding throw after this middleware, which a matched dynamic
// route ([id], [slug], [...path]) answers with a bare 500. Refuse it here as the
// client error it is, before routing, uncached like every other gate response.
function malformedPath(pathname: string): boolean {
  return pathname.split("/").some((segment) => {
    try { decodeURIComponent(segment); return false; } catch { return true; }
  });
}

export default auth((req) => {
  if (malformedPath(req.nextUrl.pathname)) {
    const headers = { "Cache-Control": "private, no-store" };
    if (req.nextUrl.pathname.startsWith("/api/")) {
      return Response.json({ error: "malformed_path" }, { status: 400, headers });
    }
    return new Response("Adresa nu este codificată corect (URL invalid).", {
      status: 400, headers: { ...headers, "Content-Type": "text/plain; charset=utf-8" },
    });
  }

  if (req.auth) {
    // Gated pages are per-user and these responses also carry the rolled
    // session cookie: no shared or browser cache may keep them (prerendered
    // routes otherwise go out as s-maxage=31536000). Refusals are marked the
    // same way in auth-denial.ts, which answers before this handler runs.
    const res = NextResponse.next();
    res.headers.set("Cache-Control", "private, no-store");
    return res;
  }

  // For API routes, return JSON 401 instead of an HTML redirect so the
  // browser doesn't follow it and break fetch() callers.
  if (req.nextUrl.pathname.startsWith("/api/")) {
    return Response.json({ error: "unauthorized" }, { status: 401 });
  }

  const signInUrl = new URL("/signin", req.nextUrl.origin);
  signInUrl.searchParams.set("callbackUrl", req.nextUrl.pathname + req.nextUrl.search);
  return Response.redirect(signInUrl);
});

// Exclude Auth.js's own routes, Next internals, static assets, and exactly
// `/signin` (with optional trailing slash). Using `signin/?` rather than the
// looser `signin` keeps unrelated paths like `/signin-help` gated. `fonts/` is
// public/fonts (bo.css), needed by the sign-in page before any session exists.
export const config = {
  matcher: ["/((?!api/auth|_next/static|_next/image|favicon.ico|fonts/|signin/?$).*)"],
};
