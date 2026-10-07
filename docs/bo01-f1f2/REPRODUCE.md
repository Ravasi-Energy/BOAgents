# Reproducere — candidat fixat, fixtures sintetice

Produs `BOAgents`, pin `00d12b675cd7bf552ac495df4160a7c78fe5bccb`. Fără clone/instalări; checkout și dependențe existente. Nu executați pe date reale. Ownerul poate lansa suita normală în mediul său; BO01 nu editează checkoutul ownerului.

## Testele normale

Selectați nodeID din REQUIREMENTS.json. Fișierele și hashurile aparțin commitului, nu copiei BO01. Exemple pentru cele mai noi probe:

```sh
cd packages/core
python -m pytest -v tests/unit/bughunt02/test_contract_r12.py tests/unit/bughunt02/test_contract_r14.py
cd ../ui
npm test
```

Nu confundați aceste comenzi de reproducere cu executarea nominală în CI. Mandatul07.10/16:35 interzice rerunCIunchanged doar pentru nume. Comenzile -v de mai sus sunt reproducere locală selectabilă, nu propunere de modificare/rerulareCI. Proveniența CIquiet este explicit observat/inferență; probeleMacnominale sunt locale.

## Headless real — indexul canonic existent

În BO01/BUGHUNT-UI-02/R12:

```sh
python3 review-index.py --verify-existing
python3 review-index.py --list
python3 review-index.py --scenario bo-critical
python3 review-index.py --scenario bo-writers
python3 review-index.py --scenario bo-mutators
python3 review-index.py --scenario bo-http-profile
```

Scenariile sunt selectabile de coordonator, fără executare implicită. Setupul canonic exportă commitul declarat, folosește runtimeurile existente readonly, data fresh în BO01, două contexte browser/admini și doi workers unde este cazul. Port străin ocupat: refuz; finally oprește doar procesele proprii. Localul are REVIEW-PINS actualizat pentru Hire/Guardian; REVIEW-PINS-F1F2 fixează toate cele3checkouturi pentru reproducere; PINS-R15 păstrează execuțiile istorice.

## Oracole produs

Admin:200/201/204 și readback/reload; viewer403 cu starea intactă; missing auth pagină/API refuzat. Stale409 păstrează draftul după refetch; rebase explicit, versiune actuală, persist/readback. Omit/empty nu șterg; delta adaugă, delete/remove explicit; baza v−3 și 20→10 testate. Profile race: un200/un409 la aceeași versiune, restartdurabil. People clear revocă și JWT deja emis. Hire timeout fărăversiune:zeroPUT/draftpăstrat;0valid acceptat. Guardian delete compus: fiecare segment encodat separat exact routerului. Beta rămâne intact, nu dedus din statusHTTP.

Indexul normal executabil de mai sus și sursele GitHub permit reproducerea regresiilor fără publicarea runnerilor locali care depind de istoricul R6–R10/mediul Mac. Reproducerea integrată headless folosește indexul local canonic; pachetul selectiv nu se prezintă drept harness portabil autonom.

## Intrare exactă pentru reviewheadless independent

Din workspace-ul Mac canonic, fără setup ghicit/instalări:

```sh
BO-A01/boagents-val4-01/packages/core/.venv/bin/python BO01/BUGHUNT-UI-02/R12/coordinator-headless-entry.py
# Executare selectată explicit de coordonator, fixture proaspătă/export fixat, toate cele6scenarii:
BO-A01/boagents-val4-01/packages/core/.venv/bin/python BO01/BUGHUNT-UI-02/R12/coordinator-headless-entry.py --run
```

Prima comandă este numai preflight: pinnedcodecommits/dependencies/script hashes/existingChromium/porturi/spațiu; nu pornește aplicații/browser și nu generează capturi. A doua folosește exact indexul canonicexistent și scenariile allroles/readback descrise mai sus; `--only bo-critical|bo-writers|bo-mutators|bo-http-profile|hire|guardian` selectează suprafața. BO01 nu a executat --run sau reluat fluxurileunchanged în această finalizare. Nu se lansează Chrome-ul utilizatorului; numai chromium.launch(headless:true) din Playwrightexisting.

Comenzile relative se execută din `BOGuardian/agenti`; calea absolută exactă este în indexul local canonic BO01R12. Harnessul nu este copiat înrepo — dépendențele/fixtureframework rămân în workspaceul existent.
