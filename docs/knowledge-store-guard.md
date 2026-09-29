# Knowledge store integrity guard — operator procedure

`ChromaDBStore` validates every persisted collection's **serialized schema**
before any operation that could build an embedding function from it. ChromaDB
1.5.9 instantiates a collection's persisted `embedding_function` config at
schema-deserialize/embed time (`Schema.deserialize_from_json` →
`known_embedding_functions[name].build_from_config`), which means a
`schema_str` row tampered inside `/data/chroma_db/chroma.sqlite3` can turn the
next `add`/`query` into code loading (e.g. `sentence_transformer` with
`trust_remote_code`). The guard rejects any `embedding_function` node that is
not exactly what this store writes (`{"type":"known","name":"default",
"config":{}}`, `onnx_mini_lm_l6_v2`, or inert `legacy`/unnamed `unknown`),
using `col._model.serialized_schema` — a read that itself builds nothing.

**Scope.** This is *local risk reduction*, not a fix for the four open
chromadb 1.5.9 advisories (no patched upstream release exists at time of
writing). Collections are content categories inside one active company, not
tenant isolation. The deployment contract stays: **embedded
`PersistentClient` only — never `chromadb run` / `HttpClient`**, and the
vector-store volume is a privileged filesystem boundary.

## Where `PersistedEmbeddingConfigError` surfaces

| Path | Effect | What the operator sees |
|---|---|---|
| API boot (`api/main.py` lifespan) | API does not start | `PersistedEmbeddingConfigError` in logs; `/health` never answers |
| Scheduled/overnight workflows (`scheduler/runner.py`, `clients/rotation.py`, `workflows/resumer.py`) | That job/run fails | run marked failed with the error as `last_error` |
| Client-slot activation/restore (`clients/slots.py`) | Refused before any live mutation: the store is preflighted in `activate_client_slot` (and inside `_restore_slot_state`), so DB, profile/docs, MCP config and the `.active_client` sentinel all stay the previous client's. A refusal *after* preflight triggers automatic save-back-free recovery to the previous coherent state; if recovery also fails the instance enters restore-blocked (`.restore_blocked` marker, API 503, scheduler/resumer hold) until the recorded recovery succeeds. Overnight rotation additionally force-restores the original client (`_force_restore`) on activation failure. | activation error; live state provably coherent — previous state restored, or instance fenced off |
| Attachment ingest (`integrations/attachments.py`) | That request fails | HTTP 500 on the upload |

The error message names the collection and the schema path of the offending
node — it never carries document contents or config payloads.

## Situation A — persisted configuration rejected

The store opened fine, but a collection's schema contains an
`embedding_function` this application never writes. Treat the volume as
suspect until proven otherwise.

1. **Stop every writer.** Stop the API/scheduler container (`docker compose
   stop` on the service). Nothing must boot against the suspect directory.
2. **Quarantine, preserve evidence.** With all writers stopped, move the
   whole `/data/chroma_db/` aside (`mv chroma_db chroma_db.suspect-YYYYMMDD`).
   Renaming does **not** make the directory read-only — the protection is
   that no running process has it open; where the deployment supports it,
   additionally mount the volume read-only or `chmod -R a-w` the quarantined
   copy. Do **not** delete `chroma.sqlite3`, do **not** edit `schema_str`,
   do **not** disable the guard, do **not** start the app against the
   suspect copy.
3. **Identify versions.** Record `pip show chromadb` / the image tag and
   `git rev-parse HEAD` of the running build, plus the guard error line.
4. **Rebuild from trusted sources — the index is derived data:**
   - built-in knowledge re-seeds at boot from `openexecutive/knowledge/builtin/`;
   - company docs re-index from `/data/company/docs/` (the upload source
     of truth) via the normal ingest path / slot restore;
   - `inbound_attachments` and `recent_research` are **not** re-derivable —
     they restore only from a volume backup predating the tamper, or are
     lost. State this in the incident record.
5. Provision a fresh empty `VECTOR_STORE_PATH`, start the service, confirm
   boot, then re-ingest/re-sync company docs. Keep the suspect directory
   untouched until the incident is closed.

### If a client switch fails mid-restore (partial live state)

Preflighting refuses before the first mutation, so a rejected store leaves
the previous client fully live. One residual window remains: if the volume
is tampered *between* preflight and the rebuild calls, a mid-restore
refusal can still leave the live DB/docs swapped to the target while the
`.active_client` sentinel names the previous client.

**Cleanup refuses too — no silent skip.** Integrity refusal propagates out
of every store cleanup/read helper (`delete_documents`, `delete_by_ids`,
`delete_company_docs`, `collection_exists`, `get_collection_count`,
`iter_chunk_metadata`, `get_documents_by_ids`); drop+recreate validates
the persisted schema *before* dropping. An activation whose cleanup is
refused therefore **fails** — it cannot report success while client A's
rows linger for B to read after the volume is repaired. Ordinary
"collection absent" cases still pass (absence is not tamper), as do
non-integrity file errors the callers already tolerate.

#### Automatic recovery (the product path — runs before any operator step)

