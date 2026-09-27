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
degraded/incident/dead/unavailable/corrupt), `expected/queued/delivered/dead/missing`,
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

## Dovezi corupte, cadență per tenant și telemetrie administrabilă (PILOT-04)

**Dovadă coruptă sau lipsă.** Un rând `bo_pilot_observations` cu JSON necitibil
sau cu formă invalidă nu mai produce HTTP 500: rularea este raportată
`telemetry.status=corrupt` cu `observation=null` și `health=UNKNOWN` — octeții
rămân în tabelă, neatinși, iar celelalte rulări ale tenantului funcționează.
Replay-ul refuză controlat 409; nu se fabrică o observație și nu se creează
efect nou. Rând lipsă → `unavailable`/`none`, același 409 la replay. Un outage
de stocare outbox la replay → 409 cu motiv; la status → raport `degraded`
cu `error=outbox_unreachable`.

**Interval de livrare per tenant.** `bo.router.delivery_interval_s` este recitit
de worker la fiecare trezire, per tenant: `0` = strict manual pentru acel tenant
(`POST /bo/routing/flush` funcționează mereu), indiferent de ceilalți tenanturi;
un tenant cu interval mare nu încetinește unul cu interval mic (trezirea e
programată la cel mai apropiat termen scadent, nu la maximul lor). O schimbare
de interval salvată se aplică la următorul ciclu fără restart; trecerea la `0`
renunță la programarea pendinte, iar reactivarea pornește o numărătoare nouă —
nu declanșează instant backlogul.

