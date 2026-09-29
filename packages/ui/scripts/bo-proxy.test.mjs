import assert from 'node:assert/strict';
import test from 'node:test';
import fs from 'node:fs';
import vm from 'node:vm';
import { stripTypeScriptTypes } from 'node:module';

const source = fs.readFileSync(new URL('../src/app/api/backend/[...path]/route.ts', import.meta.url), 'utf8');
function handler(proxySecret, user = { email: 'user@example.invalid' }) {
  const calls = [];
  const exports = {};
  const context = {
    exports, Headers, Response, URL,
    process: { env: { BACKEND_SHARED_SECRET: 'service-only', BACKEND_PROXY_SECRET: proxySecret } },
    auth: async () => ({ user }),
    fetch: async (url, init) => {
      calls.push({ url, init });
      return new Response('synthetic', { status: 200 });
    },
  };
  // Execute the actual route, replacing only its external auth dependency.
  const executable = stripTypeScriptTypes(source)
    .replace(/^import .*;$/gm, '')
    .replace(/^export /gm, '');
  vm.runInNewContext(executable + '\nexports.GET = GET;', context);
  return { run: exports.GET, calls };
}
const request = () => ({
  method: 'GET', nextUrl: new URL('http://localhost/api/backend/bo/settings'),
  headers: new Headers({ 'x-caller-email': 'admin@example.invalid',
    'x-caller-proxy-secret': 'attacker', 'x-caller-role': 'admin', 'x-api-key': 'attacker' }),
});

test('BO proxy binds delegated identity to the authenticated session and separate credential', async () => {
  const { run, calls } = handler('proxy-only');
  assert.equal((await run(request(), { params: Promise.resolve({ path: ['bo', 'settings'] }) })).status, 200);
  const headers = calls[0].init.headers;
  assert.equal(headers.get('x-caller-email'), 'user@example.invalid');
  assert.equal(headers.get('x-caller-proxy-secret'), 'proxy-only');
  assert.equal(headers.get('x-api-key'), 'service-only');
  assert.equal(headers.get('x-caller-role'), null);
});

for (const secret of [undefined, '', '   ', 'service-only', ' service-only ']) {
  test(`BO proxy fails closed with ${secret === 'service-only' ? 'reused' : 'missing'} delegation credential`, async () => {
    const { run, calls } = handler(secret);
    assert.equal((await run(request(), { params: Promise.resolve({ path: ['bo', 'settings'] }) })).status, 503);
    assert.equal(calls.length, 0);
  });
}