A failed `activate_client_slot` no longer returns with mixed state
servable. The activation itself, still under the shared destructive-op
lock, re-restores the previous coherent state **save-back-free** (the
same contract as rotation's `_force_restore`): the recorded previous
slot, or `_user_backup` when no client was active. The half-swapped live
state is discarded — never written over a good slot copy. The request
then propagates the original error. Note for auditors: an unchanged
`.active_client` sentinel is **not** by itself proof that no exposure
happened — between the failed restore and the recovery restore, live
state was B's under A's name; the invariant is that the *request* does
not end in that state, not that the state never existed.

#### Restore-blocked (recovery itself failed — e.g. volume still refuses)

If recovery also fails, the instance writes
`_client_slots/.restore_blocked` (JSON: `failed_slug`, `restore_slug`,
`kind`, `at`), quarantines the sentinel to `.active_client.refused`
(kept, not deleted), and refuses to serve ambiguous state:

- the API answers `503 restore_blocked` on every route except `OPTIONS`
  preflights, `/health`, `POST /fixtures/unload`, and
  `POST /clients/{slug}/activate` — and of the activations only the
  recorded `restore_slug` may proceed. `/health` answers but withholds
  `company_name` (the live profile may be the half-swapped one, and
  health is unauthenticated);
- slot mutations (`save`, `create`, `park`, `delete`, other activations)
  refuse — nothing can save mixed live state over a good slot;
- the scheduler boot prologue and tick, the resumer (startup sweep, poll
  loop, kicked resumes), the shared inbound resolver (Socket-mode
  Slack/Discord replies get a maintenance notice instead of resolving a
  gate), `Executive.chat` (returns a maintenance reply on every channel),
  and the email poller's whole poll cycle all hold — inbound mail stays
  unread and retriable, due rows and resumed runs do not fire the wrong
  company's outbound work. All gates fail **closed**: a marker read
  error also holds;
- fixture/CLI paths refuse too: `load_fixture`/`load_fixture_any`,
  `snapshot_user_state`, `reset_all_state` — the snapshot above all,
  since it would overwrite `_user_backup` with mixed state;
- `get_active_client` returns `None` while blocked — nothing may
  attribute live state to a client;
- the marker is a file: the block survives restarts, and a booted
  instance comes up blocked, not quietly serving. If the file itself
  cannot be written, a process-local flag fences the process until a
  verified recovery clears it — log line: `could not write
  restore-blocked marker`.

Operator recovery while blocked:

1. **Repair or replace the vector volume** (procedure above: quarantine
   the suspect dir read-only, fresh `VECTOR_STORE_PATH`, re-ingest).
2. Complete the recorded recovery path — either
   `POST /clients/<restore_slug>/activate` (re-restores that slot, clears
   the marker on success) or `POST /fixtures/unload` (restores the user
   backup, exits client mode, clears the marker). A successful restore is
   the *only* thing that clears the marker; `unload_fixture` unlinks it
   only after `_apply_state_from_source` returns.
3. If the marker's `kind` is `user_backup` and `_user_backup/profile.yaml`
   is missing, there is no verified restore point — recovery of that
   live state is undemonstrated; restore from a real backup instead of
   improvising.
4. Do **not** delete `.restore_blocked` or the quarantined sentinel by
   hand while live state is still mixed — the marker is what stops
   save-back contamination and wrong-identity serving. After a
   successful recovery, `.active_client.refused` can be archived with
   the incident record.

Validate before declaring the incident closed: active client names the
recovered client, live decisions/profile/docs match the slot copy,
`app_state.store` serves queries, and the slot copy itself is
byte-identical (it was the source, not the destination). The rebuilt
vector index is *reconstructed*, not byte-verbatim — re-ingest produces
equivalent search, not the old chunk ids.

## Situation B — private interface incompatible

The same error with "cannot inspect persisted schema — failing closed"
means the guard could not read `_model.serialized_schema`. That is a
**symptom, not a diagnosis**: version drift is the likely cause (the field
is a private implementation detail of 1.5.9), but a chromadb downgrade,
a store created by a different major version, or an upstream API change
in a patch release produce the same refusal. Confirm before concluding.

1. Check the actual version: `uv pip show chromadb` in the deployed
   environment vs the `uv.lock` pin — and compare against the image/lock
   that *created* the volume, not just the currently running one.
2. Do **not** treat the refusal as corruption, and do **not** roll back to
   a build without the guard as a "fix" — that removes the protection.
3. Upgrade path: update the dependency deliberately, re-verify the guard's
   assumptions on the new version (`serialized_schema` presence and the
   deserialize/build semantics), adjust the validator if upstream changed
   the shapes, and only then redeploy against the existing volume.

## Verification (synthetic example)

On a scratch checkout, not production:

```bash
python -m pytest packages/core/tests/unit/test_knowledge_store_ef_guard.py -q
```

Covers: clean DB boots; dense/sparse/defaults tamper rejected at init;
post-init tamper rejected before embed; `python -O` still raises; missing
`_model.serialized_schema` fails closed.
