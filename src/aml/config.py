"""Configuration: the numbers a regulator can change without a release.

Everything in this file exists because it is a *policy* choice rather than a
*code* choice, and the two have different lifecycles. The covered-transaction
threshold is set by law and has moved before; the filing clock is five working
days until the AMLC says otherwise; which instruments count as "cash or other
equivalent monetary instrument" is an institution's documented interpretation
and it will be argued about in an examination. None of those should require a
deployment, and none of them should be scattered through the rules as literals.

Secrets are the exception that proves the rule: they are *not* configuration
values here. A World-Check secret or a portal password in a TOML file is a
secret in a backup, in a ticket attachment and eventually in a repository, so
this module stores only a *reference* — ``env:WORLDCHECK_API_SECRET`` — and
resolves it at the moment of use.
"""

from __future__ import annotations

import os
import pathlib
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any

from aml.model.enums import PaymentInstrument
from aml.money import PHP, Money, php
from aml.phcalendar import PhilippineCalendar

__all__ = [
    "SecretRef",
    "resolve_secret",
    "InstitutionProfile",
    "Thresholds",
    "Deadlines",
    "ScreeningConfig",
    "PortalConfig",
    "RulesConfig",
    "SourcePaths",
    "AmlConfig",
    "EXAMPLE_TOML",
]


class SecretRef(str):
    """A pointer to a secret, not the secret.

    ``env:NAME`` reads an environment variable, ``file:/path`` reads a file
    (for a mounted Kubernetes or Docker secret). A bare value is accepted so a
    developer can get moving, and :func:`resolve_secret` says so out loud when
    it happens.
    """


def resolve_secret(ref: str | None, *, label: str = "secret") -> str | None:
    """Resolve a secret reference at the point of use."""
    if not ref:
        return None
    text = str(ref)
    if text.startswith("env:"):
        name = text[4:]
        value = os.environ.get(name)
        if value is None:
            raise KeyError(f"{label} not set: environment variable {name} is empty")
        return value
    if text.startswith("file:"):
        path = pathlib.Path(text[5:]).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"{label} not found at {path}")
        return path.read_text(encoding="utf-8").strip()
    return text


@dataclass(frozen=True, slots=True)
class InstitutionProfile:
    """Who is filing. Every report header is built from this."""

    covered_person_name: str = ""
    #: Code from ``aml.model.codes.COVERED_PERSON_TYPES``.
    covered_person_type: str = "INS-LIFE"
    #: Institution code issued by the AMLC on registration. Reports are
    #: rejected without the right one, so it is required before submission.
    amlc_institution_code: str = ""
    amlc_registration_number: str = ""
    insurance_commission_licence: str = ""
    tin: str = ""
    address_line: str = ""
    city: str = ""
    province: str = ""
    postal_code: str = ""
    country: str = "PH"
    compliance_officer_name: str = ""
    compliance_officer_position: str = "Chief Compliance Officer"
    compliance_officer_email: str = ""
    compliance_officer_phone: str = ""
    #: Alternate responsible officer, for filings made while the CO is away.
    deputy_officer_name: str = ""

    def missing_for_filing(self) -> list[str]:
        """Fields a submission cannot legally go out without."""
        required = {
            "covered_person_name": self.covered_person_name,
            "amlc_institution_code": self.amlc_institution_code,
            "compliance_officer_name": self.compliance_officer_name,
        }
        return sorted(name for name, value in required.items() if not str(value).strip())


