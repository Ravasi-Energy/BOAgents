// Mobile geometry of the pages where content was squeezed or clipped at 390px, through the real
// Next server, proxy and FastAPI backend, two firms, three viewports: /council and /skills (list
// beside the editor/detail left it ~70px wide), /artifacts/[id] (header buttons pushed the title
// to 0px), /demo (snapshot text one word per line), and long identifiers running past their card
// on /bo/pilot (ledger digest), /bo/runs/[id] (plan hash) and /bo/packages (verdict digest).
// Desktop and tablet must keep their layout; the baseline is expected to fail at 390px only.
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
const OUT = path.resolve(process.env.BO_OUT || path.join(process.env.BO_READ_ERROR_OUTPUT || 'review-roles-results', 'mobile-geometry'));
const CORE = path.resolve(process.env.BO_CORE || path.resolve(UI, '../core'));
const PY = process.env.BO_CORE_PYTHON || path.join(path.resolve(UI, '../core'), '.venv/bin/python');
const SERVER = process.env.BO_SERVER ? path.resolve(process.env.BO_SERVER) : null;
const NEXT_DIR = path.resolve(process.env.BO_NEXT_DIR || UI);
const NEXT = path.join(NEXT_DIR, 'node_modules/next/dist/bin/next');
const require = createRequire(UI + '/package.json');
fs.mkdirSync(OUT + '/tmp', { recursive: true });
process.env.TMPDIR = OUT + '/tmp';
const { chromium } = require('playwright');
const { encode } = await import(pathToFileURL(path.join(UI, 'node_modules/next-auth/jwt.js')));
const VIEWPORTS = [['desktop', 1440, 900], ['tablet', 1024, 768], ['mobile', 390, 844]];
const FIRMS = [
  { tenant: 'firma-a', admin: 'admin@firma-a.test', secret: 'BO01-mobile-geo-firma-a-secret-32-chars-min' },
  { tenant: 'firma-b', admin: 'admin@firma-b.test', secret: 'BO01-mobile-geo-firma-b-secret-32-chars-min' },
];
const SVC = 'svc-secret-test-32-chars-minimum-xx', PXY = 'proxy-secret-test-32-characters';
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
const is2xx = (s) => s >= 200 && s < 300;
const result = { at: new Date().toISOString(), server: SERVER ? 'standalone:' + SERVER : 'next start:' + NEXT_DIR, firms: [] };

async function freePort() { const s = http.createServer(); await new Promise((ok, no) => { s.once('error', no); s.listen(0, '127.0.0.1', ok); }); const p = s.address().port; await new Promise((ok) => s.close(ok)); return p; }
function kill(p) { if (p && p.exitCode === null && p.signalCode === null) { try { process.kill(-p.pid, 'SIGTERM'); } catch { try { p.kill('SIGTERM'); } catch {} } } }
async function ready(url, p) { for (let i = 0; i < 240; i++) { if (p.exitCode !== null) throw new Error('process exited early: ' + url); try { await fetch(url); return; } catch { await new Promise((ok) => setTimeout(ok, 250)); } } throw new Error('timeout ' + url); }

const SEED = "import json\nfrom openexecutive.alerts import store as a\na.initialize_db()\nprint(json.dumps({'alert': a.insert_alert(source='artifact', external_id='bo01-geo', severity='info', headline='Memo BO01 furnizori și termene de plată', body='# Memo BO01', dedup_key='bo01-geo')}))";
const PACKAGE = "import json\nfrom openexecutive.bo.pilot.fixture import package\nroot = package('pilot-work')\nprint(json.dumps({'root': str(root.resolve()), 'registry': open('pilot-work/registry.json').read()}))";

// Boxes the assertions compare, measured in the page.
const measure = (p, spec) => p.evaluate((s) => {
  const r = (e) => { const b = e.getBoundingClientRect(); return { left: Math.round(b.left), right: Math.round(b.right), width: Math.round(b.width) }; };
  const out = { innerWidth: window.innerWidth, docHScroll: document.documentElement.scrollWidth - document.documentElement.clientWidth };
  if (s.width) { const e = document.querySelector(s.width); out.width = e ? r(e).width : null; }
  if (s.visible) out.visible = [...document.querySelectorAll(s.visible)].filter((e) => e.offsetParent).map((e) => ({ text: e.innerText.trim().slice(0, 30), ...r(e) }));
  if (s.contained) out.contained = [...document.querySelectorAll(s.contained)].filter((e) => e.offsetParent).map((e) => { const box = e.closest(s.within); return { text: e.innerText.trim().slice(0, 40), el: r(e), box: box ? r(box) : null, scroll: e.scrollWidth - e.clientWidth }; });
  return out;
}, spec);

