# Instalare și operare — pilot ERP sintetic

Extensie sandbox a candidatului VAL4 `37e80c2df8a63d082007ba38bcde342e58cbd470`.
Nu este integrare cu ERP real, certificare sau autorizare de producție.
Nu instalează scheduler, nu scanează rețeaua și nu trimite notificări.
Folosiți runtime-urile Python/Node existente; nu sunt dependențe noi.

## Ordine și identitate

1. Instalați întâi Guardian cu `bo.service-observation.v1` (publicat în215b326,
   schema SHA256 `a0421f3506aaecacd295b0f7c2c00594ff0f508cb06d712d9f5547a3566b9d27`).
   Contractele telemetry/execution v1 existente rămân neschimbate.
2. Provisionați explicit sursa Guardian și identitățile tenant/product `BOAgents`/
   producer/installation. Observații: `modelobs:write`; telemetrie: `telemetry:write`;
   autoritate/receipt: `execobs:write` conform contractului existent. Numai ownerul
   Guardian creează mandatul cu acțiune exactă `diagnose`, resursă `synth.erp`.
3. Porniți BOAgents cu `BO_TENANT_ID` fix și variabilele de mai jos exportate în
   procesele corespunzătoare. Citirea unui fișier dotenv de către Settings Python
   nu exportă automat variabilele consumate prin `os.environ`.
4. Bootstrap admin: utilizator sintetic inclus în `ALLOWED_EMAILS` la UI și
   `BO_ADMIN_EMAILS` la API. Un API key fără identitate delegată este operator:
   poate procesa cicluri, nu poate aproba pachete/setări/mandate. Un utilizator
   autentificat fără rol admin este viewer. Nu există selector public de rol/tenant.

| Variabilă | Proces / scop |
|---|---|
| BACKEND_SHARED_SECRET | API și proxy UI, credential serviciu |
| BACKEND_PROXY_SECRET | API și proxy UI, secret separat, server-only, diferit de cel de serviciu |
| AUTH_SECRET, AUTH_URL, AUTH_TRUST_HOST | UI, configurația Auth.js existentă |
| ALLOWED_EMAILS / BO_ADMIN_EMAILS | UI / API, numai identități sintetice în acest pilot |
| BOAGENTS_DB_PATH, EPISODIC_DB_PATH, BO_PACKAGES_DIR | API, căi persistente proprii; nicio DB urmărită Git |
| BO_TENANT_ID, BO_TELEMETRY_PRODUCER_ID, BO_INSTALLATION_ID | API, identitatea instalației, stabilă la restart |
| BO_PILOT_SERVICE_TOKEN | API, tokenul serviciului sintetic; numele poate fi schimbat prin SecretRef |
| BO_PILOT_FIXTURE_TOKENS | Numai fixture serve, JSON credential→tenant; valori numai în mediu |
| BO_GUARDIAN_TOKEN | API, credentialul autorității existente |
| BO_PILOT_OBSERVATION_TOKEN | API, credential `modelobs:write` separat pentru observații |
| BO_TELEMETRY_TOKEN, BO_TELEMETRY_ENABLED | API, telemetria existentă; oprirea ei nu oprește verificarea autorității |

Niciun secret nu are prefix NEXT_PUBLIC. Proxy absent/identic cu cheia de serviciu:
UI503, API401 pentru delegare; nepotrivire: API401. Lipsa secretului ERP: refuz înainte
de apel. Secret ERP al altui tenant:403 înainte de persistarea probei. Redirecturile
nu sunt urmate. Fără profil+allowlist+endpoint+activare, nu există execuție pilot.

## Flux UI și serviciul local

Generați fixture-ul semnat într-un director de probe propriu (nu surse/DB reale):

```sh
PYTHONPATH=packages/core python -m openexecutive.bo.pilot.fixture package --work "$PILOT_WORK"
PYTHONPATH=packages/core python -m openexecutive.bo.pilot.fixture serve --work "$PILOT_WORK" --port 8325
```

Alimentați `BO_PILOT_FIXTURE_TOKENS` în mediu înaintea `serve`; aceeași valoare sintetică
trebuie să se afle în SecretRef-ul API. Nu includeți valorile în comenzi/loguri publicate.
Serviciul ascultă exclusiv127.0.0.1, nu este daemon global. Opriți procesul după probe.

În **Setări BOAgents** activați importul de pachete și înrolați registrul public
`registry.json` generat, numai în acest tenant sandbox. Registrul fixture nu este
pentru producție. În secțiunea Pilot: profil `synthetic-loopback`, endpoint
`http://127.0.0.1:8325`, allowlist JSON conținând exact endpointul, SecretRef,
timeout/prag stale/limită coadă și supraveghere. Activați pilotul și execuția delegată.
Toate setările au CAS, RBAC, audit și applyMode IMMEDIATE. Snapshotul planului fixează
configurația; modificările refuză dispatch-ul unui plan vechi.

În **Pachete**, importați `$PILOT_WORK/package`: verificare→carantină. Promovați la
DRAFT. În **Pilot sintetic**, selectați pachetul, scrieți motivul și aprobați activarea.
Semnătura, emitentul, compatibilitatea și gramatica sunt reverificate la activare și
dispatch; nicio instrucțiune a pachetului nu se execută la import.

