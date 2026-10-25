import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { formatBoTime } from "../src/lib/bo-time.ts";

// BO-REST-OBS: /bo/routing showed quality.observed_at.slice(0, 10), the UTC day,
// which is the previous/next day near midnight in the tenant zone.
const lateUtc = "2026-10-24T22:30:00Z";

test("date-only BO stamps use the tenant day, not the UTC day", () => {
  assert.equal(formatBoTime(lateUtc, "Europe/Bucharest", false, true), "25.10.2026");
  assert.equal(formatBoTime(lateUtc, "America/New_York", false, true), "24.10.2026");
  assert.equal(formatBoTime(lateUtc, null, false, true), "24.10.2026 UTC");
});

test("no BO page cuts the UTC day out of a timestamp", () => {
  const root = new URL("../src/app/bo", import.meta.url).pathname;
  const offenders = [];
  const walk = (dir) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) walk(p);
      else if (p.endsWith(".tsx")) {
        fs.readFileSync(p, "utf8").split("\n").forEach((line, i) => {
          if (/_at\??\.slice\(0, ?10\)/.test(line)) offenders.push(`${path.relative(root, p)}:${i + 1}`);
        });
      }
    }
  };
  walk(root);
  assert.deepEqual(offenders, []);
});
