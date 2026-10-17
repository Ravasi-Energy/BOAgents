# Index dovezi — candidatul BOAgents `00d12b6`

Toate căile de mai jos sunt relative la rădăcina BOGuardian (spațiul de
coordonare), nu la acest repo. Repo-ul ține copiile durabile; probele
executate și logurile complete rămân în `coordonare/rapoarte/BO-A01/`.

## CI pe SHA final

| Produs | SHA | Run CI | Rezultat |
|---|---|---|---|
| BOAgents | `00d12b6` | `37491644118` | SUCCESS — 4276 passed / 1 skipped |
| Hire | `7bec467` | `37531369568` | SUCCESS — backend 993/47 + backend-postgres 38 (PG16 real, Python 3.11) |

## Probe runtime

- Python 3.11 (imaginea de producție `python:3.11-slim`):
  `rapoarte/BO-A01/BUGHUNT-02-BOAGENTS/r15/py311-docker-v3.log` (suită
  completă) + `r15/py311-docker-v4.log` (rerulare țintită) → reuniune
  4276/1, paritate exactă cu CI.
- Fence profil multiproces: `PREDARE-R14.md` + 5 regresii
  (`test_r14_two_process_cas_race`, `test_r14_http_multiworker_cas`,
  `test_r14_lock_timeout_refuses`, `test_r14_crash_releases_lock`,
  `test_r14_version_monotone_across_writers`) — reprobate independent de
  coordonator (CONTROL-R15 REVIEW).
- Probe Mac coordonator 07.10:
  `rapoarte/coordonator/INCHIDERE-F1-F2-20261007/` (MAC-TESTS.json,
  Hire.xml, Guardian.xml) — 4 teste PASS nominale pe headurile finale.

## Matricea completă

`rapoarte/BO-A01/EVIDENTE-F1-F2.json` — 54 rânduri (30 BOAgents, 24 Hire),
fiecare cu cerință/rută/rol/tenant/SHA/înainte→după/readback/CI/limită.
Sumarul în `DOSAR-F1-F2.md` (copie locală); integritatea copiilor e
verificabilă prin `rapoarte/BO-A01/MANIFEST.sha256`.

## Matrice PR → lanț

`rapoarte/BO-A01/MATRICE-PR-LANTURI.md` — lanțul PR6→PR7→PR11→main
(BOAgents) și PR16→PR17→PR20→main (Hire), commituri, CI și ordinea
logică de propagare. Ramura de livrare: `codex/livrare-completa`.

## Închidere F5 (CONTROL-F5-DOSAR-20261010-235953)

- `rapoarte/BO-A01/PUBLICARE-WHITELIST-F5.json` — whitelist final pe
  headurile remote (Hire `3f30640`, BOAgents `533b420`): 122 fișiere,
  0 nepotriviri.
- `rapoarte/BO-A01/DOSAR-F5.md` — addendumul de închidere: 4PG în CI
  (artefact `pg-junit-f5` al rulării `38087062579`, 42 testcase, cele 4
  `test_setari_servicii_pg` PASSED, 0 skip), proba Mac pe baza dedicată
  (`verificari/f5/pg-mac-junit.xml`, 4/4 PASS), referința canonică.
- `docs/bo01-f5/REFERINTA-CANONICA.json` (în acest repo și în Hire) —
  punctează importul canonic A02: Guardian `97ba6e7`, `docs/bo01-f5/`,
  745 poziții, hashuri manifest/pachet din decizie.

### Închidere F5 — dosar + HOLD-uri (CONTROL-F5-20261011)

- `rapoarte/BO-A01/EVIDENTE-F5.json` — reconcilierea nominală: 17
  rânduri cerință→rută/apelant→rol/tenant→SHA→regresie→probă→
  readback/efect/restart/concurență→CI.
- `rapoarte/BO-A01/HOLD-EXTERN-F5.md` — tabel precis HOLD-OBLIO-1 și
  HOLD-S3-1: probă lipsă, SecretRef necesar, comandă+limită, rezultat
  așteptat. Acces de test autorizat: inexistent.
- Gate A02 atașat: `gate-f5-final-97ba6e7.{log,manifest.json}` —
  GATE-EXEC-OK 46/0 pe pinii finali (a896da1/3f30640/97ba6e7).

### HOLD-uri — runneri executabili (CONTROL-F5-FINAL)

