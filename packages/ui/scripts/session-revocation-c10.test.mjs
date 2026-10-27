// C10 UI side: legacy (sid-less) tokens, revocation authority answers and the
// logout acknowledgement. The cookies go through the REAL @auth/core session
// action (decode -> our jwt callback -> encode), and the authority is a real
// local HTTP server speaking the A01 contract — no mock assumes a stable jti.
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import test from 'node:test';
import { Auth } from '@auth/core';
import { decode, encode } from '@auth/core/jwt';
import {
  checkSessionRevocation,
  confirmSessionRevocation,
  logoutUnconfirmedResponse,
  sessionIdOf,
  sessionRefusedResponse,
  signOutOutcome,
  withSessionId,
} from '../src/lib/session-revocation.ts';

const SECRET = 'c10-test-secret-not-a-real-credential-0123456789';
const COOKIE = 'authjs.session-token';
const ORIGIN = 'http://127.0.0.1:3999';

const config = {
  secret: SECRET,
  trustHost: true,
  basePath: '/api/auth', // as next-auth configures it
  providers: [],
  session: { strategy: 'jwt', maxAge: 24 * 60 * 60 },
  callbacks: {
    jwt: async ({ token, trigger }) => withSessionId(token, trigger),
    session: async ({ session, token }) => {
      if (typeof token.sid === 'string') session.sid = token.sid;
      return session;
    },
  },
};

const mint = (token) => encode({ token, secret: SECRET, salt: COOKIE });
const open = (jwt) => decode({ token: jwt, secret: SECRET, salt: COOKIE });

// One /api/auth/session round trip through the real Auth.js session action.
async function refresh(cookie) {
  const res = await Auth(new Request(ORIGIN + '/api/auth/session', { headers: { cookie: `${COOKIE}=${cookie}` } }), config);
  const set = res.headers.getSetCookie().find((c) => c.startsWith(COOKIE + '='));
  const value = set ? set.split(';')[0].slice(COOKIE.length + 1) : null;
  return { body: await res.json(), set, value };
}

const user = { email: 'operator@bo01.invalid', name: 'Op' };

test('jwt callback: sid minted only at sign-in, kept verbatim, legacy refused', () => {
  const minted = withSessionId({ ...user }, 'signIn', () => 'sid-1');
  assert.equal(minted.sid, 'sid-1');
  assert.equal(withSessionId(minted, undefined).sid, 'sid-1');
  assert.equal(withSessionId(minted, 'update').sid, 'sid-1');
  for (const trigger of [undefined, 'update']) assert.equal(withSessionId({ ...user }, trigger), null);
  assert.equal(withSessionId({ ...user, sid: '' }, undefined), null);
  assert.equal(withSessionId({ ...user, sid: 42 }, undefined), null);
});

test('legacy cookie copied: two independent decodes are both refused and the cookie is cleared, no sid minted', async () => {
  const legacy = await mint({ ...user, sub: 'u1' });
  for (let replay = 0; replay < 2; replay++) {
    const r = await refresh(legacy);
    assert.equal(r.body, null, 'legacy session must not be admitted');
    assert.match(r.set, /Max-Age=0/, 'cookie must be cleared');
    assert.equal(r.value, '', 'no replacement cookie (no fresh sid) is issued');
  }
});

test('modern session: cookie0 -> refresh -> cookie1 keep one sid while @auth/core re-mints jti', async () => {
  const cookie0 = await mint(withSessionId({ ...user, sub: 'u1' }, 'signIn'));
  const r1 = await refresh(cookie0);
  assert.ok(r1.value && r1.value !== cookie0, 'refresh rolls a new cookie');
  const r2 = await refresh(r1.value);
  const [t0, t1, t2] = await Promise.all([open(cookie0), open(r1.value), open(r2.value)]);
  assert.equal(t1.sid, t0.sid); assert.equal(t2.sid, t0.sid);
  assert.notEqual(t1.jti, t0.jti, 'jti is re-minted on encode — not usable as the key');
  assert.equal(r1.body.sid, t0.sid, 'session exposes the sid the proxy stamps');
  assert.equal(sessionIdOf(r1.body), t0.sid);
  // Two sign-ins of the same user are two independent sessions.
  const other = await open(await mint(withSessionId({ ...user, sub: 'u1' }, 'signIn')));
  assert.notEqual(other.sid, t0.sid);
});

// Local stand-in for the A01 authority; `mode` decides each answer.
async function authority(handler) {
  const seen = [];
  const server = createServer((req, res) => {
    let body = '';
    req.on('data', (c) => { body += c; });
    req.on('end', () => { seen.push({ method: req.method, url: req.url, key: req.headers['x-api-key'], body }); handler(req, res, body); });
  });
  await new Promise((ok) => server.listen(0, '127.0.0.1', ok));
  const baseUrl = `http://127.0.0.1:${server.address().port}`;
  return {
    seen,
    close: () => new Promise((ok) => { server.closeAllConnections(); server.close(ok); }),
    auth: (extra = {}) => ({ baseUrl, sharedSecret: 'svc', timeoutMs: 300, fetchImpl: (u, i) => fetch(u, i), ...extra }),
  };
}

const json = (res, status, value) => res.writeHead(status, { 'content-type': 'application/json' }).end(typeof value === 'string' ? value : JSON.stringify(value));

