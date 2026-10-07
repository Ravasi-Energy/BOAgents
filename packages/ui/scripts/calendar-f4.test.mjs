import test from "node:test";
import assert from "node:assert/strict";
import {formatRunAt} from "../src/lib/calendar.ts";
const cases = [
 ["2026-03-30T21:30:00Z","31.03.2026, 00:30:00"],
 ["2026-03-28T23:30:00Z","29.03.2026, 01:30:00"],
 ["2026-03-29T01:30:00Z","29.03.2026, 04:30:00"],
 ["2026-10-25T00:30:00Z","25.10.2026, 03:30:00"],
 ["2026-10-25T01:30:00Z","25.10.2026, 03:30:00"],
 ["2026-01-31T22:30:00Z","01.02.2026, 00:30:00"]
];
for (const [instant,display] of cases) test(`F4 local midnight/DST ${instant}`,()=>{
 assert.equal(formatRunAt(instant,"Europe/Bucharest",Date.parse(instant)).absolute, display+" (Europe/Bucharest)");
 assert.equal(formatRunAt(instant,"Europe/Bucharest",Date.parse(instant)).relative,"in 0m");
});
test("F4 catalog zone readback is authoritative",()=>{
 assert.match(formatRunAt("2026-03-30T21:30:00Z","UTC").absolute,/30.03.2026, 21:30:00 \(UTC\)/);
 assert.match(formatRunAt("2026-03-30T21:30:00Z",null).absolute,/fus orar necitit/);
 assert.match(formatRunAt("2026-03-30T21:30:00Z","not-a-zone").absolute,/fus orar invalid/);
});
test("F4 naive legacy timestamp remains explicit; no browser-zone interpretation",()=>{
 assert.deepEqual(formatRunAt("2026-03-31T00:30:00"),{absolute:"2026-03-31T00:30:00 — fus orar absent",relative:""});
 assert.match(formatRunAt("not-a-time").absolute,/absent/);
});
