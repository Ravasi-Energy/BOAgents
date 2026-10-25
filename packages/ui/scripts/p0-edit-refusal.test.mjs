import assert from "node:assert/strict";
import test from "node:test";

import { P0EditError, settingPatch } from "../src/lib/p0-edit.ts";

// A local edit refusal must reach the settings card as its own message (the
// card shows P0EditError text), never as the generic "Salvarea a eșuat.
// Încearcă din nou." that reads like a transport failure worth retrying.
const trust = "bo.packages.trust_store_json";
const refused = (fn, re) => assert.throws(fn, (e) => e instanceof P0EditError && re.test(e.message));

test("malformed trust store JSON is a named local refusal", () => {
  refused(() => settingPatch(trust, "{bo01", { value: "", version: 3 }), /JSON invalid/);
});

test("non-object trust store document is a named local refusal", () => {
  refused(() => settingPatch(trust, "[]", { value: "", version: 3 }), /obiect obligatoriu/);
});

test("unchanged trust store document is a named local refusal", () => {
  refused(() => settingPatch(trust, '{"version":"1"}', { value: '{"version":"1"}', version: 3 }), /Nicio modificare/);
});

test("missing read version is a named local refusal", () => {
  refused(() => settingPatch("bo.router.allowed_providers", "a", { value: "", version: undefined }), /Versiunea citită lipsește/);
});

test("valid edits still produce the sparse patch", () => {
  assert.deepEqual(settingPatch(trust, '{"version":"2"}', { value: '{"version":"1"}', version: 3 }), { value: '{"version":"2"}', expected_version: 3 });
});
