import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { breadcrumbCrumbs, segmentText } from "../src/lib/breadcrumb.ts";

// Every breadcrumb link must land on a real page. /bo has no page of its own,
// so "bo" in /bo/bots, /bo/runs/<id>, … must render as text, not as a 404 link.
const app = new URL("../src/app", import.meta.url).pathname;

function pageRoutes(dir = app, prefix = "") {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    if (!entry.isDirectory()) continue;
    if (entry.name === "api" || entry.name.startsWith("_")) continue;
    const seg = entry.name.startsWith("(") ? "" : "/" + entry.name;
    const sub = path.join(dir, entry.name);
    if (fs.existsSync(path.join(sub, "page.tsx"))) out.push(prefix + seg);
    out.push(...pageRoutes(sub, prefix + seg));
  }
  return out;
}

const routes = pageRoutes();
const isPage = (href) =>
  routes.some((r) => new RegExp("^" + r.replace(/\[[^\]]+\]/g, "[^/]+") + "$").test(href));

test("every linked breadcrumb points at an existing page", () => {
  assert(routes.includes("/bo/bots") && routes.includes("/settings/bo"));
  for (const route of routes) {
    const concrete = route.replace(/\[[^\]]+\]/g, "x1");
    const crumbs = breadcrumbCrumbs(concrete.split("/").filter(Boolean));
    for (const c of crumbs) if (c.href) assert(isPage(c.href), `${route} links ${c.href}, which has no page`);
  }
});

test("BOAgents section crumb is text, existing sections stay links", () => {
  assert.equal(breadcrumbCrumbs(["bo", "bots", "bot_1"])[0].href, null);
  assert.equal(breadcrumbCrumbs(["bo", "runs", "run_1"])[0].href, null);
  assert.equal(breadcrumbCrumbs(["settings", "bo"])[0].href, "/settings");
  assert.equal(breadcrumbCrumbs(["jobs", "runs", "r1"])[0].href, "/jobs");
  assert.equal(breadcrumbCrumbs(["jobs", "runs", "r1"])[1].href, null);
});

test("dynamic segments are shown decoded, never double-encoded", () => {
  assert.equal(segmentText("alert%3A1"), "alert:1");
  assert.equal(segmentText("Raport%20Q3%20%E2%80%94%20%C8%99"), "Raport Q3 — ș");
  assert.equal(segmentText("%3Cimg%20src%3Dx%3E"), "<img src=x>", "decoded text; React escapes it on render");
  assert.equal(segmentText("100%"), "100%", "malformed escape stays as typed");
  assert.equal(segmentText("%E0%A4%A"), "%E0%A4%A");
  assert.equal(segmentText("run_1"), "run_1");
  const shell = fs.readFileSync(new URL("../src/components/shell/AppShell.tsx", import.meta.url), "utf8");
  assert.match(shell, /SEGMENT_LABELS\[segment\] \?\? segmentText\(segment\)/);
  assert.doesNotMatch(shell, /dangerouslySetInnerHTML/);
});
