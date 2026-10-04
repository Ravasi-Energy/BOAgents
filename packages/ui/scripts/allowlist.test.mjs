import assert from "node:assert/strict";
import test from "node:test";
import {
  createRosterLoader,
  decideAllowed,
  decideSessionAction,
  describeDenial,
  normalizeEmail,
  parseAllowedEmails,
  resolveAllowed,
} from "../src/lib/allowlist.ts";

const set = (...emails) => new Set(emails);
const NONE = set();

// --- parseAllowedEmails ----------------------------------------------------

test("parseAllowedEmails trims, lowercases, and drops blanks", () => {
  const parsed = parseAllowedEmails(" A@X.com , b@Y.com ,, ");
  assert.deepEqual([...parsed].sort(), ["a@x.com", "b@y.com"]);
});

test("parseAllowedEmails treats missing and empty config as no entries", () => {
  assert.equal(parseAllowedEmails(undefined).size, 0);
  assert.equal(parseAllowedEmails(null).size, 0);
  assert.equal(parseAllowedEmails("").size, 0);
  assert.equal(parseAllowedEmails(",,  ,").size, 0);
});

test("normalizeEmail lowercases and trims, and survives null", () => {
  assert.equal(normalizeEmail("  Alex@Example.COM "), "alex@example.com");
  assert.equal(normalizeEmail(null), "");
  assert.equal(normalizeEmail(undefined), "");
});

// --- decideAllowed: the authoritative-roster rule ---------------------------

test("BUGHUNT-02 C12: a readable roster revokes an env-listed email it does not hold", () => {
  // The revocation contract: archived/removed Person whose email still sits
  // in ALLOWED_EMAILS. A readable roster decides outright — the env list is
  // only the fallback for an UNREADABLE roster.
  const decision = decideAllowed(
    "operator@corp.com",
    set("operator@corp.com"),
    set("jordan.avery@example.com", "sam.chen@example.com"),
  );
  assert.deepEqual(decision, { allowed: false, source: "no_match", rosterUnknown: false });
});

test("a roster member not in ALLOWED_EMAILS is admitted", () => {
  const decision = decideAllowed("teammate@corp.com", NONE, set("teammate@corp.com"));
  assert.deepEqual(decision, { allowed: true, source: "roster", rosterUnknown: false });
});

test("an email in neither list is denied, with both lists readable", () => {
  const decision = decideAllowed("stranger@evil.com", set("op@corp.com"), set("teammate@corp.com"));
  assert.deepEqual(decision, {
    allowed: false,
    source: "no_match",
    rosterUnknown: false,
  });
});

test("an empty but readable roster revokes rather than failing open", () => {
  const decision = decideAllowed("stranger@evil.com", NONE, NONE);
  assert.deepEqual(decision, {
    allowed: false,
    source: "no_match",
    rosterUnknown: false,
  });
});

test("an unreadable roster does not make an env entry uncertain", () => {
  // rosterUnknown stays false: the decision never consulted the roster, so
  // this is a definite allow, not a fail-open.
  const decision = decideAllowed("operator@corp.com", set("operator@corp.com"), null);
  assert.deepEqual(decision, { allowed: true, source: "env", rosterUnknown: false });
});

test("an unreadable roster leaves a non-env email unknown, not denied outright", () => {
  const decision = decideAllowed("teammate@corp.com", set("operator@corp.com"), null);
  assert.deepEqual(decision, {
    allowed: false,
    source: "env_only_roster_unavailable",
    rosterUnknown: true,
  });
});

test("an email in both lists is attributed to the roster", () => {
  const both = decideAllowed("op@corp.com", set("op@corp.com"), set("op@corp.com"));
  assert.equal(both.source, "roster");
  assert.equal(both.allowed, true);
});

test("matching is case-insensitive against both lists", () => {
  // Env hit is only reachable with the roster unreadable; roster hit is
  // case-insensitive while readable.
  assert.equal(decideAllowed("Alex@Example.COM", set("alex@example.com"), null).allowed, true);
  assert.equal(decideAllowed("Alex@Example.COM", NONE, set("alex@example.com")).allowed, true);
});

test("an empty email is never admitted by an empty-string list entry", () => {
  // parseAllowedEmails drops blanks, but decideAllowed is exported, so guard
  // the case where a caller hands it a hand-built set containing "".
  const decision = decideAllowed("", set(""), null);
  assert.equal(decision.allowed, false);
});

// --- resolveAllowed: roster consulted first ---------------------------------

