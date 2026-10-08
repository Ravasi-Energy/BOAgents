from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat
import threading
import time
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

# ISO-4217 alphabetic code (e.g. USD, EUR, RON). Money amounts carry their
# currency explicitly — None means "not recorded", never an implied USD.
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")

# Active ISO-4217 alphabetic codes (incl. funds/precious-metal X-codes —
# the same list the UI gets from ``Intl.supportedValuesOf("currency")``).
# Shape alone ("^[A-Z]{3}$") is not enough: ZZZ passes the regex but is
# not a real currency, and money must never carry an invented currency
# (F3/D07).
ISO_4217_CODES = frozenset([
    "AED", "AFN", "ALL", "AMD", "ANG", "AOA", "ARS", "AUD", "AWG", "AZN",
    "BAM", "BBD", "BDT", "BGN", "BHD", "BIF", "BMD", "BND", "BOB", "BOV",
    "BRL", "BSD", "BTN", "BWP", "BYN", "BZD",
    "CAD", "CDF", "CHE", "CHF", "CHW", "CLF", "CLP", "CNY", "COP", "COU",
    "CRC", "CUC", "CUP", "CVE", "CZK",
    "DJF", "DKK", "DOP", "DZD",
    "EGP", "ERN", "ETB", "EUR",
    "FJD", "FKP",
    "GBP", "GEL", "GHS", "GIP", "GMD", "GNF", "GTQ", "GYD",
    "HKD", "HNL", "HTG", "HUF",
    "IDR", "ILS", "INR", "IQD", "IRR", "ISK",
    "JMD", "JOD", "JPY",
    "KES", "KGS", "KHR", "KMF", "KPW", "KRW", "KWD", "KYD", "KZT",
    "LAK", "LBP", "LKR", "LRD", "LSL", "LYD",
    "MAD", "MDL", "MGA", "MKD", "MMK", "MNT", "MOP", "MRU", "MUR", "MVR",
    "MWK", "MXN", "MXV", "MYR", "MZN",
    "NAD", "NGN", "NIO", "NOK", "NPR", "NZD",
    "OMR",
    "PAB", "PEN", "PGK", "PHP", "PKR", "PLN", "PYG",
    "QAR",
    "RON", "RSD", "RUB", "RWF",
    "SAR", "SBD", "SCR", "SDG", "SEK", "SGD", "SHP", "SLE", "SLL", "SOS",
    "SRD", "SSP", "STN", "SVC", "SYP", "SZL",
    "THB", "TJS", "TMT", "TND", "TOP", "TRY", "TTD", "TWD", "TZS",
    "UAH", "UGX", "USD", "USN", "UYI", "UYU", "UYW", "UZS",
    "VED", "VES", "VND", "VUV",
    "WST",
    "XAF", "XAG", "XAU", "XBA", "XBB", "XBC", "XBD", "XCD", "XDR", "XOF",
    "XPD", "XPF", "XPT", "XSU", "XTS", "XUA", "XXX",
    "YER",
    "ZAR", "ZMW", "ZWL",
])


# ---------------------------------------------------------------------------
# Cross-process write fence (CONTROL R14 — PROFILE-CAS-RACE)
#
# profile.yaml is shared state mutated by several writers (PATCH route,
# onboarding commit, fixture load, client-slot restore, CLI). The CAS
# contract is only honest if read→check→merge→write is one critical
# section ACROSS PROCESSES — a thread lock or a check-before-save would
# still let two writers both pass CAS on v1 and both write v2 (the R14
# probe). The fence is a kernel lock on a sidecar file, so it lives on
# the filesystem next to the profile and is held by the OS — a crashed
# holder's fd closes and the lock releases automatically, so a leftover
# ``.lock`` file is inert state, never a deadlock. There is exactly one
# lock per profile path, so no lock ordering exists to deadlock on.
#
# The lock is re-entrant per thread: ``save_to_yaml`` acquires it itself
# (every writer is fenced by construction), and a caller that already
# holds it — the PATCH route spanning read→CAS→merge→save — just reuses
# the same critical section.
#
# Blocking acquisition is bounded by PROFILE_LOCK_TIMEOUT_S so a wedged
# holder surfaces as ProfileLockTimeout (HTTP 503) instead of hanging
# the request forever. The retry sleep below paces acquisition; it is
# never used to order the race — the lock itself orders it.
# ---------------------------------------------------------------------------