**Telemetrie administrabilă.** `bo.telemetry.enabled`, `bo.telemetry.transport`
(`buffered`|`http`), `bo.telemetry.endpoint` și `bo.telemetry.token_ref` sunt
setări de tenant în registrul Setărilor (tab „Telemetrie"), cu CAS, RBAC admin
și audit ca orice setare. Precedență per cheie: rândul salvat al tenantului →
bootstrap de proces (`BO_TELEMETRY_*` / adaptor injectat) → implicit registrul.
`GET /bo/telemetry/status` raportează `effective` (ce folosește următorul plic)
distinct de `bootstrap` (ce a construit procesul), cu `source` per cheie.
Secretul rămâne server-only: `token_ref` este numele variabilei de mediu, niciodată
valoarea; UI/API expun doar `token_configured` (da/nu). `http` fără endpoint sau
fără token disponibil este o stare vizibilă `incomplete` — plicurile rămân în
outbox, nu sunt marcate livrate. Tokenul bootstrap nu este mutat pe un endpoint
nou administrat (fără carry-over de secret pe altă destinație).

## Credential legat de destinație, backlog și rutare uniformă (PILOT-06)

**Provisioning explicit.** `bo.telemetry.token_ref` acceptă numai nume
provisionate de operator prin `BO_TELEMETRY_SECRET_REFS` (CSV de nume env) sau
referințele bootstrap încorporate (`BO_TELEMETRY_TOKEN`,
`BO_PILOT_OBSERVATION_TOKEN`). Un nume arbitrar este respins la salvare (422) —
administratorul nu poate transforma o variabilă de mediu oarecare în secret
administrabil. O intrare din listă este `NUME` (utilizabilă de orice tenant)
sau `NUME@tenant` (utilizabilă numai de acel tenant — re-scopabilă prin
repetare `NUME@t1,NUME@t2`); scoparea se aplică și la salvare (422 pentru
referința altui tenant) și la rezolvare/livrare — același format pentru
`BO_GUARDIAN_SECRET_REFS`.

**Legătura credential↔destinație↔tenant.** Un `endpoint` administrat per tenant
cere și un `token_ref` administrat și provisionat; altfel adaptorul raportează
`credential_state=endpoint_without_ref|unprovisioned|missing` și livrarea
refuză controlat (plicul rămâne pending, nimic nu pleacă pe fir). Fallback la
tokenul bootstrap există numai pe calea bootstrap neatinsă — niciodată pe o
destinație administrată.

**Legarea persistată în outbox.** Fiecare rând nou poartă `dest_endpoint`,
`dest_ref` și `dest_bound=1` — fotografia destinației efective de la enqueue.
O schimbare de setări nu re-rutează plicuri persistate: ele livrează strict pe
destinația și referința legate, sau refuză dacă referința nu mai are valoare.
Rândurile legacy pre-migrare (`dest_bound=NULL`) refuză cu motiv vizibil
(„plic legacy fără destinație asociată — reautorizare prin rebind"), nu sunt
ghicite și nu ard bugetul de tentative — refuzul se înregistrează o singură
dată, apoi rândul rămâne pending fără să mai fie revendicat (nu ajunge în
dead-letter prin capul de attempts, nu înfometează rândurile legate).

**Rebind auditat.** `POST /bo/execution/outbox/rebind` (`execution:write`,
motiv obligatoriu, opțional `event_ids`) reasociază plicurile nelivrate la
destinația curent efectivă pe kind-ul lor — singura cale autorizată de a muta
backlog sau rânduri legacy. Octeții și identitățile plicurilor nu se rescriu;
audit `bo_outbox_rebind` cu actor/motiv/destinații. Un rând sub lease activ
(posibil send în curs) este sărit, nu reasociat în zbor; un `event_id` deja
livrat e conflict (409) — istoricul nu se rescrie. Retry și replay nu schimbă
destinația — re-trimit octeții pe legătura înregistrată.

**Legătura de sink.** Un plic persistat când transportul nu avea destinație
HTTP (sink buffered/dezactivat) poartă `("", "")`: livrează numai cât
transportul efectiv rămâne sink; o destinație HTTP administrată ulterior
refuză plicul — nu primește un plic legat înainte de a fi configurată.

**Canal Guardian.** `bo.exec.guardian_secret_ref` și
`guardian_policy_secret_ref` acceptă numai nume provisionate de operator
(`BO_GUARDIAN_SECRET_REFS`, CSV) sau referințele încorporate
(`BO_GUARDIAN_TOKEN`, `BO_GUARDIAN_POLICY_TOKEN`, `BO_TELEMETRY_TOKEN`).
Rândul legat înregistrează referința care alimentează efectiv tokenul —
inclusiv fallbackul bootstrap `BO_TELEMETRY_TOKEN` pe canalul de bootstrap —
iar pe un endpoint administrat nu se substituie nimic. Detaliul de eroare
venit de la receptor este redactat înainte de a ajunge în `last_error` —
un receptor ostil nu poate reflecta tokenul Bearer înapoi în DB/UI.
`bo.telemetry.enabled=false` suspendă numai kind-urile de telemetrie —
plicurile `execution` continuă să se scurgă automat.

## Audit durabil, izolare la livrare și upgrade (PILOT-07)

**Intenții de audit durabile.** Evenimentul de audit al unui rebind este o
intență în `bo_audit_intents`, comisă în ACEEAȘI tranzacție cu mutația —
un crash între commitul rutei și scrierea în jurnal nu mai poate pierde
auditul. `drain_audit_intents` (apelat după rebind și la fiecare ciclu al
workerului) reia intențile către jurnalul central cu deduplicare pe
`intent_id`; emisia e confirmată prin re-citirea jurnalului, deci un emit
urmat de crash la marcaj nu dublează rândul. Statul real e raportat
(`audit=delivered|pending|failed`) și expus în `outbox_stats`
(`audit_pending`/`audit_failed`) + UI — fără afirmație falsă de „auditat".

**Izolarea scopului la livrare.** Legătura persistată fixează NUMELE
referinței, nu valoarea: la fiecare livrare pe un rând legat, ref-ul este
re-verificat în `BO_TELEMETRY_SECRET_REFS` PENTRU TENANTUL curent —
re-scoparea `NUME@alt-tenant` sau ștergerea intrării revocă credentialul
chiar dacă variabila env există încă (refuz închis, zero octeți), iar
rotația valorii env este preluată cât timp referința rămâne autorizată.

**Concurență.** `PRAGMA busy_timeout=5000` pe conexiunile `bo` face ca
rebind×claim×delivery concurente să aștepte lockul, nu să crape
(`database is locked`). Un rând sub lease activ este sărit de rebind și
nu livrat de alt worker; lease-ul eliberat la `resolve_outbox` îl
reîntoarce în pending — convergență garantată pe runde ulterioare, fiecare
plic o singură dată pe fir.

**Upgrade d519→head.** Migrarea e aditivă (coloane `dest_*` noi, tabelul
`bo_audit_intents`): rândurile pre-existente rămân `dest_bound=NULL` —
vizibile ca legacy, refuzate închis până la rebind auditat; istoricul
delivered/dead-letter nu se atinge; lease-urile active moștenite sunt
respectate. Restartul workerului (proces nou pe aceeași DB) reia totul.

**Rutare uniformă.** `pilot/delivery` consumă aceeași configurație efectivă
ca adaptorul general (`adapter.resolve`) — endpointul și referința administrate
guvernează observațiile pilot și telemetria derivată (Heartbeat/RunFinished)
identic. Plicurile `kind="execution"` rămân pe canalul de autoritate
`bo.exec.*`/`guardian`, legat separat la enqueue — cele două canale nu se
substituie.

**Cadență corectă.** După o trimitere, workerul reprogramează scadența
tenantului și așteaptă cel mult până la cea mai apropiată scadență — un send
nu mai poate întârzia un tenant rapid până la boundul de recheck.

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
