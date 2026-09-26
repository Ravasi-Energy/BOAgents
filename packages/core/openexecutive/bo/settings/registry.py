"""Registry of the five BOAgents settings shipped in Valul 1 (BO-SET-001).

Each entry is a typed contract, not just a name: it carries labels/help in
Romanian (the new UI surfaces are Romanian), a validator with explicit bounds,
an apply mode (IMMEDIATE | NEW_RUN — the only two this slice uses), the role
required to edit, and an audit redaction class. ``sensitivity="normal"`` for
all five — none is a secret, so values may appear in audit details.

The 420-key catalog in the coordination package is backlog; only keys
registered here exist at runtime, and the UI states exactly that.
"""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

SettingType = Literal["text", "enum", "integer", "timezone", "boolean"]
ApplyMode = Literal["IMMEDIATE", "NEW_RUN", "RESTART", "MIGRATION"]
Scope = Literal["tenant"]

_SCHEMA_VERSION = "bo.settings.v1"

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


class SettingValidationError(ValueError):
    """Raised when a candidate value fails the key's validation rules."""


def _validate_text(v: Any, *, min_len: int, max_len: int, label: str) -> str:
    if not isinstance(v, str):
        raise SettingValidationError(f"{label}: așteptat text")
    v = v.strip()
    if _CONTROL_CHAR_RE.search(v):
        raise SettingValidationError(f"{label}: caractere de control interzise")
    if not (min_len <= len(v) <= max_len):
        raise SettingValidationError(
            f"{label}: lungimea trebuie să fie între {min_len} și {max_len}"
        )
    return v


def _validate_enum(v: Any, *, allowed: tuple[str, ...], label: str) -> str:
    if not isinstance(v, str) or v not in allowed:
        raise SettingValidationError(
            f"{label}: valoare permisă: {', '.join(allowed)}"
        )
    return v


def _validate_int(v: Any, *, minimum: int, maximum: int, label: str) -> int:
    # bool is a subclass of int — reject it explicitly so `true` isn't 1.
    if isinstance(v, bool) or not isinstance(v, int):
        raise SettingValidationError(f"{label}: așteptat număr întreg")
    if not (minimum <= v <= maximum):
        raise SettingValidationError(
            f"{label}: valoarea trebuie să fie între {minimum} și {maximum}"
        )
    return v


def _validate_bool(v: Any, *, label: str) -> bool:
    if not isinstance(v, bool):
        raise SettingValidationError(f"{label}: așteptat true/false")
    return v


def _validate_trust_store(v: Any) -> str:
    """Text = document bo.package.registry.v1 validat structural la salvare.

    Unlike `_validate_text`, JSON pretty-printing characters `\n` and `\t`
    are legal here — only the remaining control characters are rejected."""
    if not isinstance(v, str):
        raise SettingValidationError("Trust store: așteptat text")
    v = v.strip()
    if len(v) > 262144:
        raise SettingValidationError("Trust store: peste limita de 256 KiB")
    if _CONTROL_CHAR_RE.search(v.replace("\n", "").replace("\t", "")):
        raise SettingValidationError("Trust store: caractere de control interzise")
    if not v:
        return v
    import json

    from openexecutive.bo.packages.errors import PackageReject
    from openexecutive.bo.packages.registry import TrustRegistry

    try:
        doc = json.loads(v)
    except json.JSONDecodeError as exc:
        raise SettingValidationError(f"Trust store: JSON invalid ({exc.msg})") from exc
    try:
        TrustRegistry.from_dict(doc)
    except PackageReject as exc:
        raise SettingValidationError(f"Trust store: {exc}") from exc
    return v


_CSV_ITEM_RE = re.compile(r"^[^@\s,]+$")
_COST_CAP_RE = re.compile(r"^\d+(\.\d{1,6})? [A-Z]{3}$")
_SECRET_REF_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


def _validate_urlish(v: Any, *, label: str) -> str:
    """http(s) base URL or empty — trailing slash stripped."""
    if not isinstance(v, str):
        raise SettingValidationError(f"{label}: așteptat text")
    v = v.strip().rstrip("/")
    if _CONTROL_CHAR_RE.search(v):
        raise SettingValidationError(f"{label}: caractere de control interzise")
    if not v:
        return v
    if len(v) > 512 or not v.startswith(("http://", "https://")) or " " in v:
        raise SettingValidationError(
            f"{label}: așteptat URL http(s) valid, max 512 caractere"
        )
    return v


