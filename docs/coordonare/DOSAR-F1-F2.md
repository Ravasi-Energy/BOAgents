# DOSAR ÎNCHIDERE F1/F2 — BO-A01 (BOAgents + Hire)

Data: 2026-10-06 · Mandat: `EXECUTIE-ETAPE-COMPLETE-F1-F8.md` +
`CONTINUARE-R15-20261006.md` · Fișă: `FISA-INCHIDERE-GRUPA.md`.
Evidențele complete pe rând: `EVIDENTE-F1-F2.json` (același director).

- **Grupe**: F1 (izolare firme, identitate, corelarea execuțiilor) și
  F2 (scrieri fără pierderi, mutații concurente). Rândurile cu grupă
  primară F3–F5 sunt listate cu status, dar nu intră în verdictul F1/F2.
- **Owner**: BO-A01 · **Reviewer**: coordonator · **Data**: 2026-10-06.
- **Baseline / head**: BOAgents `2ded6c4` → `00d12b6`
  (`codex/rem-audit-01-a01`); Hire `8aff12e` → `7bec467`
  (`codex/rem-audit-01-hire`). Checkouturi curate, sync cu origin.
- **Integrare R15**: `Hire-R15-independent-regression.patch` (BO01) aplicat
  pe `eba09e4` → commit `7bec467` — probe pozitive Alpha/Beta în suita
  normală pentru H-P0-2/3/4/5 (2 fișiere noi, fără atingere pe testele
  existente sau oracle). Probă locală: `r15/hire-r15-tests.log` — 24/24
  PASS în `python:3.11-slim` (2 noi + suprafața afectată).
- **CI exact**: BOAgents run `37491644118` SUCCESS — **4276 passed** pe
  `00d12b6`. Hire run `37531369568` SUCCESS pe `7bec467`:
  `security-and-compliance` — backend **993 passed/47 skipped** (991+2 noi)
  + regresii UI Node + `test:money` (D07); `backend-postgres` —
  **38 passed** pe PostgreSQL 16 real, **Python 3.11** (ambele joburi
  CI Hire sunt pe 3.11). Log `ci-hire-37531369568.log`.

## F1 — izolare, identitate, roluri (rânduri P0)

| ID | Rută/worker, rol, tenant | Cod | Probă înainte→după | Readback/concurență | Stare |
|---|---|---|---|---|---|
| B-P0-8 | `POST /people` is_principal; admin 201 / viewer 403 | `c526b9f` | coordonator FAIL → `test_class10_people_create_has_no_role`, `test_pd5_viewer_self_principal_then_empties_list`, `test_people_create_viewer_403_admin_201_and_persists` | persistență people DB | PREDAT |
| B-P0-9 | `PATCH /company-profile`; admin 200 / viewer 403 / anonim 401 | `c526b9f`,`0b07c9f`,`be9f6e3` | FAIL → `test_profile_patch_viewer_403_and_admin_200`, `test_r12_profile_patch_viewer_403` | CAS+fence; uvicorn 2-worker real (`test_r14_http_multiworker_cas`) | PREDAT |
| B-P0-10 | toate 5 mutațiile + people + profile au rol real | `c526b9f` | FAIL → `test_class10_other_mutations_have_no_role_parameter` (AST) + `test_http_mutation_gates.py` 5 probe HTTP (headerless→403, viewer→403+date intacte, admin→efect) | efect doar la admin | PREDAT |
| H-P0-1 | `incheie_job` — memorie job; tenant obligatoriu | `b7e726e` | FAIL (ștergea beta) → `test_incheie_job_nu_sterge_memoria_altei_firme`, `..._si_firma_vecina`, `..._fara_tenant_refuza` | DB readback; scop acme vs beta | PREDAT |
| H-P0-2 | `livrare.publica` observații; tenant explicit | `b7e726e` | FAIL → `test_publicarea_fara_tenant_nu_trimite_candidatul_altei_firme`, `test_scoped_workers_r15` (pozitiv SENT+Beta intact) | coada beta neatinsă (QUEUED păstrat) | PREDAT |
| H-P0-3 | `livrare.reluare` DEAD; tenant + scope mutual exclusiv | `b7e726e` | proba originală refuza deja → `test_reluare_fara_tenant_nu_modifica_beta`, `test_reluare_scope_mutual_exclusiv` + **pozitive**: `test_reluarea_muta_dead_in_queued_cu_acelasi_eventid`, `test_reluarea_dead_nu_repuncteaza_ruta`, `test_scoped_workers_r15` (DEAD→QUEUED același eventId) | DEAD→QUEUED doar propriul tenant | PREDAT |
| H-P0-4 | `livrare.curata_retentie`; tenant explicit | `b7e726e` | FAIL → `test_retentia_observatiilor_nu_sterge_alta_firma`, `test_scoped_workers_r15` (pozitiv: șterge 1 alpha, Beta intact) | rândurile beta păstrate | PREDAT |
| H-P0-5 | `evenimente.publica`; scope explicit | `b7e726e` | proba originală PASS (refuz existent) → `test_publica_fara_tenant_nu_marcheaza_evenimentul_altei_firme`, `test_execution_scope_r15` (pozitiv: alpha SENT, beta QUEUED intact) | eveniment beta nemarcat | PREDAT |

