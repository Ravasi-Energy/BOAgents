import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

import { createCustomWorkflow } from "../src/lib/api.ts";

const def = { name: "bo01_x", title: "X", description: "", section: "Operating Cadence", estimated_minutes: 1.5, input_fields: [], steps: [], cadence: null, cadence_person_id: null };

test("a custom-workflow shape 422 renders as 'field: message', never [object Object]", async () => {
  const saved = globalThis.fetch;
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: [{ type: "int_from_float", loc: ["body", "estimated_minutes"], msg: "Input should be a valid integer, got a number with a fractional part", input: 1.5 }] }), { status: 422 });
  try {
    await assert.rejects(createCustomWorkflow(def), (e) => {
      assert.equal(e.message, "estimated_minutes: Input should be a valid integer, got a number with a fractional part");
      return true;
    });
    globalThis.fetch = async () => new Response(JSON.stringify({ detail: ["title must not be empty", "workflow must have at least one step"] }), { status: 422 });
    await assert.rejects(createCustomWorkflow(def), { message: "title must not be empty; workflow must have at least one step" });
  } finally {
    globalThis.fetch = saved;
  }
});

test("jobs list delete handlers surface a failed delete instead of an unhandled rejection", () => {
  const page = fs.readFileSync(new URL("../src/app/jobs/page.tsx", import.meta.url), "utf8");
  for (const call of ["deleteWorkflowRun(runId)", "deleteCustomWorkflow(name)"]) {
    const at = page.indexOf(`await ${call}`);
    assert(at > 0, call);
    const block = page.slice(page.lastIndexOf("try {", at), page.indexOf("if (failure) setError(failure);", at));
    assert.match(block, /try \{\s*await /, `${call} not guarded`);
    assert.match(block, /await refresh\(\);\s*$/, `${call}: refresh must run before the error is shown`);
  }
});