def _validate_secret_ref(v: Any, *, label: str) -> str:
    """Name of an env var holding the secret — never the secret itself."""
    if not isinstance(v, str):
        raise SettingValidationError(f"{label}: așteptat text")
    v = v.strip()
    if not _SECRET_REF_RE.match(v):
        raise SettingValidationError(
            f"{label}: așteptat nume de variabilă de mediu (ex. BO_GUARDIAN_TOKEN)"
        )
    return v


def _validate_csv(v: Any, *, label: str) -> str:
    """Comma-separated opaque tokens (no spaces/@); empty = unrestricted."""
    if not isinstance(v, str):
        raise SettingValidationError(f"{label}: așteptat text CSV")
    v = v.strip()
    if _CONTROL_CHAR_RE.search(v):
        raise SettingValidationError(f"{label}: caractere de control interzise")
    if len(v) > 1024:
        raise SettingValidationError(f"{label}: peste limita de 1024 caractere")
    items = [p.strip() for p in v.split(",") if p.strip()]
    if len(items) > 64:
        raise SettingValidationError(f"{label}: cel mult 64 de elemente")
    for item in items:
        if not _CSV_ITEM_RE.match(item) or len(item) > 64:
            raise SettingValidationError(
                f"{label}: element invalid {item!r} (fără spații/@/, max 64)"
            )
    return ",".join(items)


def _validate_cost_cap(v: Any) -> str:
    """``"<decimal> <ISO-4217>"`` or empty — a decimal string, never a float."""
    if not isinstance(v, str):
        raise SettingValidationError("Plafonul de cost: așteptat text")
    v = v.strip()
    if not v:
        return v
    if not _COST_CAP_RE.match(v):
        raise SettingValidationError(
            "Plafonul de cost: format „<sumă> <monedă>“, ex. „0.05 USD“"
        )
    return v


def _validate_timezone(v: Any, *, label: str) -> str:
    if not isinstance(v, str) or not v.strip():
        raise SettingValidationError(f"{label}: așteptat un fus IANA")
    v = v.strip()
    if len(v) > 64 or _CONTROL_CHAR_RE.search(v):
        raise SettingValidationError(f"{label}: nume de fus invalid")
    try:
        ZoneInfo(v)
    except (ZoneInfoNotFoundError, ValueError, KeyError) as exc:
        raise SettingValidationError(
            f"{label}: fus necunoscut (folosește un nume IANA, ex. Europe/Bucharest)"
        ) from exc
    return v


@dataclass(frozen=True, slots=True)
class SettingSpec:
    key: str
    type: SettingType
    default: Any
    apply_mode: ApplyMode
    scope: Scope
    page: str
    tab: str
    label_ro: str
    label_en: str
    help_ro: str
    owner_role: str
    edit_role: str
    sensitivity: str
    effect_ro: str
    acceptance_ro: str
    validate: Callable[[Any], Any] = field(compare=False)

    def to_meta(self) -> dict[str, Any]:
        """Public metadata for the settings UI — never includes the value."""
        return {
            "key": self.key,
            "schema_version": _SCHEMA_VERSION,
            "type": self.type,
            "default": self.default,
            "apply_mode": self.apply_mode,
            "scope": self.scope,
            "page": self.page,
            "tab": self.tab,
            "label_ro": self.label_ro,
            "label_en": self.label_en,
            "help_ro": self.help_ro,
            "edit_role": self.edit_role,
            "sensitivity": self.sensitivity,
            "effect_ro": self.effect_ro,
        }