În **Execuții**, creați un mandat local explicit pentru diagnose/synth.erp, buget
limitat, max_steps1 și expirare. Supravegheat: legați mandatul Guardian provisionat;
copiii moștenesc toate constrângerile strămoșilor. Selectați mandatul în Pilot,
trimiteți diagnosticul (PENDING), apoi procesați ciclul local. Nu există activare
sau programare automată. Pause/cancel/revoke înainte de claim folosesc UI Execuții.

Standalone permite numai mandate nelegate când controlul global este opțional.
`bo.pilot.supervision=required` refuză submit fără autoritate Guardian în lanț;
controlul global obligatoriu este respectat separat. Un mandat legat nu devine
standalone prin schimbarea modului. Guardian indisponibil→PAUSED înainte de efect,
revocat/BLOCKED→refuz; nu este creat automat un mandat înlocuitor.

HEALTHY cere dovadă recentă și execuție confirmată; STALE cere verificarea timestampului.
UNKNOWN nu este zero. RunFinished SUCCEEDED este declarație a serviciului, nu receipt.
UNKNOWN→Caută receipt→Reia explicit→ciclu local păstrează cheia/digestul; nu repetă POST.
Readback rămâne disponibil după dezactivare, numai pe endpointul original încă
allowlisted, cu credential autorizat. Pierderea telemetriei lasă evenimentele în outbox;
flush/retry dead-letter se administrează prin suprafețele existente.

## Telemetrie degradată și reemisie (PILOT-03)

Receiptul rămâne dovada efectului: o cădere de telemetrie nu transformă un
SUCCEEDED valid în eșec și nu relaxează frontierele receiptului (tenant/digest/
cheie/provider/timestamp greșit rămân UNKNOWN chiar dacă telemetria pică).
`GET /bo/pilot` raportează per rulare `telemetry`: `status` (none/pending/ok/
degraded/incident/dead/unavailable), `expected/queued/delivered/dead/missing`,
`replayable` și marcajul din receipt (`telemetryStatus`/`telemetryError`).
„Queued" este starea locală a outboxului, nu confirmare Guardian.

Recuperare: `POST /bo/pilot/runs/{run_id}/telemetry/replay` (capabilitate
`execution:write`, audit `bo_pilot_telemetry_replay`). Re-pune în outbox numai
`eventId`-urile lipsă, reconstruite din `bo_pilot_observations` — dovada
persistată la rulare — după re-validarea identității ei. Nu apelează providerul,
nu creează efect nou, nu inventează observații. Idempotent: al doilea apel nu
pune nimic; plicurile livrate/pending/dead-letter rămân neatinse (dead-letter
rămâne sub fluxul existent `POST /bo/execution/outbox/{id}/retry`, cu motiv).
Fără observație persistată → 409 cu motiv; nu se fabrică succes.

Warning-urile de telemetrie loghează numai clasa excepției (ex.
`persistarea observației pilot a eșuat (OperationalError)`), fără `exc_info`,
text brut sau payload. Verificați logul la degradare; apoi replay, apoi
`telemetry.status` trebuie să treacă la `incident` (după enqueue) sau `ok`
(după livrare).

## Upgrade, dezinstalare și rollback

Opriți workerii proprii înainte de upgrade, salvați SHA/configurație și o copie SQLite
consistentă când aplicația este oprită; nu copiați DB cu WAL activ prin simplu cp.
Păstrați DB/quarantine/identitate/secretRefs. Inițializarea existentă adaugă numai
`bo_pilot_activation` și `bo_pilot_observations`; setările noi au implicit pilotul oprit.
Nu schimbă ID-urile, ledgerul, receipturile sau rezervările UNKNOWN/EXPOSED.
Migrarea VAL4 35193dc→37e80c2 a fost demonstrată în RC; proba PILOT completează
37e80c2→pilot cu UNKNOWN+rezervare activă și readback fără resubmit.

Dezinstalare logică: dezactivați pachetul cu motiv și opriți pilotul. Păstrați istoricul,
outbox-ul și bugetele expuse; reconciliați UNKNOWN. Nu ștergeți directoare/quarantine/DB.
Dezactivarea nu anulează un apel deja plecat pe fir.

Rollback cod: opriți workerii, setați `bo.exec.enabled=false`, dezactivați pilotul,
păstrați DB extinsă și reveniți la SHA VAL4 numai pentru citire/administrare.
**Nu reporniți workerii VAL4 peste rulări pilot PENDING/UNKNOWN:** versiunea veche nu
cunoaște providerul nou. Recuperarea acelor rulări se face cu versiunea pilot și
readback, nu prin resubmit sau restaurarea unei DB care uită efectele. Rollback live
cu workeri vechi nu este autorizat/demonstrat; oprirea execuției este condiție obligatorie.

## Probe și limite

Comenzi driver în [pilot01-wire.md](pilot01-wire.md). Scenariul delayed generează o
observație inițială veche: după un healthy mai nou al aceleiași identități, Guardian
trebuie să ignore corect evenimentul vechi. Pentru scenariu izolat utilizați identitate
distinctă provisionată, sau așteptați pragul administrat fără heartbeat nou; nu relaxați
ordonarea. Driverul este finit și oprește propriul serviciu în finally.

Teste noi: `tests/unit/test_bo_pilot01.py`; regresii afectate execution, audit_rem01,
rc01, packages, settings, routing. Toate folosesc SQLite sintetic în probe și mediul
existent. Capturile/UI/API și manifestul livrării sunt în directorul pilot01 atribuit.
Nu există probă de ERP real, notificare externă, scheduler sau certificare industrială.