@dataclass(frozen=True, slots=True)
class Thresholds:
    """The statutory numbers, and the interpretations attached to them."""

    #: A covered transaction is one in cash or other equivalent monetary
    #: instrument exceeding this amount in one banking day. PHP 500,000 for an
    #: insurance covered person.
    covered_transaction: Money = field(default_factory=lambda: php(500_000))
    #: Whether several same-day transactions by one client are added together
    #: before the threshold is applied. The statute speaks of a transaction
    #: "in one banking day"; institutions differ on whether that aggregates.
    #: Defaulting to True detects a client who pays 300k twice in a morning.
    aggregate_same_day: bool = True
    #: Instruments treated as "cash or other equivalent monetary instrument".
    #: Empty means use the package default set.
    covered_instruments: tuple[str, ...] = ()
    #: Transactions landing in this band below the threshold are the ones a
    #: structuring rule looks at. 0.60 means 300,000-500,000.
    structuring_band_floor: Decimal = Decimal("0.60")
    #: Peso equivalent above which a single transaction is treated as high
    #: value for enhanced review even when it is not covered.
    high_value_review: Money = field(default_factory=lambda: php(1_000_000))

    def covered_instrument_set(self) -> frozenset[PaymentInstrument]:
        from aml.model.enums import MONETARY_INSTRUMENTS

        default: frozenset[PaymentInstrument] = MONETARY_INSTRUMENTS
        if not self.covered_instruments:
            return default
        return frozenset(PaymentInstrument(code) for code in self.covered_instruments)

    @property
    def structuring_floor(self) -> Money:
        return Money(
            self.covered_transaction.amount * self.structuring_band_floor,
            self.covered_transaction.currency,
        ).quantized()


@dataclass(frozen=True, slots=True)
class Deadlines:
    """Filing clocks, in working days unless stated otherwise."""

    #: Working days from the occurrence of a covered transaction.
    ctr_working_days: int = 5
    #: Working days from the *determination* that a transaction is suspicious.
    str_working_days: int = 5
    #: Terrorism-financing and designated-person matters are not on the
    #: ordinary clock. Hours, not working days, and the freeze comes first.
    tf_report_hours: int = 24
    #: Days before a deadline at which a case starts being reported as at risk.
    escalate_when_days_remaining: int = 2

    def alert_at(self) -> int:
        return self.escalate_when_days_remaining


@dataclass(frozen=True, slots=True)
class ScreeningConfig:
    """Name screening: which provider, and how sure is sure enough."""

    #: ``local``, ``worldcheck``, ``dowjones``, or several, comma-separated.
    providers: tuple[str, ...] = ("local",)
    #: At or above this score a hit is raised for human adjudication.
    review_threshold: Decimal = Decimal("0.82")
    #: A designated-person list is screened on the name itself as well. When
    #: the name agreement alone reaches this, the hit is raised for a human
    #: whatever the corroborating attributes say — a client who supplies a
    #: false date of birth must not be able to suppress their own sanctions
    #: match. Applies to sanctions and designation lists only; applying it to
    #: PEP and adverse-media lists would flood the queue.
    sanctions_name_floor: Decimal = Decimal("0.95")
    #: At or above this score the hit is presented as a probable true match.
    #: Nothing is auto-confirmed: confirming a sanctions match freezes a
    #: client's property, and that decision has a human's name against it.
    strong_threshold: Decimal = Decimal("0.93")
    #: Screen every customer again on this cadence, and always on a list
    #: update. Lists change; a customer screened once is screened against a
    #: world that no longer exists.
    rescreen_days: int = 90
    cache_ttl_hours: int = 24
    #: Local list files, by list type.
    local_lists: Mapping[str, str] = field(default_factory=dict)
    worldcheck_base_url: str = "https://api-worldcheck.refinitiv.com/v2"
    worldcheck_api_key: str = "env:WORLDCHECK_API_KEY"
    worldcheck_api_secret: str = "env:WORLDCHECK_API_SECRET"
    worldcheck_group_id: str = "env:WORLDCHECK_GROUP_ID"
    dowjones_base_url: str = "https://api.dowjones.com/riskentities"
    dowjones_token_url: str = "https://accounts.dowjones.com/oauth2/v1/token"
    dowjones_client_id: str = "env:DOWJONES_CLIENT_ID"
    dowjones_client_secret: str = "env:DOWJONES_CLIENT_SECRET"
    dowjones_username: str = "env:DOWJONES_USERNAME"
    dowjones_password: str = "env:DOWJONES_PASSWORD"
    request_timeout_seconds: int = 30
    max_retries: int = 3