test("the roster is always consulted, even for an ALLOWED_EMAILS hit", async () => {
  // Revocation only reaches the process through the roster, so an env hit
  // can no longer skip the fetch. A readable empty roster revokes the
  // env-listed email outright.
  let calls = 0;
  const loadRoster = async () => {
    calls += 1;
    return NONE;
  };
  const decision = await resolveAllowed("op@corp.com", set("op@corp.com"), loadRoster);
  assert.deepEqual(decision, { allowed: false, source: "no_match", rosterUnknown: false });
  assert.equal(calls, 1);
});

test("an env hit with an unreadable roster is admitted via env", async () => {
  // The fail-open-for-operators cell: backend down, env list decides.
  const decision = await resolveAllowed("op@corp.com", set("op@corp.com"), async () => null);
  assert.deepEqual(decision, { allowed: true, source: "env", rosterUnknown: false });
});

test("an ALLOWED_EMAILS miss fetches the roster exactly once", async () => {
  let calls = 0;
  const loadRoster = async () => {
    calls += 1;
    return set("teammate@corp.com");
  };
  const decision = await resolveAllowed("teammate@corp.com", NONE, loadRoster);
  assert.equal(decision.source, "roster");
  assert.equal(decision.allowed, true);
  assert.equal(calls, 1);
});

test("resolveAllowed normalizes before matching the env list", async () => {
  const decision = await resolveAllowed("  Op@Corp.com ", set("op@corp.com"), async () => null);
  assert.equal(decision.allowed, true);
});

test("resolveAllowed normalizes before matching the roster too", async () => {
  const decision = await resolveAllowed("  Op@Corp.com ", NONE, async () => set("op@corp.com"));
  assert.deepEqual(decision, { allowed: true, source: "roster", rosterUnknown: false });
});

test("resolveAllowed surfaces an unreadable roster as unknown, not a denial", async () => {
  // The fail-open cell of the table, exercised through the real call path the
  // NextAuth callbacks use rather than only through decideAllowed.
  const decision = await resolveAllowed("teammate@corp.com", set("op@corp.com"), async () => null);
  assert.deepEqual(decision, {
    allowed: false,
    source: "env_only_roster_unavailable",
    rosterUnknown: true,
  });
});

// --- describeDenial ---------------------------------------------------------

test("denial clauses read as audit summaries and match docs/auth.md", () => {
  assert.equal(describeDenial("no_match"), "not in ALLOWED_EMAILS or the people roster");
  assert.equal(
    describeDenial("env_only_roster_unavailable"),
    "not in ALLOWED_EMAILS; people roster unavailable",
  );
});

// --- decideSessionAction: what `authorized` does with a decision -----------

test("a definite allow keeps the session, whichever list matched", () => {
  assert.equal(decideSessionAction({ allowed: true, source: "env", rosterUnknown: false }), "allow");
  assert.equal(decideSessionAction({ allowed: true, source: "roster", rosterUnknown: false }), "allow");
});

test("a definite miss revokes the session", () => {
  assert.equal(
    decideSessionAction({ allowed: false, source: "no_match", rosterUnknown: false }),
    "revoke",
  );
});

test("an unreadable roster keeps an already-vetted session", () => {
  assert.equal(
    decideSessionAction({
      allowed: false,
      source: "env_only_roster_unavailable",
      rosterUnknown: true,
    }),
    "allow_roster_unknown",
  );
});

test("allowed is honored before rosterUnknown, so a member never hits fail-open", () => {
  // Guards the ordering the old code got wrong: it returned true on any
  // roster-fetch error BEFORE consulting the decision, so an evicted user
  // could coast. `allowed` must win first, and an allow must never be
  // reported as a fail-open.
  assert.equal(
    decideSessionAction({ allowed: true, source: "env", rosterUnknown: true }),
    "allow",
  );
});

// --- createRosterLoader -----------------------------------------------------

const okResponse = (rows) => ({ ok: true, status: 200, json: async () => rows });

/** Records every call so header/URL/cache assertions can inspect them. */
const recordingFetch = (responder) => {
  const calls = [];
  const impl = async (url, init) => {
    calls.push({ url, init });
    return responder(calls.length);
  };
  impl.calls = calls;
  return impl;
};

