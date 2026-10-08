// CONTROL F5 / B-R-8 — already-issued session revocation.
//
// The production `authorized` callback (src/auth.ts) runs, on EVERY
// middleware-gated request: resolveAllowed(email, ENV_ALLOWED, loadRoster)
// → decideSessionAction → allow | deniedAuthResponse(request). A JWT issued
// earlier is only decoded by Auth.js — the roster gate decides whether it
// still counts. This probe drives that exact chain against a real local
// HTTP roster server whose answers change mid-session: identity allowed at
// "issue" time, revoked afterwards → the still-valid token is refused.
import assert from "node:assert/strict";
import { createServer } from "node:http";
import test from "node:test";
import {
  createRosterLoader,
  decideSessionAction,
  resolveAllowed,
} from "../src/lib/allowlist.ts";
import { deniedAuthResponse } from "../src/lib/auth-denial.ts";

const EMAIL = "agent@probe.local";
const API_REQUEST = new Request("https://app.test/api/chat", {
  method: "POST",
});
const PAGE_REQUEST = new Request("https://app.test/settings");

function startRoster(state) {
  // state: { status: "ok"|"fail", administered: bool, emails: [..] }
  const server = createServer((req, res) => {
    assert.equal(req.url, "/auth/allowed-emails");
    if (state.status !== "ok") {
      res.writeHead(500).end("boom");
      return;
    }
    res.writeHead(200, { "content-type": "application/json" }).end(
      JSON.stringify({
        administered: state.administered,
        emails: state.emails.map((email, i) => ({
          email,
          person_id: i + 1,
        })),
      }),
    );
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () =>
      resolve({
        url: `http://127.0.0.1:${server.address().port}`,
        close: () => server.close(),
      }),
    );
  });
}

// Mirrors src/auth.ts::authorized — the exact composition, no shortcuts.
async function authorizedLike(email, envAllowed, loadRoster, request) {
  const decision = await resolveAllowed(email, envAllowed, loadRoster);
  switch (decideSessionAction(decision)) {
    case "allow":
      return true;
    case "revoke":
      return deniedAuthResponse(request);
  }
}

test("B-R-8: token issued under env bootstrap is revoked once the roster lands without the user", async () => {
  const state = { status: "ok", administered: false, emails: [] };
  const roster = await startRoster(state);
  try {
    const envAllowed = new Set([EMAIL]);
    const loadRoster = createRosterLoader({
      baseUrl: roster.url,
      sharedSecret: "probe",
      ttlMs: 0, // never serve a stale roster — revalidate every check
      timeoutMs: 2000,
      fetchImpl: (input, init) => fetch(input, init),
      onWarn: () => {},
    });

    // "Issued": env bootstrap admits while the roster is not administered.
    assert.equal(
      await authorizedLike(EMAIL, envAllowed, loadRoster, PAGE_REQUEST),
      true,
    );

    // Roster administration begins; the user's person row is archived →
    // the same still-valid JWT must be refused on the very next request.
    state.administered = true;
    state.emails = ["alt@probe.local"];
    const denied = await authorizedLike(EMAIL, envAllowed, loadRoster, API_REQUEST);
    assert.ok(denied instanceof Response, "expected a denial Response");
    assert.equal(denied.status, 401);

    const pageDenied = await authorizedLike(EMAIL, envAllowed, loadRoster, PAGE_REQUEST);
    assert.ok(pageDenied instanceof Response);
    assert.equal(pageDenied.status, 302);
    assert.ok(pageDenied.headers.get("location").startsWith("https://app.test/signin"));

    // User re-added to the roster → the same email is admitted again
    // (proves the denial was a roster decision, not a cached/pinned one).
    state.emails = [EMAIL];
    assert.equal(
      await authorizedLike(EMAIL, envAllowed, loadRoster, PAGE_REQUEST),
      true,
    );
  } finally {
    roster.close();
  }
});

test("B-R-8: roster outage revokes rather than coasting on the issued token", async () => {
  const state = { status: "ok", administered: true, emails: [EMAIL] };
  const roster = await startRoster(state);
  try {
    const loadRoster = createRosterLoader({
      baseUrl: roster.url,
      sharedSecret: "probe",
      ttlMs: 0,
      timeoutMs: 2000,
      fetchImpl: (input, init) => fetch(input, init),
      onWarn: () => {},
    });
    assert.equal(
      await authorizedLike(EMAIL, new Set(), loadRoster, PAGE_REQUEST),
      true,
    );
    // Backend goes down mid-session: fail closed, never coast.
    state.status = "fail";
    const denied = await authorizedLike(EMAIL, new Set(), loadRoster, API_REQUEST);
    assert.equal(denied.status, 401);
  } finally {
    roster.close();
  }
});