test('revocation check: only an explicit 200 {revoked:false} admits', async (t) => {
  const answers = [];
  const a = await authority((req, res) => {
    const next = answers.shift();
    if (next === 'hang') return; // timeout
    if (next === 'reset') return req.socket.destroy();
    json(res, next[0], next[1]);
  });
  t.after(a.close);
  const cases = [
    [[200, { revoked: false }], 'active'],
    [[200, { revoked: true }], 'revoked'],
    [[404, { detail: 'Not Found' }], 'unverifiable'],
    [[500, 'boom'], 'unverifiable'],
    [[503, { revoked: false }], 'unverifiable'],
    [[200, {}], 'unverifiable'],
    [[200, { revoked: 'false' }], 'unverifiable'],
    [[200, { revoked: 0 }], 'unverifiable'],
    [[200, 'null'], 'unverifiable'],
    [[200, 'not json'], 'unverifiable'],
    ['hang', 'unverifiable'],
    ['reset', 'unverifiable'],
    [[200, { revoked: false }], 'active'], // recovery after the outage
  ];
  for (const [answer, expected] of cases) {
    answers.push(answer);
    assert.equal(await checkSessionRevocation('sid-a', a.auth()), expected, JSON.stringify(answer));
  }
  assert.equal(a.seen.at(-1).url, '/sessions/revoked/sid-a');
  assert.equal(a.seen.at(-1).key, 'svc');
  const before = a.seen.length;
  assert.equal(await checkSessionRevocation(null, a.auth()), 'unverifiable');
  assert.equal(await checkSessionRevocation('', a.auth()), 'unverifiable');
  assert.equal(await checkSessionRevocation('sid-a', a.auth({ sharedSecret: '' })), 'unverifiable');
  assert.equal(a.seen.length, before, 'no request without sid or service credential');
  answers.push([200, { revoked: false }]);
  assert.equal(await checkSessionRevocation('a/b?c', a.auth()), 'active');
  assert.equal(a.seen.at(-1).url, '/sessions/revoked/a%2Fb%3Fc');
});

test('logout acknowledgement: strict 204, bounded retry, recovery', async (t) => {
  const answers = [];
  const a = await authority((req, res) => {
    const next = answers.shift();
    if (next === 'hang') return;
    res.writeHead(next).end();
  });
  t.after(a.close);
  answers.push(204);
  assert.deepEqual(await confirmSessionRevocation('sid-a', 'op@x', a.auth()), { confirmed: true, attempts: 1, last: 204 });
  assert.deepEqual(JSON.parse(a.seen.at(-1).body), { email: 'op@x', jti: 'sid-a' });
  answers.push(500, 204);
  assert.deepEqual(await confirmSessionRevocation('sid-a', 'op@x', a.auth()), { confirmed: true, attempts: 2, last: 204 });
  answers.push(200, 202);
  assert.deepEqual(await confirmSessionRevocation('sid-a', null, a.auth()), { confirmed: false, attempts: 2, last: 202 }, 'any 2xx other than 204 is not an ACK');
  answers.push(404, 404);
  assert.equal((await confirmSessionRevocation('sid-a', null, a.auth())).confirmed, false);
  answers.push('hang', 'hang');
  assert.deepEqual(await confirmSessionRevocation('sid-a', null, a.auth()), { confirmed: false, attempts: 2, last: 'error' });
  assert.deepEqual(await confirmSessionRevocation('sid-a', null, a.auth({ sharedSecret: '' })), { confirmed: false, attempts: 0, last: 'not_configured' });
  answers.push(204);
  assert.equal((await confirmSessionRevocation('sid-a', null, a.auth())).confirmed, true, 'recovers once the backend is back');
});

test('refusal is a terminal 401 (no redirect), private, clears the cookie; unconfirmed logout keeps it', async () => {
  for (const reason of ['revoked', 'unverifiable']) {
    const api = sessionRefusedResponse(new Request(ORIGIN + '/api/backend/people'), reason);
    assert.equal(api.status, 401); assert.equal(api.headers.get('location'), null);
    assert.deepEqual(await api.json(), { error: reason === 'revoked' ? 'session_revoked' : 'session_unverifiable' });
    assert.equal(api.headers.get('cache-control'), 'private, no-store');
    const page = sessionRefusedResponse(new Request(ORIGIN + '/people'), reason);
    assert.equal(page.status, 401); assert.equal(page.headers.get('location'), null);
    assert.match(await page.text(), /href="\/signin"/);
    assert.ok(page.headers.getSetCookie().some((c) => c.startsWith('authjs.session-token=;') && /1970/.test(c)));
  }
  const unconfirmed = logoutUnconfirmedResponse({ confirmed: false, attempts: 2, last: 500 });
  assert.equal(unconfirmed.status, 503);
  assert.equal(unconfirmed.headers.getSetCookie().length, 0, 'session cookie is kept for the retry');
  assert.deepEqual(await unconfirmed.json(), { error: 'logout_unconfirmed', attempts: 2, last: 500 });
  assert.equal(signOutOutcome(await logoutUnconfirmedResponse({ confirmed: false, attempts: 2, last: 'error' }).json()), 'unconfirmed');
  assert.equal(signOutOutcome({ url: ORIGIN + '/signin' }), 'signed_out');
  for (const bad of [null, undefined, {}, { url: 1 }, { url: '/x', error: 'e' }]) assert.equal(signOutOutcome(bad), 'unconfirmed');
});
