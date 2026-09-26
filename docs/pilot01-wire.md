# ERP sintetic — interfață driver PILOT-01

Numai sandbox. Nu reprezintă integrare BioTrioERP reală sau certificare.
Pachetul este Ed25519 `bo.package.v1`; cheia generatorului este fixture publică,
niciodată rădăcină de încredere de producție. Importul verifică și pune în carantină;
promovarea DRAFT nu activează. Activarea explicită admin are CAS, motiv și audit.
Runtime-ul execută numai gramatica fixă `CONTENT`, fără import/script/LLM/scheduler.

## Pentru BO-SOL-03 / gate

Din checkout, cu mediul Python existent și `PYTHONPATH=packages/core`:

```sh
python -m openexecutive.bo.pilot.driver --guardian URL --tenant TENANT --work DIR --scenario healthy
python -m openexecutive.bo.pilot.driver --guardian URL --tenant TENANT --work DIR --scenario unknown
python -m openexecutive.bo.pilot.driver --guardian URL --tenant TENANT --work DIR --scenario recover
```

CLI acceptă `healthy|delayed|unknown|recover`, `--service-port` implicit8325.
`recover` folosește același DIR și ultima execuție, readback înainte de resume,
nu creează un mandat înlocuitor. Portul trebuie să fie liber, nu este scanat.
Driverul pornește și oprește doar fixture-ul propriu; folosește SQLite în DIR.
Director nou pentru o instalație/configurație nouă; nu modifica configurația unei
operații UNKNOWN. Tokenii sunt exclusiv environment:

- `BO_PILOT_SERVICE_TOKEN`: credential fixture sintetic local.
- `BO_PILOT_GUARDIAN_MANDATE`: mandat Guardian provisionat de owner, drept exact
  `diagnose` / `synth.erp`, product `BOAgents`, tenant/installation identice.
- `BO_GUARDIAN_TOKEN`: autoritate/receipts conform contractului existent.
- `BO_PILOT_OBSERVATION_TOKEN`: `modelobs:write` pentru observații.
- `BO_TELEMETRY_TOKEN`, `BO_TELEMETRY_ENABLED`, `BO_TELEMETRY_PRODUCER_ID`,
  `BO_INSTALLATION_ID`: contractele existente de telemetrie.

Fără `--guardian`, driverul demonstrează standalone local. Cu Guardian, lipsa
mandatului explicit refuză pornirea; indisponibilitatea autorității oprește efectul.
Pierderea livrării observațiilor păstrează outbox-ul și nu acordă autoritate.
JSON final: serviceRef, executionRef, correlationId, receiptRef (null la UNKNOWN),
payloadDigest, idempotencyKey, effectCount, submitCalls, state, provenance (Git HEAD).
Provenance trebuie corelat cu un checkout curat la gate.

Maparea comună consumată: `bo.service-observation.v1` publicată de SOL-03 în
`coordonare/contracte/bo.service-observation.v1/README.md`. Nu există contract
concurent: service-observation→`/v1/observations` (scope `modelobs:write`,
`/v1/model-observations` este aliasul echivalent; ambele dispechează pe
`schemaVersion`); Heartbeat/RunFinished→`POST /v1/telemetry` (scope
`telemetry:write`; `/v1/telemetry/events` este alias aditiv cu autorizare
identică, acceptat de receptor și disponibil dacă se preferă transportul pe
un singur endpoint — `/v1/telemetry` dispechează la fel pe `schemaVersion`);
receipt/checkpoint existente→`/v1/execution-events`.
Același tenant/product/producer/installation, executionRef/runRef/correlationId.
O declarație RunFinished SUCCEEDED păstrează verificationStatus UNKNOWN;
numai ledgerul și receiptul corelat confirmă efectul. Telemetria nu veto-ează
efectul: un eșec de persistare/validare a observației este raportat ca
`telemetryStatus: DEGRADED` în receiptul ledgerului, nu ca răspuns pierdut.

PILOT-03 adaugă două suprafețe fără schimbare de contract:

- `GET /bo/pilot` include per rulare `telemetry{status,expected,queued,
  delivered,dead,missing,replayable,marker,error}` — postura outboxului citită
  direct din `bo_telemetry_outbox`; `queued` este stare locală, nu confirmare
  Guardian.
- `POST /bo/pilot/runs/{run_id}/telemetry/replay` (capabilitate
  `execution:write`, audit): re-pune în outbox plicurile lipsă reconstruite
  exclusiv din `bo_pilot_observations`, după re-validarea identității.
  Idempotent pe `eventId`; fără apel la provider, fără efect nou; plicurile
  dead-letter rămân în fluxul existent de retry cu motiv.

## Fixture HTTP locală (nu API ERP propus)

`python -m openexecutive.bo.pilot.fixture package --work DIR` generează pachet și
registru. `serve --work DIR --port PORT` folosește `BO_PILOT_FIXTURE_TOKENS`:
obiect JSON credential→tenant, exclusiv env. Nu loghează antete sau corpuri.
`POST /probe` primește tenantRef (asertat și comparat cu credentialul), idempotencyKey,payloadDigest,executionRef,correlationId,
producerId,installationId; tenantul derivă din credential. Persistă o singură
probă per tenant+cheie; alt digest→409. Răspunsul conține observația comună și
receiptul intern al providerului existent (effect_key,digest,receipt_ref,provider,
amount,received_at), plus tenant validat. `GET /receipts/{key}` este readback.
`POST /scenario {scenario}` și `GET /stats` sunt numai controale fixture, autorizate
și izolate pe tenant. UNKNOWN ascunde receiptul după persistarea efectului;
recover îl face din nou vizibil. Retry UNKNOWN în runtime nu face al doilea POST.

Adapterul acceptă numai endpoint literal `http://127.0.0.1:PORT` fără cale,
credentiale/query/fragment, port1024–65535, profil explicit synthetic-loopback și
allowlist exactă administrată. Nu folosește DNS/proxy și nu urmează redirecturi.
Răspuns maxim64KiB, timeout1–10s, secretRef server-only; nepotrivirea identității,
cheii sau digestului nu produce succes. Modificarea configurației/activării după
plan refuză apelul; readback rămâne conservator, fără mutarea efectului pe alt host.
