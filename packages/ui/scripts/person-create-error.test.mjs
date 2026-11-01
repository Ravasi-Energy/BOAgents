import { test, after } from 'node:test';
import assert from 'node:assert/strict';
import { createPerson } from '../src/lib/api.ts';

const original = globalThis.fetch;
after(() => { globalThis.fetch = original; });

test('roster refusal exposes the server recovery message and retains the submitted draft', async () => {
  const draft = { full_name: 'Draft', email: null };
  const before = structuredClone(draft);
  const message = 'Includeți propria persoană (email de autentificare, admin-capabilă) în roster înainte de a continua.';
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: { error: 'roster_lockout', message } }), { status: 409, statusText: 'Conflict' });
  await assert.rejects(createPerson(draft), e => e.message.includes(message));
  assert.deepEqual(draft, before);
});

test('validation and plain refusal details stay readable', async () => {
  for (const detail of ['Acces refuzat', [{ loc: ['body', 'email'], msg: 'invalid email' }]]) {
    globalThis.fetch = async () => new Response(JSON.stringify({ detail }), { status: 422 });
    await assert.rejects(createPerson({ full_name: 'Draft' }), /Acces refuzat|email: invalid email/);
  }
});

test('viewer refusal retains operation and HTTP status as well as the server reason', async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ detail: 'forbidden' }), { status: 403, statusText: 'Forbidden' });
  await assert.rejects(createPerson({ full_name: 'Draft' }), e =>
    e.message.includes('Failed to create person') && e.message.includes('403') && e.message.includes('forbidden'));
});

test('unreadable or empty error bodies retain the HTTP fallback', async () => {
  for (const body of ['not json', '{}', '{"detail":null}']) {
    globalThis.fetch = async () => new Response(body, { status: 503, statusText: 'Service Unavailable' });
    await assert.rejects(createPerson({ full_name: 'Draft' }), /Failed to create person:.*503/);
  }
});

test('successful create keeps the response contract and exact request body', async () => {
  const draft = { full_name: 'Draft', email: 'actor@example.test' };
  globalThis.fetch = async (url, options) => {
    assert.equal(url, '/api/backend/people');
    assert.equal(options.method, 'POST');
    assert.deepEqual(JSON.parse(options.body), draft);
    return new Response(JSON.stringify({ id: 7, ...draft }), { status: 201 });
  };
  assert.deepEqual(await createPerson(draft), { id: 7, ...draft });
});
