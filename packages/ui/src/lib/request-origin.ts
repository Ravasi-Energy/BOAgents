// CSRF gate for cookie-authenticated mutations. SameSite=Lax keeps the session
// cookie off cross-SITE posts only: another origin on the same site (another
// port or subdomain) still sends it with a plain form or no-cors fetch, which
// needs no CORS preflight. Browsers mark every such request with Sec-Fetch-Site
// and/or Origin, so a mutation is refused unless they say it came from this app.
// A request with neither header is not browser-originated (server tooling,
// tests): it carries no ambient browser session, so it is not a CSRF vector.
const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

export function mutationOriginRefusal(method: string, headers: Headers, ownOrigins: readonly string[]): string | null {
  if (SAFE_METHODS.has(method.toUpperCase())) return null;
  const site = headers.get("sec-fetch-site");
  if (site) return site === "same-origin" ? null : `sec-fetch-site:${site}`;
  const origin = headers.get("origin");
  if (origin) return ownOrigins.includes(origin) ? null : `origin:${origin}`;
  return null;
}

// The app's own origins: the one the request reached and the configured public
// one (AUTH_URL), since behind a reverse proxy they can differ.
export function ownOrigins(requestOrigin: string, authUrl: string | undefined): string[] {
  const origins = [requestOrigin];
  try { if (authUrl?.trim()) origins.push(new URL(authUrl.trim()).origin); } catch { /* invalid AUTH_URL adds nothing */ }
  return origins;
}
