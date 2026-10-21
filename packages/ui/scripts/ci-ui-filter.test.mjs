// @requirement F10 — CI diff filter must cover every input that can change
// the browser probes' outcome, not only packages/ui.
//
// Defect reproduced by the coordinator's audit on run 39597330863: the UI
// job's diff filter matched only `^packages/ui/`, so a workflow-only change
// (the commit that fixed the Chromium sandbox) ran with every browser step
// SKIP — green without ever executing the probes. A skip is not a pass.
//
// Contract asserted here:
//   1. UI inputs (`packages/ui/`), the workflow itself
//      (`.github/workflows/` — the probes' infrastructure) and the backend
//      HTTP surface the UI consumes (`packages/core/openexecutive/api/`)
//      all trigger the UI steps.
//   2. Docs-only pushes stay skipped (`paths-ignore: **/*.md`) — the .md
//      economy is preserved, not widened.
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import test from 'node:test';
import assert from 'node:assert/strict';

const here = dirname(fileURLToPath(import.meta.url));
const workflow = readFileSync(
  join(here, '..', '..', '..', '.github', 'workflows', 'ci.yml'), 'utf8');

test('F10: the UI diff filter matches the real probe inputs', () => {
  const trigger = workflow.match(/grep -qE '([^']+)'/);
  assert(trigger, 'ci.yml must classify diff paths with a grep pattern');
  const pattern = trigger[1];
  for (const prefix of [
    'packages/ui/',
    '.github/workflows/',
    'packages/core/openexecutive/api/',
  ]) {
    assert(
      pattern.includes(prefix),
      `UI diff filter must include ${prefix} — ` +
        'a change there must run the browser probes, not skip them',
    );
  }
});

test('F10: docs-only pushes still skip the whole workflow', () => {
  assert.match(
    workflow,
    /paths-ignore:\s*\[?\s*'?\*\*\/\*\.md/,
    'the .md economy (paths-ignore) must be preserved',
  );
});
