// BUGHUNT-02 C12 — vendored from the coordinator probe
// (coordonare/rapoarte/coordonator/BUGHUNT-BOAGENTS-20261003/reproduced/session_allow.mjs).
// Runs under `npm test` (node --test on Node 22); assertions identical to the
// coordinator harness, only wrapped in test() and re-pathed to ../src/lib.
import assert from "node:assert/strict";
import test from "node:test";
import {
  createRosterLoader,
  decideAllowed,
  decideSessionAction,
} from "../src/lib/allowlist.ts";

test("C12 archived user in ALLOWED_EMAILS is revoked (authoritative roster)", () => {
  const archived = decideAllowed(
    "plecat@probe.local",
    new Set(["plecat@probe.local"]),
    { administered: true, emails: new Set() },
  );
  const action = decideSessionAction(archived);
  assert.equal(action, "revoke");
});

test("C12 roster loader revalidates inside TTL (revocation is not cached)", async () => {
  let fetches = 0;
  let roster = ["activ@probe.local"];
  let now = 1_000_000;
  const load = createRosterLoader({
    baseUrl: "http://probe.invalid",
    sharedSecret: "service-secret-probe",
    ttlMs: 5 * 60 * 1000,
    timeoutMs: 1000,
    now: () => now,
    fetchImpl: async () => ({
      ok: true,
      json: async () => {
        fetches += 1;
        return { administered: true, emails: roster.map((email, index) => ({ email, person_id: index + 1 })) };
      },
    }),
  });
  const first = await load();
  roster = [];
  now += 60 * 1000;
  const duringTtl = await load();
  assert.equal(fetches, 2);
  assert.equal(first.administered, true);
  assert.equal(first.emails.size, 1);
  assert.equal(duringTtl.administered, true);
  assert.equal(duringTtl.emails.size, 0);
});
