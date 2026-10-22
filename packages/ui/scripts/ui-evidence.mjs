import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';

export const evidenceSpecs = {
  actions: { env: 'BO_REVIEW_ACTIONS_OUTPUT', fallback: 'review-action-results', checks: 16, captures: 12 },
  changes: { env: 'BO_REVIEW_CHANGES_OUTPUT', fallback: 'review-change-results', checks: 15, captures: 9 },
  reference: { env: 'BO_REFERENCE_ASYNC_OUTPUT', fallback: 'reference-async-results', checks: 12, captures: 6 },
};
export function evidenceOutput(kind, env = process.env) {
  const spec = evidenceSpecs[kind];
  assert(spec, 'Unknown evidence kind');
  const own = env[spec.env], legacy = env.BO_READ_ERROR_OUTPUT;
  assert(!own || !legacy || path.resolve(own) === path.resolve(legacy), 'Conflicting evidence output directories');
  return path.resolve(own || legacy || spec.fallback);
}
export function evidencePin(root = fileURLToPath(new URL('../../..', import.meta.url)), env = process.env) {
  const pin = execFileSync('git', ['-C', root, 'rev-parse', 'HEAD'], { encoding: 'utf8' }).trim();
  assert.match(pin, /^[a-f0-9]{40}$/);
  for (const expected of [env.GITHUB_SHA, env.BO_UI_SOURCE_SHA, env.BO01_REVIEW_SHA].filter(Boolean)) {
    assert.equal(pin, expected, 'Evidence pin differs from checked out source');
  }
  return pin;
}
export function validateEvidence(kind, directory, expectedPin) {
  const spec = evidenceSpecs[kind];
  assert(spec, 'Unknown evidence kind');
  assert.match(expectedPin || '', /^[a-f0-9]{40}$/, 'Expected full source pin is required');
  const result = JSON.parse(fs.readFileSync(path.join(directory, 'RESULT.json'), 'utf8'));
  assert.equal(result.pin, expectedPin, 'Wrong or absent result pin');
  assert.equal(result.evidenceVersion, 1, 'Evidence contract version required');
  assert.equal(result.UIoverlay, false, 'UI overlay cannot pass integrated CI evidence');
  assert.equal(result.backendOverlay, false);
  assert.match(result.mode || '', /^(CI_.*CONTROLLED_HTTP|CI_ACTUAL_NEXT_STATIC_VERSIONED_REFERENCE_FIXTURE$|ACTUAL_)/, 'Explicit execution mode required');
  assert(!result.failure, result.failure);
  assert.deepEqual(result.pageErrors, [], 'Browser page errors');
  assert(result.runtime?.headless && result.runtime?.chromiumSandbox && result.runtime?.sandboxDisableFlagsAbsent, 'Sandbox runtime proof required');
  assert(Array.isArray(result.checks) && result.checks.length >= spec.checks, 'Missing nominal checks');
  assert(result.checks.every(c => c.id && c.status === 'PASS'), 'A check did not pass');
  assert.equal(new Set(result.checks.map(c => c.id)).size, result.checks.length, 'Duplicate checks');
  assert(Array.isArray(result.captures) && result.captures.length >= spec.captures, 'Missing captures');
  assert.equal(new Set(result.captures).size, result.captures.length, 'Duplicate captures');
  for (const name of result.captures) {
    assert.equal(path.basename(name), name, 'Capture path must be a basename');
    assert.match(name, /-(desktop|tablet|mobile)\.png$/);
    const file = path.join(directory, name);
    assert(fs.lstatSync(file).isFile(), 'Capture must be a regular file');
    const png = fs.readFileSync(file);
    assert(png.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10])), 'Invalid PNG signature');
    assert.equal(png.toString('ascii', 12, 16), 'IHDR');
    const size = name.endsWith('-desktop.png') ? [1440, 900] : name.endsWith('-tablet.png') ? [1024, 768] : [390, 844];
    assert.deepEqual([png.readUInt32BE(16), png.readUInt32BE(20)], size, 'Unexpected capture viewport');
  }
  for (const viewport of ['desktop', 'tablet', 'mobile']) assert(result.captures.some(n => n.endsWith('-' + viewport + '.png')), 'Missing viewport');
  const junit = fs.readFileSync(path.join(directory, 'JUNIT.xml'), 'utf8');
  assert.match(junit, /<testcase\b/, 'JUnit must contain executed tests');
  assert(!/<(?:failure|error|skipped)\b/.test(junit), 'JUnit contains failure, error or skip');
  assert(!/\b(?:failures|errors|skipped)="[1-9][0-9]*"/.test(junit), 'JUnit reports failure, error or skip');
  return { kind, pin: result.pin, mode: result.mode, checks: result.checks.length, captures: result.captures.length };
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const [kind, directory] = process.argv.slice(2);
  console.log(JSON.stringify(validateEvidence(kind, directory, evidencePin())));
}
