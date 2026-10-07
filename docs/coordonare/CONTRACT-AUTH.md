# CONTRACT-AUTH — `{administered, emails}` (BUGHUNT-02, CONTROL R4)

**Backend**: A01, commit curent pe `codex/rem-audit-01-a01` (după `1541e4f`).
**Client**: BO01 implementează după acest contract — clientul actual
(`packages/ui/src/lib/allowlist.ts`) așteaptă array și pică pe env la eroare;
este suprafața lui BO01, nu a A01.

## 1. GET /auth/allowed-emails — o singură formă

```json
{
  "administered": true,
  "emails": [{ "email": "ana@firma.ro", "person_id": 7 }]
}
```

| Câmp | Semantică server |
|---|---|
| `administered` | Fapt durabil (`roster_meta` în people DB): devine `true` la prima scriere administrativă în roster (create/patch/archive, inclusiv onboarding/fixture), supraviețuiește restart și arhivarea tuturor. Nu se deduce din conținut — răspuns explicit al serverului. |
| `emails` | Persoane nearhivate cu email, lowercase. Poate fi `[]` și la `administered: true` — înseamnă „administrat și gol", NU „neadministrat". |

## 2. Reguli client (BO01)

| Situație | Decizie corectă |
|---|---|
| `administered: true` | Rosterul e autoritar — inclusiv gol. `ALLOWED_EMAILS` nu mai admite nimic. |
| `administered: false` | Serverul confirmă neadministrat → bootstrap `ALLOWED_EMAILS` permis. |
| Eroare fetch / timeout / non-200 / body malformat | **NU e neadministrat.** Fail closed: nu transforma eroarea în permisiune env; refuză sau menține stare existentă, niciodată admisie nouă pe env. |

Regula de bază: bootstrap env există **numai** când serverul a spus explicit
`administered: false` — niciodată ca fallback la eroare. O revocare urmată de
eroare de rețea rămâne revocare.

## 3. POST /auth/roster/recover-env — recuperare separată, explicită, auditată

- Poartă: `auth:recover` la rang **operator** → admis `x-api-key` (identitate
  service) sau proxy admin (`x-caller-email` + `x-caller-proxy-secret` cu
  email în `BO_ADMIN_EMAILS` sau principal în roster). Viewer → 403.
- Efect: șterge flag-ul → `GET` raportează `administered: false` → env
  bootstrap permis din nou. Unica cale înapoi (lockout).
- Răspuns: `{"administered": false, "cleared": bool}` (`cleared=false` dacă
  flag-ul nu era setat — no-op auditat).
- Audit: fiecare apel scrie `auth_roster_recovery` cu actor/role/auth_source.
  Tipul de eveniment NU e în EVENT_TYPES → nu e falsificabil prin
  `POST /audit/log`.

## 4. Rânduri distincte (nu se închid prin helper)

- **B-R-1** — revocare explicită > `ALLOWED_EMAILS`: acoperită de
  `administered: true` autoritar; regresiile client sunt ale BO01.
- **B-P1-8** — revocare verificată la autorizare / cache vechi: loaderul
  revalidează la fiecare apel (deja livrat); clientul nou păstrează regula.
- **B-R-8** — JWT emis înainte de revocare refuzat pe rută protejată:
  rămâne de probat end-to-end de BO01 pe noul DTO — distinct de allowlist.

## 5. Dovezi backend (acest commit)

- `tests/unit/test_auth_allowed_emails_route.py` — 11 teste: forma
  `{administered, emails}`, fresh→false, scriere→true, arhivare-totală→true
  cu `emails: []`, durabilitate pe conexiune nouă, recovery 403
  (fără identitate / viewer), 200 service + admin, no-op auditat, reset→false.