@dataclass(frozen=True, slots=True)
class PortalConfig:
    """How a finished report reaches the AMLC.

    ``spool`` is the default and is not a stub: it produces the exact package
    a person uploads to the portal, with its checksum, and records the
    submission. An institution that has not yet completed portal enrolment —
    or whose security policy forbids an automated upload — is fully served by
    it. ``http`` drives the portal directly once the endpoints and credentials
    for the institution's enrolment are configured.
    """

    mode: str = "spool"
    spool_dir: str = "out/amlc-spool"
    base_url: str = ""
    login_path: str = "/api/auth/login"
    upload_path: str = "/api/reports/upload"
    status_path: str = "/api/reports/{submission_reference}/status"
    username: str = "env:AMLC_PORTAL_USERNAME"
    password: str = "env:AMLC_PORTAL_PASSWORD"
    #: Client certificate, if the institution's enrolment uses mutual TLS.
    client_cert: str = ""
    client_key: str = ""
    verify_tls: bool = True
    request_timeout_seconds: int = 120
    max_retries: int = 4
    #: Encrypt the package before it leaves the building. Requires a recipient
    #: key; see aml.portal.package.
    encrypt: bool = False
    encryption_recipient: str = ""
    encryption_passphrase: str = "env:AMLC_PACKAGE_PASSPHRASE"


@dataclass(frozen=True, slots=True)
class RulesConfig:
    """Which detection rules run, and with what parameters.

    Rules are enabled by name and parameterised by data. A rule whose
    thresholds live in configuration can be tuned by the compliance unit
    against last quarter's alerts, which is the only way tuning ever actually
    happens.
    """

    enabled: tuple[str, ...] = ()
    disabled: tuple[str, ...] = ()
    parameters: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: ISO-3166 alpha-2 codes treated as higher risk. Kept in configuration
    #: because the FATF lists change three times a year.
    high_risk_jurisdictions: tuple[str, ...] = (
        "IR", "KP", "MM",
    )
    #: Jurisdictions under increased monitoring ("grey list"), weighted lower
    #: than the call-for-action list above.
    monitored_jurisdictions: tuple[str, ...] = ()

    def params_for(self, rule_id: str) -> Mapping[str, Any]:
        return self.parameters.get(rule_id, {})

    def is_enabled(self, rule_id: str) -> bool:
        if rule_id in self.disabled:
            return False
        return not self.enabled or rule_id in self.enabled


@dataclass(frozen=True, slots=True)
class SourcePaths:
    """Where the administration systems' extracts land.

    Configured rather than passed on every command line: the compliance
    officer running the nightly monitoring should not have to remember four
    paths, and a path typed differently on Tuesday is a monitoring run over a
    different population.
    """

    parties: str = ""
    policies: str = ""
    transactions: str = ""
    rates: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.parties and self.transactions)


