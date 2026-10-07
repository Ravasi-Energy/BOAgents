# CONTRACT-UI — BO-A01 BUGHUNT-02 (revizia R2 — contract corectat)

Stare: **IMPLEMENTAT (corectiv R3, completat R12, fence multiproces R14)** —
contractul de mai jos descrie codul livrat la `1541e4f` + remediile R12
(`0b07c9f`) + zăvorul de profil multiproces R14.
Revizia R2 (decizia `DECIZIE-CONTRACTE-P0-20261004-R2.md`) a respins
semantica din versiunea anterioară a acestui document: **nu** se mai deduce
baza scriitorului din `expected_version − 1`, **nu** se mai șterge prin
omisiune și **nu** se mai publică fără versiune. Contractul aprobat și
implementat este: **CAS strict + delta explicită**.
Probele: `tests/unit/bughunt02/` (incl. `test_contract_r3.py` — regresiile
adverse R2), `tests/unit/test_bughunt_p0_mutators_full_app.py`,
`packages/ui/scripts/bughunt-p0-*.test.mjs` + `bot-draft-r2.test.mjs`.

## 0. Reguli globale (toate rutele de mai jos)

- **Identitate**: `openexecutive.bo.identity.resolve_identity(request)`. Tenantul vine din
  `BO_TENANT_ID` (config server) — niciodată din payload/header de selecție. Hint diferit
  (`x-bo-tenant`, `?tenant=`) → `403 tenant_mismatch`, fără divulgarea tenantului configurat.
- **Rol**: `viewer < operator < admin`, derivat din identitate (BO_ADMIN_EMAILS + roster
  `is_principal` nearhivat) — niciodată din câmp trimis de client.
- **Headere proxy** (delegated user): `x-caller-email` + `x-caller-proxy-secret` =
  `BACKEND_PROXY_SECRET`. Serviciu: `x-api-key` = `BACKEND_SHARED_SECRET` (rol `operator`).
- **Fallback dev**: identitate `local-dev` (admin) **numai când nu există niciun material
  de autentificare configurat** (fără `BACKEND_PROXY_SECRET` și fără `BACKEND_SHARED_SECRET`)
  și `OE_PUBLIC_DEPLOYMENT` e dezactivat. Pe instalare cu secrete configurate, un request
  fără identitate pe mutațiile de mai jos primește **403**.
- **CAS strict**: `expected_version` = **versiunea pe care clientul a citit-o efectiv**
  la începutul editării (baza declarată onest). Orice neconcordanță cu versiunea curentă
  → **409 înainte de orice efect**; clientul păstrează draftul, reîncarcă (`GET`), își
  rebază schimbarea pe conținutul nou și retrimite cu versiunea proaspătă. Nu există
  merge automat „din istorie" — serverul nu ghicește niciodată baza din valori vechi.
- **CAS multiproces (R14)**: pentru `profile.yaml` verificarea de versiune e onestă
  numai dacă citește versiunea persistată *sub un zăvor partajat între procese*.
  Secvența read→CAS→merge→write a PATCH /company-profile rulează într-un zăvor
  kernel pe fișierul sidecar `profile.yaml.lock` (flock/msvcrt, re-entrant per
  thread). **Toți** scriitorii de profil (PATCH, onboarding commit, fixture load,
  restore slot client, CLI) serializează prin același zăvor din `save_to_yaml`,
  iar scrierile non-CAS incrementează `version` monoton peste valoarea de pe disc —
  un pin vechi nu poate coliziona cu o suprascriere ulterioară. Un deținător
  blocat produce **503 `profile_lock_timeout`** (implicit 10 s) în loc de hang;
  la crash al procesului, kernelul eliberează flock-ul odată cu fd-ul — un fișier
  `.lock` rămas e inert, niciodată deadlock.
