// Breadcrumb chain for the app shell. Only a top-level section (segment[0])
// that is a real route is linked — intermediate segments like `runs` in
// `/jobs/runs/<id>` or `session` in `/audit/session/<id>` are not navigable
// pages, so they render as plain text to avoid linking to a 404. `/bo` is a
// path prefix for the BOAgents pages, not a page itself. Dynamic segments
// (slugs/uuids) also render as-is — pages own their own H1 with the entity name.
const NON_PAGE_SECTIONS = new Set(["bo"]);

export function breadcrumbCrumbs(segments: string[]): { segment: string; href: string | null }[] {
  return segments.map((segment, idx) => ({
    segment,
    href: idx === 0 && !NON_PAGE_SECTIONS.has(segment) ? "/" + segment : null,
  }));
}
