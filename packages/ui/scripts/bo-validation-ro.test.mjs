import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

import { boErrorDetail, boValidationReasonRo } from "../src/lib/bo.ts";

const LABELS = { model_id: "Model", capabilities: "Capabilități" };

test("a server 422 item is named in Romanian with the form label, the server message kept alongside", () => {
  const empty = { type: "string_too_short", loc: ["body", "model_id"], msg: "String should have at least 1 character", ctx: { min_length: 1 } };
  assert.equal(boValidationReasonRo([empty], LABELS), "Model: câmp obligatoriu, nu poate fi gol (server: String should have at least 1 character)");
  const many = { type: "too_long", loc: ["body", "capabilities"], msg: "List should have at most 16 items after validation, not 17", ctx: { field_type: "List", max_length: 16, actual_length: 17 } };
  assert.equal(boValidationReasonRo([empty, many], LABELS), "Model: câmp obligatoriu, nu poate fi gol (server: String should have at least 1 character); Capabilități: cel mult 16 elemente (server: List should have at most 16 items after validation, not 17)");
});

test("unknown items, strings and objects fall back to boErrorDetail; boErrorDetail itself is unchanged", () => {
  const odd = { type: "dict_type", loc: ["body", "cost"], msg: "Input should be a valid dictionary" };
  assert.equal(boValidationReasonRo([odd], LABELS), "cost: Input should be a valid dictionary");
  assert.equal(boValidationReasonRo("purpose: referință opacă invalidă"), "purpose: referință opacă invalidă");
  assert.doesNotMatch(boValidationReasonRo({ code: "x" }), /object Object/);
  assert.equal(boErrorDetail([{ type: "string_too_short", loc: ["body", "model_id"], msg: "String should have at least 1 character" }]), "model_id: String should have at least 1 character");
});

test("the catalog form refuses an empty provider/model locally and shows the server refusal through boValidationReasonRo", () => {
  const page = fs.readFileSync(new URL("../src/app/bo/routing/page.tsx", import.meta.url), "utf8");
  const submit = page.slice(page.indexOf("async function submit()"));
  assert(submit.indexOf("!modelId.trim()") > -1 && submit.indexOf("!modelId.trim()") < submit.indexOf("draft = payload()"), "local check runs before the payload is built");
  assert.match(submit, /boValidationReasonRo\(e\.detail, CATALOG_FIELD_LABELS\)/);
  assert.doesNotMatch(submit, /boErrorDetail\(e\.detail\)/);
});