test("the loader hits /auth/allowed-emails with the shared secret and no caching", async () => {
  const fetchImpl = recordingFetch(() => okResponse([{ email: "A@B.com", person_id: 1 }]));
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "s3cret",
    ttlMs: 1000,
    timeoutMs: 1000,
    fetchImpl,
  });
  assert.deepEqual([...(await load())], ["a@b.com"]);
  assert.equal(fetchImpl.calls[0].url, "http://api:8000/auth/allowed-emails");
  assert.equal(fetchImpl.calls[0].init.cache, "no-store");
  assert.equal(fetchImpl.calls[0].init.headers["x-api-key"], "s3cret");
});

test("the loader omits x-api-key when no shared secret is configured", async () => {
  const fetchImpl = recordingFetch(() => okResponse([]));
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 1000,
    timeoutMs: 1000,
    fetchImpl,
  });
  await load();
  assert.equal("x-api-key" in fetchImpl.calls[0].init.headers, false);
});

test("every call revalidates — a cached roster is never served (C12)", async () => {
  // The revocation contract: caching a membership is exactly how a revoked
  // Person would stay admitted for up to a TTL. Sequential calls each
  // fetch, even within the TTL window.
  let clock = 0;
  const fetchImpl = recordingFetch((n) => okResponse([{ email: `p${n}@corp.com`, person_id: n }]));
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 1000,
    timeoutMs: 1000,
    fetchImpl,
    now: () => clock,
  });
  assert.deepEqual([...(await load())], ["p1@corp.com"]);
  clock = 999; // well inside the former TTL window
  assert.deepEqual([...(await load())], ["p2@corp.com"]);
  assert.equal(fetchImpl.calls.length, 2, "revalidated inside the TTL window");
});

test("an HTTP error yields null, warns, and is not cached", async () => {
  const warnings = [];
  const fetchImpl = recordingFetch((n) =>
    n === 1 ? { ok: false, status: 500, json: async () => [] } : okResponse([{ email: "a@b.com", person_id: 1 }]),
  );
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 60_000,
    timeoutMs: 1000,
    fetchImpl,
    now: () => 0,
    onWarn: (m) => warnings.push(m),
  });
  assert.equal(await load(), null);
  assert.equal(warnings.length, 1);
  assert.match(warnings[0], /HTTP 500/);
  // Same instant, well inside the TTL: a failure must not poison the cache.
  assert.deepEqual([...(await load())], ["a@b.com"]);
});

test("a rejecting fetch yields null instead of throwing into NextAuth", async () => {
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 1000,
    timeoutMs: 1000,
    fetchImpl: async () => {
      throw new Error("ECONNREFUSED");
    },
  });
  assert.equal(await load(), null);
});

test("a non-array body yields null rather than an empty roster", async () => {
  // An empty roster revokes; "unparseable" must not masquerade as that.
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 1000,
    timeoutMs: 1000,
    fetchImpl: async () => ({ ok: true, status: 200, json: async () => ({ detail: "nope" }) }),
  });
  assert.equal(await load(), null);
});

test("a row with a null email is skipped without nulling the whole roster", async () => {
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 1000,
    timeoutMs: 1000,
    fetchImpl: async () =>
      okResponse([{ email: null, person_id: 1 }, { email: "Real@Corp.com", person_id: 2 }, {}]),
  });
  assert.deepEqual([...(await load())], ["real@corp.com"]);
});

test("a body that fails to parse as JSON yields null", async () => {
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 1000,
    timeoutMs: 1000,
    fetchImpl: async () => ({
      ok: true,
      status: 200,
      json: async () => {
        throw new SyntaxError("Unexpected token <");
      },
    }),
  });
  assert.equal(await load(), null);
});

test("at most one fetch is ever in flight; sequential calls each revalidate", async () => {
  // Coalescing remains — concurrent callers share one fetch — but a
  // completed result is never reused, so the roster is always fresh.
  let open = 0;
  let peak = 0;
  let n = 0;
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 1000,
    timeoutMs: 1000,
    fetchImpl: async () => {
      open += 1;
      peak = Math.max(peak, open);
      await new Promise((r) => setTimeout(r, 5));
      open -= 1;
      n += 1;
      return { ok: true, status: 200, json: async () => [{ email: `p${n}@corp.com`, person_id: n }] };
    },
  });

  await Promise.all([load(), load(), load()]);
  await Promise.all([load(), load(), load()]);
  assert.equal(peak, 1, "never more than one concurrent backend fetch");
  assert.equal(n, 2, "one fetch per sequential call, one shared per burst");

  assert.deepEqual([...(await load())], ["p3@corp.com"], "each call sees the newest roster");
});