- **Delta la scriere**: payload-ul conține doar ce a atins scriitorul — fiecare câmp
  prezent este aplicat; un câmp **omis** este întotdeauna păstrat:
  - dicționare: merge recursiv (sub-câmp omis ⇒ păstrat);
  - colecții cu identitate (`steps`, `publishers`, `keys`): **upsert pe cheie**
    (`id`/`publisherId`/`keyId`) — elementul omis supraviețuiește, `[]` = no-op;
  - liste de valori (`capabilities`, `regions`, `capability_refs`, `policy_refs`,
    `vendors`, `department_slugs`, CSV): **uniune-adăugare** — `[]`/`""` = no-op;
  - scalare: valoarea scriitorului se aplică — **revenirea intenționată la o valoare
    istorică (ex. 20→10) este o scriere legitimă** și reușește.
- **Ștergere**: numai prin operație explicită pe identitate stabilă —
  `steps_remove`, `capability_refs_remove`, `policy_refs_remove`,
  `publishers_remove`, `keys_remove`, `capabilities_remove`, `regions_remove`,
  `vendors_remove`, `department_slugs_remove`, `remove` (CSV). Op-urile sunt
  idempotente (element deja absent = no-op). **Omisiunea nu șterge niciodată.**
- **Client vechi ambiguu**: payload gol (`""`) pe documente JSON → **refuz
  explicit 422**, documentul rămâne intact; un scriitor stale → **409** +
  reîncărcare, nu ghicire și nu succes fictiv.

## 1. `PUT /bo/settings/{key}` — ident existent, contract nou pe valori delta

- Rol: `settings:write` → **admin**. Citire `GET /bo/settings` → viewer.
- Body: `{"value": <orice>, "expected_version": int, "remove": <opțional, doar CSV>}`.
  `value` lipsă → **422** (exista deja).
- Tipuri delta:
  - `bo.router.allowed_providers|allowed_regions|required_capabilities` — **CSV**:
    `value:"patru"` ⇒ uniune adăugare (3→4); `value:""` ⇒ neschimbat;
    ștergere: numai `remove:["unu","doi"]` (op explicit, idempotent).
  - `bo.packages.trust_store_json` — **JSON doc sparse-delta**: câmpurile
    prezente se aplică recursiv; `publishers`/`keys` fac upsert pe
    `publisherId`/`keyId`; `""` ⇒ **refuz 422** (payload ambiguu, doc intact);
    ștergere: `publishers_remove`/`keys_remove` în documentul delta.
    **Nested (R12)**: merge-ul pe rânduri keyed e recursiv — `allowedKinds:[]`
    într-un rând existent e **no-op** (nu mai șterge lista); op-urile
    `<field>_remove` se consumă la **orice adâncime** și nu persistă niciodată
    în document (nici pe rânduri noi adăugate).
  - Restul cheilor: valoare scalară — înlocuire normală, CAS obligatoriu.
- Exemple:
  - OK admin: `PUT /bo/settings/bo.router.allowed_providers {"value":"patru","expected_version":1}` →
    `200 {"result":"SAVED","setting":{"value":"unu,doi,trei,patru","version":2}}`
  - Refuzat viewer: același body → **403**.
  - Conflict: `expected_version` în urmă → **409 version_conflict**.
- Readback: `GET /bo/settings` → `settings[].value|version|origin`.

## 2. BoBots — `PATCH /bo/bots/{id}` + `POST /bo/bots/{id}/publish`

- Rol: `bots:write` → admin (ambele). `bots:simulate` → operator.
- PATCH body: `{"expected_version": int≥1, "name"?, "description"?, "content"?: bobot.v1,
  "steps_remove"?: [id…], "capability_refs_remove"?: […], "policy_refs_remove"?: […]}`.
  `content` este un delta rar: `steps[]` upsert pe `id` (un pas omis supraviețuiește —
  fără ștergere prin omisiune; `[]` = no-op), `capability_refs`/`policy_refs`
  uniune-adăugare, `trigger` merge pe chei, scalare aplicate. Ops-urile `*_remove`
  sunt surori ale `content`, nu câmpuri de document.
- Publicare: `POST /bo/bots/{id}/publish` cu body **obligatoriu**
  `{"expected_version": int}` — lipsa câmpului/body-ului → **422**, versiune
  stale → **409** și nimic publicat. Nu există „publică ciorna curentă" fără CAS.
- Readback: `GET /bo/bots/{id}` → `draft_version`, `versions[]`, conținut ciornă.