@dataclass(frozen=True, slots=True)
class AmlConfig:
    """Everything the tool needs to know that is not code."""

    institution: InstitutionProfile = field(default_factory=InstitutionProfile)
    thresholds: Thresholds = field(default_factory=Thresholds)
    deadlines: Deadlines = field(default_factory=Deadlines)
    screening: ScreeningConfig = field(default_factory=ScreeningConfig)
    portal: PortalConfig = field(default_factory=PortalConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    calendar: PhilippineCalendar = field(default_factory=PhilippineCalendar)
    sources: SourcePaths = field(default_factory=SourcePaths)
    #: Operational store. One SQLite file, because the compliance unit owns it
    #: and has to be able to retain, copy and hand over exactly that.
    store_path: str = "out/aml.sqlite3"
    output_dir: str = "out"
    #: Records are retained five years from the transaction date, and for as
    #: long as a case is live beyond that.
    retention_years: int = 5
    #: FX rates, ``currency,date,rate`` CSV.
    rate_table_path: str = ""
    source_path: str | None = None

    # -- loading ---------------------------------------------------------

    @classmethod
    def load(cls, path: str | pathlib.Path | None = None) -> AmlConfig:
        """Load configuration, or return documented defaults.

        Resolution order: explicit path, ``$AML_CONFIG``, ``./aml.toml``. A
        missing file is not an error — the defaults are the statutory values
        and a demo runs on them — but an institution profile is empty until
        somebody fills it in, and submission refuses to proceed without it.
        """
        candidate = path or os.environ.get("AML_CONFIG") or "aml.toml"
        file = pathlib.Path(candidate).expanduser()
        if not file.exists():
            if path is not None:
                raise FileNotFoundError(f"no configuration at {file}")
            return cls()
        with open(file, "rb") as handle:
            raw = tomllib.load(handle)
        return cls.from_mapping(raw, source_path=str(file))

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any], source_path: str | None = None) -> AmlConfig:
        def section(name: str) -> dict[str, Any]:
            value = raw.get(name, {})
            return dict(value) if isinstance(value, Mapping) else {}

        institution = InstitutionProfile(**_known(section("institution"), InstitutionProfile))

        raw_thresholds = section("thresholds")
        thresholds = Thresholds(
            covered_transaction=Money.parse(
                raw_thresholds.get("covered_transaction", 500_000), PHP
            ),
            aggregate_same_day=bool(raw_thresholds.get("aggregate_same_day", True)),
            covered_instruments=tuple(raw_thresholds.get("covered_instruments", ()) or ()),
            structuring_band_floor=Decimal(
                str(raw_thresholds.get("structuring_band_floor", "0.60"))
            ),
            high_value_review=Money.parse(
                raw_thresholds.get("high_value_review", 1_000_000), PHP
            ),
        )

        deadlines = Deadlines(**_known(section("deadlines"), Deadlines))

        raw_screening = section("screening")
        screening = ScreeningConfig(
            **{
                **_known(raw_screening, ScreeningConfig),
                "providers": tuple(raw_screening.get("providers", ("local",)) or ("local",)),
                "review_threshold": Decimal(str(raw_screening.get("review_threshold", "0.82"))),
                "sanctions_name_floor": Decimal(
                    str(raw_screening.get("sanctions_name_floor", "0.95"))
                ),
                "strong_threshold": Decimal(str(raw_screening.get("strong_threshold", "0.93"))),
                "local_lists": dict(raw_screening.get("local_lists", {}) or {}),
            }
        )

        portal = PortalConfig(**_known(section("portal"), PortalConfig))

        raw_rules = section("rules")
        rules = RulesConfig(
            enabled=tuple(raw_rules.get("enabled", ()) or ()),
            disabled=tuple(raw_rules.get("disabled", ()) or ()),
            parameters={
                str(k): dict(v)
                for k, v in (raw_rules.get("parameters", {}) or {}).items()
                if isinstance(v, Mapping)
            },
            high_risk_jurisdictions=tuple(
                raw_rules.get("high_risk_jurisdictions", ("IR", "KP", "MM")) or ()
            ),
            monitored_jurisdictions=tuple(raw_rules.get("monitored_jurisdictions", ()) or ()),
        )

        calendar = PhilippineCalendar.from_mapping(section("calendar"))
        sources = SourcePaths(**_known(section("sources"), SourcePaths))

        top = _known(raw, cls)
        top.pop("institution", None)
        return cls(
            institution=institution,
            thresholds=thresholds,
            deadlines=deadlines,
            screening=screening,
            portal=portal,
            rules=rules,
            calendar=calendar,
            sources=sources,
            store_path=str(raw.get("store_path", "out/aml.sqlite3")),
            output_dir=str(raw.get("output_dir", "out")),
            retention_years=int(raw.get("retention_years", 5)),
            rate_table_path=str(raw.get("rate_table_path", "") or ""),
            source_path=source_path,
        )

    def with_overrides(self, **kwargs: Any) -> AmlConfig:
        return replace(self, **kwargs)

    def store_file(self) -> pathlib.Path:
        return pathlib.Path(self.store_path).expanduser()

    def output_path(self) -> pathlib.Path:
        return pathlib.Path(self.output_dir).expanduser()


def _known(raw: Mapping[str, Any], target: type) -> dict[str, Any]:
    """Keep only keys the dataclass declares.

    An unknown key in a config file is a typo, and a typo that silently does
    nothing is how a threshold ends up not being applied. Callers report what
    was dropped; see ``AmlConfig.unknown_keys``.
    """
    names = {f.name for f in getattr(target, "__dataclass_fields__", {}).values()}
    return {k: v for k, v in raw.items() if k in names}


