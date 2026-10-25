import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

import { boErrorDetail } from "../src/lib/bo.ts";

test("a FastAPI validation item renders as 'field: message', never [object Object]", () => {
  const item = { type: "dict_type", loc: ["body", "content"], msg: "Input should be a valid dictionary", input: [1, 2] };
  assert.equal(boErrorDetail([item]), "content: Input should be a valid dictionary");
  assert.equal(boErrorDetail(["steps.0.type: invalid"]), "steps.0.type: invalid");
});

test("the new-bot form renders every 422 item through boErrorDetail", () => {
  const page = fs.readFileSync(new URL("../src/app/bo/bots/nou/page.tsx", import.meta.url), "utf8");
  assert.match(page, /detail\.map\(\(d\) => boErrorDetail\(\[d\]\)\)/);
  assert.doesNotMatch(page, /detail\.map\(String\)/);
});
