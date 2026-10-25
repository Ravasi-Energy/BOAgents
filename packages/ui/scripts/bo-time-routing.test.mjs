import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";

import { formatBoTime } from "../src/lib/bo-time.ts";
import { boErrorDetail } from "../src/lib/bo.ts";

const iso = "2026-10-24T14:11:18.217Z";

test("BO timestamps follow the tenant zone, not the browser zone", () => {
  assert.equal(formatBoTime(iso, "Asia/Tokyo"), "24.10.2026, 23:11:18");
  assert.equal(formatBoTime(iso, "Europe/Bucharest"), "24.10.2026, 17:11:18");
  assert.equal(formatBoTime(iso, "Asia/Tokyo", true), "23:11:18");
});

test("without a readable tenant zone the original UTC is shown and labelled", () => {
  assert.equal(formatBoTime(iso, null), "24.10.2026, 14:11:18 UTC");
  assert.equal(formatBoTime(iso, "Not/AZone"), "24.10.2026, 14:11:18 UTC");
});

test("no BO page formats a timestamp with the browser zone or raw UTC slices", () => {
  const root = new URL("../src/app/bo", import.meta.url).pathname;
  const offenders = [];
  const walk = (dir) => {
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) walk(p);
      else if (p.endsWith(".tsx")) {
        fs.readFileSync(p, "utf8").split("\n").forEach((line, i) => {
          if (/toLocaleString\(|toLocaleTimeString\(|_at\.slice\(\d+, 19\)/.test(line)) offenders.push(`${path.relative(root, p)}:${i + 1}`);
        });
      }
    }
  };
  walk(root);
  assert.deepEqual(offenders, []);
  assert.match(fs.readFileSync(path.join(root, "layout.tsx"), "utf8"), /CalendarTimezoneProvider/);
});

test("FastAPI validation details are readable, never [object Object]", () => {
  const detail = [{ type: "string_too_short", loc: ["body", "model_id"], msg: "String should have at least 1 character", input: "" }];
  assert.equal(boErrorDetail(detail), "model_id: String should have at least 1 character");
  assert.equal(boErrorDetail("purpose: referință opacă invalidă"), "purpose: referință opacă invalidă");
  assert.doesNotMatch(boErrorDetail({ code: "x" }), /object Object/);
});
