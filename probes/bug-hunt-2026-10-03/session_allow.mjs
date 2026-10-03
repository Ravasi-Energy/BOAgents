import assert from "node:assert/strict";
import {
  createRosterLoader,
  decideAllowed,
  decideSessionAction,
} from "../../packages/ui/src/lib/allowlist.ts";

const archived = decideAllowed(
  "plecat@probe.local",
  new Set(["plecat@probe.local"]),
  new Set(),
);
const action = decideSessionAction(archived);
console.log(
  "C12 arhivat dar in ALLOWED_EMAILS allowed=" +
    archived.allowed +
    " source=" +
    archived.source +
    " actiune=" +
    action,
);
const failures = [];
if (action !== "revoke") failures.push("actiune=" + action);

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
      return roster.map((email, index) => ({ email, person_id: index + 1 }));
    },
  }),
});
const first = await load();
roster = [];
now += 60 * 1000;
const duringTtl = await load();
console.log(
  "C12 roster dupa arhivare in TTL fetches=" +
    fetches +
    " inainte=" +
    [...(first ?? [])].length +
    " dupa_1min=" +
    [...(duringTtl ?? [])].length,
);
if (fetches !== 2) failures.push("fetches=" + fetches);
if ([...(duringTtl ?? [])].length !== 0) {
  failures.push("dupa_1min=" + [...(duringTtl ?? [])].length);
}
if (failures.length) {
  throw new Error("C12 contract incalcat: " + failures.join("; "));
}
