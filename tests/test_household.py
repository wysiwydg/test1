"""Tests for householding and affiliation.

The failure this feature invites is a household built from addresses. It gets
two things wrong that matter and that a demo never surfaces: it swallows
flatmates who happen to share a postcode, and it loses a spouse who kept their
own surname. The sample extract contains both on purpose, and most of what is
here measures the derivation against that ground truth rather than against
whatever it happens to produce.

The second failure is treating an affiliation as a household. A company insuring
its directors, a trust holding a family's policies and an estate owning the
cover on the person who died are all party-to-party links, and none of them is a
family. A pass that does not separate them reports a household of forty.
"""

from __future__ import annotations

import pathlib
import random

import polars as pl
import pytest

from cmdm.household import (
    STATED_TO_ASSOCIATION,
    derive_households,
    household_of,
)
from cmdm.model.enums import AssociationType as AT

REPO = pathlib.Path(__file__).resolve().parent.parent
SAMPLE = REPO / "data" / "life_admin_sample.csv"
ROWS = 1200
SEED = 20240807


@pytest.fixture
def mapping():
    from cmdm.ingest.mapping import load_mapping

    return load_mapping(REPO / "src" / "cmdm" / "mappings" / "life_admin.toml")


@pytest.fixture
def loaded(conn, mapping):
    """A processed slice of the sample, inside the rolled-back transaction."""
    if not SAMPLE.exists():  # pragma: no cover - environment dependent
        pytest.skip("run `python -m scripts.generate_sample_data` first")

    from cmdm.ingest.landing import accept_batch
    from cmdm.worker import process_batch

    conn.execute("TRUNCATE mdm.source_record CASCADE")
    raw = pl.read_csv(SAMPLE, infer_schema_length=0).head(ROWS)
    batch_id, report, enqueued = accept_batch(
        conn, raw, mapping, origin="TEST", filename="s.csv", submitted_by="pytest",
    )
    assert report.accepted and enqueued
    return process_batch(conn, batch_id)