REGISTRY: dict[str, SettingSpec] = {
    "bo.ui.display_name": SettingSpec(
        key="bo.ui.display_name",
        type="text",
        default="BOAgents",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Numele afișat",
        label_en="Display name",
        help_ro="Numele instalației afișat în interfață.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică imediat în interfață după salvare.",
        acceptance_ro="Salvare, reîncărcare și restart păstrează valoarea.",
        validate=lambda v: _validate_text(v, min_len=1, max_len=80, label="Numele afișat"),
    ),
    "bo.ui.language": SettingSpec(
        key="bo.ui.language",
        type="enum",
        default="ro",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Limba interfeței",
        label_en="UI language",
        help_ro="Limba suprafețelor noi ale produsului.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică imediat la suprafețele care respectă setarea.",
        acceptance_ro="Doar ro/en sunt acceptate; alte valori sunt respinse.",
        validate=lambda v: _validate_enum(v, allowed=("ro", "en"), label="Limba interfeței"),
    ),
    "bo.ui.timezone": SettingSpec(
        key="bo.ui.timezone",
        type="timezone",
        default="Europe/Bucharest",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Fus orar",
        label_en="Timezone",
        help_ro="Fus IANA folosit la afișarea orei în suprafețele noi.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică imediat la timestampurile afișate în paginile BO.",
        acceptance_ro="Fusurile invalide sunt respinse cu mesaj explicit.",
        validate=lambda v: _validate_timezone(v, label="Fus orar"),
    ),
    "bo.bobot.simulation.max_steps": SettingSpec(
        key="bo.bobot.simulation.max_steps",
        type="integer",
        default=50,
        apply_mode="NEW_RUN",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Limită de pași per simulare",
        label_en="Simulation step limit",
        help_ro="Numărul maxim de pași evaluați într-o simulare BoBot.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică la simulările pornite după salvare; nu modifică rulările existente.",
        acceptance_ro="O simulare nouă respectă limita; valori în afara 1–500 sunt respinse.",
        validate=lambda v: _validate_int(v, minimum=1, maximum=500, label="Limita de pași"),
    ),
    "bo.bobot.simulation.retention_days": SettingSpec(
        key="bo.bobot.simulation.retention_days",
        type="integer",
        default=30,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Retenție istoric simulări (zile)",
        label_en="Simulation history retention (days)",
        help_ro="Cât se păstrează rulările de simulare. Nu atinge auditul.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="La următoarea curățare se șterg doar rulările de simulare mai vechi decât pragul.",
        acceptance_ro="Sweep-ul șterge doar rulări de simulare expirate; auditul rămâne neatins.",
        validate=lambda v: _validate_int(v, minimum=1, maximum=3650, label="Retenția"),
    ),
    "bo.packages.enabled": SettingSpec(
        key="bo.packages.enabled",
        type="boolean",
        default=False,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Import pachete semnate",
        label_en="Signed package import",
        help_ro="Permite importul de pachete bo.package.v1 în carantină. Oprit implicit.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Pornit: POST /bo/packages/import acceptă pachete; oprit: orice import e respins.",
        acceptance_ro="Oprit implicit; importul refuză cu PACKAGES_DISABLED când e oprit.",
        validate=lambda v: _validate_bool(v, label="Import pachete"),
    ),
    "bo.packages.max_package_bytes": SettingSpec(
        key="bo.packages.max_package_bytes",
        type="integer",
        default=8 * 1024 * 1024,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Dimensiune maximă pachet (octeți)",
        label_en="Max package size (bytes)",
        help_ro="Plafon suplimentar peste cel din trust store; verificarea folosește minimul.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică la următorul import; nu redeschide verdictul existent.",
        acceptance_ro="Pachetul peste limită este respins cu PACKAGE_TOO_LARGE înainte/după citire parțială.",
        validate=lambda v: _validate_int(
            v, minimum=65536, maximum=268435456, label="Dimensiunea maximă"),
    ),
    "bo.packages.trust_store_json": SettingSpec(
        key="bo.packages.trust_store_json",
        type="text",
        default="",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Trust store pachete (JSON)",
        label_en="Package trust store (JSON)",
        help_ro="Document bo.package.registry.v1 cu emitenți, chei și politică. Gol = niciun pachet nu verifică.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică la următorul import; părțile revocate nu mai verifică.",
        acceptance_ro="JSON invalid sau registru malformat este respins la salvare.",
        validate=lambda v: _validate_trust_store(v),
    ),
    "bo.packages.rollback_requires_approval": SettingSpec(
        key="bo.packages.rollback_requires_approval",
        type="boolean",
        default=True,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="general",
        label_ro="Downgrade doar cu aprobare",
        label_en="Rollback requires approval",
        help_ro="Reinstalarea unei versiuni <= cea importată cere aprobare legată de digest.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Aplicat la fiecare import; dezactivarea permite downgrade fără aprobare.",
        acceptance_ro="Fără aprobare activă legată de tenant/digest/versiuni → ROLLBACK_UNAUTHORIZED.",
        validate=lambda v: _validate_bool(v, label="Aprobare downgrade"),
    ),
    "bo.router.observe_enabled": SettingSpec(
        key="bo.router.observe_enabled",
        type="boolean",
        default=False,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="routing",
        label_ro="Observare rutare (fără efect)",
        label_en="Routing observe mode",
        help_ro="Calculează și înregistrează ce model ar recomanda catalogul la fiecare apel real. Nu schimbă modelul folosit.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Pornit: fiecare apel de model produce o observație persistată; oprit: zero scrieri.",
        acceptance_ro="Oprit implicit; cu el oprit nu se scrie nicio observație și ruta reală nu se schimbă niciodată.",
        validate=lambda v: _validate_bool(v, label="Observare rutare"),
    ),
    "bo.router.allowed_providers": SettingSpec(
        key="bo.router.allowed_providers",
        type="text",
        default="",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="routing",
        label_ro="Provideri permiși (CSV)",
        label_en="Allowed providers (CSV)",
        help_ro="Listă CSV de provideri eligibili pentru recomandare. Gol = fără restricție.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică la următoarea observație; candidații în afara listei → PROVIDER_DENIED.",
        acceptance_ro="CSV invalid sau cu spații/@ este respins la salvare.",
        validate=lambda v: _validate_csv(v, label="Provideri permiși"),
    ),
    "bo.router.allowed_regions": SettingSpec(
        key="bo.router.allowed_regions",
        type="text",
        default="",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="routing",
        label_ro="Regiuni permise (CSV)",
        label_en="Allowed regions (CSV)",
        help_ro="Listă CSV de regiuni acceptate pentru date. Gol = fără restricție.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Candidatul fără regiune comună cu lista → REGION_DENIED.",
        acceptance_ro="CSV invalid este respins la salvare.",
        validate=lambda v: _validate_csv(v, label="Regiuni permise"),
    ),
    "bo.router.required_capabilities": SettingSpec(
        key="bo.router.required_capabilities",
        type="text",
        default="",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="routing",
        label_ro="Capabilități cerute (CSV)",
        label_en="Required capabilities (CSV)",
        help_ro="Capabilități pe care orice candidat trebuie să le declare. Gol = niciuna.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Candidatul care nu acoperă lista → CAPABILITY_MISSING.",
        acceptance_ro="CSV invalid este respins la salvare.",
        validate=lambda v: _validate_csv(v, label="Capabilități cerute"),
    ),
    "bo.router.min_quality": SettingSpec(
        key="bo.router.min_quality",
        type="integer",
        default=60,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="routing",
        label_ro="Prag minim de calitate (%)",
        label_en="Minimum quality bar (%)",
        help_ro="Scorul de calitate [0–100] sub care candidatul nu poate fi recomandat.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Sub prag → metBar=false, decizia observată este REFUSE (QUALITY_BAR_UNMET).",
        acceptance_ro="Valori în afara 0–100 sunt respinse; pragul nu blochează apelul real.",
        validate=lambda v: _validate_int(v, minimum=0, maximum=100, label="Pragul de calitate"),
    ),
    "bo.router.eval_max_age_days": SettingSpec(
        key="bo.router.eval_max_age_days",
        type="integer",
        default=90,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="routing",
        label_ro="Vârsta maximă a evaluării (zile)",
        label_en="Max evaluation age (days)",
        help_ro="Peste această vârstă dovada de evaluare e considerată expirată.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Dovadă mai veche decât pragul → STALE_EVALUATION, candidat eliminat.",
        acceptance_ro="Valori în afara 1–3650 sunt respinse.",
        validate=lambda v: _validate_int(
            v, minimum=1, maximum=3650, label="Vârsta maximă a evaluării"
        ),
    ),
    "bo.router.max_estimated_cost": SettingSpec(
        key="bo.router.max_estimated_cost",
        type="text",
        default="",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="routing",
        label_ro="Plafon cost estimat per apel",
        label_en="Max estimated cost per call",
        help_ro="Format „<sumă> <monedă>“, ex. „0.05 USD“. Gol = fără plafon. Estimare, nu debitare.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Estimarea peste plafon → BUDGET_EXCEEDED; lipsa datelor de cost → COST_DATA_MISSING.",
        acceptance_ro="Format invalid este respins la salvare; plafonul nu debită și nu rezervă buget.",
        validate=lambda v: _validate_cost_cap(v),
    ),
    "bo.router.observation_retention_days": SettingSpec(
        key="bo.router.observation_retention_days",
        type="integer",
        default=90,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="routing",
        label_ro="Retenție observații (zile)",
        label_en="Observation retention (days)",
        help_ro="Cât se păstrează observațiile de rutare. Nu atinge auditul.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="La următoarea curățare se șterg doar observațiile mai vechi decât pragul.",
        acceptance_ro="Sweep-ul șterge doar observații expirate; auditul rămâne neatins.",
        validate=lambda v: _validate_int(
            v, minimum=1, maximum=3650, label="Retenția observațiilor"
        ),
    ),
    "bo.router.delivery_interval_s": SettingSpec(
        key="bo.router.delivery_interval_s",
        type="integer",
        default=30,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="routing",
        label_ro="Interval livrare telemetrie (s)",
        label_en="Telemetry delivery interval (s)",
        help_ro="Cât des golește workerul coada de livrare. 0 = doar flush manual; I/O Guardian nu ajunge niciodată în hookul de observare.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="La următorul tick al workerului; observațiile rămân persistate indiferent.",
        acceptance_ro="0 dezactivează workerul; flush-ul manual rămâne funcțional.",
        validate=lambda v: _validate_int(
            v, minimum=0, maximum=3600, label="Intervalul de livrare"
        ),
    ),
    "bo.router.delivery_batch_size": SettingSpec(
        key="bo.router.delivery_batch_size",
        type="integer",
        default=50,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="routing",
        label_ro="Livrat per ciclu (evenimente)",
        label_en="Delivery batch size",
        help_ro="Câte plicuri se trimit într-un ciclu de livrare. Mărginește burst-ul spre Guardian.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Ciclurile următoare procesează cel mult această valoare.",
        acceptance_ro="Limita se aplică la fiecare ciclu worker/flush.",
        validate=lambda v: _validate_int(
            v, minimum=1, maximum=500, label="Batch-ul de livrare"
        ),
    ),
    "bo.router.delivery_max_attempts": SettingSpec(
        key="bo.router.delivery_max_attempts",
        type="integer",
        default=25,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="routing",
        label_ro="Tentative maxime de livrare",
        label_en="Max delivery attempts",
        help_ro="Peste acest prag plicul e marcat eșuat definitiv (vizibil în stare), nu abandonat tăcut.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Plicurile peste prag devin eșuate definitive la următorul ciclu.",
        acceptance_ro="Eșuările definitive apar în status/outbox, nu se pierd tăcut.",
        validate=lambda v: _validate_int(
            v, minimum=1, maximum=1000, label="Tentativele maxime"
        ),
    ),
    "bo.exec.enabled": SettingSpec(
        key="bo.exec.enabled",
        type="boolean",
        default=False,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Execuție delegată",
        label_en="Delegated execution",
        help_ro="Permite trimiterea și rularea execuțiilor delegate. Oprit implicit — niciun mandat nu produce efecte.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Pornit: acceptă rulări. Oprit: refuză submit, nu preia rulări și oprește efectele noi la frontieră; efectele deja trimise nu sunt anulate.",
        acceptance_ro="Oprit implicit; submit și claim sunt blocate; rulările în curs intră în pauză înaintea următorului efect.",
        validate=lambda v: _validate_bool(v, label="Execuție delegată"),
    ),
    "bo.exec.max_delegation_depth": SettingSpec(
        key="bo.exec.max_delegation_depth",
        type="integer",
        default=3,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Adâncime maximă de delegare",
        label_en="Max delegation depth",
        help_ro="Câte niveluri de mandate copil pot exista sub o rădăcină. Copilul nu depășește niciodată părintele.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Crearea unui mandat peste adâncime este refuzată la salvare/execuție.",
        acceptance_ro="Valori în afara 0–8 sunt respinse; mandatele peste adâncime nu se creează.",
        validate=lambda v: _validate_int(
            v, minimum=0, maximum=8, label="Adâncimea maximă"
        ),
    ),
    "bo.exec.max_steps": SettingSpec(
        key="bo.exec.max_steps",
        type="integer",
        default=50,
        apply_mode="NEW_RUN",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Limită de pași per execuție",
        label_en="Steps per execution",
        help_ro="Plafon administrat al pașilor unei rulări — se intersectează cu max_steps din mandat.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Rulările noi nu pot depăși minimul dintre mandat și această valoare.",
        acceptance_ro="Valori în afara 1–200 sunt respinse.",
        validate=lambda v: _validate_int(
            v, minimum=1, maximum=200, label="Limita de pași"
        ),
    ),
    "bo.exec.default_concurrency": SettingSpec(
        key="bo.exec.default_concurrency",
        type="integer",
        default=2,
        apply_mode="NEW_RUN",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Concurență implicită per rulare",
        label_en="Default run concurrency",
        help_ro="Sloturile de concurență rezervate implicit per rulare, plafonate de mandat.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Rezervarea atomică folosește minimul dintre această valoare și mandat.",
        acceptance_ro="Valori în afara 1–16 sunt respinse.",
        validate=lambda v: _validate_int(
            v, minimum=1, maximum=16, label="Concurența implicită"
        ),
    ),
    "bo.exec.lease_seconds": SettingSpec(
        key="bo.exec.lease_seconds",
        type="integer",
        default=60,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Durata lease-ului (s)",
        label_en="Lease duration (s)",
        help_ro="Cât deține un worker o rulare/o intrare de ledger. La expirare alt worker poate prelua — fencing-ul oprește finalizarea stale.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică la următoarele claim-uri; lease-urile emise rămân valide până expiră.",
        acceptance_ro="Valori în afara 5–600 sunt respinse.",
        validate=lambda v: _validate_int(
            v, minimum=5, maximum=600, label="Durata lease-ului"
        ),
    ),
    "bo.exec.max_effect_attempts": SettingSpec(
        key="bo.exec.max_effect_attempts",
        type="integer",
        default=3,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Tentative maxime per efect",
        label_en="Max effect attempts",
        help_ro="Câte claim-uri poate lua o intrare de ledger înainte de reconciliere obligatorie.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Peste prag, intrarea nu mai e revendicabilă automat — cere reconciliere.",
        acceptance_ro="Valori în afara 1–10 sunt respinse.",
        validate=lambda v: _validate_int(
            v, minimum=1, maximum=10, label="Tentativele maxime"
        ),
    ),
    "bo.exec.checkpoint_required": SettingSpec(
        key="bo.exec.checkpoint_required",
        type="boolean",
        default=True,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Checkpoint obligatoriu",
        label_en="Mandatory checkpoint",
        help_ro="Pasul dependent nu rulează fără checkpoint persistat. Eșecul de scriere blochează efectul, nu îl execută orbeste.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Pornit (implicit): fiecare pas scrie checkpoint pre/post; eșecul de persistare oprește rularea.",
        acceptance_ro="Cu checkpoint-ul oprit, rularea continuă fără stare de reluare — documentat în UI.",
        validate=lambda v: _validate_bool(v, label="Checkpoint obligatoriu"),
    ),
    "bo.exec.retry_backoff_s": SettingSpec(
        key="bo.exec.retry_backoff_s",
        type="integer",
        default=0,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Backoff minim la reîncercare (s)",
        label_en="Min retry backoff (s)",
        help_ro="Delay minim după expirarea lease-ului înainte ca o intrare SUBMITTED/UNKNOWN să poată fi revendicată. Lease-ul e deja un delay; backoff-ul e plafonul suplimentar.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Se aplică la următoarele claim-uri de ledger; 0 = doar durata lease-ului separă tentativele.",
        acceptance_ro="Valori în afara 0–300 sunt respinse; retry-ul nu poate fi mai dens decât lease+backoff.",
        validate=lambda v: _validate_int(
            v, minimum=0, maximum=300, label="Backoff-ul la reîncercare"
        ),
    ),
    "bo.exec.budget_cap": SettingSpec(
        key="bo.exec.budget_cap",
        type="text",
        default="",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Plafon de buget per rulare",
        label_en="Per-run budget cap",
        help_ro="Plafon tenant pentru budget_amount la trimitere — format „<sumă> <monedă>“, ex. „100.00 USD“. Gol = fără plafon peste cel al mandatului.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Setat: POST /bo/execution/runs respinge orice budget_amount peste plafon, indiferent de mandat.",
        acceptance_ro="Format invalid → 422 la salvare; depășire → trimitere refuzată cu motiv explicit.",
        validate=_validate_cost_cap,
    ),
    "bo.exec.retention_days": SettingSpec(
        key="bo.exec.retention_days",
        type="integer",
        default=90,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Retenție execuții (zile)",
        label_en="Execution retention (days)",
        help_ro="Cât se păstrează rulările, checkpointurile și ledgerul. Nu atinge auditul.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="La următoarea curățare se șterg doar execuțiile finalizate mai vechi decât pragul.",
        acceptance_ro="Sweep-ul nu atinge rulări active sau ledgerul nefinalizat.",
        validate=lambda v: _validate_int(
            v, minimum=0, maximum=3650, label="Retenția execuțiilor"
        ),
    ),
    "bo.exec.guardian_endpoint": SettingSpec(
        key="bo.exec.guardian_endpoint",
        type="text",
        default="",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Endpoint Guardian (autorizare + evenimente)",
        label_en="Guardian endpoint (authorization + events)",
        help_ro="URL de bază Guardian pentru statusul mandatului și livrarea evenimentelor. Gol: se încearcă BO_TELEMETRY_ENDPOINT; fără niciun endpoint, mandatele legate sau supravegherea obligatorie opresc efectele.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Setat: fiecare frontieră de efect reverifică starea mandatului în Guardian; outbox-ul livrează către /v1/execution-events.",
        acceptance_ro="Standalone este permis doar pentru mandate nelegate și autorizare opțională. Un mandat legat nu devine standalone când endpointul lipsește.",
        validate=lambda v: _validate_urlish(v, label="Endpoint Guardian"),
    ),
    "bo.exec.guardian_secret_ref": SettingSpec(
        key="bo.exec.guardian_secret_ref",
        type="text",
        default="BO_GUARDIAN_TOKEN",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Referință secret Guardian",
        label_en="Guardian secret reference",
        help_ro="Numele variabilei de mediu care ține tokenul Bearer către Guardian — SecretRef, niciodată valoarea.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Clientul Guardian citește tokenul din variabila de mediu numită aici; lipsa ei e tratată ca eroare de configurare.",
        acceptance_ro="Doar numele variabilei e persistat; tokenul se trimite numai în antetul Bearer către Guardian, nu în DB, cereri de Setări sau loguri.",
        validate=lambda v: _validate_secret_ref(v, label="Referința de secret"),
    ),
    "bo.exec.guardian_policy_secret_ref": SettingSpec(
        key="bo.exec.guardian_policy_secret_ref",
        type="text",
        default="BO_GUARDIAN_POLICY_TOKEN",
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Referință secret politică Guardian",
        label_en="Guardian policy secret reference",
        help_ro="Numele variabilei de mediu cu un credențial execpolicy:read — activează o citire suplimentară a politicii. Verdictul efectiv al mandatului include deja politica curentă.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Prezent: verifică suplimentar acțiunea/resursa în politica curentă. Lipsă: se păstrează verdictul efectiv Guardian, care blochează politici revocate sau mandate în afara politicii.",
        acceptance_ro="Credențialul din env trebuie să aibă execpolicy:read; altfel efectul se oprește (unavailable), nu continuă.",
        validate=lambda v: _validate_secret_ref(v, label="Referința de secret politică"),
    ),
    "bo.exec.guardian_timeout_s": SettingSpec(
        key="bo.exec.guardian_timeout_s",
        type="integer",
        default=5,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Timeout cereri Guardian (s)",
        label_en="Guardian request timeout (s)",
        help_ro="Timeout pe cererea de autorizare/livrare către Guardian.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Depășirea timeoutului la autorizare blochează efectul când legătura e obligatorie.",
        acceptance_ro="Între 1 și 30 secunde.",
        validate=lambda v: _validate_int(
            v, minimum=1, maximum=30, label="Timeout Guardian"
        ),
    ),
    "bo.exec.guardian_auth_required": SettingSpec(
        key="bo.exec.guardian_auth_required",
        type="boolean",
        default=False,
        apply_mode="IMMEDIATE",
        scope="tenant",
        page="setari",
        tab="exec",
        label_ro="Autorizare Guardian obligatorie",
        label_en="Guardian authorization required",
        help_ro="Pornit: și mandatele NELEGATE (fără guardian_ref) sunt respinse la frontieră. Pentru mandatele legate verificarea e obligatorie mereu — această setare nu le poate face standalone.",
        owner_role="admin",
        edit_role="admin",
        sensitivity="normal",
        effect_ro="Pornit: mandat fără guardian_ref → deny. Indisponibilitatea Guardian → pauză recuperabilă pentru ORICE mandat legat, indiferent de această setare.",
        acceptance_ro="Fereastra dintre verificare și efect e minimă, dar nenulă — cursele distribuite nu sunt eliminate.",
        validate=lambda v: _validate_bool(v, label="Autorizarea Guardian obligatorie"),
    ),
}


