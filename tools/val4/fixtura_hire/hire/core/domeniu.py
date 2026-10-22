# @requirement HIR-REQ-138
"""Domeniul functional activ: Oblio e eliminat prin decizia lui Stefan
(19.10.2026). Oglinda backend a lui
HIRE-handoff/hire-app/src/data/functional-scope.ts — aceeasi regula pe
nume (actiune sau furnizor), acelasi motiv, pentru ca frontendul se
poate ocoli.

Regula: nicio emitere, configurare sau proba noua la furnizorul Oblio.
Ce nu inseamna: stergerea istoricului — randurile si raspunsurile
pastrate raman citibile, iar anularea interna a unui item Oblio ramane
permisa (anuleaza nu atinge furnizorul).
"""
from __future__ import annotations

import re

MOTIV_OBLIO_EXCLUS = (
    "Oblio: ELIMINAT DIN DOMENIU PRIN DECIZIA ȘTEFAN (19.10.2026). "
    "Istoricul se păstrează."
)

# Aceeasi forma ca in functional-scope.ts: `oblio`, `Oblio`,
# `oblio.write`, `connector:oblio.write`, `oblio.ro` — dar nu
# `oblious` sau `oblio-x-ca-nume-de-altceva` dupa alt separator.
_OBLIO = re.compile(r"^(?:connector:)?oblio(?:[.:/_-]|$)", re.IGNORECASE)


def este_oblio(valoare) -> bool:
    """Adevarat pentru actiuni/furnizori din spatiul Oblio, in orice forma."""
    return isinstance(valoare, str) and bool(_OBLIO.match(valoare.strip()))


def recuperare_in_domeniu(action_kind, comanda: str) -> bool:
    """Pe un item Oblio ramane numai anularea interna: fara reluare,
    fara rezolvare, fara proba noua la furnizor."""
    return not este_oblio(action_kind) or comanda == "anuleaza"