- `agenti/BO-A01/instrumente-f5/proba-extern-oblio.py` (sha256
  `6c276cee3547b8e82c5f538947da74a1806aa4b3230c9a39d86227afeef1166e`) și
  `proba-extern-s3.py` (`95b51719719d4b3c21a7d97c59bad60ebfc6f0e4af365f19c88acddc62530e3d`)
  — parametrizați prin SecretRef/env; refuz înainte de trafic/efect.
- `instrumente-f5/extern/probe-negative-extern.log` — 6/6 refuzuri,
  `apeluri_http: 0`, `efect: zero`.
- `rapoarte/BO-A01/EVIDENTE-F5.json` — `mapare_canonica`: cele 23
  ID-uri canonice (B-R-1..12, H-R-1/2/4..12) → dosar/probă/commit.

### Runneri v2 + regresie CI (CONTROL-F5-RUNNER, commit `ecdee71`)

- Runneri canonici mutati in repo: `tools/val4/proba_extern_oblio.py`
  (sha256 `48166c0129615fb005438d1ea5a46fc45a443ce01f3ab41ef9a9dd0c6449d605`),
  `tools/val4/proba_extern_s3.py` (`903c09997f0c91fadf0648f3480d7f0a30ea8efaac6e8ea972a502d6b7a1cff8`).
  Copiile din `instrumente-f5/` sunt derivate, identice la hash.
- Regresie: `packages/core/tests/unit/test_f5_extern_runneri.py` —
  17 probe (cele 3 cazuri reproduse de coordonator + matricea completa
  de preflight/transport/proces); colectata in jobul `python-test`.
- CI `38147947861` pe `ecdee71`: SUCCESS — 4324 teste, 0 fail, 0 error;
  artefact `junit-unit-ecdee71e…`, cele 17 probe runneri nominale PASS.
- Dovada Mac: `instrumente-f5/extern/junit-runneri-mac.xml` 17/17;
  proba negativa pe copii: `probe-negative-extern-v2.log` 7/7 exit=2.
- HOLD-urile Oblio/S3 raman HOLD — fara acces de test autorizat.

### Runneri v3 — adaptorul Oblio REAL (CONTROL-F5-RUNNER-V2, commit `6874775`)

- Defect remediar: `KeyError: cif_emitent` la --emit cu adaptorul real —
  payload dupa schema `_corp_proforma` (cif_emitent/client.nume/moneda/
  serie), validare inainte de HTTP.
- Fixtura `tools/val4/fixtura_hire/` = adaptorul real byte-identic
  (PIN.json, Hire@3f30640); test de drift Mac vs checkoutul real.
- 22 probe: Mac 22/22 (`junit-runneri-mac-v2.xml`); CI `38158608146`
  SUCCESS — 21 PASS + drift skip explicit; junit artifact per SHA.
- S3: snapshot ales dupa `paths`=sursele curente + `time`, nu ultimul.

### Proba pe disc restic — pachet SC00 (F5-PREDARE-COMPLETA-SC00, commit `3e8d0b9`)

- `tools/val4/proba_disc_restic.py` — runner portabil: backup→restore
  prin adaptorul de productie `backups/restic-backup.sh` (vendoruit
  byte-identic Hire@3f30640, PIN.json sha256 `04a8a4aa…`), repository
  local dedicat cu marcaj `.proba-disc-f5`.
- `test_f5_proba_disc.py` — **30 probe**: containment/symlink/suprapunere,
  repo strain/valid-strain/fisier/dedicat, director pregatit, zona
  radacina/fisier, retentie (format/interval/0-0/unicode), hash adaptor
  (gresit/gol), newline in cai, marcaj gresit, cache symlink, restic/
  password lipsa, env-sanitizare spy, init→check, backup/restore/
  snapshot esuat → FAIL, pozitiv spy + **lant real** restic+adaptor +
  refolosire repo real + drift vs checkout Hire (Mac-only).
- Mac: **30/30 PASS** (`predare-sc00/probe-locale-mac/junit-disc-mac.xml`);
  proba reala pe copia din pachet PASS — `PROBA-PE-DISC-mac.json`.
- CI `38374430167` SUCCESS pe `3e8d0b9` — 27 PASS + 3 skip oneste
  (restic real si checkoutul Hire lipsesc in CI).
