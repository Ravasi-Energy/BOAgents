#!/usr/bin/env python3
"""Runner executabil al probei externe Oblio (HOLD-OBLIO-1, HIR-REQ-138/306).

Sursa canonica: tools/val4/proba_extern_oblio.py (repo BOAgents).
Copia din agenti/BO-A01/instrumente-f5/ este derivata, cu hash consemnat.

Se ruleaza numai cand exista acces de test autorizat:

    python3 proba_extern_oblio.py \
        --base-url https://www.oblio.eu \
        --client-id <id-cont-test> \
        --secret-ref OBLIO_TEST \
        --firma-cui <CUI-firma-test> \
        --emit \
        --hire-backend <cale backend Hire> \
        --out <dir-propriu>

Fara `--emit` proba este read-only: autentificare + listare/readback.
Cu `--emit` emite CEL MULT 5 proforme (hard cap) pe firma de test,
niciodata `docs/invoice`, niciodata email.

Refuzuri INAINTE de orice trafic (exit 2, zero apeluri HTTP — masurat):
- secretul `CONECTOR_SECRET_<ref>` neprovisionat sau referinta invalida
- `--base-url` fara schema https
- `--emit` fara `--firma-cui`
- `--out` fara containment real in --zona (resolve+commonpath, fara
  scapare prin sibling/traversal/symlink)

Erorile adaptorului sunt clasificate: refuz de autentificare (HTTP
401/403 la token) e distinct de outage/transport/raspuns invalid —
un outage NU poate fi raportat ca refuz pozitiv. Raportul nu contine
token/parole/secrete: detaliile sunt trunchiate si elidat `access_token`.
"""
import argparse
import json
import os
import re
import sys
import urllib.request

_ROOT = os.path.dirname(os.path.abspath(__file__))
_ZONA_DEFAULT = os.path.join(_ROOT, "extern")

_MAX_PROFORME = 5
_apeluri_http = {"n": 0}
_urlopen_real = urllib.request.urlopen


def _urlopen_numarat(req, *a, **kw):
    _apeluri_http["n"] += 1
    return _urlopen_real(req, *a, **kw)


def _curata(text) -> str:
    """Detaliile din raport nu poarta tokenuri/secrete."""
    t = re.sub(r'(access_token["\']?\s*[:=]\s*["\']?)[^"\'\s,}]+',
               r'\1***', str(text))
    return t[:200]


def in_zona(cale: str, zona: str) -> bool:
    """Containment real: resolve()+commonpath, fara scapare sibling/
    traversal/symlink. Zona insasi este acceptata ca destinatie."""
    try:
        rz = os.path.realpath(zona)
        r = os.path.realpath(cale)
        return os.path.commonpath([r, rz]) == rz
    except (ValueError, OSError):
        return False


