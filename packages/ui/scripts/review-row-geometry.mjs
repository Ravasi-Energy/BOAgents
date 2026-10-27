// Review list row geometry through the real Next server, proxy and FastAPI
// backend, two firms, three viewports. At every width the reviewer must be able
// to read which file a row is (filename visible, not clipped to nothing), read
// the date on one line, and the priority control must not cover the row text.
// Baseline is expected to fail at 390px; the delivery must pass everywhere.
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
const OUT = path.resolve(process.env.BO_OUT || path.join(process.env.BO_READ_ERROR_OUTPUT || 'review-roles-results', 'review-row-geometry'));
const CORE = path.resolve(UI, '../core');
const PY = process.env.BO_CORE_PYTHON || path.join(CORE, '.venv/bin/python');
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
  { tenant: 'firma-a', admin: 'admin@firma-a.test', secret: 'BO01-review-row-firma-a-secret-32-chars-min', file: 'firma-a-contract-cadru-2026.pdf' },
  { tenant: 'firma-b', admin: 'admin@firma-b.test', secret: 'BO01-review-row-firma-b-secret-32-chars-min', file: 'firma-b-proces-verbal-receptie.pdf' },
];
const SVC = 'svc-secret-test-32-chars-minimum-xx', PXY = 'proxy-secret-test-32-characters';
const sha = (b) => crypto.createHash('sha256').update(b).digest('hex');
const result = { at: new Date().toISOString(), server: SERVER ? 'standalone:' + SERVER : 'next start:' + NEXT_DIR, firms: [] };

async function freePort() { const s = http.createServer(); await new Promise((ok, no) => { s.once('error', no); s.listen(0, '127.0.0.1', ok); }); const p = s.address().port; await new Promise((ok) => s.close(ok)); return p; }
function kill(p) { if (p && p.exitCode === null && p.signalCode === null) { try { process.kill(-p.pid, 'SIGTERM'); } catch { try { p.kill('SIGTERM'); } catch {} } } }
async function ready(url, p) { for (let i = 0; i < 240; i++) { if (p.exitCode !== null) throw new Error('process exited early: ' + url); try { await fetch(url); return; } catch { await new Promise((ok) => setTimeout(ok, 250)); } } throw new Error('timeout ' + url); }

let browser;
test.before(async () => {
  browser = await chromium.launch({ headless: true, chromiumSandbox: true, args: ['--enable-automation'], env: { ...process.env, TMPDIR: OUT + '/tmp' } });
  const cd = await browser.newBrowserCDPSession(); const cl = (await cd.send('Browser.getBrowserCommandLine')).arguments; await cd.detach();
  assert(!cl.includes('--no-sandbox') && !cl.includes('--disable-setuid-sandbox'));
  result.browser = { chromiumSandbox: true, version: browser.version() };
});
test.after(async () => {
  await browser?.close();
  result.captures = fs.readdirSync(OUT).filter((f) => f.endsWith('.png')).sort().map((f) => ({ file: f, sha256: sha(fs.readFileSync(path.join(OUT, f))) }));
  fs.writeFileSync(OUT + '/RESULT.json', JSON.stringify(result, null, 2));
});

