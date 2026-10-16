# CONTRACTE — suprafața publică BOAgents `/bo/*` (F6)

Pin de referință: `8b97cd1` (codex/livrare-completa). Documentul descrie
contractele **existente** ale backendului — schema exactă, scope/rol/tenant,
CAS, refuzuri și erori — pentru BO01 (F7 UI) și A02 (integrare). Contractele
lipsă sunt marcate explicit la final; nu se inventează API ca să închidă un
rând de matrice.

## Identitate, tenant, roluri

- Autentificarea: `openexecutive/bo/identity.py`. Proxy-ul Next.js stampilează
  `x-caller-email` după verificarea `BACKEND_PROXY_SECRET`; antetele
  `x-caller-*` furnizate de client sunt eliminate în upstream.
- `tenant` vine **numai** din `BO_TENANT_ID` (config server). Un antet
  `x-bo-tenant` sau `?tenant=` care prezintă alt tenant → **403**, fără
  dezvăluire. Nu există selector de tenant.
- Roluri: `viewer` < `operator` < `admin`. `admin` din `BO_ADMIN_EMAILS` +
  `is_principal`; `operator` = identitate de serviciu fără email.
- Pe `OE_PUBLIC_DEPLOYMENT` cererea neautentificată este refuzată (fail-closed),
  nu promovată la identitatea dev.

Capabilități → rol minim (sursa: `_CAP_MIN_ROLE`):

| Capabilitate | Rol minim |
|---|---|
| settings:read, bots:read, telemetry:read, packages:read, routing:read, execution:read | viewer |
| bots:simulate, execution:operate | operator |
| settings:write, bots:write, packages:write, routing:write, execution:write | admin |

Erori comune: `401` neautentificat · `403` rol insuficient sau tenant prezentat
diferit · `404` resursă inexistentă sau aparținând altui tenant (nu se
dezvăluie existența) · `409` conflict CAS/stare · `422` validare de domeniu.

CAS: toate mutațiile cu conținut cer `expected_version` și returnează `409`
la conflict; răspunsul neschimbat nu rescrie nimic.

## BoBots (BO-BOT-002/003)

| Metodă | Rută | Rol | Corp / semantică |
|---|---|---|---|
| GET | `/bo/bots` | viewer | listă definiții tenant |
| POST | `/bo/bots` (201) | admin | `{name≤120, kind=BOT|AI|MIXED, description≤500, content: bo.bobot.v1}` → draft v1 |
| POST | `/bo/bots/example` (201) | admin | creează exemplul HEARTBEAT_STALE |
| GET | `/bo/bots/{def_id}` | viewer | detaliu + versiuni |
| PATCH | `/bo/bots/{def_id}` | admin | `{expected_version≥1, name?, description?, content?, steps_remove?, capability_refs_remove?, policy_refs_remove?}` — omiterea nu șterge; ștergerea cere lista explicită |
| POST | `/bo/bots/{def_id}/publish` | admin | `{expected_version≥1}` — draft→published imuabil + activ |
| POST | `/bo/bots/{def_id}/simulate` (201) | operator | `{input?, version_no?, draft?}` — simulare deterministă; run `kind=simulation`, receipts `NOT_EXECUTED` |
| GET | `/bo/bots/{def_id}/runs` | viewer | timeline rulări |
| GET | `/bo/runs/{run_id}` | viewer | rezultat + steps |

Refuzuri: kind≠BOT la simulate → 422; `simulate` pe draft fără `draft:true` →
422; lipsa versiunii active → 404/409 conform stării.

## Setări (BO-SET-001)

| Metodă | Rută | Rol | Corp |
|---|---|---|---|
| GET | `/bo/settings` | viewer | `{tenant, config_version, settings[], role}` — valoare efectivă + origine |
| PUT | `/bo/settings/{key}` | admin | `{value, expected_version≥0, remove?≤64}` — `remove` = singurul mod de a șterge elemente din chei `csv_union`; `""` nu e wipe |

Chei necunoscute → 422; valori invalide → 422; conflict versiune → 409.
`applyMode`: `IMMEDIATE` sau `NEW_RUN` documentate pe cheie (registru).

## Pachete semnate (BO-R02)

| Metodă | Rută | Rol | Corp / efect |
|---|---|---|---|
| GET | `/bo/packages` | viewer | listă importuri tenant |
| POST | `/bo/packages/import` | admin | `{source_dir≤1024}` — verificare `bo.package.v1` completă (§4) apoi copie în carantină; răspunsul are întotdeauna `verdict`; respingerile se persistă ca `REJECTED` |
| GET | `/bo/packages/{import_id}` | viewer | import + verdict |
| POST | `/bo/packages/{import_id}/promote` | admin | QUARANTINED→DRAFT după re-verificarea copiei stocate (drift) |
| GET | `/bo/packages-approvals` | viewer | aprobări downgrade tenant |
| POST | `/bo/packages-approvals` (201) | admin | `{package_id, from_version, to_version, artifact_set_digest, expires_at RFC3339}` — legată, expirantă, unică |
| POST | `/bo/packages-approvals/{approval_id}/revoke` | admin | revocare |

Coduri verdict: `INVALID_MANIFEST, DUPLICATE_KEY, TRAVERSAL, ARTIFACT_MISSING,
ARTIFACT_MODIFIED, UNSIGNED_ARTIFACT, MANIFEST_TOO_LARGE, PACKAGE_TOO_LARGE,
PUBLISHER_UNKNOWN, PUBLISHER_SUSPENDED, KEY_UNKNOWN, KEY_REVOKED, KEY_EXPIRED,
BAD_SIGNATURE, INCOMPATIBLE, CAPABILITY_UNKNOWN, CAPABILITY_EXCESSIVE,
KIND_NOT_ALLOWED, VERSION_CONFLICT, ROLLBACK_UNAUTHORIZED, APPROVAL_INVALID,
ARTIFACT_DRIFT, REGISTRY_UNAVAILABLE`.

