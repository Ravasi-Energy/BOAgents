import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";

import { pilotActionNotice, PILOT_UNCONFIRMED_TEXT } from "../src/lib/bo-pilot.ts";

test("a policy refusal (409) is not presented as a reload-to-fix CAS conflict", () => {
  for (const detail of ["Endpointul nu este în allowlist-ul administrat", "Pilotul cere autoritate Guardian; leagă un mandat existent", "1 efecte cer reconciliere explicită înainte de reluare"]) {
    const t = pilotActionNotice(409, "version_conflict", detail);
    assert.doesNotMatch(t, /reîncarcă datele/, t);
    assert(t.endsWith(detail));
  }
});

test("a real CAS conflict keeps the server instruction to reload", () => {
  assert.match(pilotActionNotice(409, "version_conflict", "Activarea a fost modificată; reîncarcă înainte de salvare"), /reîncarcă înainte de salvare$/);
});

test("other refusals keep the detail, or the code when detail is not text", () => {
  assert.equal(pilotActionNotice(403, "forbidden", "execution:write requires role admin"), "Acțiune refuzată: execution:write requires role admin");
  assert.equal(pilotActionNotice(422, "invalid_value", [{ msg: "x" }]), "Acțiune refuzată: invalid_value");
});

test("the pilot page uses the shared notice for every refused action", () => {
  const page = fs.readFileSync(new URL("../src/app/bo/pilot/page.tsx", import.meta.url), "utf8");
  assert.match(page, /pilotActionNotice\(err\.status, err\.code, err\.detail\) : PILOT_UNCONFIRMED_TEXT/);
  assert.doesNotMatch(page, /reîncarcă datele înainte de reluare/);
  assert.match(PILOT_UNCONFIRMED_TEXT, /^Cererea nu a fost confirmată/);
});

test("pilot observation times follow the tenant timezone (BoTime), not raw UTC ISO", () => {
  const page = fs.readFileSync(new URL("../src/app/bo/pilot/page.tsx", import.meta.url), "utf8");
  assert.match(page, /<BoTime iso=\{run\.observation\.observedAt\} \/>/);
  assert.match(page, /<BoTime iso=\{run\.observation\.service\.oldestPendingAt\} \/>/);
  assert.doesNotMatch(page, /\{run\.observation\?\.observedAt \?\? "Nemăsurat"\}/);
});
