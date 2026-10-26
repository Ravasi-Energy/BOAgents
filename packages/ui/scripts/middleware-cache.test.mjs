// Norma v2 D1/C10: every response of the auth gate is private, no-store (they can
// carry Auth.js cookies; prerendered pages otherwise go out as s-maxage=31536000),
// and public/fonts stay reachable without a session (the sign-in page needs them).
import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import { stripTypeScriptTypes } from 'node:module';
import { NextResponse } from 'next/server.js';

const source = fs.readFileSync(new URL('../src/middleware.ts', import.meta.url), 'utf8');
const exports = {};
// Execute the actual middleware; only `auth` (the Auth.js wrapper) is replaced by identity.
vm.runInNewContext(stripTypeScriptTypes(source).replace(/^import .*;$/gm, '').replace(/^export default /m, 'exports.mw = ').replace(/^export const config/m, 'exports.config'), { exports, NextResponse, Response, URL, auth: (fn) => fn });
const req = (path, user) => ({ auth: user ? { user } : null, nextUrl: new URL('http://127.0.0.1:3000' + path) });

test('signed-in page response is private, no-store', () => {
  const res = exports.mw(req('/people', { email: 'a@x.test' }));
  assert.equal(res.headers.get('cache-control'), 'private, no-store');
  assert.equal(res.headers.get('x-middleware-next'), '1');
});
test('refusals (answered by auth-denial before the handler) are private, no-store', async () => {
  const { deniedAuthResponse } = await import('../src/lib/auth-denial.ts');
  const api = deniedAuthResponse(new Request('http://127.0.0.1:3000/api/backend/people'));
  assert.equal(api.status, 401); assert.deepEqual(await api.json(), { error: 'unauthorized' });
  assert.equal(api.headers.get('cache-control'), 'private, no-store');
  const page = deniedAuthResponse(new Request('http://127.0.0.1:3000/people?x=1'));
  assert.equal(page.status, 302);
  assert.equal(page.headers.get('location'), 'http://127.0.0.1:3000/signin?callbackUrl=%2Fpeople%3Fx%3D1');
  assert.equal(page.headers.get('cache-control'), 'private, no-store');
});
test('matcher: fonts and Next internals ungated, look-alikes and pages gated', () => {
  const re = new RegExp('^' + exports.config.matcher[0] + '$');
  for (const p of ['/fonts/PlusJakartaSans-latin.woff2', '/_next/static/chunks/a.js', '/api/auth/session', '/signin', '/signin/']) assert.equal(re.test(p), false, p);
  for (const p of ['/people', '/api/backend/people', '/fontsX', '/signin-help', '/']) assert.equal(re.test(p), true, p);
});
