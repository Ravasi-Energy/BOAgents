// Mermaid consumer through the real React mount, the real Next production
// server, the real /api/backend proxy and the real FastAPI backend, for two
// firms and three viewports. A loopback relay between Next and the backend
// swaps the diagram of two diagram-less sections (api, schemas) for inert
// canaries; every other section is the version-controlled backend content.
// Run from packages/ui after `next build`; collected by test:review-roles.
import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import http from 'node:http';
import crypto from 'node:crypto';
import { createRequire } from 'node:module';
import { pathToFileURL } from 'node:url';
import { spawn, execFileSync } from 'node:child_process';

const UI = path.resolve(process.env.BO_UI || '.');
const OUT = path.resolve(process.env.BO_OUT || path.join(process.env.BO_READ_ERROR_OUTPUT || 'review-roles-results', 'mermaid-proxy'));
const CORE = path.resolve(UI, '../core');
const PY = process.env.BO_CORE_PYTHON || path.join(CORE, '.venv/bin/python');
const NEXT = process.env.BO_NEXT_BIN || path.join(UI, 'node_modules/next/dist/bin/next');
// Optional copy of a standalone build, so the server never writes into the source checkout.
const SERVER = process.env.BO_SERVER ? path.resolve(process.env.BO_SERVER) : null;
const BUILD = SERVER ? SERVER + '/.next' : UI + '/.next';
const require = createRequire(UI + '/package.json');
fs.mkdirSync(OUT + '/tmp', { recursive: true });
process.env.TMPDIR = OUT + '/tmp';
const { chromium } = require('playwright');
const { encode } = await import(pathToFileURL(path.join(UI, 'node_modules/next-auth/jwt.js')));

const VIEWPORTS = [['desktop', 1440, 900], ['tablet', 1024, 768], ['mobile', 390, 844]];
const FIRMS = [
  { tenant: 'firma-a', admin: 'admin@firma-a.test', secret: 'BO01-mermaid-firma-a-jwt-secret-32-chars-min' },
  { tenant: 'firma-b', admin: 'admin@firma-b.test', secret: 'BO01-mermaid-firma-b-jwt-secret-32-chars-min' },
];
const CANARY = {
  api: 'flowchart LR\n A["<img src=https://beacon.invalid/BO01.png onerror=BO01_INERT>Ordinary canary api"]\n click A href "javascript:BO01_INERT" "inert"',
  schemas: '%%{init: {"securityLevel":"loose"}}%%\nflowchart LR\n B["Ordinary canary schemas"]\n click B href "javascript:BO01_INERT" "inert"',
};
const SVC = 'svc-secret-test-32-chars-minimum-xx', PXY = 'proxy-secret-test-32-characters';
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
const git = (...a) => execFileSync('git', ['-C', UI, ...a], { encoding: 'utf8' }).trim();

const result = {
  at: new Date().toISOString(),
  pin: git('rev-parse', 'HEAD'),
  trackedStatus: git('status', '--porcelain', '--untracked-files=no'),
  consumerSHA256: sha(fs.readFileSync(UI + '/src/components/architecture/MermaidDiagram.tsx')),
  securityLevelInSource: /securityLevel:\s*'(\w+)'/.exec(fs.readFileSync(UI + '/src/components/architecture/MermaidDiagram.tsx', 'utf8'))?.[1],
  build: { server: SERVER ? 'standalone-copy:' + SERVER : 'next start:' + UI, buildId: fs.readFileSync(BUILD + '/BUILD_ID', 'utf8').trim(), buildIdMtime: fs.statSync(BUILD + '/BUILD_ID').mtime.toISOString(), ownerBuildId: fs.readFileSync(UI + '/.next/BUILD_ID', 'utf8').trim() },
  versions: Object.fromEntries(['next', 'mermaid', 'dompurify', 'react', 'playwright'].map((p) => [p, JSON.parse(fs.readFileSync(path.join(UI, 'node_modules', p, 'package.json'))).version])),
  firms: [],
  limits: [
    'Synthetic Auth.js JWT session cookies (no Google OAuth); real Next production server, middleware, proxy route and FastAPI backend.',
    'Relay replaces only the mermaid field of api/schemas responses with inert canaries; no click or execution. Every non-loopback request is aborted and recorded (layout Google Fonts stylesheet), so fonts fall back.',
    'Canary checks the rendered DOM; Mermaid run-time behaviour on click is not exercised.',
  ],
};

