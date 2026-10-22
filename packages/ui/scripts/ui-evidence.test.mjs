import test from 'node:test';
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { evidenceOutput, evidencePin, evidenceSpecs, validateEvidence } from './ui-evidence.mjs';
const scripts = path.dirname(fileURLToPath(import.meta.url));
const pin = 'a'.repeat(40);
const work = path.resolve(process.env.BO_EVIDENCE_TEST_OUTPUT || 'ui-evidence-unit-results');
fs.mkdirSync(work, { recursive: true });
let serial = 0;
function fixture(kind = 'actions') {
  const dir = fs.mkdtempSync(path.join(work, `${kind}-${++serial}-`));
  const spec = evidenceSpecs[kind];
  const result = { evidenceVersion: 1, pin, UIoverlay: false, backendOverlay: false,
    mode: kind === 'reference' ? 'CI_ACTUAL_NEXT_STATIC_VERSIONED_REFERENCE_FIXTURE' : 'CI_CONTROLLED_HTTP_NOT_DOMAIN_ACCEPTANCE',
    checks: Array.from({ length: spec.checks }, (_, i) => ({ id: `case-${i}`, status: 'PASS' })),
    captures: [], pageErrors: [], runtime: { headless: true, chromiumSandbox: true, sandboxDisableFlagsAbsent: true } };
  for (let i = 0; i < spec.captures; i++) {
    const [name, width, height] = [['desktop', 1440, 900], ['tablet', 1024, 768], ['mobile', 390, 844]][i % 3];
    // Header fixtures exercise the evidence contract, these are not browser screenshots.
    const png = Buffer.alloc(33); Buffer.from([137,80,78,71,13,10,26,10]).copy(png);
    png.write('IHDR', 12); png.writeUInt32BE(width, 16); png.writeUInt32BE(height, 20);
    const file = `capture-${i}-${name}.png`; fs.writeFileSync(path.join(dir, file), png); result.captures.push(file);
  }
  fs.writeFileSync(path.join(dir, 'JUNIT.xml'), '<testsuites><testsuite tests="1" failures="0" skipped="0"><testcase name="executed"/></testsuite></testsuites>');
  const save = () => fs.writeFileSync(path.join(dir, 'RESULT.json'), JSON.stringify(result)); save();
  return { dir, result, save };
}
for (const kind of Object.keys(evidenceSpecs)) {
  test(`${kind} own output, legacy compatibility, conflicting directories`, () => {
    const spec = evidenceSpecs[kind];
    assert.equal(evidenceOutput(kind, { [spec.env]: work }), work);
    assert.equal(evidenceOutput(kind, { BO_READ_ERROR_OUTPUT: work }), work);
    assert.throws(() => evidenceOutput(kind, { [spec.env]: work, BO_READ_ERROR_OUTPUT: work + '/other' }));
  });
  test(`${kind} driver and workflow share output, pin and validation`, () => {
    const name = { actions: 'review-action', changes: 'review-change', reference: 'reference-async' }[kind];
    const driverScripts = process.env.BO_EVIDENCE_DRIVER_ROOT ? path.join(process.env.BO_EVIDENCE_DRIVER_ROOT, 'packages/ui/scripts') : scripts;
    const source = fs.readFileSync(path.join(driverScripts, name + '-states.mjs'), 'utf8');
    assert(source.includes(`const output=evidenceOutput('${kind}')`));
    assert(source.includes('pin:evidencePin()'));
    const workflow = fs.readFileSync(path.resolve(scripts, '../../../.github/workflows/ci.yml'), 'utf8');
    assert(workflow.includes(`node scripts/ui-evidence.mjs ${kind} "$BO_READ_ERROR_OUTPUT"`));
    assert(workflow.includes('BO_UI_SOURCE_SHA: ${{ github.sha }}'));
  });
  test(`${kind} complete synthetic contract passes`, () => { const f = fixture(kind); assert.equal(validateEvidence(kind, f.dir, pin).captures, evidenceSpecs[kind].captures); });
}
for (const [name, mutate] of [
  ['absent pin', r => { delete r.pin; }], ['wrong pin', r => { r.pin = 'b'.repeat(40); }],
  ['overlay', r => { r.UIoverlay = true; }], ['missing mode', r => { delete r.mode; }],
  ['failed check', r => { r.checks[0].status = 'FAIL'; }], ['missing check', r => { r.checks.pop(); }],
  ['duplicate check', r => { r.checks[0].id = r.checks[1].id; }],
  ['missing captures', r => { r.captures = []; }], ['browser error', r => { r.pageErrors = ['error']; }],
  ['sandbox disabled', r => { r.runtime.chromiumSandbox = false; }], ['failure', r => { r.failure = 'timeout'; }],
  ['capture traversal', r => { r.captures[0] = '../capture-desktop.png'; }],
]) test(`reject ${name}`, () => { const f = fixture(); mutate(f.result); f.save(); assert.throws(() => validateEvidence('actions', f.dir, pin)); });
test('reject JUnit only artifact', () => {
  const dir = fs.mkdtempSync(path.join(work, 'junit-only-')); fs.writeFileSync(path.join(dir, 'JUNIT.xml'), '<testcase/>');
  assert.throws(() => validateEvidence('actions', dir, pin), /ENOENT/);
});
for (const [name, value] of [['skip', '<testcase><skipped/></testcase>'], ['failure', '<testcase><failure/></testcase>'], ['empty', '<testsuite tests="0"/>']])
  test(`reject JUnit ${name}`, () => { const f = fixture(); fs.writeFileSync(path.join(f.dir, 'JUNIT.xml'), value); assert.throws(() => validateEvidence('actions', f.dir, pin)); });
test('reject missing and corrupt captures', () => {
  const f = fixture(); f.result.captures[0] = 'missing-desktop.png'; f.save(); assert.throws(() => validateEvidence('actions', f.dir, pin));
  const g = fixture(); fs.writeFileSync(path.join(g.dir, g.result.captures[0]), 'not a PNG'); assert.throws(() => validateEvidence('actions', g.dir, pin));
});
if (process.env.BO_EVIDENCE_SOURCE_ROOT) test('read actual Git pin and reject false claimed pin', () => {
  const root = process.env.BO_EVIDENCE_SOURCE_ROOT, actual = evidencePin(root, {});
  assert.match(actual, /^[0-9a-f]{40}$/); assert.throws(() => evidencePin(root, { GITHUB_SHA: pin }));
});
