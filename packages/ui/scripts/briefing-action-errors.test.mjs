import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

const src = fs.readFileSync(new URL("../src/components/Briefing.tsx", import.meta.url), "utf8");

test("every reverted Briefing card action shows its failure, not only the console", () => {
  const failures = [...src.matchAll(/console\.error\("([^"]+) failed", e\);/g)].map((m) => m[1]);
  assert.deepEqual(failures.sort(), ["Alert review", "Approve", "Approve-with-edits", "Bulk dismiss", "Dismiss", "Reopen"]);
  for (const what of failures) {
    assert.match(src, new RegExp(`console\\.error\\("${what} failed", e\\);\\s*reportActionError\\("${what}", e\\);`), `${what} failure is console-only`);
  }
});

test("the action failure is rendered as an alert", () => {
  assert.match(src, /\{actionError && \(\s*<div role="alert"[^>]*>\s*\{actionError\}/);
});
