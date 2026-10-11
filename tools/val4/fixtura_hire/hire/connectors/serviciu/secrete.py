"""@requirement HIR-REQ-306
@requirement HIR-REQ-317

SecretRef: aplicația și logurile văd doar *numele* unei referințe;
valoarea stă în afara datelor aplicației (variabile de mediu,
`CONECTOR_SECRET_<REF>`). Un secret lipsește înseamnă refuz — niciodată
fallback la valoare golă sau la simulator.

Regula absolută: `rezolva()` returnează valoarea doar către adaptorul
de furnizor. Niciun handler, test sau jurnal nu o afișează.
"""
from __future__ import annotations

import os


class SecretLipsesc(Exception):
    """Referința e cunoscută, dar valoarea nu e provisionată."""


def rezolva(ref: str) -> str:
    """Citește `CONECTOR_SECRET_<REF>`. Ref e nume de cheie, nu valoare."""
    if not ref or not ref.replace("_", "").replace("-", "").isalnum():
        raise SecretLipsesc(f"referință secret invalidă: {ref!r}")
    nume = f"CONECTOR_SECRET_{ref.upper().replace('-', '_')}"
    valoare = os.environ.get(nume)
    if not valoare:
        raise SecretLipsesc(nume)  # doar numele variabilei, nu valoare
    return valoare
