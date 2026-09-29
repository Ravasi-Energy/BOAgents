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
| Client-slot restore (`clients/slots.py:_rebuild_vector_state`) | Restore aborts before the active-client sentinel is written | restore failure in logs; previous state not switched in |
| Attachment ingest (`integrations/attachments.py`) | That request fails | HTTP 500 on the upload |

The error message names the collection and the schema path of the offending
node — it never carries document contents or config payloads.

## Situation A — persisted configuration rejected

The store opened fine, but a collection's schema contains an
`embedding_function` this application never writes. Treat the volume as
suspect until proven otherwise.

1. **Stop writes.** Stop the API/scheduler container (`docker compose stop`
   on the service). Do not let anything boot against the suspect directory.
2. **Preserve evidence.** Move the whole `/data/chroma_db/` aside
   read-only (`mv chroma_db chroma_db.suspect-YYYYMMDD`); do **not** delete
   `chroma.sqlite3`, do **not** edit `schema_str`, do **not** disable the
   guard, do **not** start the app against the suspect copy.
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

## Situation B — private interface incompatible

The same error with "cannot inspect persisted schema — failing closed"
means the guard could not read `_model.serialized_schema` — almost always
because **chromadb is no longer 1.5.9** (the field is a private
implementation detail of that version).

1. Confirm drift: `uv pip show chromadb` in the deployed environment vs the
   `uv.lock` pin. This guard is calibrated to 1.5.9 semantics.
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
