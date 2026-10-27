import {backendUrl} from '../src/lib/backend-url.ts';
import {mutationOriginRefusal, ownOrigins} from '../src/lib/request-origin.ts';
import {sessionIdOf} from '../src/lib/session-revocation.ts';
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import { stripTypeScriptTypes } from 'node:module';

const source = fs.readFileSync(new URL('../src/app/api/backend/[...path]/route.ts', import.meta.url), 'utf8');
function handler(proxySecret, user = { email: 'user@example.invalid' }, sid = 'sid-session-1') {
  const calls = [];
  const exports = {};
  const context = {
    exports, Headers, Response, URL, backendUrl, mutationOriginRefusal, ownOrigins, sessionIdOf,
    process: { env: { BACKEND_SHARED_SECRET: 'service-only', BACKEND_PROXY_SECRET: proxySecret } },
    auth: async () => (sid === null ? { user } : { user, sid }),
    fetch: async (url, init) => {
      calls.push({ url, init });
      return new Response('synthetic', { status: 200 });
    },
  };
  // Execute the actual route, replacing only its external auth dependency.
  const executable = stripTypeScriptTypes(source)
    .replace(/^import .*;$/gm, '')
    .replace(/^export /gm, '');
  vm.runInNewContext(executable + '\nexports.GET = GET; exports.POST = POST;', context);
  return { run: exports.GET, post: exports.POST, calls };
}
const request = () => ({
  method: 'GET', nextUrl: new URL('http://localhost/api/backend/bo/settings'),
  headers: new Headers({ 'x-caller-email': 'admin@example.invalid',
    'x-caller-proxy-secret': 'attacker', 'x-caller-role': 'admin', 'x-api-key': 'attacker',
    'x-caller-session-sid': 'attacker-sid' }),
});

test('BO proxy binds delegated identity to the authenticated session and separate credential', async () => {
  const { run, calls } = handler('proxy-only');
  assert.equal((await run(request(), { params: Promise.resolve({ path: ['bo', 'settings'] }) })).status, 200);
  const headers = calls[0].init.headers;
  assert.equal(headers.get('x-caller-email'), 'user@example.invalid');
  assert.equal(headers.get('x-caller-proxy-secret'), 'proxy-only');
  assert.equal(headers.get('x-api-key'), 'service-only');
  assert.equal(headers.get('x-caller-role'), null);
  // C10: the revocation key comes from the verified session, never the client.
  assert.equal(headers.get('x-caller-session-sid'), 'sid-session-1');
});

// C10: a session without sid is a legacy token; it is never forwarded without
// its revocation key (the backend would skip the check), whatever the client sends.
for (const sid of [null, '', 42]) {
  test(`BO proxy refuses a session without sid (${JSON.stringify(sid)}) before the backend`, async () => {
    const { run, calls } = handler('proxy-only', undefined, sid);
    const res = await run(request(), { params: Promise.resolve({ path: ['bo', 'settings'] }) });
    assert.equal(res.status, 401);
    assert.deepEqual(await res.json(), { error: 'session_unverifiable' });
    assert.equal(res.headers.get('cache-control'), 'private, no-store');
    assert.equal(calls.length, 0);
  });
}

for (const secret of [undefined, '', '   ', 'service-only', ' service-only ']) {
  test(`BO proxy fails closed with ${secret === 'service-only' ? 'reused' : 'missing'} delegation credential`, async () => {
    const { run, calls } = handler(secret);
    assert.equal((await run(request(), { params: Promise.resolve({ path: ['bo', 'settings'] }) })).status, 503);
    assert.equal(calls.length, 0);
  });
}

// Norma v2 C10: SameSite=Lax lets another origin of the same site (other port or
// subdomain) post with the session cookie; the proxy refuses it before reading
// the session and never reaches the backend.
const mutation = (headers) => ({ method: 'POST', nextUrl: new URL('http://localhost/api/backend/people/2/archive'), headers: new Headers(headers), body: null });
const params = { params: Promise.resolve({ path: ['people', '2', 'archive'] }) };
for (const [label, headers] of [
  ['same-site form (Sec-Fetch-Site: same-site)', { 'sec-fetch-site': 'same-site', origin: 'http://localhost:4000' }],
  ['cross-site no-cors fetch', { 'sec-fetch-site': 'cross-site', origin: 'http://evil.invalid' }],
  ['opaque origin (sandboxed frame)', { origin: 'null' }],
  ['foreign Origin without Sec-Fetch-Site', { origin: 'http://localhost:4000' }],
]) {
  test(`BO proxy refuses cross-origin mutation: ${label}`, async () => {
    const { post, calls } = handler('proxy-only');
    const res = await post(mutation(headers), params);
    assert.equal(res.status, 403);
    assert.equal((await res.json()).error, 'csrf_origin_refused');
    assert.equal(res.headers.get('cache-control'), 'private, no-store');
    assert.equal(calls.length, 0);
  });
}
for (const [label, headers] of [
  ['same-origin browser fetch', { 'sec-fetch-site': 'same-origin', origin: 'http://localhost' }],
  ['own Origin without Sec-Fetch-Site', { origin: 'http://localhost' }],
  ['non-browser client (no Origin, no Sec-Fetch-Site)', {}],
]) {
  test(`BO proxy forwards mutation: ${label}`, async () => {
    const { post, calls } = handler('proxy-only');
    const res = await post(mutation(headers), params);
    assert.equal(res.status, 200);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].init.method, 'POST');
  });
}
test('BO proxy marks every proxied response private, no-store', async () => {
  const { run } = handler('proxy-only');
  const res = await run(request(), { params: Promise.resolve({ path: ['bo', 'settings'] }) });
  assert.equal(res.headers.get('cache-control'), 'private, no-store, no-transform');
});