def _pilot_specs() -> None:
    from openexecutive.bo.pilot.config import allowlist, endpoint, secret_ref

    entries = [
        ("enabled", "boolean", False, "Pilot ERP sintetic", "Activare globală explicită; nu pornește un scheduler.", lambda v: _validate_bool(v, label="Pilot")),
        ("profile", "enum", "disabled", "Profil pilot", "Loopback este permis numai în profilul synthetic-loopback.", lambda v: _validate_enum(v, allowed=("disabled", "synthetic-loopback"), label="Profil")),
        ("endpoint", "text", "", "Endpoint ERP sintetic", "Adresă exactă prezentă în allowlist; fără DNS, proxy sau redirect.", endpoint),
        ("allowlist", "text", "[]", "Allowlist ERP sintetic", "Listă JSON administrată de endpointuri loopback explicite.", allowlist),
        ("secret_ref", "text", "BO_PILOT_SERVICE_TOKEN", "Referință secret ERP sintetic", "Numele variabilei server-only; credentialul serviciului fixează tenantul.", secret_ref),
        ("timeout_s", "integer", 3, "Timeout ERP sintetic (s)", "Timeout de transport; lipsa răspunsului nu dovedește eșecul efectului.", lambda v: _validate_int(v, minimum=1, maximum=10, label="Timeout")),
        ("stale_s", "integer", 60, "Prag stale pilot (s)", "Vârsta dovezii; fără dovadă starea este UNKNOWN.", lambda v: _validate_int(v, minimum=5, maximum=3600, label="Prag stale")),
        ("max_queue", "integer", 100, "Limită coadă pilot", "Peste limită diagnosticul este DEGRADED, fără remediere automată.", lambda v: _validate_int(v, minimum=0, maximum=10000, label="Coada")),
        ("supervision", "enum", "standalone", "Supraveghere pilot", "required cere mandat Guardian; mandatele legate rămân obligatorii și în standalone.", lambda v: _validate_enum(v, allowed=("standalone", "required"), label="Supraveghere")),
    ]
    for suffix, typ, default, label, help_text, validator in entries:
        key = f"bo.pilot.{suffix}"
        REGISTRY[key] = SettingSpec(
            key=key, type=typ, default=default, apply_mode="IMMEDIATE",
            scope="tenant", page="setari", tab="pilot", label_ro=label,
            label_en=label, help_ro=help_text, owner_role="admin", edit_role="admin",
            sensitivity="normal", effect_ro="Reverificat înainte de apel; schimbarea configurației cere o rulare nouă. Dovezile vechi se păstrează.",
            acceptance_ro="CAS/RBAC/audit; refuz sigur fără configurație explicită.", validate=validator,
        )


_pilot_specs()


def validate_value(key: str, value: Any) -> Any:
    spec = REGISTRY.get(key)
    if spec is None:
        raise SettingValidationError(f"Setare necunoscută: {key}")
    return spec.validate(value)