def refuza(motiv: str, cod: int = 2) -> int:
    print(json.dumps({"verdict": "REFUZ", "motiv": _curata(motiv),
                      "apeluri_http": _apeluri_http["n"],
                      "efect": "zero"}))
    return cod


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        description="Proba externa Oblio (HOLD-OBLIO-1) — numai cu acces autorizat")
    p.add_argument("--base-url", required=True, help="ex. https://www.oblio.eu")
    p.add_argument("--client-id", required=True, help="client_id al contului de test")
    p.add_argument("--secret-ref", required=True,
                   help="numele referintei; valoarea sta in CONECTOR_SECRET_<ref>")
    p.add_argument("--firma-cui", default="",
                   help="CUI al firmei de test = cif_emitent (obligatoriu cu --emit)")
    p.add_argument("--client-nume", default="",
                   help="numele clientului sintetic (obligatoriu cu --emit)")
    p.add_argument("--moneda", default="RON",
                   help="moneda ISO4217 explicita (ex. RON) — fara implicit din adaptor")
    p.add_argument("--serie", default="PROBA",
                   help="seria de test a documentelor emise")
    p.add_argument("--emit", action="store_true",
                   help="emite cel mult 5 proforme de test (fara = read-only)")
    p.add_argument("--max-proforme", type=int, default=1)
    p.add_argument("--hire-backend", required=True,
                   help="calea catre backend/ din checkoutul Hire (sursa adaptorului)")
    p.add_argument("--zona", default=_ZONA_DEFAULT,
                   help="zona proprie permisa; --out trebuie sa fie in interior")
    p.add_argument("--out", default=None,
                   help="director de iesire; implicit <zona>/oblio")
    args = p.parse_args(argv)

    _apeluri_http["n"] = 0
    urllib.request.urlopen = _urlopen_numarat

    out = args.out or os.path.join(args.zona, "oblio")

    # --- refuzuri inainte de trafic ---------------------------------
    if not args.base_url.startswith("https://"):
        return refuza("base-url fara https — nu se trimite nimic")
    if args.max_proforme < 1 or args.max_proforme > _MAX_PROFORME:
        return refuza(f"--max-proforme in afara 1..{_MAX_PROFORME}")
    if not re.fullmatch(r"[A-Z]{3}", args.moneda or ""):
        return refuza("--moneda nu e ISO4217 (3 litere mari)")
    if args.emit:
        if not args.firma_cui:
            return refuza("--emit cere --firma-cui (cif_emitent, firma de test)")
        if not args.client_nume:
            return refuza("--emit cere --client-nume (clientul sintetic)")
    if not in_zona(out, args.zona):
        return refuza("--out in afara zonei proprii (sibling/traversal/symlink)")
    if not os.path.isdir(args.hire_backend):
        return refuza("--hire-backend inexistent")
    if not os.path.isfile(os.path.join(
            args.hire_backend, "hire", "connectors", "serviciu",
            "providers", "oblio.py")):
        return refuza("adaptorul de productie lipseste in --hire-backend")

    nume_secret = f"CONECTOR_SECRET_{args.secret_ref.upper().replace('-', '_')}"
    if not os.environ.get(nume_secret):
        return refuza(f"{nume_secret} neprovisionat — acces lipsa, zero trafic")

    sys.path.insert(0, os.path.abspath(args.hire_backend))
    from hire.connectors.serviciu.providers.oblio import (  # noqa: E402
        AdaptorOblio,
        EroareFurnizor,
    )

    os.makedirs(out, exist_ok=True)
    raport = {"verdict": "PASS", "pasi": [], "emit": args.emit,
              "base_url": args.base_url, "client_id": args.client_id,
              "secret_ref": args.secret_ref, "apeluri_http": 0,
              "proforme_emise": 0}

    def pas(nume, ok, detalii=""):
        raport["pasi"].append({"pas": nume, "ok": bool(ok),
                               "detalii": _curata(detalii)})
        if not ok:
            raport["verdict"] = "FAIL"

    def clasa_eroare(e) -> str:
        """auth | outage | invalid — nu orice eroare e refuz de auth."""
        m = str(e)
        if re.search(r"Oblio (401|403)\b", m):
            return "auth"
        if "inaccesibil" in m or "timeout" in m.lower():
            return "outage"
        return "invalid"

    try:
        adaptor = AdaptorOblio(args.base_url, args.client_id, args.secret_ref)
        docs = adaptor.documente("proba-extern")
        # Adaptorul real intoarce SourceDocument, nu dicturi: fiecare
        # intrare trebuie sa aiba external_id — nu ajunge "lista goala ok".
        pas("autentificare+listare",
            isinstance(docs, list)
            and all(getattr(d, "external_id", None) is not None
                    for d in docs),
            f"{len(docs)} intrari")

        if args.emit:
            refs = []
            for i in range(args.max_proforme):
                # Schema reala a adaptorului: cif_emitent, client.nume,
                # moneda ISO4217 explicita, produse cu nume+pret.
                r = adaptor.executa("oblio.write", {
                    "cif_emitent": args.firma_cui,
                    "client": {"nume": args.client_nume},
                    "moneda": args.moneda,
                    "serie": args.serie,
                    "produse": [{"nume": f"proba-extern-{i+1}",
                                 "pret": "1.00", "cantitate": "1",
                                 "unitate": "buc",
                                 "cota_tva": "Normala"}],
                }, cheie_idempotenta=f"proba-extern-{args.firma_cui}-{i+1}")
                refs.append(r.get("provider_ref"))
                raport["proforme_emise"] += 1
            pas("emitere proforme", all(refs) and len(refs) == args.max_proforme,
                f"{len(refs)} ref-uri: {refs}")
            # Readback EXACT per document emis — niciodata doar ultimul.
            for ref in refs:
                gasit = adaptor.cauta(ref, "proba-extern")
                pas(f"readback {ref}", gasit == "gasit", gasit)

        # refuz: adaptor cu secret gresit — trebuie esec AUTH, nu outage
        os.environ[nume_secret + "_GRESIT"] = "valoare-gresita-deliberat"
        try:
            AdaptorOblio(args.base_url, args.client_id,
                         args.secret_ref + "_GRESIT").documente("proba-extern")
            pas("refuz pe secret gresit", False, "nu a picat")
        except EroareFurnizor as e:
            pas("refuz pe secret gresit", clasa_eroare(e) == "auth",
                f"clasa={clasa_eroare(e)}")
        finally:
            del os.environ[nume_secret + "_GRESIT"]
    except EroareFurnizor as e:
        pas("lant adaptor", False, f"clasa={clasa_eroare(e)} {_curata(e)}")
        raport["verdict"] = "FAIL"
    finally:
        raport["apeluri_http"] = _apeluri_http["n"]

    with open(os.path.join(out, "PROBA-EXTERN-OBLIO.json"), "w") as f:
        json.dump(raport, f, indent=1, ensure_ascii=False)
    print(json.dumps(raport, ensure_ascii=False))
    return 0 if raport["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