// Companion to the TTL-straddling test above: this is the simple baseline
// (one window, no expiry), that one is the TTL-boundary case. Neither
// subsumes the other — keep both.
test("concurrent misses share one in-flight fetch", async () => {
  let fetches = 0;
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 1000,
    timeoutMs: 1000,
    now: () => 0,
    fetchImpl: async () => {
      fetches += 1;
      await new Promise((r) => setTimeout(r, 5));
      return { ok: true, status: 200, json: async () => [{ email: "a@b.com", person_id: 1 }] };
    },
  });
  const results = await Promise.all([load(), load(), load(), load(), load()]);
  assert.equal(fetches, 1, "five concurrent callers, one backend call");
  for (const r of results) assert.deepEqual([...r], ["a@b.com"]);
});

test("a failed in-flight fetch is not retained, so the next caller retries", async () => {
  let n = 0;
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 60_000,
    timeoutMs: 1000,
    now: () => 0,
    fetchImpl: async () => {
      n += 1;
      if (n === 1) throw new Error("ECONNREFUSED");
      return { ok: true, status: 200, json: async () => [{ email: "a@b.com", person_id: 1 }] };
    },
  });
  assert.equal(await load(), null);
  assert.deepEqual([...(await load())], ["a@b.com"]);
  assert.equal(n, 2);
});

test("ENV_HIT cannot be mutated by a caller into a lockout", () => {
  // Both decision functions return one shared object for an env hit, so an
  // accidental write would poison every later env hit in the process — the
  // #132 lockout class. Frozen, the write throws under ESM strict mode.
  // (Env hits require the roster to be unreadable: `null`, not a set.)
  const env = set("op@corp.com");
  const first = decideAllowed("op@corp.com", env, null);
  assert.equal(Object.isFrozen(first), true);
  assert.throws(() => {
    first.allowed = false;
  }, TypeError);
  assert.equal(decideAllowed("op@corp.com", env, null).allowed, true);
});

test("a hung fetch is abandoned at the timeout instead of pinning joiners", async () => {
  // Regression: callers share one in-flight request, so without a timeout a
  // hung backend blocked every joiner until undici's ~300s default and kept
  // denying roster-only sign-ins long after recovery.
  //
  // The signal is captured and asserted on in TEST scope, not inside the
  // mock: anything thrown inside `fetchImpl` is swallowed by the loader's
  // catch and degrades to `null`, so an in-mock assertion would let the
  // test pass even with no timeout wired up at all.
  let attempts = 0;
  let signal;
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 60_000,
    timeoutMs: 20,
    now: () => 0,
    fetchImpl: async (_url, init) => {
      attempts += 1;
      if (attempts === 1) {
        signal = init.signal;
        // Never resolves on its own — only the abort ends it. The ref'd
        // timer is required: AbortSignal.timeout's own timer is UNREF'd, so
        // without something holding the loop open the test would drain
        // before the abort fires.
        return new Promise((_resolve, reject) => {
          const keepAlive = setTimeout(() => reject(new Error("never aborted")), 5000);
          init.signal.addEventListener("abort", () => {
            clearTimeout(keepAlive);
            reject(init.signal.reason);
          });
        });
      }
      return { ok: true, status: 200, json: async () => [{ email: "a@b.com", person_id: 1 }] };
    },
  });

  assert.equal(await load(), null, "the hung attempt degrades to unknown");
  assert.ok(signal instanceof AbortSignal, "the loader must pass an abort signal");
  assert.equal(signal.aborted, true, "and it must have fired");
  assert.equal(signal.reason?.name, "TimeoutError", "aborted by the timeout, not something else");
  // The failure was not cached, so the recovered backend is reachable at once.
  assert.deepEqual([...(await load())], ["a@b.com"]);
  assert.equal(attempts, 2);
});

test("no roster result is ever reused — the second call sees the newest answer", async () => {
  let n = 0;
  const load = createRosterLoader({
    baseUrl: "http://api:8000",
    sharedSecret: "",
    ttlMs: 60_000,
    timeoutMs: 1000,
    fetchImpl: async () => {
      n += 1;
      return { ok: true, status: 200, json: async () => [{ email: `p${n}@corp.com`, person_id: n }] };
    },
  });
  assert.deepEqual([...(await load())], ["p1@corp.com"]);
  assert.deepEqual([...(await load())], ["p2@corp.com"], "refetched, not replayed");
  assert.equal(n, 2);
});
