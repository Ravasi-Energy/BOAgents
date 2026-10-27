// Images inside rendered Markdown (artifacts, runs, chat, briefing, skills,
// knowledge, architecture) come from content the app does not control: an
// agent output or a pasted document can embed ![](https://anywhere/pixel?id=…)
// and every viewer's browser would fetch it — a beacon carrying the app's
// Referer and, for a host on the same site, the session cookie.
//
// Policy, decided without `window` so server render and hydration agree:
// - relative paths ("/x.png", "img/x.png") are the app's own files and load,
//   except /api/* (an image must never be a request into the app's API);
// - absolute http(s) URLs load only from origins listed in
//   NEXT_PUBLIC_BO_MARKDOWN_IMAGE_SOURCES, and only over https;
// - every other scheme is refused, data: included (react-markdown's default
//   urlTransform already blanks data: before it reaches the image);
// - an allowed image is fetched lazily, without Referer and without
//   credentials (crossorigin=anonymous: a listed host must answer with CORS);
//   a refused one is never fetched — its alt text and host are shown instead.
// Nothing is proxied through the app (no SSRF surface).

export interface ImagePolicy {
  origins: readonly string[];
  /** Entries that were ignored, for diagnostics; never applied. */
  rejected: readonly string[];
}

export type ImageDecision =
  | { allow: true; url: string; kind: "relative" | "listed" }
  | { allow: false; reason: "empty" | "scheme" | "origin" | "api" | "invalid"; host: string | null };

export function parseImagePolicy(raw: string | undefined): ImagePolicy {
  const origins: string[] = [];
  const rejected: string[] = [];
  for (const entry of (raw ?? "").split(",").map((s) => s.trim()).filter(Boolean)) {
    let url: URL;
    try { url = new URL(entry); } catch { rejected.push(entry); continue; }
    // An origin, nothing more: https, no credentials, no path/query/fragment.
    if (url.protocol !== "https:" || url.username || url.password ||
        url.pathname !== "/" || url.search || url.hash || entry.replace(/\/$/, "") !== url.origin) {
      rejected.push(entry);
      continue;
    }
    if (!origins.includes(url.origin)) origins.push(url.origin);
  }
  return { origins, rejected };
}

const BASE = "https://app.invalid";

export function decideImage(src: unknown, policy: ImagePolicy): ImageDecision {
  if (typeof src !== "string") return { allow: false, reason: "empty", host: null };
  // As the URL parser does: drop tab/CR/LF anywhere and C0/space at the ends,
  // so "/\t/evil" cannot pass as relative and then load as "//evil".
  const raw = src.replace(/[\t\n\r]/g, "").replace(/^[\u0000-\u0020]+|[\u0000-\u0020]+$/g, "");
  if (!raw) return { allow: false, reason: "empty", host: null };
  // Scheme-relative ("//host/x") is absolute; a backslash is treated as a
  // slash by browsers ("/\host"), so both count as absolute here.
  const absolute = /^[a-z][a-z0-9+.-]*:/i.test(raw) || /^[\\/]{2}/.test(raw) || /^\/\\/.test(raw);
  let url: URL;
  try { url = new URL(raw, BASE); } catch { return { allow: false, reason: "invalid", host: null }; }
  if (!absolute) {
    if (url.origin !== BASE) return { allow: false, reason: "invalid", host: null };
    if (url.pathname === "/api" || url.pathname.startsWith("/api/")) return { allow: false, reason: "api", host: null };
    return { allow: true, url: raw, kind: "relative" };
  }
  if (url.protocol !== "https:" && url.protocol !== "http:") return { allow: false, reason: "scheme", host: null };
  if (url.username || url.password) return { allow: false, reason: "invalid", host: url.host };
  if (url.protocol === "https:" && policy.origins.includes(url.origin)) return { allow: true, url: url.href, kind: "listed" };
  return { allow: false, reason: "origin", host: url.host };
}

/** Attributes of an allowed <img>: lazy, no Referer, no credentials, fluid. */
export function allowedImageAttributes(decision: Extract<ImageDecision, { allow: true }>) {
  return {
    src: decision.url,
    loading: "lazy" as const,
    decoding: "async" as const,
    referrerPolicy: "no-referrer" as const,
    // Relative = the app's own file; cross-origin fetches carry no cookies.
    ...(decision.kind === "listed" ? { crossOrigin: "anonymous" as const } : {}),
    style: { maxWidth: "100%", height: "auto" },
  };
}

export function blockedImageLabel(decision: Extract<ImageDecision, { allow: false }>, alt: string): string {
  const what = alt.trim() ? `„${alt.trim()}”` : "fără descriere";
  const where = decision.host ? ` de la ${decision.host}` : "";
  return `Imagine neîncărcată (${what})${where}: sursa nu este permisă.`;
}

export const markdownImagePolicy = parseImagePolicy(process.env.NEXT_PUBLIC_BO_MARKDOWN_IMAGE_SOURCES);