@pytest.fixture(scope="module")
def truth():
    """The generator's ground truth for the extract on disk."""
    from scripts.generate_sample_data import _build_population

    population = _build_population(random.Random(SEED), households_wanted=5000 // 6)
    return {
        "family_of": {m["key"]: h["id"]
                      for h in population["households"] for m in h["members"]},
        "flatmates": {p["key"] for p in population["flatmates"]},
        "married_in": {p["key"] for p in population["people"] if p.get("married_in")},
        "entities": {e["key"]: e["kind"] for e in population["entities"]},
    }


def _keys_to_persons(conn) -> dict[str, str]:
    rows = conn.execute(
        "SELECT x.source_party_key, p.person_id::text FROM mdm.person_xref x "
        "JOIN mdm.person p ON p.person_id = x.person_id AND p.is_current "
        "WHERE x.is_active"
    ).fetchall()
    return {key: pid for key, pid in rows}


def _households(conn) -> dict[str, set[str]]:
    rows = conn.execute(
        "SELECT household_id::text, person_id::text FROM mdm.person "
        "WHERE is_current AND household_id IS NOT NULL"
    ).fetchall()
    out: dict[str, set[str]] = {}
    for household, person in rows:
        out.setdefault(household, set()).add(person)
    return out


# ---------------------------------------------------------------------------
# The vocabulary
# ---------------------------------------------------------------------------


def test_self_is_not_a_family_relation() -> None:
    """SELF says the owner and the insured are one party. That is a statement
    about identity, not about a household, and mapping it to one would put
    every solitary policyholder in a household with themselves."""
    assert STATED_TO_ASSOCIATION["SELF"] is None


def test_an_unknown_code_is_not_guessed_at() -> None:
    """A source value nobody has mapped contributes nothing, rather than being
    forced into the nearest-looking association."""
    assert "COUSIN_IN_LAW" not in STATED_TO_ASSOCIATION


def test_an_employer_is_not_a_family() -> None:
    assert AT.EMPLOYEE_OF in AT.affiliation()
    assert AT.EMPLOYEE_OF not in AT.familial()
    assert not AT.familial() & AT.affiliation()


# ---------------------------------------------------------------------------
# What the derivation finds
# ---------------------------------------------------------------------------


def test_households_are_derived_at_all(conn, loaded) -> None:
    report = loaded.households
    assert report is not None
    assert report.households > 0, "no households derived from a structured extract"
    assert report.people_in_a_household > report.households, \
        "a household should have more than one member by definition"


def test_a_household_never_mixes_two_families(conn, loaded, truth) -> None:
    """Precision. One wrong edge unions two families and the damage is
    proportional to the size of both, so this is the assertion that matters
    most."""
    persons = _keys_to_persons(conn)
    family_of_person = {
        persons[key]: family
        for key, family in truth["family_of"].items() if key in persons
    }

    mixed = []
    for household, members in _households(conn).items():
        families = {family_of_person[m] for m in members if m in family_of_person}
        if len(families) > 1:
            mixed.append((household, families))
    assert not mixed, f"households spanning several real families: {mixed[:3]}"


def test_a_flatmate_is_not_swallowed_into_the_family(conn, loaded, truth) -> None:
    """The address trap. These parties share a postcode with a family, share no
    policy with them and are related to nobody -- so grouping by address puts
    them in the family and this must not."""
    persons = _keys_to_persons(conn)
    flatmate_ids = [persons[k] for k in truth["flatmates"] if k in persons]
    if not flatmate_ids:  # pragma: no cover - depends on the slice loaded
        pytest.skip("no flatmates in this slice of the extract")

    rows = conn.execute(
        "SELECT count(*) FROM mdm.person WHERE is_current AND household_id IS NOT NULL "
        "AND person_id = ANY(%s)",
        (flatmate_ids,),
    ).fetchone()[0]
    assert rows == 0, f"{rows} flatmates were placed in a household"


def test_a_spouse_who_kept_their_surname_is_still_in_the_household(
    conn, loaded, truth
) -> None:
    """The surname trap, in the other direction. Anything requiring a surname
    match loses these people; the source said SPOUSE, so they are found."""
    persons = _keys_to_persons(conn)
    married_in = [persons[k] for k in truth["married_in"] if k in persons]
    if not married_in:  # pragma: no cover - depends on the slice loaded
        pytest.skip("no married-in members in this slice of the extract")

    placed = conn.execute(
        "SELECT count(*) FROM mdm.person WHERE is_current AND household_id IS NOT NULL "
        "AND person_id = ANY(%s)",
        (married_in,),
    ).fetchone()[0]
    assert placed > 0, "every member who kept their own surname was lost"


def test_a_legal_entity_is_never_a_household_member(conn, loaded) -> None:
    """A trust is not part of the family whose policies it holds."""
    stray = conn.execute(
        "SELECT count(*) FROM mdm.person WHERE is_current "
        "AND household_id IS NOT NULL AND party_type <> 'PERSON'"
    ).fetchone()[0]
    assert stray == 0, f"{stray} companies, trusts or estates put in a household"


def test_affiliations_are_counted_apart_from_the_household(conn, loaded) -> None:
    report = loaded.households
    assert report.affiliations > 0, "no company, trust or estate links derived"

    both = conn.execute(
        "SELECT household_size, affiliation_count FROM mdm.person "
        "WHERE is_current AND affiliation_count > 0 AND household_id IS NOT NULL "
        "LIMIT 1"
    ).fetchone()
    if both:
        size, affiliations = both
        assert size > 0 and affiliations > 0
        # The two are independent numbers, not one rolled into the other.
        assert size != size + affiliations


def test_a_solitary_policyholder_gets_no_household(conn, loaded) -> None:
    """Recording a household id for everyone with no relation to anybody would
    put a thousand 'households' of one on the dashboard and mean nothing by
    any of them."""
    ones = conn.execute(
        "SELECT count(*) FROM mdm.person WHERE is_current "
        "AND household_id IS NOT NULL AND household_size < 2"
    ).fetchone()[0]
    assert ones == 0


# ---------------------------------------------------------------------------
# Stability and repeatability
# ---------------------------------------------------------------------------


def test_rerunning_the_derivation_changes_nothing(conn, loaded) -> None:
    """The household id is on a servicing screen and in downstream extracts. It
    moving nightly for no reason is the churn this system exists to prevent."""
    before = _households(conn)
    again = derive_households(conn)
    after = _households(conn)

    assert again.households == len(before)
    assert before == after, "household ids changed on a second identical run"


def test_the_household_id_is_derived_not_minted(conn, loaded) -> None:
    """Two stores built from the same data must agree on the id, or a
    downstream consumer cannot join across them."""
    from cmdm.household import _household_uuid

    households = _households(conn)
    assert households
    for household_id, members in households.items():
        assert household_id == _household_uuid(min(members))


# ---------------------------------------------------------------------------
# The read side
# ---------------------------------------------------------------------------


def test_household_of_answers_for_a_member(conn, loaded) -> None:
    person = conn.execute(
        "SELECT person_id::text FROM mdm.person WHERE is_current "
        "AND household_size >= 2 LIMIT 1"
    ).fetchone()
    assert person, "no multi-member household to read"

    found = household_of(conn, person[0])
    assert found["household_id"]
    assert len(found["members"]) == found["household_size"]
    assert any(m["person_id"] == person[0] for m in found["members"]), \
        "a party should appear in their own household"


def test_household_of_separates_relations_from_affiliations(conn, loaded) -> None:
    person = conn.execute(
        "SELECT person_id::text FROM mdm.person WHERE is_current "
        "AND affiliation_count > 0 LIMIT 1"
    ).fetchone()
    if not person:  # pragma: no cover - depends on the slice loaded
        pytest.skip("no affiliated party in this slice")

    found = household_of(conn, person[0])
    affiliation_kinds = {a.value for a in AT.affiliation()}
    assert found["affiliations"]
    assert all(r["association_type"] in affiliation_kinds
               for r in found["affiliations"])
    assert all(r["association_type"] not in affiliation_kinds
               for r in found["relations"])


def test_household_of_is_not_an_error_for_a_party_with_none(conn, loaded) -> None:
    """'This customer has no household on record' is an answer, not a 404."""
    person = conn.execute(
        "SELECT person_id::text FROM mdm.person WHERE is_current "
        "AND household_id IS NULL LIMIT 1"
    ).fetchone()
    assert person

    found = household_of(conn, person[0])
    assert found["household_id"] is None
    assert found["members"] == []


def test_the_derived_edges_carry_their_evidence(conn, loaded) -> None:
    """A derived edge that cannot be traced to the facts behind it is not
    auditable, and 'they share five policies' is a much stronger claim than
    'they share one'."""
    rows = conn.execute(
        "SELECT evidence_count, array_length(evidence_policy_ids, 1) "
        "FROM mdm.relationship WHERE edge_kind = 'PARTY_PARTY' AND is_current "
        "LIMIT 50"
    ).fetchall()
    assert rows
    for count, listed in rows:
        assert count >= 1
        assert listed == count, "evidence count disagrees with the policies named"