## F2 — scrieri fără pierderi (rânduri P0 + fence)

| ID | Rută/writer, rol, tenant | Cod | Probă înainte→după | Concurență/crash | Stare |
|---|---|---|---|---|---|
| B-P0-1 | CSV settings; admin | `1541e4f` | FAIL 3→1 → `test_pd1_settings_csv_three_plus_one`, `test_pd3_settings_csv_empty_keeps_list`, `test_pd_stale_csv_version_keeps_list`, `test_csv_remove_op_and_empty_noop` | stale→409 fără efect; readback listă | PREDAT |
| B-P0-2 | trust store keyed; admin | `1541e4f`,`0b07c9f` | FAIL → `test_pd1/3/4_trust_store_*`, `test_trust_store_keyed_upsert_and_remove`, `test_r12_trust_*` (7 teste: nested `[]` păstrat, union, remove la orice adâncime) | câmpuri independente supraviețuiesc | PREDAT |
| B-P0-3 | `PATCH /bo/bots/{id}` draft | `1541e4f` | FAIL → `test_pd1..pd6_bot_*` (6 teste) + `test_contract_r3.py` (upsert/remove, concurență add+delete, două taburi+rebase) | draft_version, PD6 delete concurent | PREDAT |
| B-P0-4 | catalog routing | `1541e4f` | FAIL → `test_pd1/3/4_catalog_*`, `test_catalog_upsert_and_explicit_remove` | stale 409 | PREDAT |
| B-P0-5 | profil vendors | `1541e4f` | FAIL → `test_pd1/3_company_profile_*vendors*`, `test_pd_company_profile_omitted_vendors_stay` | `vendors_remove` explicit | PREDAT |
| B-P0-6 | profil financials | `1541e4f` | FAIL → `test_pd4_company_profile_partial_financials` + `test_class8_profile_float_1005` | merge recursiv; Decimal persistat | PREDAT |
| B-P0-7 | `PATCH /people/{id}` department_slugs | `1541e4f`,`0b07c9f` | FAIL → `test_pd1_people_department_slugs`, `test_r12_people_department_slugs_delta`, `test_r12_people_scope_only_update_fenced` | CAS obligatoriu, tranzacție unică BEGIN IMMEDIATE | PREDAT |
| H-P0-6 | timeouturi ERP delta + CAS | `b7e726e`,`1790664`,`376d453` | FAIL (pierdea readback=9) → `test_timeouts_campuri_diferite_raman_ambele`, `test_timeouts_delta_goala_si_revenire_explicita`; UI: draft `{base,touched}` + `timeoutDelta` fail-closed pe versiune lipsă | 409→rebase cu draft păstrat; network/protocol fără replay | PREDAT |
| PROFILE-CAS-RACE | fence multiproces `profile.yaml.lock` pe toți writerii | `be9f6e3` | FAIL coordonator (200/v2 ambele, editură pierdută) → `test_r14_two_process_cas_race` (subprocese reale: un 200/v2, un 409), `test_r14_http_multiworker_cas`, `test_r14_lock_timeout_refuses` (503), `test_r14_crash_releases_lock` (SIGKILL), `test_r14_version_monotone_across_writers` | lock kernel re-entrant; toți writerii prin `save_to_yaml`; **limită**: filesystem local, nu NFS/multi-nod | PREDAT — reprobata independent de coordonator PASS |

