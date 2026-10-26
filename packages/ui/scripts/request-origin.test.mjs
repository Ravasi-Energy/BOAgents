import assert from 'node:assert/strict';
import test from 'node:test';
import { mutationOriginRefusal, ownOrigins } from '../src/lib/request-origin.ts';

const own = ownOrigins('http://127.0.0.1:3000', 'https://bo.example.test/');
const h = (x) => new Headers(x);

test('own origins: request origin plus the configured public AUTH_URL origin', () => {
  assert.deepEqual(own, ['http://127.0.0.1:3000', 'https://bo.example.test']);
  assert.deepEqual(ownOrigins('http://a.test', ''), ['http://a.test']);
  assert.deepEqual(ownOrigins('http://a.test', 'not a url'), ['http://a.test']);
});

test('safe methods are never refused, whatever their origin', () => {
  for (const m of ['GET', 'HEAD', 'OPTIONS', 'get']) assert.equal(mutationOriginRefusal(m, h({ 'sec-fetch-site': 'cross-site', origin: 'http://evil.invalid' }), own), null);
});

test('Sec-Fetch-Site decides when present: only same-origin passes', () => {
  for (const m of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    assert.equal(mutationOriginRefusal(m, h({ 'sec-fetch-site': 'same-origin' }), own), null);
    for (const site of ['same-site', 'cross-site', 'none']) assert.equal(mutationOriginRefusal(m, h({ 'sec-fetch-site': site, origin: 'https://bo.example.test' }), own), 'sec-fetch-site:' + site);
  }
});

test('Origin decides without Sec-Fetch-Site; null and foreign origins are refused', () => {
  assert.equal(mutationOriginRefusal('POST', h({ origin: 'https://bo.example.test' }), own), null);
  assert.equal(mutationOriginRefusal('POST', h({ origin: 'http://127.0.0.1:3000' }), own), null);
  assert.equal(mutationOriginRefusal('POST', h({ origin: 'http://127.0.0.1:4000' }), own), 'origin:http://127.0.0.1:4000');
  assert.equal(mutationOriginRefusal('POST', h({ origin: 'null' }), own), 'origin:null');
});

test('no browser markers at all is not a browser request and passes', () => {
  assert.equal(mutationOriginRefusal('POST', h({}), own), null);
});
