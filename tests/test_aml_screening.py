"""Name screening: the matching, the thresholds, and the things that must not
be allowed to suppress a hit."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from aml.config import AmlConfig, ScreeningConfig
from aml.model.entities import Party
from aml.model.enums import ListType, PartyType, ScreeningDecision
from aml.screening.base import ListEntry, Subject
from aml.screening.local import LocalListProvider, load_csv_list
from aml.screening.matcher import score_entry, screen_candidates
from aml.screening.names import name_similarity, normalize_name, phonetic_key
from aml.screening.service import ScreeningCache, ScreeningService, alerts_from_results


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Juan Dela Cruz", "Juan de la Cruz"),
        ("Juan Dela Cruz", "JUAN DELACRUZ"),
        ("Ma. Teresa Santos-Reyes", "Maria Teresa S. Reyes"),
        ("Roberto Peña", "Roberto Pena"),
        ("Ali Hassan Abdullah", "Aly Hasan Abdulla"),
        ("ABC Trading Corporation", "ABC Trading Corp"),
    ],
)
def test_variants_of_the_same_name_agree(left, right):
    assert name_similarity(left, right) >= Decimal("0.90")


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Juan Dela Cruz", "Maria Santos"),
        ("Faisal Ahmad Rahman", "Corazon Mendoza Tolentino"),
    ],
)
def test_unrelated_names_are_well_below_the_review_threshold(left, right):
    assert name_similarity(left, right) < Decimal("0.60")


def test_particles_are_joined_to_the_surname():
    assert normalize_name("Juan de la Cruz") == "JUAN DELACRUZ"
    assert normalize_name("Ma. Teresa del Rosario") == "MARIA TERESA DELROSARIO"


def test_sound_alike_names_share_a_phonetic_key():
    assert phonetic_key("Catherine") == phonetic_key("Katherine")


def test_a_generational_suffix_is_a_real_difference():
    assert name_similarity("Juan Cruz Jr", "Juan Cruz Sr") < name_similarity(
        "Juan Cruz Jr", "Juan Cruz Jr"
    )


def test_a_matching_identifier_settles_it():
    subject = Subject("C1", "J. D. Cruz", id_numbers=("1234-5678-9012",))
    entry = ListEntry("X", "Juan Dela Cruz", ListType.SANCTIONS, id_numbers=("123456789012",))
    hit = score_entry(subject, entry)
    assert hit.score >= Decimal("0.99")
    assert any("identification number" in reason for reason in hit.reasons)


def test_a_conflicting_date_of_birth_lowers_but_does_not_clear_a_designation():
    subject = Subject("C1", "Juan Dela Cruz", birth_date=dt.date(1999, 1, 1))
    entry = ListEntry(
        "UN-1", "Juan Dela Cruz", ListType.UN_DESIGNATED, birth_dates=("1975-04-12",)
    )
    hits = screen_candidates(subject, [entry], ScreeningConfig())
    assert hits and hits[0].decision is ScreeningDecision.POTENTIAL_MATCH
    assert hits[0].score < Decimal("0.82")  # scored down...
    assert any("name floor" in reason for reason in hits[0].reasons)  # ...but retained


def test_the_name_floor_does_not_apply_to_pep_lists():
    subject = Subject("C1", "Juan Dela Cruz", birth_date=dt.date(1999, 1, 1))
    entry = ListEntry("PEP-1", "Juan Dela Cruz", ListType.PEP, birth_dates=("1975-04-12",))
    assert not screen_candidates(subject, [entry], ScreeningConfig())


def test_a_person_is_not_a_company():
    subject = Subject("C1", "Orient Star Holdings", party_type=PartyType.INDIVIDUAL)
    entry = ListEntry("E-1", "Orient Star Holdings", ListType.SANCTIONS, entity_type="ENTITY")
    hit = score_entry(subject, entry)
    assert hit.score < hit.name_score


def test_the_local_provider_blocks_rather_than_scanning():
    entries = [
        ListEntry(f"E-{i}", f"Person Number {i}", ListType.SANCTIONS) for i in range(500)
    ] + [ListEntry("TARGET", "Juan Dela Cruz", ListType.SANCTIONS)]
    provider = LocalListProvider(entries)
    candidates = provider.candidates(Subject("C1", "Juan de la Cruz"))
    assert candidates and candidates[0].entry_id == "TARGET"
    assert len(candidates) < 30


def test_csv_lists_load_with_only_a_name(tmp_path):
    path = tmp_path / "list.csv"
    path.write_text("name\nJuan Dela Cruz\n", encoding="utf-8")
    entries = load_csv_list(path, list_type=ListType.INTERNAL_WATCHLIST)
    assert len(entries) == 1 and entries[0].list_type is ListType.INTERNAL_WATCHLIST


def test_screening_records_a_negative_and_caches_the_positive(tmp_path):
    entries = [
        ListEntry("UN-1", "Faisal Ahmad Rahman", ListType.UN_DESIGNATED, "Designations",
                  birth_dates=("1979-06-22",))
    ]
    cache = ScreeningCache(tmp_path / "cache.db", ttl_hours=24)
    service = ScreeningService(AmlConfig(), [LocalListProvider(entries)], cache)
    parties = [
        Party("C1", full_name="Faisal Ahmad Rahman", birth_date=dt.date(1979, 6, 22)),
        Party("C2", full_name="Perfectly Ordinary Client"),
    ]
    first = service.screen_parties(parties)
    assert first[0].decision is ScreeningDecision.POTENTIAL_MATCH
    assert first[1].decision is ScreeningDecision.NO_MATCH  # recorded, not absent
    assert not any(r.from_cache for r in first)
    assert all(r.from_cache for r in service.screen_parties(parties))


def test_a_provider_failure_is_never_recorded_as_no_match():
    class Broken:
        name = "broken"

        def candidates(self, subject, limit=25):
            raise TimeoutError("vendor unavailable")

        def lists(self):
            return ()

    result = ScreeningService(AmlConfig(), [Broken()]).screen(Subject("C1", "Anyone"))
    assert result.error
    assert result.decision is ScreeningDecision.PENDING
    assert result.decision is not ScreeningDecision.NO_MATCH


def test_sanctions_hits_produce_a_critical_alert_naming_the_freeze():
    entries = [ListEntry("UN-1", "Faisal Ahmad Rahman", ListType.UN_DESIGNATED, "Designations")]
    service = ScreeningService(AmlConfig(), [LocalListProvider(entries)])
    results = [service.screen(Subject("C4", "Faisal Ahmad Rahman"))]
    alerts = alerts_from_results(results)
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert.rule_id == "screening.sanctions_match"
    assert str(alert.severity) == "CRITICAL"
    assert "ST6" in alert.st_codes
    assert "frozen without delay" in alert.narrative
    assert alert.evidence["requires_freeze"] is True


def test_pep_hits_are_not_treated_as_suspicion():
    entries = [ListEntry("PEP-1", "Corazon Mendoza Tolentino", ListType.PEP, "PEP list")]
    service = ScreeningService(AmlConfig(), [LocalListProvider(entries)])
    alerts = alerts_from_results([service.screen(Subject("C9", "Corazon Mendoza Tolentino"))])
    assert alerts[0].rule_id == "screening.pep_match"
    assert alerts[0].st_codes == ()


def test_two_hits_on_one_subject_are_one_alert_per_list_type():
    entries = [
        ListEntry("UN-1", "Faisal Ahmad Rahman", ListType.UN_DESIGNATED, "List A"),
        ListEntry("UN-2", "Faisal A. Rahman", ListType.UN_DESIGNATED, "List B"),
    ]
    service = ScreeningService(AmlConfig(), [LocalListProvider(entries)])
    alerts = alerts_from_results([service.screen(Subject("C4", "Faisal Ahmad Rahman"))])
    assert len(alerts) == 1
    assert len(alerts[0].evidence["hits"]) == 2
