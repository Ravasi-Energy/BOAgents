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
