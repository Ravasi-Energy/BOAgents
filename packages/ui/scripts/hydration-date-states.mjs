// React #418 on / (and /today, which redirects to /): the page is prerendered at
// build time, so anything time-dependent computed during render is frozen to the
// build date in the HTML and differs on the client on any later day. The browser
// clock is moved to other days/time zones; hydration must stay clean.
// Runs the real production build (`next start`) against a synthetic read-only backend.
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import http from 'node:http';
import path from 'node:path';
import test from 'node:test';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';

const require = createRequire(import.meta.url);
const { chromium } = require('playwright');
const { encode } = await import(pathToFileURL(path.resolve('node_modules/next-auth/jwt.js')));
const SECRET = process.env.AUTH_SECRET || 'BO01-synthetic-jwt-secret-minimum-32-characters';
const listen = (s) => new Promise((ok) => s.listen(0, '127.0.0.1', () => ok(s.address().port)));

test('/ and /today hydrate without mismatch on days other than the build day', { timeout: 300000 }, async () => {
  const backend = http.createServer((req, res) => {
    const u = new URL(req.url, 'http://localhost'); let body;
    if (u.pathname.startsWith('/sessions/revoked/')) body = { revoked: false };
    else if (u.pathname === '/auth/allowed-emails') body = { administered: false, emails: [] };
    else if (u.pathname === '/health') body = { status: 'ok', company_profile_loaded: false };
    else if (['/sessions', '/people'].includes(u.pathname)) body = [];
    res.writeHead(body === undefined ? 404 : 200, { 'content-type': 'application/json' });
    res.end(JSON.stringify(body ?? { detail: 'synthetic fixture: not served' }));
  });
  const bp = await listen(backend);
  const probe = http.createServer(); const port = await listen(probe); await new Promise((ok) => probe.close(ok));
  const O = 'http://127.0.0.1:' + port;
  const next = spawn(process.execPath, ['node_modules/next/dist/bin/next', 'start', '--hostname', '127.0.0.1', '--port', String(port)], {
    env: { ...process.env, NEXT_TELEMETRY_DISABLED: '1', AUTH_SECRET: SECRET, AUTH_URL: O, AUTH_TRUST_HOST: 'true', ALLOWED_EMAILS: 'admin@bo01.invalid', BACKEND_BASE_URL: 'http://127.0.0.1:' + bp, BACKEND_SHARED_SECRET: 'synthetic', BACKEND_PROXY_SECRET: 'synthetic-proxy' },
    stdio: ['ignore', 'pipe', 'pipe'], detached: true,
  });
  let log = ''; next.stdout.on('data', (d) => { log += d; }); next.stderr.on('data', (d) => { log += d; });
  const browser = await chromium.launch({ headless: true, chromiumSandbox: true });
  try {
    for (let i = 0; i < 120; i++) { try { await fetch(O + '/signin'); break; } catch { await new Promise((ok) => setTimeout(ok, 250)); } }
    const cookie = await encode({ token: { email: 'admin@bo01.invalid', sub: 'admin@bo01.invalid', name: 'Admin Probe', sid: crypto.randomUUID() }, secret: SECRET, salt: 'authjs.session-token', maxAge: 3600 });
    const shifts = [['same day Bucharest (control)', 0, 'Europe/Bucharest'], ['+3 days Bucharest', 3, 'Europe/Bucharest'], ['+40 days Bucharest', 40, 'Europe/Bucharest'], ['-1 day Tokyo', -1, 'Asia/Tokyo'], ['+1 day Los Angeles', 1, 'America/Los_Angeles']];
    const report = [];
    for (const [label, days, tz] of shifts) for (const route of ['/', '/today']) {
      const ctx = await browser.newContext({ timezoneId: tz, locale: 'en-US' });
      await ctx.addCookies([{ name: 'authjs.session-token', value: cookie, url: O }]);
      const page = await ctx.newPage(); const errors = [];
      page.on('pageerror', (e) => errors.push(String(e).slice(0, 200)));
      await page.clock.install({ time: new Date(Date.now() + days * 86400000) });
      await page.goto(O + route); await page.locator('h1').first().waitFor(); await page.waitForTimeout(1500);
      const shown = await page.locator('h1 + span').first().textContent().catch(() => null);
      const expected = await page.evaluate(() => new Date().toLocaleDateString('en-US', { weekday: 'long', month: 'long', day: 'numeric' }));
      report.push({ label, route, final: new URL(page.url()).pathname, errors, shown, expected });
      await ctx.close();
    }
    console.log(JSON.stringify(report));
    const failures = report.filter((r) => r.errors.length || r.shown !== r.expected);
    assert.deepEqual(failures, [], 'hydration mismatch or wrong day label: ' + JSON.stringify(report));
  } finally {
    await browser.close(); try { process.kill(-next.pid, 'SIGTERM'); } catch {} backend.close();
  }
});