## Router observe-mode (BO-R03)

| Metodă | Rută | Rol | Corp / efect |
|---|---|---|---|
| GET | `/bo/routing/catalog` | viewer | catalog tenant |
| POST | `/bo/routing/catalog` (201) | admin | `CatalogEntry` (provider, model_id, model_version?, state, capabilities≤16, regions≤16, cost{…valid_until}, quality?, purpose, source) |
| PUT | `/bo/routing/catalog/{entry_id}` | admin | idem + `expected_version≥1` + `capabilities_remove?/regions_remove?` |
| GET | `/bo/routing/observations` | viewer | observații persistate |
| GET | `/bo/routing/observations/{obs_id}` | viewer | detaliu |
| GET | `/bo/routing/status` | viewer | stare outbox/observații |
| POST | `/bo/routing/flush` | operator | reemitere pending byte-identic |

Mod observare: nimic de aici nu schimbă ruta reală a modelului.

## Execuție delegată (BO-R04)

| Metodă | Rută | Rol | Corp / efect |
|---|---|---|---|
| GET | `/bo/execution/mandates` | viewer | listă |
| POST | `/bo/execution/mandates` (201) | admin | `{parent_mandate_id?, guardian_ref?, allowed_resources≤64, allowed_actions≤64, budget_limit, concurrency_limit 1..64, max_steps 1..500, max_depth 0..16, expires_at}` — copil = intersecție cu părintele, niciodată uniune |
| POST | `/bo/execution/mandates/{id}/revoke` | admin | `{reason≤300}` |
| GET | `/bo/execution/runs` | viewer | `?state=` |
| POST | `/bo/execution/runs` (201) | operator | `{mandate_id, steps≤500, budget_amount, parent_run_id?, correlation_id?}` |
| GET | `/bo/execution/runs/{run_id}` | viewer | detaliu + pași + ledger |
| POST | `/bo/execution/runs/{run_id}/pause` | operator | `{reason?}` |
| POST | `/bo/execution/runs/{run_id}/cancel` | operator | `{reason}` obligatoriu — nu anulează efectul extern |
| POST | `/bo/execution/runs/{run_id}/resume` | operator | aceeași identitate/idempotencyKey |
| POST | `/bo/execution/runs/{run_id}/reconcile` | operator | `{resolution=receipt\|mark_failed}` |
| GET | `/bo/execution/runs/{run_id}/authority` | viewer | autoritate efectivă (booleene) |
| GET | `/bo/execution/outbox` | viewer | `?status=&limit=&offset=` |
| POST | `/bo/execution/outbox/{event_id}/retry` | admin | `{reason}` — numai din DEAD |
| POST | `/bo/execution/outbox/rebind` | admin | `{reason, event_ids?≤200}` — reasocieze la destinația curentă; nu rescrie octeți |
| GET | `/bo/execution/audit-intents` | viewer | dovezi intenții de audit (`journal_row_present` onest) |
| POST | `/bo/execution/outbox/audit-requeue` | admin | requeue failed; demotare delivered numai cu jurnal lipsă dovedit |
| POST | `/bo/execution/work` | operator | `{limit≤50, worker_id?}` — un ciclu mărginit pe providerul sintetic |
| GET | `/bo/execution/status` | viewer | stare + sumar Guardian (booleene) |

## Pilot sintetic (VAL5-00)

| Metodă | Rută | Rol | Corp |
|---|---|---|---|
| GET | `/bo/pilot` | viewer | stare pilot |
| PUT | `/bo/pilot/activation` | admin | `{import_id, active, expected_version≥0, reason≤300}` |
| POST | `/bo/pilot/runs` (201) | operator | `{mandate_id, correlation_id?}` |
| POST | `/bo/pilot/runs/{run_id}/telemetry/replay` | operator | re-queue dovezi; idempotent, fără efect nou |

## Telemetrie (BO-INT-001)

| Metodă | Rută | Rol | Corp / efect |
|---|---|---|---|
| GET | `/bo/telemetry/status` | viewer | stare transport/destinație (fără secrete) |
| POST | `/bo/telemetry/validate` | admin | validare payload `bo.telemetry.v1` |

Emiterea reală: outbox durabil + `HttpTransport` Bearer din `secret_ref`
administrat; schema închisă respinge chei necunoscute și câmpuri PII.

## Contracte lipsă / întrebări de domeniu (la coordonator)

- **F6-BO-G3 — activarea pachetului `kind=bobot`**: ciclul implementat se
  oprește la `DRAFT`; nu există rută care să materializeze artefactul verificat
  ca proiect de `AutomationDefinition` legat de `artifactSetDigest`. Propunere:
  `POST /bo/packages/{import_id}/activate` pe `DRAFT` + `kind=bobot`, care creează
  o definiție în starea `draft` (nu active) citind artefactul `bobot.json`
  (conținut `bo.bobot.v1`) din copia de carantină re-verificată. Necesită
  decizia de contract pentru numele artefactului.
- **F6-BO-G2 — limite comerciale BoBot**: lipsesc chei administrate pentru
  concurența simulărilor și numărul maxim de definiții (chei noi = schimbare
  de schemă administrată, nu se inventează unilateral).
