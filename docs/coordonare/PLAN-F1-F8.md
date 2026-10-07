# Execuție pe etape complete F1–F8

Decizie Ștefan, 06.10.2026: etapele nu se fragmentează. Acesta este punctul stabil de coordonare pentru execuție; planul normativ PLAN-IMPLEMENTARE-INCHIDERE-V4.md și scopeul lui rămân. R1–R15 sunt istoricul verificărilor, NU etape suplimentare de implementare. Nu creați altă serie de valuri pentru aceeași muncă.

## Sarcina activă: închiderea integrală F1 și F2

A01, A02 și BO01 lucrează în paralel pe responsabilități distincte. Nu se opresc după un patch, commit, test sau document. Rezolvă defectele din aria proprie, integrează, probează și predă etapa completă. Checkpointul transmite contractul/SHA colegului și permite continuarea, nu înseamnă finalizarea etapei. Dacă există blocaj, se raportează exact și se continuă sarcinile independente din aceeași etapă.

| Agent | Mandat complet F1/F2 | Predare obligatorie |
|---|---|---|
| BO-A01 | BOAgents și Hire: toate mutațiile P0, identitate/roluri/tenant, job memory/publicare/retenție/requeue; scrieri delta/CAS/delete explicit, profil multiproces, People clear/JWT, nested trust, bot/catalog/settings/timeouts; toți workerii/rutele/apelanții, migrare/restart și integrarea patchurilor BO01 | Dosar F1 și dosar F2 pentru ambele produse, toate cerințele mapate, SHA finale, CI, probe înainte→după, persistență/readback/AlphaBeta și zero restanțe pentru acceptare |
| BO-A02 | Guardian: toate P0, identitate execution_ref/receipts/checkpoint, izolarea pe toate rutele fleet/ingest; colecții/scalari/politici/DELETE/CAS, migrare/restart/crash/idempotență, integrarea transportului UI și CI pe candidat | Dosare F1/F2 Guardian exhaustive, RA15 legate, CI pe SHA curent și zero restanțe pentru acceptare |
| BO01 | Toate fluxurile și apelanții UI din F1/F2 pe candidatul integrat: 21P0/27callers, Next auth pagina/API, profil/People/trust/bot/catalog/settings, timeouturi Hire și fluxuri Guardian; roluri și refuzuri, două contexte browser și backend multiproces, draft/rebase/persistență/readback/reload | Dosare F1/F2 UI: rute/acțiuni/roluri/tenant, teste normale CI, probe headless reale și capturi desktop/tabletă/mobil, SHA/proveniență și zero restanțe pentru acceptare |

A01 lucrează numai în checkouturile atribuite BOAgents/Hire, A02 în Guardian. UI se implementează numai de BO01 în Terminal; Devin integrează patchurile BO01. Păstrați munca necomisă și istoricul. Pinii curenți de intrare sunt BO00d12b6/Hireeba09e4/G534595c; verificați dacă au avansat înainte de probă. Nu repetați teste neschimbate fără motiv; proba nouă sau eșecul justifică rerularea suprafeței afectate.

## Ordinea rămasă — opt etape, fără subdiviziuni noi

| Etapă | Rezultat complet cerut | Responsabili |
|---|---|---|
| F1 | Izolare firme, identitate și corelarea execuțiilor complete | A01/A02 backend, BO01 fluxuri, coordonator acceptare |
| F2 | Scrieri fără pierderi și toate mutațiile concurente probate | A01/A02 backend, BO01 fluxuri, coordonator acceptare |
| F3 | D07 cap-coadă: validare/calcul/extracție/API/persistență/UI, monedă din date și setare server0/2 | A01/A02 domeniu, BO01 UI, coordonator decizii/acceptare |
| F4 | Toate celelalte P1: calendar, revocare, retenție/bindings, recuperare | A01/A02, BO01 UI necesară, coordonator acceptare |
| F5 | Fiecare P2/P3/UNKNOWN probat și rezolvat, nicio lacună ascunsă | A01/A02, BO01 fluxuri, coordonator acceptare |
| F6 | Toate cerințele produsului implementate, integrate și probate | Coordonator scope, A01/A02 implementare, BO01 fluxuri |
| F7 | Toate rutele/paginile/butoanele/rolurile și funcțiile închise funcțional | BO01 probe și UI, A01/A02 corecții backend, coordonator acceptare |
| F8 | Integrarea finală BOAgents/Guardian/Hire, migrare/recuperare/operare și candidat complet de predare | Toți pe ariile proprii, coordonator acceptare |

După F1/F2 acceptate, agenții trec la F3/F4 deja atribuite conform V4; apoi F5, F6, F7 și F8. Nu este nevoie de prompt nou pentru fiecare subpas. Coordonatorul consemnează acceptarea porții pe tablă. HOLD, test lipsă sau blocaj mențin etapa deschisă. Designul începe numai după închiderea tuturor celor opt etape.

## Dovada de închidere

Folosiți coordonare/sarcini/FISA-INCHIDERE-GRUPA.md. Pentru fiecare cerință: sursă, rută/worker/acțiune, rol/tenant, SHA integrat, regresie înainte→după, persistență/readback/restart/concurență/crash conform criteriului, CI pe SHA, limită explicită. Zero cerințe, rute sau probe restante. Testele verzi ale unei componente nu închid etapa întreagă. Predarea autorului este PREDAT PENTRU REVIEW; coordonatorul verifică independent și înscrie verdictul.

Nu declarați închisă o etapă ca să începeți următoarea. Nu mutați cerințe restante după design. Nu faceți alte inventare generale în locul implementării și probelor. Livrați contract/SHA deblocant imediat și continuați; la sfârșit predare exhaustivă, nu o listă de promisiuni.

## Reguli persistente

Fără main/merge/deploy fără Ștefan; push normal pe ramurile atribuite. Fără force/reset, sesiuni/agenți/clone/medii noi. D07, Europe/Bucharest, runtimeuri existente, plafon30GB/prag24GB. Playwright numai headless, fără actualizarea automată a referințelor vizuale. Întrebările la coordonator. Această decizie nu reduce scopeul V4 și nu autorizează redesign înainte de F8.