Contractul de scriere (CAS strict + delta explicită, fără bază v−1, fără
ștergere prin omisiune): `BUGHUNT-02-BOAGENTS/CONTRACT-UI.md`, probele
`test_contract_r3.py` (11 regresii: revert legitim, bază<v−1→409, omisie
păstrată, gol=no-op, publish fără CAS→422/409).

## Apelanți P0 (din produse, binding BO01)

`agenti/BO01/BUGHUNT-UI-02/R12/P0-CALLER-BINDINGS.json`: 27 calleri —
**12 BOAgents** (toți legați de B-P0-1..10/B-P1-7, fiecare cu
`nativeEffectReadback` spre testele de mai sus), **6 Hire** (H-P0-2/3/6,
incl. `CALL-HIRE-TIMEOUT-93` — handler UI real cu CAS eronat → eroare
înainte de transport, draft păstrat; în CI `37493410518`), **9 Guardian**
(owner A02, nu în acest dosar). Wire tests BOAgents: `p0-wire.test.mjs`
(patche R12 integrate în `00d12b6`).

## Rânduri cu grupă primară F3–F5 — status (nu intră în verdictul F1/F2)

- **F3** (bani): B-P1-3/4/5, H-P1-1/2/3 — remediate, regresii în CI
  (`test_class8_*`, `test_bughunt01_p1.py`, `money.test.mjs`).
- **F4** (restul P1): B-P1-1/2/6/7/8, H-P1-4/5/6 — remediate, regresii în
  CI.
- **F5** (rest audit): remediate — B-R-1/2/3/4/5/11, **B-R-12**
  (RA15-BO01-03/06/09: `3370dca`+`5574086`+`47f3da7`+`0b303d5`+`04ca9fa`,
  strămoși ai `00d12b6`; `test_turn_barrier.py` 32 teste incl.
  `test_decorated_generator_fenced_mid_stream` — lanțul exact
  expire→reconcile→switch→resume→zero efect tardiv — și
  `test_shielded_dispatch_survives_parent_cancel`,
  `test_late_child_outcome_audit_after_parent_cancel`), H-R-1/2/12.
  **HOLD deschise (14)**: B-R-6/7/8/9/10, H-R-3/4/5/6/7/8/9/10/11 —
  condițiile de deblocare sunt în `EVIDENTE-F1-F2.json` per rând.

## Compatibilitate Python 3.11 BOAgents — REZOLVATĂ (runtime real)

CI-ul BOAgents rulează pe 3.12, dar runtime-ul de producție
(`docker/Dockerfile`) e `python:3.11-slim`. Probă de runtime real în
container `python:3.11-slim` (**Python 3.11.16**), pe HEAD `00d12b6`:

- **Rulare completă** (`r15/py311-docker-v3.log`): `tests/unit/` —
  **4266 passed, 1 skipped**; 8 eșecuri + 2 erori de fixture, toate
  artefacte ale copiei incomplete din proba v3, consemnate:
  6×`test_docker_dependency_layer` + 1×`test_mcp_gateway` citesc
  `docker/Dockerfile`/`uv.lock`/`pyproject.toml` la rădăcină (necopiate),
  1×`test_research_controls` citește `.env.example` (necopiat),
  1×`test_review_store::test_shipped_manifest_matches_git` cere `git
  ls-files` (`.git` e pointer de worktree, nerezolvabil în copie),
  1×`test_client_slots::test_marker_write_failure_fences_process_in_memory`
  cere chmod 0555 care nu oprește uid 0.