for (const firm of FIRMS) {
  test(`Review row stays readable at three widths — ${firm.tenant}`, { timeout: 300000 }, async () => {
    const fr = { tenant: firm.tenant, file: firm.file, rows: [], pageErrors: [] };
    result.firms.push(fr);
    const dir = OUT + '/' + firm.tenant; fs.mkdirSync(dir, { recursive: true });
    const env = { ...process.env, PYTHONPATH: CORE, PYTHONDONTWRITEBYTECODE: '1', VECTOR_STORE_PATH: dir + '/chroma_db', EPISODIC_DB_PATH: dir + '/episodic.db', BOAGENTS_DB_PATH: dir + '/bo_agents.db', EXEC_EMAIL_ADDRESS: 'exec@' + firm.tenant + '.test', ANTHROPIC_API_KEY: 'sk-test-not-used', BO_TENANT_ID: firm.tenant, BO_ADMIN_EMAILS: firm.admin, BACKEND_SHARED_SECRET: SVC, BACKEND_PROXY_SECRET: PXY };
    execFileSync(PY, ['-c', "import sys;from openexecutive.knowledge.review_store import ContentType,ReviewStore;db=sys.argv[1];ReviewStore.initialize_db(db);ReviewStore(db_path=db).register('external:row:'+sys.argv[2],ContentType.EXTERNAL,'row',sys.argv[2])", dir + '/episodic.db', firm.file], { cwd: dir, env });
    let backend, next;
    try {
      const bp = await freePort(), np = await freePort(), O = 'http://127.0.0.1:' + np;
      const bfd = fs.openSync(dir + '/backend.log', 'w');
      backend = spawn(PY, ['-m', 'uvicorn', 'openexecutive.api.main:app', '--host', '127.0.0.1', '--port', String(bp)], { cwd: dir, env, stdio: ['ignore', bfd, bfd], detached: true });
      fs.closeSync(bfd); await ready('http://127.0.0.1:' + bp + '/health', backend);
      const nfd = fs.openSync(dir + '/next.log', 'w');
      next = spawn(process.execPath, SERVER ? [SERVER + '/server.js'] : [NEXT, 'start', '--hostname', '127.0.0.1', '--port', String(np)], { cwd: SERVER || NEXT_DIR, env: { ...process.env, PORT: String(np), HOSTNAME: '127.0.0.1', NEXT_TELEMETRY_DISABLED: '1', AUTH_SECRET: firm.secret, AUTH_URL: O, AUTH_TRUST_HOST: 'true', ALLOWED_EMAILS: firm.admin, BACKEND_BASE_URL: 'http://127.0.0.1:' + bp, BACKEND_SHARED_SECRET: SVC, BACKEND_PROXY_SECRET: PXY }, stdio: ['ignore', nfd, nfd], detached: true });
      fs.closeSync(nfd); await ready(O + '/signin', next);
      const token = await encode({ token: { sid: crypto.randomUUID(), email: firm.admin, sub: firm.admin }, secret: firm.secret, salt: 'authjs.session-token', maxAge: 3600 });
      for (const [name, width, height] of VIEWPORTS) {
        const ctx = await browser.newContext({ viewport: { width, height } });
        await ctx.addCookies([{ name: 'authjs.session-token', value: token, url: O }]);
        await ctx.route('**/*', (r) => (new URL(r.request().url()).hostname === '127.0.0.1' ? r.continue() : r.abort()));
        const page = await ctx.newPage();
        page.on('pageerror', (e) => fr.pageErrors.push({ viewport: name, message: String(e) }));
        await page.goto(O + '/review', { waitUntil: 'networkidle' });
        await page.getByText(firm.file, { exact: true }).waitFor({ state: 'attached' });
        const g = await page.evaluate((file) => {
          const name = [...document.querySelectorAll('span')].find((s) => s.textContent === file);
          const row = name.closest('div.rounded-lg');
          const date = [...row.querySelectorAll('p')].find((p) => p.textContent.startsWith('Added'));
          const select = row.querySelector('select');
          const r = (e) => { const b = e.getBoundingClientRect(); return { x: b.x, y: b.y, w: b.width, h: b.height, right: b.right, bottom: b.bottom }; };
          const lh = parseFloat(getComputedStyle(date).lineHeight) || parseFloat(getComputedStyle(date).fontSize) * 1.5;
          const info = name.parentElement.parentElement;
          const ib = r(info), sb = r(select);
          const overlap = !(sb.right <= ib.x || sb.x >= ib.right || sb.bottom <= ib.y || sb.y >= ib.bottom);
          const buttons = [...row.querySelectorAll('button')].map((b) => ({ text: b.textContent.trim(), ...r(b) }));
          return { name: r(name), nameScrollWidth: name.scrollWidth, date: r(date), dateLines: Math.round(r(date).h / lh), info: ib, select: sb, infoSelectOverlap: overlap, row: r(row), buttonsInsideRow: buttons.every((b) => b.x >= r(row).x - 0.5 && b.right <= r(row).right + 0.5), buttons };
        }, firm.file);
        fr.rows.push({ viewport: name, ...g });
        await page.locator('div.rounded-lg', { hasText: firm.file }).last().screenshot({ path: `${OUT}/${firm.tenant}-${name}-row.png` });
        await page.screenshot({ path: `${OUT}/${firm.tenant}-${name}-review.png` });
        await ctx.close();
      }
      for (const row of fr.rows) {
        // The filename must be readable: either fully shown or truncated with at least ~12 characters visible.
        assert(row.name.w >= Math.min(row.nameScrollWidth, 96), `${row.viewport}: filename clipped to ${row.name.w.toFixed(0)}px`);
        assert.equal(row.dateLines, 1, `${row.viewport}: date wraps over ${row.dateLines} lines`);
        assert.equal(row.infoSelectOverlap, false, `${row.viewport}: priority control overlaps the row text`);
        assert(row.buttonsInsideRow, `${row.viewport}: action buttons overflow the row`);
      }
      assert.deepEqual(fr.pageErrors, []);
    } finally { kill(next); kill(backend); }
  });
}
