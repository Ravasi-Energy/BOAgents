"""@requirement HIR-REQ-306
@requirement HIR-REQ-138
@requirement HIR-REQ-139

Adaptorul Oblio — traduce forma documentată (docs externe: API Oblio
v1, `authorize/token` + `docs/invoice`) în limbajul furnizorului.

Decizii scrise:

- **Client credentials**: `POST {base}/api/authorize/token` cu
  `client_id` + `client_secret` → `access_token` (Bearer). Secretul vine
  prin SecretRef, nu prin parametri de cerere.
- **Emitere proformă/factură**: `POST {base}/api/docs/proforma` —
  endpoint real documentat; factura fiscală (`docs/invoice`) rămâne
  în afara scope-ului F5 până la un cont autorizat, caz în care se
  schimbă doar `workstation`/`document` din corp.
- **Extrase bancare**: Oblio nu are API de extrase — `extras()` ridică
  EroareFurnizor explicit, nu întoarce listă goală (HIR-REQ-139: liniile
  de bancă vin din fișier importat, nu din Oblio).
- **Reconciliere**: `cauta` interoghează `docs` pe seria+numărul
  furnizorului. Dacă endpointul nu e disponibil în mediul autorizat,
  răspunsul e `nedeterminat` — serviciul nu reemite pe el.
- **Bani**: `price`/`total` se trimit ca numere cu 2 zecimale în forma
  Oblio doar după validarea `Decimal` pe forma noastră (text). Niciodată
  `float` în sens invers: răspunsurile Oblio se citesc cu
  `parse_float=Decimal`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from decimal import Decimal

from hire.connectors.base import (
    BankStatementLine, ConnectorError, SourceDocument,
)
from hire.connectors.serviciu.providers.baza import (
    NEDETERMINAT, EroareFurnizor,
)
from hire.connectors.serviciu.secrete import SecretLipsesc, rezolva

CAPABILITATI = {
    "oblio.write": {"document": "proforma"},
}


def _json_http(metoda: str, url: str, corp: dict | None,
               antete: dict, timeout: float = 20.0) -> dict:
    """Un singur loc HTTP: răspunsurile se parsează cu Decimal, nu float."""
    date = json.dumps(corp, ensure_ascii=False).encode("utf-8") if corp is not None else None
    req = urllib.request.Request(url, data=date, method=metoda)
    for k, v in antete.items():
        req.add_header(k, v)
    if date is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as rasp:
            text = rasp.read().decode("utf-8")
            return {"status": rasp.status, "corp": json.loads(text, parse_float=Decimal)}
    except urllib.error.HTTPError as e:
        try:
            detalii = e.read().decode("utf-8")[:400]
        except Exception:
            detalii = ""
        raise EroareFurnizor(f"Oblio {e.code}: {detalii}") from e
    except urllib.error.URLError as e:
        raise EroareFurnizor(f"Oblio inaccesibil: {e.reason}") from e


class AdaptorOblio:
    """Oblio prin contractul serviciului-conector."""

    nume = "oblio"

    def __init__(self, base_url: str, client_id: str, secret_ref: str):
        if not base_url:
            raise ConnectorError("AdaptorOblio: lipsește adresa de bază")
        if not client_id:
            raise ConnectorError("AdaptorOblio: lipsește client_id")
        self._base = base_url.rstrip("/")
        self._client_id = client_id
        self._secret_ref = secret_ref
        self._token: str | None = None

    # -- autentificare ------------------------------------------------

    def _autentifica(self) -> str:
        if self._token:
            return self._token
        secret = rezolva(self._secret_ref)
        corp = {"client_id": self._client_id, "client_secret": secret}
        rasp = _json_http("POST", f"{self._base}/api/authorize/token", corp, {})
        token = (rasp.get("corp") or {}).get("access_token")
        if not token or not isinstance(token, str):
            raise EroareFurnizor("Oblio: răspuns de token fără access_token")
        self._token = token
        return token

    def _antete(self) -> dict:
        return {"Authorization": f"Bearer {self._autentifica()}"}

    # -- citire -------------------------------------------------------

    def documente(self, tenant_id: str) -> list[SourceDocument]:
        # Lista documentelor emise — pentru citirea de intrări Oblio are
        # `nomenclatures`; facturile primite vin pe calea de scan/e-Factura.
        rasp = _json_http("GET", f"{self._base}/api/docs/proforma", None,
                          self._antete())
        date = rasp.get("corp") or {}
        liste = date.get("data") if isinstance(date, dict) else None
        out = []
        for d in liste or []:
            out.append(SourceDocument(
                external_id=str(d.get("id") or d.get("number") or ""),
                kind="ubl", content=json.dumps(d, ensure_ascii=False, default=str),
                trust="trusted", fetched_from="oblio"))
        return out

    def extras(self, tenant_id: str) -> list[BankStatementLine]:
        raise EroareFurnizor(
            "Oblio nu expune extrase bancare; liniile vin din fișier "
            "importat (HIR-REQ-139)")

    # -- scriere ------------------------------------------------------

    def executa(self, capabilitate: str, parametri: dict,
                cheie_idempotenta: str, cont=None) -> dict:
        if capabilitate not in CAPABILITATI:
            raise EroareFurnizor(f"capabilitate necunoscută: {capabilitate}")
        # Instanța e construită pe contul rezolvat din binding — `cont` e
        # contextul de audit; un cont de alt furnizor nu ajunge aici.
        if cont is not None and getattr(cont, "furnizor", "oblio") != "oblio":
            raise EroareFurnizor(
                f"cont {getattr(cont, 'ref', '?')} nu e Oblio — refuzat")
        corp = self._corp_proforma(parametri)
        # Oblio nu are câmp nativ de idempotență; cheia merge în `idempotency`
        # (câmp acceptat ca referință externă pe obiectul document) — dacă
        # furnizorul o ignoră, dublurarea se prinde tot din store-ul nostru,
        # iar reconcilierea `cauta` acoperă crash-ul între efect și răspuns.
        corp["idempotency"] = cheie_idempotenta
        rasp = _json_http("POST", f"{self._base}/api/docs/proforma", corp,
                          self._antete())
        date = rasp.get("corp") or {}
        if not isinstance(date, dict):
            raise EroareFurnizor("Oblio: răspuns de emitere fără obiect")
        serie = date.get("seriesName") or date.get("series_name") or ""
        numar = date.get("number") or ""
        ref = f"{serie}-{numar}".strip("-") or str(date.get("id") or "")
        if not ref:
            raise EroareFurnizor("Oblio: document emis fără referință")
        return {"provider_ref": ref, "efect": "emis",
                "detalii": {"link": date.get("link")}}

    def cauta(self, provider_ref: str, tenant_id: str) -> str:
        try:
            rasp = _json_http("GET", f"{self._base}/api/docs/proforma", None,
                              self._antete())
        except EroareFurnizor:
            return NEDETERMINAT
        date = rasp.get("corp") or {}
        liste = date.get("data") if isinstance(date, dict) else None
        for d in liste or []:
            serie = d.get("seriesName") or ""
            numar = d.get("number") or ""
            if f"{serie}-{numar}" == provider_ref:
                return "gasit"
        return "lipsa"

    # -- traducerea formei documentate --------------------------------

    def _corp_proforma(self, p: dict) -> dict:
        """Forma comună → forma Oblio. Validarea sumelor e aici, o dată."""
        def suma(x, camp):
            try:
                d = Decimal(str(x))
            except Exception:
                raise EroareFurnizor(f"{camp}: sumă invalidă")
            if d.as_tuple().exponent < -2:
                raise EroareFurnizor(f"{camp}: mai mult de 2 zecimale")
            return str(d.quantize(Decimal("0.01")))

        produse = []
        for i, prod in enumerate(p.get("produse") or []):
            try:
                pret = suma(prod["pret"], f"produse[{i}].pret")
            except KeyError as e:
                raise EroareFurnizor(f"produs fără {e}")
            produse.append({
                "name": prod["nume"],
                "productType": "Serviciu",
                "measuringUnit": prod.get("unitate", "buc"),
                "quantity": str(Decimal(str(prod.get("cantitate", "1")))),
                "price": pret,
                "currency": p.get("moneda", "RON"),
                "vatName": prod.get("cota_tva", "Normala"),
                "vatIncluded": 0,
            })
        if not produse:
            raise EroareFurnizor("cerere fără produse")
        return {
            "cif": p["cif_emitent"],
            "client": {
                "name": p["client"]["nume"],
                "cif": p["client"].get("cif", ""),
                "email": p["client"].get("email", ""),
                "address": p["client"].get("adresa", ""),
            },
            "seriesName": p.get("serie", "HIRE"),
            "document": "proforma",
            "products": produse,
            "currency": p.get("moneda", "RON"),
            "language": "RO",
            "precision": 2,
        }