## 3. Catalog rutare — `PUT /bo/routing/catalog/{entry_id}`

- Rol: `routing:write` → admin; `routing:read` → viewer.
- Body `_CatalogEntryPatch`: `expected_version` + câmpuri opționale (`provider`, `model_id`
  și `source` rămân cerute pentru identitate) + `capabilities_remove`/`regions_remove`.
  Câmp `null`/omis/`[]` ⇒ păstrat; `capabilities[]`/`regions[]` uniune-adăugare;
  `cost`/`quality` merge pe chei; scalare aplicate. Ștergere din colecții numai
  prin `*_remove` — omisiunea nu șterge.
- Conflict: versiune veche → **409**; identitate duplicată → **409 duplicate**.
- Readback: `GET /bo/routing/catalog` → `entries[].version` + toate câmpurile.

## 4. `PATCH /company-profile` — poartă nouă + delta vendors/financials + CAS durabil multiproces

- Rol: **admin** (`profile:write`, nou). Citire GET → neschimbată.
- HTTP fără identitate validă sau viewer → **403**, profilul rămâne intact.
- Apel direct în proces (`ident=None` prin `Depends`) = apelant local privilegiat —
  păstrat pentru consumatorii interni (onboarding commit).
- **Versionare durabilă (R12)**: `CompanyProfile.version` persistă în
  `profile.yaml`; fișiere vechi fără câmp migrează logic la `version=1`.
  GET îl expune (`CompanyProfileResponse.version`).
- **CAS obligatoriu**: `expected_version` este **cerut** — lipsă → **422**
  `expected_version_required`, stale → **409** `version_conflict` cu
  `current_version`, zero efecte. Fiecare scriere reușită incrementează cu 1.
- **Zăvor multiproces (R14)**: verificarea de versiune se face pe o
  **recitire proaspătă din fișier înăuntrul zăvorului** `profile.yaml.lock`
  (nu pe obiectul citit înainte de achiziție). Zăvorul acoperă
  read→validate→merge→bump→write atomic; timeout → **503**
  `profile_lock_timeout`. Aceeași secțiune e partajată cu onboarding commit,
  fixture load și restore-ul de slot (toți trec prin `save_to_yaml`, care
  ține zăvorul și aplică bump monoton).
- Body `CompanyProfileUpdateRequest` extins:
  - `vendors: list[str]` ⇒ **adăugare** (uniune, fără duplicate); `vendors: []` ⇒ neschimbat;
    ștergere: `vendors_remove: list[str]` (noul câmp). `tickers`/`tickers_remove` la fel.
  - `financials` ⇒ merge parțial pe câmpuri (`runway_months` nu mai șterge
    `burn_rate_monthly`/`burn_rate_currency`/`key_metrics`); `key_metrics` merge pe chei.
  - restul câmpurilor scalare: înlocuire când sunt prezente, omis ⇒ păstrat (neschimbat).
  - `annual_revenue_arr` ⇒ **Decimal** (șir zecimal, nu float) — P1-4.
  - `expected_version: int` — obligatoriu (vezi mai sus).
- Readback: `GET /company-profile` + reîncărcare YAML.

## 5. People — `POST /people`, `PATCH /people/{id}`, `POST /people/{id}/archive`

- Rol: **admin** (`people:write`, nou) pe toate trei. Fără identitate/viewer → **403**.
- `is_principal: true` cere același admin — viewer nu se poate auto-promova
  (P0-8: secretul proxy autentifică, nu ridică rolul).
- `PATCH` (R12): `expected_version` este **obligatoriu** — lipsă → **422**
  `expected_version_required`, stale → **409** cu zero efecte. Toate scrierile
  unui PATCH (câmpuri, `department_slugs` uniune + `department_slugs_remove`,
  `authority_scope`, `availability`) rulează în **aceeași tranzacție
  `BEGIN IMMEDIATE`** cu un singur bump de `version`.
- **Clear explicit**: câmpurile nullable (`email`, `chat_id`-uri,
  `reports_to`, `on_leave_until`, `preferred_channel` etc.) se șterg **numai**
  prin flaguri `clear_*` dedicate; JSON `null` → **422 `ambiguous_null`**.
  `clear_email` taie și admiterea din roster-ul auth. `preferred_channel`
  invalid → **422** înainte de orice scriere.
