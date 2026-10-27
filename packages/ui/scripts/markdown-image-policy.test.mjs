// D5/C10: images inside rendered Markdown never beacon to an unlisted host,
// never carry Referer/credentials, and every Markdown sink goes through the
// one policy-enforcing wrapper.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import {
  allowedImageAttributes,
  blockedImageLabel,
  decideImage,
  parseImagePolicy,
} from '../src/lib/markdown-image-policy.ts';

const none = parseImagePolicy(undefined);
const listed = parseImagePolicy(' https://img.example.com , https://cdn.example.org/ ,data:raster, http://plain.example.com, https://x.example.com/path, https://u:p@y.example.com, not a url, https://img.example.com ');

test('policy parsing keeps only https origins', () => {
  assert.deepEqual(none, { origins: [], rejected: [] });
  assert.deepEqual(listed.origins, ['https://img.example.com', 'https://cdn.example.org']);
  assert.deepEqual(listed.rejected, ['data:raster', 'http://plain.example.com', 'https://x.example.com/path', 'https://u:p@y.example.com', 'not a url']);
});

test('default policy: own relative files load, everything external is refused without a request', () => {
  for (const src of ['/logo.png', 'img/chart.png', './a.webp', '/fonts/x.png?v=1']) {
    assert.deepEqual(decideImage(src, none), { allow: true, url: src, kind: 'relative' }, src);
  }
  const refused = {
    'https://tracker.example.net/p.gif?u=1': 'origin',
    'http://127.0.0.1:9/p.png': 'origin',
    '//evil.example/p.png': 'origin',
    '/\\evil.example/p.png': 'origin',
    '/\t/evil.example/p.png': 'origin',
    '\n//evil.example/p.png': 'origin',
    'HTTPS://img.example.com/a.png': 'origin',
    '/api/backend/people/1/archive': 'api',
    '/api': 'api',
    'api/../api/backend/x': 'api',
    'javascript:alert(1)': 'scheme',
    'data:image/png;base64,iVBORw0KGgo=': 'scheme',
    'blob:https://x/1': 'scheme',
    'file:///etc/passwd': 'scheme',
    'ftp://x/a.png': 'scheme',
    '': 'empty', '   ': 'empty',
  };
  for (const [src, reason] of Object.entries(refused)) {
    const d = decideImage(src, none);
    assert.equal(d.allow, false, src); assert.equal(d.reason, reason, src);
  }
  assert.equal(decideImage(undefined, none).reason, 'empty');
  assert.equal(decideImage(42, none).reason, 'empty');
});

test('listed origins load over https only; data: never, whatever the list says', () => {
  assert.deepEqual(decideImage('https://img.example.com/a.png', listed), { allow: true, url: 'https://img.example.com/a.png', kind: 'listed' });
  assert.equal(decideImage('https://IMG.example.com/a.png', listed).allow, true);
  for (const src of ['http://img.example.com/a.png', 'https://img.example.com:8443/a.png', 'https://evil.img.example.com/a.png', 'https://img.example.com.evil.net/a.png', 'https://u:p@img.example.com/a.png']) {
    assert.equal(decideImage(src, listed).allow, false, src);
  }
  for (const src of ['data:image/png;base64,iVBORw0KGgo=', 'data:image/svg+xml;base64,PHN2Zz4=', 'data:text/html;base64,PHNjcmlwdD4=', 'data:image/png,<svg onload=x>']) {
    assert.equal(decideImage(src, listed).allow, false, src);
  }
});

test('allowed images are lazy, fluid, without Referer, and cross-origin ones without credentials', () => {
  const ext = allowedImageAttributes(decideImage('https://img.example.com/a.png', listed));
  assert.equal(ext.loading, 'lazy'); assert.equal(ext.decoding, 'async');
  assert.equal(ext.referrerPolicy, 'no-referrer'); assert.equal(ext.crossOrigin, 'anonymous');
  assert.deepEqual(ext.style, { maxWidth: '100%', height: 'auto' });
  const own = allowedImageAttributes(decideImage('/logo.png', none));
  assert.equal(own.referrerPolicy, 'no-referrer'); assert.equal(own.crossOrigin, undefined);
  assert.equal(blockedImageLabel(decideImage('https://t.example.net/p.gif', none), 'grafic'), 'Imagine neîncărcată („grafic”) de la t.example.net: sursa nu este permisă.');
  assert.equal(blockedImageLabel(decideImage('javascript:x', none), ''), 'Imagine neîncărcată (fără descriere): sursa nu este permisă.');
});

test('every Markdown sink renders through SafeMarkdown, which always owns img', () => {
  const src = path.resolve(new URL('../src', import.meta.url).pathname);
  const files = [];
  const walk = (d) => { for (const e of fs.readdirSync(d, { withFileTypes: true })) { const p = path.join(d, e.name); if (e.isDirectory()) walk(p); else if (/\.(tsx?|jsx?)$/.test(e.name)) files.push(p); } };
  walk(src);
  const direct = files.filter((f) => /from\s+["']react-markdown["']/.test(fs.readFileSync(f, 'utf8'))).map((f) => path.relative(src, f));
  assert.deepEqual(direct, ['components/SafeMarkdown.tsx']);
  const sinks = files.filter((f) => /from\s+["']@\/components\/SafeMarkdown["']/.test(fs.readFileSync(f, 'utf8'))).map((f) => path.relative(src, f)).sort();
  assert.deepEqual(sinks, ['app/artifacts/[id]/page.tsx', 'app/jobs/runs/[id]/page.tsx', 'components/Briefing.tsx', 'components/Message.tsx', 'components/SkillsBrowser.tsx', 'components/architecture/DynamicSection.tsx', 'components/knowledge/CompanyPanel.tsx', 'components/knowledge/FileEditor.tsx']);
  const wrapper = fs.readFileSync(path.join(src, 'components/SafeMarkdown.tsx'), 'utf8');
  assert.match(wrapper, /components=\{\{ \.\.\.props\.components, img \}\}/, 'img override must come last so no caller can replace it');
  assert.doesNotMatch(wrapper, /rehypeRaw|rehype-raw/, 'raw HTML would bypass the img override');
});