- Pachetul `agenti/BO-A01/instrumente-f5/predare-sc00/` (MANIFEST 7/7):
  README cu comanda exacta Linux, template raport SC00 cu etichetele
  PROBĂ PE DISC / S3 NEEXECUTAT/HOLD. Executia reala: SC00 pe server.
- 2 runde review adversarial intern; constatari remediate inainte de
  predare (check-invalid, bare-name which, repo refolosit fara marcaj,
  pin-sha gol, injectie newline, symlink cache).
- H-R-8 Oblio: AMANAT explicit de Stefan. Componenta S3 a H-R-10:
  HOLD distinct — proba pe disc nu o acopera.

### F6 — inchidere functionala BOAgents (decizia Stefan 16.10, commit `8bad411`)

- Inventar atomic: `coordonare/rapoarte/BO-A01/F6-F8/MATRICE-F6-BOAGENTS.md/.json`
  — 66 randuri criteriu→cod→ruta→rol→proba pe BO-BOT-001/002/003,
  BO-SET-001, BO-INT-001, BO-HIRE-001, BO-R02, BO-R03, BO-R04, BO-R05
  (producator), PILOT-VAL5, RA13-A02-01.
- Contracte publice: `docs/f6/CONTRACTE.md` — rute reale, rol minim,
  tenant, CAS, refuzuri, coduri verdict (deblocant BO01/A02).
- Remedieri: `8b97cd1` — `CURRENCY_MISMATCH` declarat in REASONS +
  regresie emis⊆declarat (`test_currency_mismatch_refused_and_declared`,
  86/86 PASS routing); `8bad411` — drift `bo_agents.json`
  (verify-authority→authority, +audit-intents/+audit-requeue, chei 49→50,
  router 11→15) + fisierul de contracte.
- Proba Mac pe `8bad411`: **548/548 PASS** bo/* + bughunt02 in 141s —
  `coordonare/rapoarte/BO-A01/F6-F8/probe-mac/junit-bo-surface-8bad411.xml`.
- Intrebari de domeniu catre coordonator:
  `coordonare/rapoarte/BO-A01/F6-F8/INTREBARI-DOMENIU-F6.md`
  (Q-F6-01 limite concurenta/entitlement BoBot; Q-F6-02 activarea
  generica a pachetului kind=bobot — pilotul are activare, bobotul
  generic nu are conventie de artefact ratificata).
- CI pe `8b97cd1` anulat de `8bad411` (concurrency pe non-main, conform
  mandatului); CI `38952876547` pe `8bad411` — **SUCCESS**: 4360 teste,
  0 fail/error, 5 skip oneste (restic real/checkout Hire lipsesc in CI +
  workflows_meta preexistent); 913 cazuri bo/* verzi. JUnit CI:
  `F6-F8/probe-ci/junit-unit-8bad411.xml` sha256 `db1fa97b…`.

## 2026-10-17 — Regula monetara v2.0 (BOAgents)

- Dosar `coordonare/rapoarte/BO-A01/F6-F8/`: `MONETAR-V2-INVENTAR.{md,json}`,
  `ABATERI-MONETARE-V2.md` (AB-01..08 remediate), `MONETAR-V2-PROBE.md`.
- `7944e9b` — budget_currency expand pe mandate/runuri (mostenire de lant,
  mismatch refuz, invariant nedeterminat); cap denominat -> refuz cross-ccy;
  cost_usd exact (sir+Decimal, cost{amount,currency}); _fmt_amount RO;
  stoc currency din meta; setari contra ISO_4217; float JSON -> 422;
  pilot/CLI declara USD.
- Proba Mac: **331/331 PASS** suprafete atinse — JUnit
  `F6-F8/probe-mac/junit-monetar-v2.xml` sha256 `39a63988…`; mutanti
  scalare+eticheta RED->PASS in `F6-F8/mutanti/` (restaurati, nu raman in cod).
- CI `39022895346` pe `7944e9b` — **SUCCESS**: 4373 teste, 0 fail/error,
  5 skip oneste. JUnit `F6-F8/probe-ci/junit-unit-7944e9b.xml` sha256
  `6ed0267d…`.
- Deschise la coordonator: Q-M2 (budget_usd USD-fix), Q-M3 (semantica
  bugetelor exec); Q-M1 rezolvat (bo.ui.money_display_decimals exista).
