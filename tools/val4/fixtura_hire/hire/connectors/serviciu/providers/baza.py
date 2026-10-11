"""@requirement HIR-REQ-306

Protocolul furnizorilor. Serviciul vorbește cu furnizorul numai prin
aceste funcții — fiecare adaptor le îndeplinește pe ale lui.

Două funcții contează pentru idempotență:

- `executa` e singura care face efect la furnizor. Nu se apelează pe
  replay și nu se apelează a doua oară la aceeași cheie.
- `cauta` e reconcilierea: după un crash între efect și răspuns,
  serviciul întreabă furnizorul dacă referința există deja — în loc să
  emită a doua factură. Un furnizor fără `cauta` declară limita
  explicit (`NEDETERMINAT`), nu o ascunde.
"""
from __future__ import annotations

from typing import Protocol

from hire.connectors.base import BankStatementLine, SourceDocument


class EroareFurnizor(Exception):
    """Furnizorul a răspuns cu eroare sau nu a răspuns deloc."""


class SecretLipsesc(Exception):
    pass


# Triș-tare reconciliere: un furnizor poate dovedi că referința există,
# poate dovedi că nu există, sau nu poate spune. „Nu pot spune" nu e
# „nu există" — pe el nu se reemite.
NEDETERMINAT = "nedeterminat"


class Furnizor(Protocol):
    nume: str

    def documente(self, tenant_id: str) -> list[SourceDocument]:
        ...

    def extras(self, tenant_id: str) -> list[BankStatementLine]:
        ...

    def executa(self, capabilitate: str, parametri: dict,
                cheie_idempotenta: str, cont) -> dict:
        """Face efectul PE CONTUL `cont` — niciodată pe altul.
        `cont` e ContFurnizor rezolvat din bindingul jetonului.
        Întoarce cel puțin `provider_ref` și `efect`."""
        ...

    def cauta(self, provider_ref: str, tenant_id: str) -> str:
        """gasit | lipsa | nedeterminat — reconcilierea după crash."""
        ...
