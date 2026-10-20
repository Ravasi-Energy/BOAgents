import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { createRequire } from 'node:module';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const root = process.env.BO01_UI_APP || path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const appRequire = createRequire(path.join(root, 'package.json'));
// A candidate harness may supply integrity-verified package sources in RAM.
// In normal CI no override exists: the exact installed lock modules are used.
function loadRAM(packages, name, fallback) {
  const p = fallback('node:path').posix, pkg = packages[name], cache = new Map();
  function load(key) {
    key = p.normalize(key);
    if (cache.has(key)) return cache.get(key).exports;
    if (!(key in pkg.sources)) throw new Error('Missing RAM source ' + key);
    const mod = { exports: {} }; cache.set(key, mod);
    const req = spec => {
      if (!spec.startsWith('.')) return fallback(spec);
      const base = p.join(p.dirname(key), spec);
      const found = [base, base + '.js', p.join(base, 'index.js')].find(k => k in pkg.sources);
      if (!found) throw new Error('Missing RAM dependency ' + spec);
      return load(found);
    };
    Function('require', 'module', 'exports', pkg.sources[key])(req, mod, mod.exports);
    return mod.exports;
  }
  return load(pkg.entry);
}
const load = name => globalThis.__BO01_TEST_PACKAGES?.[name]
  ? loadRAM(globalThis.__BO01_TEST_PACKAGES, name, appRequire) : appRequire(name);

test('GHSA-68fv-2mgg-jv7q: indexed map far past generated code finishes without empty-line expansion', () => {
  const program = `const fs=require('node:fs'),{createRequire}=require('node:module');
    const cfg=JSON.parse(fs.readFileSync(0,'utf8')),req=createRequire(cfg.root+'/package.json');
    const loadRAM=${loadRAM.toString()};
    const sm=cfg.packages?.['source-map-js']?loadRAM(cfg.packages,'source-map-js',req):req('source-map-js');
    try {
      const consumer=new sm.SourceMapConsumer({version:3,sections:[{offset:{line:1000000000,column:0},
        map:{version:3,sources:['safe.js'],names:[],mappings:'AAAA'}}]});
      const node=sm.SourceNode.fromStringWithSourceMap('safe\\n',consumer);
      require('node:assert/strict').equal(node.toString(),'safe\\n');
    } catch(error) {
      // Rejecting an offset outside the published bound is a safe outcome.
      require('node:assert/strict').match(error.message,/Section offset line must not exceed/);
    }
    process.stdout.write('bounded');`;
  const child = spawnSync(process.execPath, ['-e', program], {
    input: JSON.stringify({root, packages: globalThis.__BO01_TEST_PACKAGES}), encoding: 'utf8',
    timeout: 1500, killSignal: 'SIGKILL', maxBuffer: 4096,
  });
  assert.equal(child.status, 0, `Indexed-map worker failed/expired: ${child.error?.code || child.signal || child.stderr}`);
  assert.equal(child.stdout, 'bounded');
});

test('source-map-js: ordinary generated content and source mapping are retained', () => {
  const {SourceMapConsumer, SourceNode} = load('source-map-js');
  const consumer = new SourceMapConsumer({version:3,sources:['safe.js'],names:[],mappings:'AAAA'});
  const node = SourceNode.fromStringWithSourceMap('const safe = 1;\n', consumer);
  assert.equal(node.toString(), 'const safe = 1;\n');
  assert.equal(node.toStringWithSourceMap().map.toJSON().sources[0], 'safe.js');
});

// BOAgents only: Guardian receives the two source-map tests above.
test('GHSA-238p-pmpm-9mq7: inherited trust does not permit a javascript math link', () => {
  const katex = load('katex');
  const options = Object.create({trust: true});
  options.throwOnError = false;
  const html = katex.renderToString('\\href{javascript:BO01_INERT_CANARY}{safe}', options);
  assert.doesNotMatch(html, /href\s*=\s*["']\s*javascript:/i);
});

test('KaTeX: ordinary formula rendering remains available without trusted links', () => {
  const html = load('katex').renderToString('x^2 + 1', {trust: false, throwOnError: true});
  assert.match(html, /class="katex"/);
  assert.match(html, /<math/);
});