let browser;
test.before(async () => {
  browser = await chromium.launch({ headless: true, chromiumSandbox: true, args: ['--enable-automation'], env: { ...process.env, TMPDIR: OUT + '/tmp' } });
  const cd = await browser.newBrowserCDPSession(); const cl = (await cd.send('Browser.getBrowserCommandLine')).arguments; await cd.detach();
  assert(!cl.includes('--no-sandbox') && !cl.includes('--disable-setuid-sandbox'));
  result.browser = { chromiumSandbox: true, version: browser.version() };
});
test.after(async () => {
  await browser?.close();
  const caps = [];
  for (const f of FIRMS) { const d = OUT + '/' + f.tenant; if (fs.existsSync(d)) for (const x of fs.readdirSync(d).filter((x) => x.endsWith('.png')).sort()) caps.push({ file: f.tenant + '/' + x, sha256: sha(fs.readFileSync(d + '/' + x)) }); }
  result.captures = caps;
  fs.writeFileSync(OUT + '/RESULT.json', JSON.stringify(result, null, 2));
});

for (const firm of FIRMS) {
  test(`Mobile geometry of council, skills, artifact, demo, pilot, run, package verdict — ${firm.tenant}`, { timeout: 600000 }, async () => {
    const fr = { tenant: firm.tenant, geometry: {}, failures: [], pageErrors: [] };
    result.firms.push(fr);
    const dir = OUT + '/' + firm.tenant; fs.mkdirSync(dir, { recursive: true });
    const token = 'bo01-geo-' + firm.tenant;
    const env = { ...process.env, PYTHONPATH: CORE, PYTHONDONTWRITEBYTECODE: '1', VECTOR_STORE_PATH: dir + '/chroma_db', EPISODIC_DB_PATH: dir + '/episodic.db', BOAGENTS_DB_PATH: dir + '/bo_agents.db', EXEC_EMAIL_ADDRESS: 'exec@' + firm.tenant + '.test', ANTHROPIC_API_KEY: 'sk-test-not-used', BO_TENANT_ID: firm.tenant, BO_ADMIN_EMAILS: firm.admin, BACKEND_SHARED_SECRET: SVC, BACKEND_PROXY_SECRET: PXY, BO_PILOT_SERVICE_TOKEN: token };
    const python = (code) => execFileSync(PY, ['-c', code], { cwd: dir, env, encoding: 'utf8', timeout: 60000 }).trim().split('\n').pop();
    let backend, next, svc;
    try {
      const bp = await freePort(), np = await freePort(), sp = await freePort(), O = 'http://127.0.0.1:' + np, base = 'http://127.0.0.1:' + sp;
      const bfd = fs.openSync(dir + '/backend.log', 'w');
      backend = spawn(PY, ['-m', 'uvicorn', 'openexecutive.api.main:app', '--host', '127.0.0.1', '--port', String(bp)], { cwd: dir, env, stdio: ['ignore', bfd, bfd], detached: true });
      fs.closeSync(bfd); await ready('http://127.0.0.1:' + bp + '/health', backend);
      const nfd = fs.openSync(dir + '/next.log', 'w');
      next = spawn(process.execPath, SERVER ? [SERVER + '/server.js'] : [NEXT, 'start', '--hostname', '127.0.0.1', '--port', String(np)], { cwd: SERVER || NEXT_DIR, env: { ...process.env, PORT: String(np), HOSTNAME: '127.0.0.1', NEXT_TELEMETRY_DISABLED: '1', AUTH_SECRET: firm.secret, AUTH_URL: O, AUTH_TRUST_HOST: 'true', ALLOWED_EMAILS: firm.admin, BACKEND_BASE_URL: 'http://127.0.0.1:' + bp, BACKEND_SHARED_SECRET: SVC, BACKEND_PROXY_SECRET: PXY }, stdio: ['ignore', nfd, nfd], detached: true });
      fs.closeSync(nfd); await ready(O + '/signin', next);
      const cookie = await encode({ token: { sid: crypto.randomUUID(), email: firm.admin, sub: firm.admin }, secret: firm.secret, salt: 'authjs.session-token', maxAge: 3600 });
      const api = async (p, init = {}) => { const r = await fetch(O + '/api/backend/bo' + p, { ...init, headers: { cookie: 'authjs.session-token=' + cookie, 'content-type': 'application/json' } }); const t = await r.text(); let body = null; try { body = t ? JSON.parse(t) : null; } catch { body = t; } assert(is2xx(r.status), p + ' ' + r.status + ' ' + t.slice(0, 200)); return body; };

      // Data through the product: an artifact draft, a published BoBot simulation, a signed pilot package and one pilot run.
      const alert = JSON.parse(python(SEED)).alert;
      const ex = await api('/bots/example', { method: 'POST' });
      const botId = ex.bot?.id ?? ex.id;
      const bot = (await api('/bots/' + botId)).bot;
      await api(`/bots/${botId}/publish`, { method: 'POST', body: JSON.stringify({ expected_version: bot.draft_version }) });
      await api(`/bots/${botId}/simulate`, { method: 'POST', body: JSON.stringify({}) });
      const runId = (await api(`/bots/${botId}/runs`)).runs[0].id;
      fs.mkdirSync(dir + '/pilot-svc', { recursive: true });
      const sfd = fs.openSync(dir + '/pilot-svc/service.log', 'w');
      svc = spawn(PY, ['-m', 'openexecutive.bo.pilot.fixture', 'serve', '--work', dir + '/pilot-svc', '--port', String(sp)], { cwd: dir, env: { ...env, BO_PILOT_FIXTURE_TOKENS: JSON.stringify({ [token]: firm.tenant }) }, stdio: ['ignore', sfd, sfd], detached: true });
      fs.closeSync(sfd);
      for (let i = 0; ; i++) { try { await fetch(base + '/stats', { headers: { authorization: 'Bearer ' + token } }); break; } catch { if (i > 120) throw new Error('fixture not ready'); await new Promise((r) => setTimeout(r, 250)); } }
      const gen = JSON.parse(python(PACKAGE));
      const settings = (await api('/settings')).settings;
      for (const [k, v] of [['bo.exec.enabled', true], ['bo.packages.enabled', true], ['bo.packages.trust_store_json', gen.registry], ['bo.pilot.enabled', true], ['bo.pilot.profile', 'synthetic-loopback'], ['bo.pilot.endpoint', base], ['bo.pilot.allowlist', JSON.stringify([base])]]) await api('/settings/' + k, { method: 'PUT', body: JSON.stringify({ value: v, expected_version: settings.find((s) => s.key === k).version }) });
      const imp = await api('/packages/import', { method: 'POST', body: JSON.stringify({ source_dir: gen.root }) });
      await api(`/packages/${imp.id}/promote`, { method: 'POST' });
      await api('/pilot/activation', { method: 'PUT', body: JSON.stringify({ import_id: imp.id, active: true, expected_version: 0, reason: 'Aprobare pilot sintetic BO01' }) });
      const mk = await api('/execution/mandates', { method: 'POST', body: JSON.stringify({ allowed_resources: ['synth.erp'], allowed_actions: ['diagnose'], budget_limit: '100', budget_currency: 'USD', concurrency_limit: 4, max_steps: 1, max_depth: 2, expires_at: new Date(Date.now() + 3 * 3600e3).toISOString() }) });
      await api('/pilot/runs', { method: 'POST', body: JSON.stringify({ mandate_id: mk.mandate?.mandate_id ?? mk.mandate_id }) });
      await api('/execution/work', { method: 'POST', body: '{}' });

      const ctx = await browser.newContext({ viewport: { width: 1440, height: 900 }, locale: 'ro-RO', timezoneId: 'Europe/Bucharest' });
      await ctx.addCookies([{ name: 'authjs.session-token', value: cookie, url: O }]);
      await ctx.route('**/*', (r) => (new URL(r.request().url()).hostname === '127.0.0.1' ? r.continue() : r.abort()));
      const page = await ctx.newPage(); page.setDefaultTimeout(15000);
      page.on('pageerror', (e) => fr.pageErrors.push(String(e).slice(0, 300)));
      const contained = (vp, m) => { if (!m.contained.length) return [vp + ': nothing measured']; return m.contained.flatMap((c) => [!c.box && `${vp}: no card around «${c.text}»`, c.box && c.el.right > c.box.right + 1 && `${vp}: «${c.text}» ends at ${c.el.right}px, card at ${c.box.right}px`, c.scroll > 1 && `${vp}: «${c.text}» runs ${c.scroll}px past its box`].filter(Boolean)); };
      const minWidth = (min, what) => (vp, m) => (m.width >= Math.min(min, m.innerWidth - 80) ? [] : [`${vp}: ${what} ${m.width}px wide`]);
      const surfaces = [
        ['council', async () => { await page.goto(O + '/council'); await page.getByRole('heading', { name: 'Agent Council', level: 1 }).waitFor(); }, { width: 'main h1' }, minWidth(320, 'council editor')],
        ['skills', async () => { await page.goto(O + '/skills'); await page.getByText('board-prep-deck', { exact: true }).first().click(); await page.getByRole('heading', { name: 'board-prep-deck' }).first().waitFor(); }, { width: 'main h2' }, minWidth(300, 'skill detail')],
        ['artifact', async () => { await page.goto(O + '/artifacts/' + encodeURIComponent('alert:' + alert)); await page.getByRole('heading', { name: 'Memo BO01 furnizori și termene de plată', level: 1 }).waitFor({ state: 'attached' }); }, { width: 'h1.font-semibold.mb-1', visible: 'main button' },
          (vp, m) => [...minWidth(240, 'artifact title')(vp, m), ...m.visible.filter((b) => b.right > m.innerWidth || b.left < 0).map((b) => `${vp}: «${b.text}» at ${b.left}..${b.right}px`)]],
        ['demo', async () => { await page.goto(O + '/demo'); await page.getByRole('button', { name: 'Snapshot current as my company' }).waitFor(); }, { width: 'p.text-xs.flex-1' }, minWidth(220, 'snapshot text')],
        ['pilot', async () => { await page.goto(O + '/bo/pilot'); await page.locator('details summary').first().waitFor(); await page.evaluate(() => document.querySelectorAll('details').forEach((d) => { d.open = true; })); }, { contained: '.bo-card p.bo-mono, .bo-card details p', within: '.bo-card' }, contained],
        ['run', async () => { await page.goto(O + '/bo/runs/' + encodeURIComponent(runId)); await page.locator('dl.bo-kv').first().waitFor(); }, { contained: 'dl.bo-kv dd', within: '.bo-card' }, contained],
        ['package-verdict', async () => { await page.goto(O + '/bo/packages'); const b = page.getByRole('button', { name: /^Verdict pentru / }).first(); await b.waitFor(); if ((await b.getAttribute('aria-expanded')) !== 'true') await b.click(); await page.getByText('manifestDigest', { exact: true }).first().waitFor(); }, { contained: 'dl dd.bo-mono', within: '.bo-card' }, contained],
      ];
      for (const [name, go, spec, judge] of surfaces) {
        fr.geometry[name] = {};
        for (const [vp, width, height] of VIEWPORTS) {
          await page.setViewportSize({ width, height });
          await go(); await page.waitForTimeout(400);
          const m = await measure(page, spec); fr.geometry[name][vp] = m;
          await page.screenshot({ path: `${dir}/${name}-${vp}.png`, animations: 'disabled' });
          if (m.docHScroll !== 0) fr.failures.push(`${name} ${vp}: page scrolls horizontally ${m.docHScroll}px`);
          fr.failures.push(...judge(vp, m).map((f) => name + ' ' + f));
        }
      }
      await ctx.close();
      assert.deepEqual(fr.failures, []);
      assert.deepEqual(fr.pageErrors, []);
    } finally { kill(svc); kill(next); kill(backend); }
  });
}