async function freePort() { const s = http.createServer(); await new Promise((ok, no) => { s.once('error', no); s.listen(0, '127.0.0.1', ok); }); const p = s.address().port; await new Promise((ok) => s.close(ok)); return p; }
function kill(p) { if (p && p.exitCode === null && p.signalCode === null) { try { process.kill(-p.pid, 'SIGTERM'); } catch { try { p.kill('SIGTERM'); } catch {} } } }
async function ready(url, p) { for (let i = 0; i < 240; i++) { if (p.exitCode !== null) throw new Error('process exited early: ' + url); try { await fetch(url); return; } catch { await new Promise((ok) => setTimeout(ok, 250)); } } throw new Error('timeout ' + url); }

function relay(target, log) {
  return http.createServer((req, res) => {
    const m = /^\/architecture\/sections\/(api|schemas)$/.exec(req.url);
    const up = http.request({ host: '127.0.0.1', port: target, path: req.url, method: req.method, headers: req.headers }, (ur) => {
      if (!m || ur.statusCode !== 200) { res.writeHead(ur.statusCode, ur.headers); ur.pipe(res); return; }
      const chunks = []; ur.on('data', (c) => chunks.push(c)); ur.on('end', () => {
        const body = JSON.parse(Buffer.concat(chunks)); body.mermaid = CANARY[m[1]]; log.push({ section: m[1], injected: true });
        const out = Buffer.from(JSON.stringify(body)); const h = { ...ur.headers, 'content-length': out.length }; delete h['transfer-encoding'];
        res.writeHead(200, h); res.end(out);
      });
    });
    up.on('error', (e) => { res.writeHead(502); res.end(String(e)); });
    req.pipe(up);
  });
}

let browser;
test.before(async () => {
  browser = await chromium.launch({ headless: true, chromiumSandbox: true, args: ['--enable-automation'], env: { ...process.env, TMPDIR: OUT + '/tmp' } });
  const cd = await browser.newBrowserCDPSession(); const cl = (await cd.send('Browser.getBrowserCommandLine')).arguments; await cd.detach();
  assert(!cl.includes('--no-sandbox') && !cl.includes('--disable-setuid-sandbox'));
  result.browser = { chromiumSandbox: true, version: browser.version(), noSandboxFlag: false };
});
test.after(async () => {
  await browser?.close();
  const caps = fs.readdirSync(OUT).filter((f) => f.endsWith('.png')).sort().map((f) => ({ file: f, sha256: sha(fs.readFileSync(path.join(OUT, f))) }));
  result.captures = caps;
  fs.writeFileSync(OUT + '/RESULT.json', JSON.stringify(result, null, 2));
});

