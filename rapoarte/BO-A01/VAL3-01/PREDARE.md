# BO-A01 — VAL3-01 — PREDARE (router în mod observare)

**Stare: TESTAT_LOCAL / PREDAT** — nu ACCEPTAT. Verificarea independentă aparține Codex; merge numai Ștefan.

- **Repo:** Ravasi-Energy/BOAgents
- **Ramură:** `bo/val3-01-a01-router` — **PR draft:** https://github.com/Ravasi-Energy/BOAgents/pull/6
- **baseSHA:** `e3abaf74a1e9998a506287e3519c7213ee9e0661` (main integrat, merge PR #5)
- **headSHA:** vezi `coordonare/rapoarte/BO-A01/VAL3-01/PREDARE.md` (referința canonică — copia din repo nu poate conține propriul SHA)
- **Bundle:** `agenti/BO-A01/bo-a01-val3-01.bundle` — `git bundle verify` OK, istoric complet; **SHA256 `acf70488a06818f104e82270306e0246664c11a7dfe9b3d4bb406f4f11b65a50`**
- **CI:** rulată local pe head (nu există gate CI extern confirmat); rezultatele de mai jos sunt pe head-ul curent (vezi referința canonică).

## Matrice cerință → cod → probă → rezultat

| # | Cerință mandat | Cod | Probă | Rezultat |
|---|---|---|---|---|
| 1 | Motor determinist observe: filtre înainte de scor, motive, prag, stale, tie-break | `openexecutive/bo/routing/engine.py`, `catalog.py` | `test_bo_routing.py::TestEngine` (17 teste: replay determinist, tie-break, provider/region/capability/budget/disabled/eval-missing/task-mismatch/stale/prag/fallback/filtre-înainte-de-scor/catalog gol) | 36 teste routing verzi |
| 1 | Legare la fluxul real fără schimbarea rutei, fără apeluri provider suplimentare | `routing/observe.py` + hook în `audit/usage.py:log_model_usage` (choke-point post-call) | `test_actual_route_unchanged_regardless_of_recommendation`, `test_observation_persisted`; e2e live (mai jos) | ruta reală persistată separat; `decision` ≠ rută |
| 1 | Recomandare + rezultat real persistate separat; necunoscut rămâne necunoscut | `routing/store.py` (`bo_routing_observations`), serialize | teste + schema A02 (quality/cost `null` permis) | `recommendation`/`actual_route` coloane distincte |
| 1 | BoBots fără dependență LLM/router | niciun import `routing`/`providers` în `bo/bots/` | `test_bobots_have_no_router_dependency` (scan static de importuri) | verde |
| 2 | Catalog administrabil provider/model/versiune/capabilități/regiune/disponibilitate/cost/evaluare | `routing/catalog.py` + `store.py` (`bo_routing_catalog`) | `TestStore`: create/list, CAS conflict, duplicat respins, izolare tenant, intrare invalidă respinsă | verde |
| 2 | Setări persistente: observe_enabled (false), eligibilitate, praguri, prospețime, retenție; autorizare, CAS/audit, efect | `bo/settings/registry.py` — 8 chei `bo.router.*` (vezi lista) | `test_settings_new_keys_roundtrip` (HTTP), suite `test_bo_settings` | verde; `enabled=false→true` comută emisia (probat live) |
| 3 | Pagină Modele și rutare: catalog, editor autorizat, observații filtrabile, comparație, refuz, «observare», teme, stări | `packages/ui/src/app/bo/routing/page.tsx`, `lib/bo.ts`, `navConfig.ts` | Probe Playwright live: 9/9 + 5/5 (titlu, etichetă observare, catalog, observații, fără „economie realizată", 0 erori JS, 0 overflow 390/1440 × light/dark, neautentificat→signin, detaliu cu motive, editor, setări vizibile) | capturi în probe |
| 4 | Metadate prin adaptorul existent, conform artefactului A02 | `telemetry/adapter.py` (`emit_model_observation`), `routing/serialize.py` | `test_routing_doc_validates`, `test_refuse_doc_validates`, `test_models_doc_validates` contra schemei A02 reală (`tests/unit/fixtures/bo.model-observation.v1.schema.json`) | 3 fixture-uri exportate din serializatorul real valide |
| 4 | Retenție locală + receptor indisponibil; fără Guardian în calea critică | `store.py` (`delivered`, retention sweep), `/bo/routing/flush` | `test_receiver_loss_persists_and_flushes`, `test_retention_sweep`, `test_disabled_by_default_no_write` + e2e live (pending → flush → 200) | `{"sent":2,"failed":0}` la flush |
| 5 | Probe: replay, lipsă eval/cost, prag, buget/regiune/provider, fallback, stale, izolare, CAS, pierdere receptor, rută neschimbată, BoBot fără LLM | — | toate în `test_bo_routing.py` (36 teste) + TestClient HTTP (401/403/404/409/422) | verzi |
| 5 | Evaluare sintetică calibrare/test separate + limite | `scripts/eval_bo_router_synthetic.py` | calibrare 12 cazuri → prag ales 0.6 (12/12); test disjunct 8/8 | raport limite mai jos |
| — | Fixture-uri pentru A02 din serializatorul real | `scripts/generate_bo_model_observation_fixtures.py` | `fixtures/bo/model-observation/valid/{routing-route,routing-refuse,models-sync}.json` + copii în `coordonare/contracte/bo.model-observation.v1/fixtures/valid/` | valide contra `bo.model-observation.v1` |

## Artefacte și amprente

- Schema comună A02 folosită (nemodificată): `coordonare/contracte/bo.model-observation.v1/bo.model-observation.v1.schema.json` — SHA256 `9c3850f425b212d0878088700f44c7a186a6538d3e7bf30eeef959ccc6d0f14b`
- Fixture-uri produse (repo `fixtures/bo/model-observation/valid/`):
  - `routing-route.json` — `c30293d17c75bc9af8301601d109e026400eeb8ef7075d02fdaa84f32060e2ea`
  - `routing-refuse.json` — `942fdcc7d5e1dd7f69f68bc7f5e56c2351015d2e59c1a61fd630c9ec928c4ad5`
  - `models-sync.json` — `90b7d943bddbc2d2582678cab5202547cbed419b43bf717503cb1fae3986c3e2`

## Setări noi `bo.router.*` (toate tenant-scope, CAS + audit `bo_setting_change`)

`observe_enabled` (bool, implicit **false**), `allowed_providers`, `allowed_regions`, `required_capabilities` (CSV), `min_quality` (0–1), `eval_max_age_days`, `max_estimated_cost`, `observation_retention_days`. Capabilități noi: `routing:read` (viewer), `routing:write` (admin).

## Demonstrație locală cap-coadă (revizii reale)

Pe head-ul curent (vezi referința canonică), backend real + receptor-stub Guardian compatibil A02:

1. `bo.router.observe_enabled` false → true prin API Setări (CAS) → status `observe_enabled:true`.
2. Intrarе catalog creată prin API (`anthropic/claude-sonnet-5`, scor 0.87, EU).
3. Două apeluri sintetice prin `log_model_usage` real → 2 observații persistate: una `met_bar:true` cu recomandare = ruta reală, una `REFUSE` observat (`EVAL_TASK_MISMATCH`, `MODEL_DISABLED`) cu ruta reală `claude-haiku-4-5` neatinsă.
4. Fără transport configurat: `pending_delivery:2` (receptor indisponibil → retenție locală).
5. `POST /bo/routing/flush` → `{"sent":2,"failed":0}`; receptorul-stub a returnat 200 și a validat payload-urile contra schemei A02.
6. UI `/bo/routing` prin proxy Next real (sesiune mintită): catalog, observații, detaliu cu Recomandat/Folosit efectiv/costuri/motive/candidați, eticheta «observare — ruta reală nu se schimbă», ambele teme, 0 overflow.

## Rezultate verificare

- `pytest tests/unit/test_bo_routing.py test_bo_routes.py test_bo_settings.py`: **68 passed** pe head.
- Suită completă `tests/unit` (rulată înainte de commit, cod identic): **3676 passed, 1 skipped**; ruff curat; `mypy` 295 fișiere fără erori; `next build` verde; `architecture-facts.yaml` + `prebuilt/{bo_agents,api}.json` validate.

## Migrare / rollback

- Tabele noi create de `initialize_db`: `bo_routing_catalog`, `bo_routing_observations` (SQLite, per-instalare). Fără migrare destructivă; rollback = revert commit + drop cele 2 tabele (date doar de observare).
- `observe_enabled` implicit **false** → comportament implicit identic cu main: hook-ul iese devreme, zero scrieri.

## Limite declarate

- Evaluarea e **sintetică** (scoruri/costuri inventate, 12+8 cazuri) — demonstrează pragul și determinismul, nu calitatea reală a modelelor.
- Receptorul Guardian din probă e un stub local care validează schema A02; integrarea cu receptorul real revine A02.
- Costul facturat apare numai cu evidență; estimările nu sunt economii realizate (UI nu le prezintă ca atare).
- `REFUSE` e un rezultat observat al recomandării, nu oprește execuția.
- Review-ul multi-model Anvil nu a fost rulat (interzis de mandat — fără subagenți); auto-revizie + ledger local.
