"""Screening as an operation: cache, call, score, record, alert.

Three things this layer is responsible for that the providers and the scorer
deliberately are not.

**Recording negatives.** A screening that found nothing is a result, and it is
the one an examiner asks for: *show me that you screened this client against
the current lists on the date you onboarded them.* Every screening produces a
:class:`~aml.screening.base.ScreeningResult`, hits or no hits.

**Surviving a provider outage.** A vendor timeout is recorded as an error on
the result and leaves the decision ``PENDING``. It is never recorded as
"no match" — a screening gap that looks like a clean screening is the worst
possible failure of this component, because nobody ever goes back to it.

**Caching, because these calls are metered.** Screening a book of two hundred
thousand customers nightly against a per-query contract is not affordable, and
a customer whose name, date of birth and identifiers have not changed does not
need re-querying within the TTL. The cache key is the *subject*, not the
customer id, so a change of name misses the cache by construction — which is
exactly when re-screening matters.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import pathlib
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import replace
from typing import Any

from aml.config import AmlConfig
from aml.model.entities import Alert, Party
from aml.model.enums import AlertState, ListType, ScreeningDecision, Severity
from aml.phcalendar import now_manila
from aml.screening.base import ListEntry, ScreeningProvider, ScreeningResult, Subject
from aml.screening.local import LocalListProvider
from aml.screening.matcher import screen_candidates

__all__ = ["ScreeningCache", "ScreeningService", "build_providers", "alerts_from_results"]

log = logging.getLogger("aml.screening.service")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS screening_cache (
    cache_key   TEXT PRIMARY KEY,
    provider    TEXT NOT NULL,
    subject     TEXT NOT NULL,
    entries     TEXT NOT NULL,
    fetched_at  TEXT NOT NULL
);
"""