for (const firm of FIRMS) {
  test(`Mermaid via React/Next/proxy — ${firm.tenant}`, { timeout: 600000 }, async () => {
    const fr = { tenant: firm.tenant, checks: [], relay: [], pageErrors: [], consoleErrors: [], blockedExternal: [] };
    result.firms.push(fr);
    const dir = OUT + '/' + firm.tenant; fs.mkdirSync(dir, { recursive: true });
    let backend, next, rel;
    try {
      const bp = await freePort(), rp = await freePort(), np = await freePort(), O = 'http://127.0.0.1:' + np;
      const bfd = fs.openSync(dir + '/backend.log', 'w');
      backend = spawn(PY, ['-m', 'uvicorn', 'openexecutive.api.main:app', '--host', '127.0.0.1', '--port', String(bp)], { cwd: dir, env: { ...process.env, PYTHONPATH: CORE, PYTHONDONTWRITEBYTECODE: '1', VECTOR_STORE_PATH: dir + '/chroma_db', EPISODIC_DB_PATH: dir + '/episodic.db', BOAGENTS_DB_PATH: dir + '/bo_agents.db', EXEC_EMAIL_ADDRESS: 'exec@' + firm.tenant + '.test', ANTHROPIC_API_KEY: 'sk-test-not-used', BO_TENANT_ID: firm.tenant, BO_ADMIN_EMAILS: firm.admin, BACKEND_SHARED_SECRET: SVC, BACKEND_PROXY_SECRET: PXY }, stdio: ['ignore', bfd, bfd], detached: true });
      fs.closeSync(bfd); await ready('http://127.0.0.1:' + bp + '/health', backend);
      rel = relay(bp, fr.relay); await new Promise((ok) => rel.listen(rp, '127.0.0.1', ok));
      const nfd = fs.openSync(dir + '/next.log', 'w');
      next = spawn(process.execPath, SERVER ? [SERVER + '/server.js'] : [NEXT, 'start', '--hostname', '127.0.0.1', '--port', String(np)], { cwd: SERVER || UI, env: { ...process.env, PORT: String(np), HOSTNAME: '127.0.0.1', NEXT_TELEMETRY_DISABLED: '1', AUTH_SECRET: firm.secret, AUTH_URL: O, AUTH_TRUST_HOST: 'true', ALLOWED_EMAILS: firm.admin, BACKEND_BASE_URL: 'http://127.0.0.1:' + rp, BACKEND_SHARED_SECRET: SVC, BACKEND_PROXY_SECRET: PXY }, stdio: ['ignore', nfd, nfd], detached: true });
      fs.closeSync(nfd); await ready(O + '/signin', next);

      const own = await encode({ token: { email: firm.admin, sub: firm.admin }, secret: firm.secret, salt: 'authjs.session-token', maxAge: 3600 });
      const other = FIRMS.find((f) => f !== firm);
      const foreign = await encode({ token: { email: other.admin, sub: other.admin }, secret: other.secret, salt: 'authjs.session-token', maxAge: 3600 });

      // Proxy gate: no session and the other firm's session are refused before the backend.
      const noSess = await fetch(O + '/api/backend/architecture/sections');
      const forSess = await fetch(O + '/api/backend/architecture/sections', { headers: { cookie: 'authjs.session-token=' + foreign } });
      const ownSess = await fetch(O + '/api/backend/architecture/sections', { headers: { cookie: 'authjs.session-token=' + own } });
      fr.checks.push({ id: 'proxy-no-session', status: noSess.status });
      fr.checks.push({ id: 'proxy-foreign-firm-session', status: forSess.status });
      fr.checks.push({ id: 'proxy-own-session', status: ownSess.status });
      assert.equal(noSess.status, 401); assert.equal(forSess.status, 401); assert.equal(ownSess.status, 200);

      for (const [name, width, height] of VIEWPORTS) {
        const ctx = await browser.newContext({ viewport: { width, height } });
        await ctx.addCookies([{ name: 'authjs.session-token', value: own, url: O }]);
        await ctx.route('**/*', (r) => { const u = new URL(r.request().url()); if (u.hostname === '127.0.0.1') return r.continue(); fr.blockedExternal.push(u.href); return r.abort(); });
        const page = await ctx.newPage();
        page.on('pageerror', (e) => fr.pageErrors.push({ viewport: name, message: String(e) }));
        page.on('console', (m) => { if (m.type() === 'error') fr.consoleErrors.push({ viewport: name, text: m.text().slice(0, 300) }); });
        page.on('response', (r) => { if (r.status() >= 400) (fr.httpErrors ||= []).push({ viewport: name, status: r.status(), url: r.url() }); });
        await page.goto(O + '/architecture', { waitUntil: 'networkidle' });
        await page.waitForFunction(() => document.querySelectorAll('section svg[id^="mermaid-"]').length >= 25 && !document.body.innerText.includes('Rendering diagram'), null, { timeout: 60000 });
        const dom = await page.evaluate(() => {
          const svgs = [...document.querySelectorAll('section svg[id^="mermaid-"]')];
          const all = svgs.flatMap((s) => [s, ...s.querySelectorAll('*')]);
          const sectionOf = (id) => document.getElementById(id)?.closest('section');
          return {
            svgCount: svgs.length,
            unavailable: (document.body.innerText.match(/Diagram unavailable/g) || []).length,
            brInSvg: svgs.reduce((n, s) => n + s.querySelectorAll('br').length, 0),
            sequence: ['lifecycle', 'nojargon_proactive'].map((id) => ({ id, svg: !!sectionOf(id)?.querySelector('svg[id^="mermaid-"]'), text: sectionOf(id)?.querySelector('svg')?.textContent.length || 0 })),
            executableUrls: all.flatMap((n) => [...n.attributes].filter((a) => /^(?:href|xlink:href|src)$/i.test(a.name) && /^\s*(?:javascript|vbscript|data):/i.test(a.value)).map((a) => ({ section: n.closest('section')?.querySelector('[id]')?.id, tag: n.tagName, attr: a.name, value: a.value }))),
            media: svgs.reduce((n, s) => n + s.querySelectorAll('img,image,picture,source,video,audio,iframe,object,embed').length, 0),
            eventAttrs: all.flatMap((n) => [...n.attributes].filter((a) => /^on/i.test(a.name)).map((a) => ({ section: n.closest('section')?.querySelector('[id]')?.id, tag: n.tagName, attr: a.name }))),
            canaryText: ['api', 'schemas'].map((id) => ({ id, ordinary: (sectionOf(id)?.querySelector('svg')?.textContent || '').includes('Ordinary canary ' + id) })),
            hScroll: document.documentElement.scrollWidth > window.innerWidth,
          };
        });
        fr.checks.push({ id: 'dom-' + name, ...dom });
        for (const id of ['overview', 'lifecycle', 'api', 'schemas']) {
          // The page scrolls inside an inner container: capture what the user sees with the
          // section heading at the top, then the mounted diagram itself.
          await page.evaluate((sid) => document.getElementById(sid).scrollIntoView({ block: 'start' }), id);
          await page.waitForTimeout(150);
          await page.screenshot({ path: `${OUT}/${firm.tenant}-${name}-${id}.png`, animations: 'disabled' });
          const svg = page.locator('#' + id).locator('xpath=ancestor::section[1]').locator('svg[id^="mermaid-"]').first();
          await svg.scrollIntoViewIfNeeded();
          await svg.screenshot({ path: `${OUT}/${firm.tenant}-${name}-${id}-diagram.png`, animations: 'disabled' });
        }
        await page.locator('#overview').scrollIntoViewIfNeeded();
        await page.screenshot({ path: `${OUT}/${firm.tenant}-${name}-viewport.png`, animations: 'disabled' });
        await ctx.close();
      }
      for (const { id, ...dom } of fr.checks.filter((c) => c.id.startsWith('dom-'))) {
        const name = id.slice(4);
          // Legitimate content preserved.
          assert.equal(dom.unavailable, 0, name + ': a legitimate diagram failed to render');
          assert(dom.svgCount >= 25, name + ': expected 23 legitimate + 2 canary diagrams');
          assert(dom.brInSvg > 0, name + ': multiline <br/> labels lost');
          for (const s of dom.sequence) assert(s.svg && s.text > 0, name + ': sequence diagram missing ' + s.id);
          for (const c of dom.canaryText) assert(c.ordinary, name + ': canary label missing ' + c.id);
          assert.equal(fr.pageErrors.length, 0, name + ': page errors');
          assert.deepEqual(fr.blockedExternal.filter((u) => !u.startsWith('https://fonts.googleapis.com/')), [], name + ': unexpected external request');
          assert(!fr.blockedExternal.some((u) => u.includes('BO01')), name + ': canary reached the network');
          assert.equal(dom.media, 0, name + ': media element survived in mounted SVG');
          // Security: no executable link or inline handler survives in the real mounted SVG.
          assert.deepEqual(dom.eventAttrs, [], name + ': inline event attribute survived in mounted SVG');
          assert.deepEqual(dom.executableUrls, [], name + ': executable link survived in mounted SVG');
      }
    } finally {
      kill(next); kill(backend); rel?.close();
    }
  });
}