- **Rerulare țintită** (`r15/py311-docker-v4.log`): exact aceleași 10
  teste pe copie completă (inclusiv `docker/`, `.env.example`, gitdir
  montat ro) + git instalat + execuție ca `nobody` → **10/10 PASS**
  nominal. Erorile de setup v1/v2 (fixture `heartbeat_stale` lipsă din
  copie) sunt în `r15/py311-docker{,-v2}.log`.
- **Skipul necesar**: 1 skipped =
  `test_workflows_meta.py::test_sample_inputs_validate[department_check_in]`
  — identic cu CI 3.12 (acolo tot 1 skipped), nu legat de versiune.
- Reuniunea = **4276 passed, 1 skipped** — paritate exactă cu CI
  `37491644118` pe 3.12. Nu există eșec atribuibil versiunii 3.11.
  Proba container nu înlocuiește testele Mac; probele Mac ale
  coordonatorului sunt listate separat mai jos.

## Proveniență CI și probe Mac — H-P0-2/3/4/5

- **Comanda jobului CI** (`security-and-compliance` → «Backend tests»):
  `cd backend && pip install -r requirements-dev.txt && python -m pytest
  -q`. Config: `backend/pytest.ini` — `testpaths=tests`, `pythonpath=.`,
  `filterwarnings=error::DeprecationWarning:hire.*`. Regula de skip:
  testele `*_pg.py` sar când lipsește `HIRE_PG_REQUIRED` (cele 47 skipped);
  jobul `backend-postgres` le rulează explicit pe PG16 real cu
  `HIRE_PG_REQUIRED=1` (38 passed).
- **Observat în logul CI `37531369568`** (quiet `-q`): doar agregatul
  `993 passed, 47 skipped` pe `7bec467` — fără nume nominale. Delta față
  de `eba09e4` (991) este +2, corespunzător celor 2 fișiere noi din
  commit (unicul diff al pushului).
- **Inferență consemnată**: cele +2 sunt `test_scoped_workers_r15` +
  `test_execution_scope_r15`. Nu s-a refăcut CI doar pentru nume.
- **Probă Mac nominală coordonator** (locală, distinctă de CI):
  `coordonare/rapoarte/coordonator/INCHIDERE-F1-F2-20261007/Hire.xml` +
  `Hire.log` + `MAC-TESTS.json` — ambele teste **PASS nominale** pe
  `7bec467`, Darwin/Python 3.12.14, pytest 9.1.1. Legate în
  `EVIDENTE-F1-F2.json` per rând (`junit_mac`, `provenienta_ci`).
- **Probă autor** (distinctă): `r15/hire-r15-tests.log` — 24/24 PASS în
  `python:3.11-slim`, cu nume nominale `-v`.

## Restanțe

1. **Python 3.11 BOAgents** — închisă; vezi secțiunea de mai sus.
2. **HOLD-urile F5** — enumerate mai sus; niciunul nu e P0, dar țin
   lotul deschis pentru închiderea F5.
3. **Reverificare independentă** — probele coordonatorului pe export proaspăt
   (PROFILE-CAS-RACE pe `00d12b6` deja PASS la coordonator; restul
   suprafeței așteaptă controlul independent).

## Verdict coordonator

- Toate cerințele inventariate și implementate: _(de completat)_
- Toate integrate în candidat: _(de completat)_
- Toate rutele și funcțiile probate: _(de completat)_
- Regresii colectate în suita normală și CI exact: _(de completat)_
- Probe independente complete: _(de completat)_
- Zero restanțe: _(de completat)_
- Verdict: **DESCHISĂ** (semnătura coordonatorului după review)

**PREDAT PENTRU REVIEW — nu ACCEPTAT. Fără merge/deploy.**