def unknown_keys(raw: Mapping[str, Any]) -> list[str]:
    """Configuration keys this version does not understand, dotted."""
    sections = {
        "institution": InstitutionProfile,
        "thresholds": Thresholds,
        "deadlines": Deadlines,
        "screening": ScreeningConfig,
        "portal": PortalConfig,
        "sources": SourcePaths,
    }
    stray: list[str] = []
    for name, target in sections.items():
        body = raw.get(name, {})
        if not isinstance(body, Mapping):
            continue
        known = {f.name for f in getattr(target, "__dataclass_fields__", {}).values()}
        stray.extend(f"{name}.{k}" for k in body if k not in known)
    return sorted(stray)


EXAMPLE_TOML = '''\
# AML and transaction monitoring — configuration
#
# Statutory defaults are already correct for an insurance covered person in the
# Philippines. What must be filled in is the institution block: reports cannot
# be submitted without the AMLC institution code and a named compliance officer.
#
# Secrets are never written here. Use env:NAME or file:/path.

store_path = "out/aml.sqlite3"
output_dir = "out"
retention_years = 5
# rate_table_path = "data/bsp-reference-rates.csv"   # currency,date,rate

[sources]
# The extracts monitoring runs over. Set here so the nightly run is one command.
# parties = "data/extract/parties.csv"
# policies = "data/extract/policies.csv"
# transactions = "data/extract/transactions.csv"
# rates = "data/extract/rates.csv"

[institution]
covered_person_name = ""            # exactly as registered with the AMLC
covered_person_type = "INS-LIFE"    # see aml.model.codes.COVERED_PERSON_TYPES
amlc_institution_code = ""          # issued by the AMLC on registration
amlc_registration_number = ""
insurance_commission_licence = ""
tin = ""
address_line = ""
city = ""
province = ""
postal_code = ""
compliance_officer_name = ""
compliance_officer_position = "Chief Compliance Officer"
compliance_officer_email = ""
compliance_officer_phone = ""

[thresholds]
covered_transaction = 500000        # PHP, one banking day
aggregate_same_day = true           # add up a client's same-day transactions
structuring_band_floor = "0.60"     # look at 300k-500k for structuring
high_value_review = 1000000
# covered_instruments = ["CASH", "MANAGERS_CHECK", "BANK_TRANSFER"]

[deadlines]
ctr_working_days = 5
str_working_days = 5
tf_report_hours = 24
escalate_when_days_remaining = 2

[calendar]
observe_special_days = true
# Eid'l Fitr, Eid'l Adha and any special days are fixed by proclamation each
# year and cannot be derived. Load them, or deadlines will be computed on a
# calendar that thinks those are working days.
[calendar.proclaimed_holidays]
# "2026-03-20" = "Eid'l Fitr"
# "2026-05-27" = "Eid'l Adha"

[screening]
providers = ["local"]               # local, worldcheck, dowjones
review_threshold = "0.82"
sanctions_name_floor = "0.95"
strong_threshold = "0.93"
rescreen_days = 90
[screening.local_lists]
# UN_DESIGNATED = "data/lists/un-consolidated.xml"
# ATC_DESIGNATED = "data/lists/atc-designations.csv"
# INTERNAL_WATCHLIST = "data/lists/internal.csv"

[portal]
mode = "spool"                      # spool | http
spool_dir = "out/amlc-spool"
# base_url = "https://portal.amlc.gov.ph"
# username = "env:AMLC_PORTAL_USERNAME"
# password = "env:AMLC_PORTAL_PASSWORD"
encrypt = false

[rules]
# enabled = []                      # empty means all registered rules
disabled = []
high_risk_jurisdictions = ["IR", "KP", "MM"]
monitored_jurisdictions = []

[rules.parameters]
[rules.parameters."str.structuring"]
window_days = 5
minimum_count = 3
[rules.parameters."str.early_surrender"]
within_days = 180
'''