PROFILE_LOCK_TIMEOUT_S = 10.0


class ProfileLockTimeout(TimeoutError):
    """The profile write fence could not be acquired within the deadline."""


if os.name == "nt":  # pragma: no cover — exercised on Windows runners only
    import msvcrt

    def _try_lock(fd: int) -> bool:
        os.lseek(fd, 0, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # type: ignore[attr-defined]
            return True
        except OSError:
            return False

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)  # type: ignore[attr-defined]

else:
    import fcntl

    def _try_lock(fd: int) -> bool:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)


def _resolve_profile_path(path: Path | str) -> Path:
    """Resolve a symlinked profile path the way ``save_to_yaml`` does, so
    the lock sidecar lands next to the TARGET and every writer contends
    on the same file."""
    path = Path(path)
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path.absolute()


_held = threading.local()


@contextlib.contextmanager
def profile_lock(
    path: Path | str, timeout_s: float | None = None
) -> Iterator[None]:
    """Hold the cross-process write fence for the profile at ``path``.

    Re-entrant per thread and per path: nested acquisition on the same
    profile is a no-op, so ``save_to_yaml`` (which locks internally) can
    be called inside a caller-held critical section.
    """
    resolved = _resolve_profile_path(path)
    key = str(resolved)
    held: set[str] = getattr(_held, "paths", set())
    if key in held:
        yield
        return
    resolved.parent.mkdir(parents=True, exist_ok=True)
    lock_path = resolved.with_name(resolved.name + ".lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    timeout = PROFILE_LOCK_TIMEOUT_S if timeout_s is None else timeout_s
    deadline = time.monotonic() + timeout
    try:
        while not _try_lock(fd):
            if time.monotonic() >= deadline:
                raise ProfileLockTimeout(
                    f"profilul e blocat de alt writer (> {timeout}s)"
                )
            time.sleep(0.01)
        held.add(key)
        _held.paths = held
        try:
            yield
        finally:
            held.discard(key)
    finally:
        with contextlib.suppress(OSError):
            _unlock(fd)
        os.close(fd)


def _persisted_version(path: Path) -> int:
    """Version currently on disk for ``path`` (0 if absent/unreadable)."""
    if not path.exists():
        # load_from_yaml synthesizes a default profile for a missing file —
        # that would fake version 1 out of thin air and skip the counter.
        return 0
    try:
        return CompanyProfile.load_from_yaml(path).version
    except Exception:
        return 0


def _currency_code(v: str | None) -> str | None:
    if v is None:
        return None
    if not isinstance(v, str) or not _CURRENCY_RE.match(v) or v not in ISO_4217_CODES:
        raise ValueError("așteptat cod ISO-4217 valid (ex. USD, EUR, RON)")
    return v


def _fmt_amount(amount: float | Decimal, currency: str | None) -> str:
    """Amount + explicit currency; unknown currency renders the bare number
    rather than silently claiming USD."""
    if currency == "USD":
        return f"${amount:,.0f}"
    if currency:
        return f"{currency} {amount:,.0f}"
    return f"{amount:,.0f}"


class TargetCustomer(BaseModel):
    profile: str = ""
    pain_points: list[str] = Field(default_factory=list)


class CompetitiveLandscape(BaseModel):
    primary_competitors: list[str] = Field(default_factory=list)
    competitive_advantages: list[str] = Field(default_factory=list)


class OrgStructure(BaseModel):
    departments: list[str] = Field(default_factory=list)
    leadership_team: list[str] = Field(default_factory=list)


class StrategicPriorities(BaseModel):
    current_year: list[str] = Field(default_factory=list)
    north_star_metric: str = ""


class Culture(BaseModel):
    values: list[str] = Field(default_factory=list)
    operating_principles: list[str] = Field(default_factory=list)


class Financials(BaseModel):
    burn_rate_monthly: Decimal | None = None
    burn_rate_currency: str | None = None
    runway_months: float | None = None
    key_metrics: dict[str, Any] = Field(default_factory=dict)

    _check_ccy = field_validator("burn_rate_currency")(_currency_code)


class CompanyProfile(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # Durable CAS counter (CONTROL R12): persisted inside profile.yaml, so
    # it survives restarts; every PATCH /company-profile write bumps it and
    # the writer must declare the version they read via expected_version.
    # Older files without the field migrate to version 1.
    version: int = 1
    name: str = ""
    industry: str = ""
    stage: str = ""
    founding_year: int | None = None
    headcount: int | None = None
    annual_revenue_arr: Decimal | None = None
    annual_revenue_arr_currency: str | None = None
    mission: str = ""
    vision: str = ""
    target_customer: TargetCustomer = Field(default_factory=TargetCustomer)
    competitive_landscape: CompetitiveLandscape = Field(
        default_factory=CompetitiveLandscape
    )
    org_structure: OrgStructure = Field(default_factory=OrgStructure)
    strategic_priorities: StrategicPriorities = Field(
        default_factory=StrategicPriorities
    )
    culture: Culture = Field(default_factory=Culture)
    financials: Financials = Field(default_factory=Financials)
    # External entities the company depends on or tracks. Named here so the
    # research watchlist policy can treat a watch on them as grounded in
    # company data (auto-added) rather than inferred (needs approval).
    vendors: list[str] = Field(default_factory=list)  # e.g. ["Stripe", "AWS"]
    tickers: list[str] = Field(default_factory=list)  # own + competitor tickers

    _check_arr_ccy = field_validator("annual_revenue_arr_currency")(_currency_code)

    @classmethod
    def load_from_yaml(cls, path: Path | str) -> CompanyProfile:
        path = Path(path)
        if not path.exists():
            return cls()
        # Explicit UTF-8: profile.yaml is routinely hand-edited, so it can hold
        # real non-ASCII text. Without this, the platform default (cp1252 on
        # Windows) silently mojibakes it into the cached company-profile prompt
        # block rather than raising.
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        company_data = data.get("company", data)
        return cls.model_validate(company_data)

    def save_to_yaml(self, path: Path | str) -> None:
        # The write runs under the cross-process fence (CONTROL R14): the
        # lock is re-entrant, so a caller spanning read→CAS→merge→write
        # (the PATCH route) simply widens its own critical section here.
        with profile_lock(path):
            self._save_locked(path)

    def _save_locked(self, path: Path | str) -> None:
        # Resolve first so a symlinked profile path (a common deployment
        # pattern for COMPANY_PROFILE_PATH) is written at its target and the
        # link survives, instead of being replaced by a regular file. A
        # symlink loop raises RuntimeError on 3.11 and OSError on 3.13; fall
        # back to the unresolved path and let the open() below report it.
        path = _resolve_profile_path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # The persisted version is monotone per file (CONTROL R14): a write
        # can never stamp a version at or below the one already on disk, so
        # a stale reader's expected_version can never collide with a later
        # overwrite. CAS writers (PATCH) set version=stored+1 and pass
        # through unchanged; non-CAS writers (onboarding, fixtures, slot
        # restore) get a fresh number so old pins die with the overwrite.
        persisted = _persisted_version(path)
        # mode="json" renders Decimal amounts as exact strings ("1.005") —
        # the default python dump would either crash yaml.dump or emit a
        # lossy float. Loading coerces the string straight back to Decimal.
        data = {"company": self.model_dump(mode="json")}
        data["company"]["version"] = max(self.version, persisted + 1)
        # Write-then-rename so a failure mid-dump (disk full, unrepresentable
        # value) can never leave a truncated profile behind: every other
        # subsystem loads this file, and callers such as the onboarding
        # route roll back on failure assuming the old profile survived.
        #
        # - O_EXCL via `opener`: the temp name is created fresh and never
        #   follows a pre-planted symlink. Going through open() (not
        #   os.fdopen) keeps the explicit-encoding contract visible.
        # - A random component plus O_EXCL: two processes on one volume
        #   (both PID 1 in their containers) cannot collide.
        # - The temp is always created 0600 and only widened to the
        #   destination's mode after fsync, right before the rename. A
        #   crash leftover (SIGKILL, OOM) is therefore private clutter,
        #   never a readable copy of the financials. There is no sweep of
        #   leftovers: one cannot be told apart from another process's
        #   in-flight file, and unlinking that makes its rename fail.
        # - The destination's mode is carried over: rename creates a new
        #   inode, so a file an operator chmod'd 0600 would otherwise
        #   silently revert to the umask default. A symlink's own mode
        #   (always 0777) is never used.
        # - fsync file and directory: a hard crash between write and
        #   rename must not leave a zero-length profile.
        tmp_path = path.with_name(f".tmp-{os.getpid()}-{secrets.token_hex(4)}-{path.name}")
        existing_mode: int | None = None
        with contextlib.suppress(OSError):
            st = path.lstat()
            if not stat.S_ISLNK(st.st_mode):
                existing_mode = stat.S_IMODE(st.st_mode)

        def _exclusive(p: str, flags: int) -> int:
            return os.open(p, flags | os.O_EXCL, 0o600)

        try:
            with open(tmp_path, "w", encoding="utf-8", opener=_exclusive) as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
                if existing_mode is not None and hasattr(os, "fchmod"):
                    os.fchmod(f.fileno(), existing_mode)
            os.replace(tmp_path, path)
            with contextlib.suppress(OSError):
                dir_fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    def to_prompt_block(self) -> str:
        if not self.name:
            return ""

        lines = ["## Company Context", ""]
        lines.append(f"**Company**: {self.name}")
        if self.industry:
            lines.append(f"**Industry**: {self.industry}")
        if self.stage:
            lines.append(f"**Stage**: {self.stage}")
        if self.founding_year:
            lines.append(f"**Founded**: {self.founding_year}")
        if self.headcount:
            lines.append(f"**Headcount**: {self.headcount}")
        if self.annual_revenue_arr:
            lines.append(
                f"**ARR**: {_fmt_amount(self.annual_revenue_arr, self.annual_revenue_arr_currency)}"
            )

        if self.mission:
            lines.extend(["", f"**Mission**: {self.mission}"])
        if self.vision:
            lines.append(f"**Vision**: {self.vision}")

        if self.target_customer.profile:
            lines.extend(["", "**Target Customer**:"])
            lines.append(f"  {self.target_customer.profile}")
            if self.target_customer.pain_points:
                lines.append("  Pain points: " + "; ".join(self.target_customer.pain_points))

        if self.competitive_landscape.primary_competitors:
            lines.extend(["", "**Competitive Landscape**:"])
            lines.append(
                "  Competitors: " + ", ".join(self.competitive_landscape.primary_competitors)
            )
            if self.competitive_landscape.competitive_advantages:
                lines.append(
                    "  Our advantages: "
                    + "; ".join(self.competitive_landscape.competitive_advantages)
                )

        if self.vendors or self.tickers:
            lines.extend(["", "**External Dependencies**:"])
            if self.vendors:
                lines.append("  Vendors: " + ", ".join(self.vendors))
            if self.tickers:
                lines.append("  Tracked tickers: " + ", ".join(self.tickers))

        if self.strategic_priorities.current_year:
            lines.extend(["", "**Strategic Priorities (Current Year)**:"])
            for p in self.strategic_priorities.current_year:
                lines.append(f"  - {p}")
            if self.strategic_priorities.north_star_metric:
                lines.append(
                    f"  North Star: {self.strategic_priorities.north_star_metric}"
                )

        if self.culture.values:
            lines.extend(["", f"**Values**: {', '.join(self.culture.values)}"])

        if self.financials.burn_rate_monthly is not None:
            lines.extend(["", "**Financial Position**:"])
            lines.append(
                "  Monthly burn: "
                + _fmt_amount(self.financials.burn_rate_monthly, self.financials.burn_rate_currency)
            )
            if self.financials.runway_months is not None:
                lines.append(f"  Runway: {self.financials.runway_months:.1f} months")
            for k, v in self.financials.key_metrics.items():
                lines.append(f"  {k}: {v}")

        if self.org_structure.leadership_team:
            lines.extend(["", "**Leadership**: " + ", ".join(self.org_structure.leadership_team)])

        return "\n".join(lines)

    def is_empty(self) -> bool:
        return not bool(self.name)