def _subject_key(provider: str, subject: Subject) -> str:
    payload = json.dumps(
        {
            "provider": provider,
            "name": subject.name.upper(),
            "aliases": sorted(a.upper() for a in subject.aliases),
            "birth_date": subject.birth_date.isoformat() if subject.birth_date else "",
            "ids": sorted(subject.id_numbers),
            "party_type": str(subject.party_type),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ScreeningCache:
    """Provider responses, keyed by subject, with a time to live."""

    def __init__(self, path: str | pathlib.Path, ttl_hours: int = 24) -> None:
        self.path = pathlib.Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.ttl = dt.timedelta(hours=ttl_hours)
        with sqlite3.connect(self.path) as conn:
            conn.executescript(_SCHEMA)

    def get(self, provider: str, subject: Subject, now: dt.datetime) -> list[ListEntry] | None:
        key = _subject_key(provider, subject)
        with sqlite3.connect(self.path) as conn:
            row = conn.execute(
                "SELECT entries, fetched_at FROM screening_cache WHERE cache_key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        fetched = dt.datetime.fromisoformat(row[1])
        if now - fetched > self.ttl:
            return None
        return [ListEntry.from_dict(item) for item in json.loads(row[0])]

    def put(
        self, provider: str, subject: Subject, entries: Sequence[ListEntry], now: dt.datetime
    ) -> None:
        key = _subject_key(provider, subject)
        payload = json.dumps([e.as_dict() for e in entries])
        with sqlite3.connect(self.path) as conn:
            conn.execute(
                "INSERT INTO screening_cache (cache_key, provider, subject, entries, fetched_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(cache_key) DO UPDATE SET "
                "entries = excluded.entries, fetched_at = excluded.fetched_at",
                (key, provider, subject.name, payload, now.isoformat()),
            )


def build_providers(config: AmlConfig) -> list[ScreeningProvider]:
    """Instantiate the configured providers.

    Vendor providers are constructed lazily in the sense that matters: nothing
    contacts a network here, so a misconfigured World-Check tenant surfaces on
    the first screening with a clear error rather than at import time.
    """
    providers: list[ScreeningProvider] = []
    for name in config.screening.providers:
        key = name.strip().lower()
        if key == "local":
            providers.append(LocalListProvider.from_files(config.screening.local_lists))
        elif key == "worldcheck":
            from aml.screening.worldcheck import WorldCheckProvider

            providers.append(WorldCheckProvider(config.screening))
        elif key == "dowjones":
            from aml.screening.dowjones import DowJonesProvider

            providers.append(DowJonesProvider(config.screening))
        else:
            raise ValueError(f"unknown screening provider {name!r}")
    return providers


class ScreeningService:
    """Screen subjects against every configured provider."""

    def __init__(
        self,
        config: AmlConfig,
        providers: Sequence[ScreeningProvider] | None = None,
        cache: ScreeningCache | None = None,
    ) -> None:
        self.config = config
        self.providers = list(providers) if providers is not None else build_providers(config)
        self.cache = cache

    def screen(self, subject: Subject, *, now: dt.datetime | None = None) -> ScreeningResult:
        moment = now or now_manila()
        candidates: list[ListEntry] = []
        lists: list[str] = []
        errors: list[str] = []
        from_cache = bool(self.providers)

        for provider in self.providers:
            try:
                cached = (
                    self.cache.get(provider.name, subject, moment) if self.cache else None
                )
                if cached is None:
                    from_cache = False
                    found = list(provider.candidates(subject))
                    if self.cache:
                        self.cache.put(provider.name, subject, found, moment)
                else:
                    found = cached
                candidates.extend(found)
                lists.extend(provider.lists())
            except Exception as exc:  # noqa: BLE001 - one provider must not blind the rest
                errors.append(f"{provider.name}: {type(exc).__name__}: {exc}")
                log.error("screening provider %s failed for %s", provider.name, subject.name)

        hits = screen_candidates(subject, candidates, self.config.screening)
        return ScreeningResult(
            subject=subject,
            screened_at=moment,
            providers=tuple(p.name for p in self.providers),
            hits=hits,
            lists_searched=tuple(sorted(set(lists))),
            from_cache=from_cache and not errors,
            error="; ".join(errors),
        )

    def screen_parties(
        self, parties: Iterable[Party], *, role: str = "customer", now: dt.datetime | None = None
    ) -> list[ScreeningResult]:
        moment = now or now_manila()
        return [self.screen(Subject.from_party(p, role=role), now=moment) for p in parties]


#: Which kind of hit produces which kind of alert. The distinction is the
#: obligation: a designation means freeze and report on the terrorism-financing
#: clock, a PEP means enhanced due diligence and senior approval, adverse media
#: means investigate before concluding anything.
_ALERT_SHAPES: dict[ListType, tuple[str, str, Severity, tuple[str, ...], tuple[str, ...]]] = {
    ListType.UN_DESIGNATED: (
        "screening.sanctions_match",
        "Possible match against a designated person or entity",
        Severity.CRITICAL,
        ("ST6",),
        ("UA13", "UA14"),
    ),
    ListType.ATC_DESIGNATED: (
        "screening.sanctions_match",
        "Possible match against a domestic designation",
        Severity.CRITICAL,
        ("ST6",),
        ("UA13", "UA14"),
    ),
    ListType.SANCTIONS: (
        "screening.sanctions_match",
        "Possible sanctions match",
        Severity.CRITICAL,
        ("ST6",),
        (),
    ),
    ListType.PEP: (
        "screening.pep_match",
        "Possible politically exposed person",
        Severity.MEDIUM,
        (),
        (),
    ),
    ListType.LAW_ENFORCEMENT: (
        "screening.law_enforcement_match",
        "Possible match against a law-enforcement listing",
        Severity.HIGH,
        ("ST6",),
        (),
    ),
    ListType.REGULATORY_ENFORCEMENT: (
        "screening.regulatory_match",
        "Possible match against a regulatory enforcement listing",
        Severity.MEDIUM,
        (),
        (),
    ),
    ListType.ADVERSE_MEDIA: (
        "screening.adverse_media",
        "Adverse media match",
        Severity.MEDIUM,
        (),
        (),
    ),
    ListType.INTERNAL_WATCHLIST: (
        "screening.internal_watchlist",
        "Match against the institution's own watchlist",
        Severity.HIGH,
        (),
        (),
    ),
}


def _sentence(text: str) -> str:
    """Capitalise a reason without flattening the names inside it."""
    return text[:1].upper() + text[1:] if text else text


def alerts_from_results(
    results: Iterable[ScreeningResult], *, now: dt.datetime | None = None
) -> list[Alert]:
    """Turn potential matches into alerts an analyst can work.

    One alert per subject per list category, carrying every hit in that
    category. Not one per hit: a name on four sanctions programmes is one
    decision to make, and four alerts is three ways to get it wrong.
    """
    moment = now or now_manila()
    alerts: list[Alert] = []
    for result in results:
        grouped: dict[ListType, list[Any]] = {}
        for hit in result.hits:
            if hit.decision is ScreeningDecision.NO_MATCH:
                continue
            grouped.setdefault(hit.entry.list_type, []).append(hit)

        for list_type, hits in sorted(grouped.items(), key=lambda item: str(item[0])):
            shape = _ALERT_SHAPES.get(
                list_type,
                ("screening.match", "Screening match", Severity.MEDIUM, (), ()),
            )
            rule_id, title, severity, st_codes, ua_codes = shape
            best = max(hits, key=lambda h: h.score)
            names = ", ".join(sorted({h.entry.name for h in hits}))
            lists_named = ", ".join(sorted({h.entry.list_name for h in hits if h.entry.list_name}))
            narrative = (
                f"{result.subject.name} ({result.subject.role}) matched {len(hits)} "
                f"{str(list_type).replace('_', ' ').lower()} "
                f"{'entry' if len(hits) == 1 else 'entries'} — {names} — on {lists_named} "
                f"at a confidence of {best.score}. {_sentence(best.reasons[0])}."
            )
            if best.entry.is_sanctions:
                narrative += (
                    " If confirmed, the property must be frozen without delay and the AMLC "
                    "informed; the report is due on the terrorism-financing timetable rather "
                    "than the ordinary five working days."
                )
            elif list_type is ListType.PEP:
                narrative += (
                    " A PEP relationship is not suspicious in itself: it requires senior "
                    "management approval, an established source of wealth and ongoing "
                    "enhanced monitoring."
                )
            if best.entry.designations:
                narrative += f" Listed under: {', '.join(best.entry.designations)}."

            draft = Alert(
                alert_id="",
                rule_id=rule_id,
                rule_version="1",
                subject_party_id=result.subject.subject_id,
                created_at=moment,
                severity=severity,
                score=best.score,
                title=title,
                narrative=narrative,
                st_codes=st_codes,
                unlawful_activity_codes=ua_codes,
                state=AlertState.OPEN,
                dedup_extra=tuple(sorted(h.entry.entry_id for h in hits)),
                evidence={
                    "subject": result.subject.as_dict(),
                    "providers": list(result.providers),
                    "lists_searched": list(result.lists_searched),
                    "screened_at": result.screened_at.isoformat(),
                    "hits": [h.as_dict() for h in hits],
                    "requires_freeze": bool(best.entry.is_sanctions),
                },
            )
            alerts.append(replace(draft, alert_id=f"ALR-{draft.dedup_key[:16].upper()}"))
    return alerts