- **Audit**: fiecare mutație emite un eveniment `people_person_updated`
  (actor, persoană, câmpuri atinse).
- Readback: `GET /people`, `GET /people/{id}` (răspunsul include `version`).

## 6. Alte mutații P0-10 — poartă ident + rol

- `POST /departments`, `DELETE /departments/{slug}` → `departments:write` → admin.
- `POST /documents` (upload) → `documents:write` → admin.
- `POST /onboard/interview/commit` → `onboarding:write` → admin.
- Fiecare primește parametru `ident` (Depends pe `resolve_identity`); eșec identitate → **403**.
- Probe HTTP proprii pentru fiecare: viewer → 403 + date intacte; admin → efect real.

## 7. Execuție — `submit_run` idempotent pe `correlation_id` [propunere→implementat]

- Același `(tenant, correlation_id)` + același payload ⇒ se întoarce runul existent,
  **o singură rezervare** (unic index `tenant+correlation`, verificat în `BEGIN IMMEDIATE`).
- Același `correlation_id` cu payload diferit ⇒ **409 conflict** (`correlation_conflict`),
  nu reutilizare greșită și nu al doilea run.

## 8. Roster / sesiune — `GET /auth/allowed-emails` + `allowlist.ts`

- Endpointul raportează **`{administered, emails}`**: `administered=false` când nu există
  nicio persoană (inclusiv arhivate) — bootstrapul ALLOWED_EMAILS rămâne viu pe instalații
  neadministrate. Forma listă pură = administrat (compat sondă coordonator).
- `decideAllowed(email, env, roster)`: roster citit (chiar gol) ⇒ **autoritar**
  (absența = revocare, chiar și pentru email din ALLOWED_EMAILS — decizia coordonatorului B-R-1);
  roster indisponibil (`null`) ⇒ fallback ALLOWED_EMAILS.
- `createRosterLoader`: **fără servire din cache TTL** — fiecare apel revalidează la backend
  (concurența e coalesced; `ttlMs` rămâne doar hint). Revocarea are efect la următorul
  `authorized`, nu după 5 minute.
- Politica de recuperare (separată, auditată): re-adăugarea persoanei în roster sau
  indisponibilitatea backendului — nu ALLOWED_EMAILS în paralel.

## 9. Bani și timp (P1)

- ARR în modele/persistență/API: **Decimal** (șir), niciodată float; YAML păstrează șirul.
- Wizard: `1.234,56 lei` → `1234.56` RON; `1.005 million` → `1005000` exact (Decimal).
- Politică cost mixt: valută candidat ≠ valută politică ⇒ **REFUSE `CURRENCY_MISMATCH`**
  (fără curs+sursă+dată nu se compară numeric).
- `run_at` naiv (fără fus) în `schedule_followup`/nudge ⇒ interpretat **Europe/Bucharest**,
  stocat UTC. Cadence `daily|weekly|quarterly@HH:MM` ⇒ fusul `bo.ui.timezone`
  (implicit `Europe/Bucharest`); ore inexistente → decalare înainte; ore ambigue →
  prima apariție (fold=0), documentat.
- `requeue_orphaned_running`: numai lease-uri **expirate** (`lease_until < now` sau legacy
  fără lease); un claim proaspăt nu e re-revendicat (C13).

## 10. Compatibilitate client vechi

- Clienții care trimit documente complete la `expected_version` corect: rezultat identic
  cu azi (toate câmpurile „se aplică" — diferența față de bază acoperă tot documentul).
- Clienții care trimit documente parțiale/stale: anterior înlocuiau → acum merge delta.
  Nicio rută nu mai acceptă ștergere implicită prin listă goală.
- `PATCH /people` fără `expected_version` (R12): **422 `expected_version_required`** —
  clientul vechi trebuie actualizat să pin-uie versiunea citită (la fel ca profilul).
- `PATCH /company-profile` fără `expected_version` (R12): același **422** — CAS e
  obligatoriu pe ambele suprafețe.
