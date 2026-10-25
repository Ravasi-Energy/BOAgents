import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

import { submitOutcome, UNCERTAIN_SUBMIT_TEXT } from "../src/lib/execution-replay.ts";

test("no HTTP answer or 5xx keeps the submission uncertain for same-correlation replay", () => {
  for (const s of [null, 500, 502, 503, 504]) assert.equal(submitOutcome(s), "uncertain", String(s));
});

test("a 4xx is a definitive refusal: nothing is kept as uncertain", () => {
  for (const s of [400, 403, 404, 409, 422]) assert.equal(submitOutcome(s), "refused", String(s));
});

test("the submit form drops a refused draft and explains a network failure in Romanian", () => {
  const page = fs.readFileSync(new URL("../src/app/bo/executions/page.tsx", import.meta.url), "utf8");
  assert.match(page, /submitOutcome\(status\) === "refused"[\s\S]{0,120}sessionStorage\.removeItem\(storageKey\)/);
  assert.match(page, /status === null \? UNCERTAIN_SUBMIT_TEXT/);
  assert.match(UNCERTAIN_SUBMIT_TEXT, /^Trimitere neconfirmată păstrată/);
});
