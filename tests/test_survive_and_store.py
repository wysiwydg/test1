"""Tests for survivorship, the golden writer, and end-to-end composition.

Survivorship is tested for the properties that make a golden record defensible:
the declared strategy is the one applied, lineage names the winner and the
losers, and a re-run over identical input produces an identical result. The
writer is tested for idempotence and for the SCD-2 invariants the schema
promises.
"""

from __future__ import annotations

import datetime as dt
import json

import polars as pl
import pytest

from cmdm.model.enums import SurvivorshipStrategy as SS
from cmdm.model.fields import PERSON
from cmdm.model.ids import uuid7
from cmdm.store.writer import fill_required_defaults, write_entities
from cmdm.survive import SourceTrust, survive

TRUST = SourceTrust(weights={"HIGH": 0.9, "LOW": 0.2})


def contributors(rows: list[dict]) -> pl.DataFrame:
    """Contributing records for one or more master ids."""
    template = {
        "master_id": "m1",
        "source_record_id": None,
        "source_system": "HIGH",
        "source_timestamp": dt.datetime(2024, 1, 1),
    }
    filled = []
    for i, row in enumerate(rows):
        merged = {**template, **row}
        merged.setdefault("source_record_id", f"r{i}")
        merged["source_record_id"] = merged["source_record_id"] or f"r{i}"
        filled.append(merged)
    return pl.DataFrame(filled)


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------


def test_most_complete_takes_the_longest_value() -> None:
    """Recovers names truncated by a source's column width."""
    frame = contributors([
        {"full_name": "JOHN SMITH"},
        {"full_name": "JOHN MICHAEL SMITH"},
    ])
    golden, _, _ = survive(frame, PERSON, trust=TRUST)
    assert golden["full_name"][0] == "JOHN MICHAEL SMITH"


def test_most_recent_takes_the_newest_assertion() -> None:
    frame = contributors([
        {"email_address": "old@x.com", "source_timestamp": dt.datetime(2020, 1, 1)},
        {"email_address": "new@x.com", "source_timestamp": dt.datetime(2024, 1, 1)},
    ])
    golden, _, _ = survive(frame, PERSON, trust=TRUST)
    assert golden["email_address"][0] == "new@x.com"


def test_any_true_cannot_be_outvoted() -> None:
    """An opt-out lost in a merge is a compliance breach, not a blemish."""
    frame = contributors([
        {"do_not_contact": False}, {"do_not_contact": False}, {"do_not_contact": True},
    ])
    golden, _, _ = survive(frame, PERSON, trust=TRUST)
    assert golden["do_not_contact"][0] is True


def test_aggregate_min_takes_the_earliest() -> None:
    """A later feed cannot make someone a newer customer than they were."""
    frame = contributors([
        {"customer_since_date": dt.date(2015, 1, 1)},
        {"customer_since_date": dt.date(2010, 1, 1)},
    ])
    golden, _, _ = survive(frame, PERSON, trust=TRUST)
    assert golden["customer_since_date"][0] == dt.date(2010, 1, 1)


def test_most_trusted_source_wins_on_trust_not_order() -> None:
    frame = contributors([
        {"national_id_last4": "1111", "source_system": "LOW"},
        {"national_id_last4": "2222", "source_system": "HIGH"},
    ])
    golden, _, _ = survive(frame, PERSON, trust=TRUST)
    assert golden["national_id_last4"][0] == "2222"


def test_most_frequent_takes_the_modal_value() -> None:
    frame = contributors([
        {"gender": "MALE"}, {"gender": "MALE"}, {"gender": "FEMALE"},
    ])
    golden, _, _ = survive(frame, PERSON, trust=TRUST)
    assert golden["gender"][0] == "MALE"


def test_a_sparse_winner_yields_to_a_contributor_that_has_the_value() -> None:
    """The best record must not make the group survive as null.

    A record that wins the ordering but lacks this attribute should not blank
    it out; the next-best contributor supplies it.
    """
    frame = contributors([
        {"email_address": None, "source_timestamp": dt.datetime(2024, 6, 1)},
        {"email_address": "only@x.com", "source_timestamp": dt.datetime(2020, 1, 1)},
    ])
    golden, _, _ = survive(frame, PERSON, trust=TRUST)
    assert golden["email_address"][0] == "only@x.com"


def test_survivorship_is_deterministic_across_row_order() -> None:
    """A re-run over identical input must not produce a different record.

    Without a final tie-break the result depends on row order, which makes every
    downstream diff untrustworthy.
    """
    rows = [
        {"source_record_id": "a", "full_name": "JOHN SMITH", "city": "London"},
        {"source_record_id": "b", "full_name": "JANE SMITH", "city": "Leeds"},
    ]
    first, _, _ = survive(contributors(rows), PERSON, trust=TRUST)
    second, _, _ = survive(contributors(list(reversed(rows))), PERSON, trust=TRUST)
    assert first.select(sorted(first.columns)).equals(second.select(sorted(second.columns)))


def test_each_master_id_produces_one_golden_row() -> None:
    frame = contributors([
        {"master_id": "m1", "full_name": "A B"},
        {"master_id": "m1", "full_name": "A B C"},
        {"master_id": "m2", "full_name": "X Y"},
    ])
    golden, _, _ = survive(frame, PERSON, trust=TRUST)
    assert golden.height == 2


def test_source_count_records_the_evidence() -> None:
    frame = contributors([{"full_name": "A B"}, {"full_name": "A B"}])
    golden, _, _ = survive(frame, PERSON, trust=TRUST)
    assert golden["source_count"][0] == 2


def test_empty_contributor_frame() -> None:
    golden, prov, report = survive(pl.DataFrame(), PERSON, trust=TRUST)
    assert golden.height == 0
    assert report.entities == 0


def test_frame_with_no_registry_columns_is_rejected() -> None:
    frame = pl.DataFrame({"master_id": ["m1"], "unrelated": ["x"]})
    with pytest.raises(ValueError, match="none of the registry"):
        survive(frame, PERSON, trust=TRUST)


# ---------------------------------------------------------------------------
# Lineage
# ---------------------------------------------------------------------------


def test_provenance_names_the_winning_source() -> None:
    frame = contributors([
        {"source_record_id": "r_old", "email_address": "old@x.com",
         "source_timestamp": dt.datetime(2020, 1, 1)},
        {"source_record_id": "r_new", "email_address": "new@x.com",
         "source_timestamp": dt.datetime(2024, 1, 1)},
    ])
    _, prov, _ = survive(frame, PERSON, trust=TRUST)
    row = prov.filter(pl.col("attribute_name") == "email_address").row(0, named=True)
    assert row["winning_source_record_id"] == "r_new"
    assert row["strategy"] == SS.MOST_RECENT.value


def test_provenance_keeps_the_losing_candidates() -> None:
    """A contested field must be re-adjudicable without re-reading the source."""
    frame = contributors([
        {"email_address": "old@x.com", "source_timestamp": dt.datetime(2020, 1, 1)},
        {"email_address": "new@x.com", "source_timestamp": dt.datetime(2024, 1, 1)},
    ])
    _, prov, _ = survive(frame, PERSON, trust=TRUST)
    row = prov.filter(pl.col("attribute_name") == "email_address").row(0, named=True)
    rejected = json.loads(row["rejected_values"])
    assert [r["value"] for r in rejected] == ["old@x.com"]


def test_boolean_provenance_identifies_its_source() -> None:
    """Python's str(True) and Polars' "true" must still be recognized as equal."""
    frame = contributors([
        {"source_record_id": "r_false", "do_not_contact": False},
        {"source_record_id": "r_true", "do_not_contact": True},
    ])
    _, prov, _ = survive(frame, PERSON, trust=TRUST)
    row = prov.filter(pl.col("attribute_name") == "do_not_contact").row(0, named=True)
    assert row["winning_source_record_id"] == "r_true"


def test_uncontested_attributes_produce_no_provenance() -> None:
    """One row per attribute per entity would bury the decisions that matter."""
    frame = contributors([{"full_name": "A B"}, {"full_name": "A B"}])
    _, prov, _ = survive(frame, PERSON, trust=TRUST)
    assert prov.filter(pl.col("attribute_name") == "full_name").height == 0


def test_a_source_with_no_opinion_is_not_a_conflict() -> None:
    frame = contributors([{"city": "London"}, {"city": None}])
    _, prov, _ = survive(frame, PERSON, trust=TRUST)
    assert prov.filter(pl.col("attribute_name") == "city").height == 0


def test_list_columns_are_excluded_from_provenance() -> None:
    """Derived keys are recomputed, so they have no source to attribute."""
    frame = contributors([
        {"name_tokens": ["A", "B"]}, {"name_tokens": ["A", "B", "C"]},
    ])
    _, prov, _ = survive(frame, PERSON, trust=TRUST)
    assert prov.filter(pl.col("attribute_name") == "name_tokens").height == 0


def test_contested_rate_is_reported() -> None:
    frame = contributors([
        {"city": "London", "email_address": "a@x.com"},
        {"city": "Leeds", "email_address": "a@x.com"},
    ])
    _, _, report = survive(frame, PERSON, trust=TRUST)
    assert report.contested_attributes >= 1
    assert 0.0 < report.contested_rate <= 1.0


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


def golden_person(**overrides) -> pl.DataFrame:
    base = {
        "person_id": str(uuid7()), "party_type": "PERSON",
        "full_name": "JOHN SMITH", "full_name_normalized": "JOHN SMITH",
        "name_sorted_key": "JOHN SMITH", "name_phonetic_key": "JHN SMT",
        "name_tokens": [["JOHN", "SMITH"]], "name_parse_method": "RULE_BASED",
        "date_of_birth": dt.date(1980, 1, 1), "email_address": "j@x.com",
        "source_count": 1,
    }
    base.update({k: [v] if not isinstance(v, list) else v for k, v in overrides.items()})
    return pl.DataFrame({
        k: (v if isinstance(v, list) else [v]) for k, v in base.items()
    })


def test_missing_not_null_columns_get_defaults() -> None:
    """Stages legitimately produce partial frames; the schema still requires them."""
    filled = fill_required_defaults(golden_person(), PERSON)
    assert filled["is_deceased"][0] is False
    assert filled["policy_count"][0] == 0


def test_a_missing_non_nullable_text_column_is_an_error() -> None:
    """Papering over it with an empty string would be indistinguishable from data."""
    frame = pl.DataFrame({"person_id": [str(uuid7())]})
    with pytest.raises(ValueError, match="NOT NULL with no default"):
        fill_required_defaults(frame, PERSON)


def test_first_write_inserts(conn) -> None:
    report = write_entities(conn, golden_person(), PERSON)
    assert report.inserted == 1
    assert report.versioned == 0


def test_rewriting_identical_data_is_a_no_op(conn) -> None:
    """Idempotence: a re-run must not manufacture a version."""
    frame = golden_person()
    write_entities(conn, frame, PERSON)
    report = write_entities(conn, frame, PERSON)
    assert report.unchanged == 1
    assert report.changed == 0


def test_a_change_closes_the_old_version_and_opens_a_new_one(conn) -> None:
    frame = golden_person()
    write_entities(conn, frame, PERSON)
    changed = frame.with_columns(pl.lit("new@x.com").alias("email_address"))
    report = write_entities(conn, changed, PERSON)
    assert report.versioned == 1

    person_id = frame["person_id"][0]
    rows = conn.execute(
        "SELECT version, is_current, valid_to IS NULL, email_address FROM mdm.person "
        "WHERE person_id = %s ORDER BY version", (person_id,)
    ).fetchall()
    assert [r[0] for r in rows] == [1, 2]
    assert [r[1] for r in rows] == [False, True]
    assert rows[1][3] == "new@x.com"


def test_exactly_one_current_version_survives(conn) -> None:
    """The partial unique index makes this a guarantee, not a convention."""
    frame = golden_person()
    write_entities(conn, frame, PERSON)
    for value in ("a@x.com", "b@x.com", "c@x.com"):
        write_entities(conn, frame.with_columns(pl.lit(value).alias("email_address")), PERSON)

    count = conn.execute(
        "SELECT count(*) FROM mdm.person WHERE person_id = %s AND is_current",
        (frame["person_id"][0],),
    ).fetchone()[0]
    assert count == 1


def test_created_at_is_preserved_across_versions(conn) -> None:
    """The identity's birth date must not be rewritten by each update."""
    frame = golden_person()
    write_entities(conn, frame, PERSON)
    write_entities(conn, frame.with_columns(pl.lit("z@x.com").alias("email_address")), PERSON)
    created = conn.execute(
        "SELECT count(DISTINCT created_at) FROM mdm.person WHERE person_id = %s",
        (frame["person_id"][0],),
    ).fetchone()[0]
    assert created == 1


def test_anchor_row_is_created(conn) -> None:
    """Referential integrity targets the anchor, so it must exist first."""
    frame = golden_person()
    write_entities(conn, frame, PERSON)
    assert conn.execute(
        "SELECT count(*) FROM mdm.person_master WHERE person_id = %s",
        (frame["person_id"][0],),
    ).fetchone()[0] == 1


def test_array_columns_round_trip(conn) -> None:
    frame = golden_person()
    write_entities(conn, frame, PERSON)
    tokens = conn.execute(
        "SELECT name_tokens FROM mdm.person WHERE person_id = %s AND is_current",
        (frame["person_id"][0],),
    ).fetchone()[0]
    assert tokens == ["JOHN", "SMITH"]


def test_enum_columns_round_trip(conn) -> None:
    frame = golden_person()
    write_entities(conn, frame, PERSON)
    party_type = conn.execute(
        "SELECT party_type FROM mdm.person WHERE person_id = %s AND is_current",
        (frame["person_id"][0],),
    ).fetchone()[0]
    assert party_type == "PERSON"


def test_writing_an_empty_frame(conn) -> None:
    assert write_entities(conn, pl.DataFrame(), PERSON).changed == 0


def test_frame_without_the_key_column_is_rejected(conn) -> None:
    """The writer needs the surrogate key to version against."""
    with pytest.raises(ValueError, match="person_id"):
        write_entities(conn, pl.DataFrame({"full_name": ["X"]}), PERSON)


# ---------------------------------------------------------------------------
# End to end
# ---------------------------------------------------------------------------


def test_pipeline_runs_every_stage(conn) -> None:
    """The seams are where the interesting failures live."""
    import pathlib

    from cmdm.ingest.mapping import load_mapping
    from cmdm.pipeline import run_pipeline

    repo = pathlib.Path(__file__).resolve().parent.parent
    mapping = load_mapping(repo / "src" / "cmdm" / "mappings" / "life_admin.toml")

    from tests.test_queue_and_landing import raw as _raw  # noqa: F401

    n = 6
    raw = pl.DataFrame({
        "PolicyNumber": [f"POL-{i:06d}" for i in range(n)],
        "ProductCode": ["TL10"] * n, "ProductName": ["Term Life"] * n,
        "ProductLine": ["LIFE"] * n, "PlanCode": ["P1"] * n,
        "Status": ["In Force"] * n, "IssuingCompany": ["ACME"] * n,
        "BranchCode": ["BR1"] * n, "Channel": ["BROKER"] * n, "IssueState": ["GB"] * n,
        "UwClass": ["STANDARD"] * n, "PaymentMethod": ["DD"] * n,
        "PremiumFrequency": ["M"] * n, "ApplicationDate": ["2020-01-01"] * n,
        "IssueDate": ["2020-02-01"] * n, "EffectiveDate": ["2020-03-01"] * n,
        "MaturityDate": ["2040-03-01"] * n, "TerminationDate": [""] * n,
        "PaidToDate": ["2021-03-01"] * n, "SumAssured": ["100000"] * n,
        "AnnualPremium": ["1200"] * n, "ModalPremium": ["100"] * n,
        "AccountValue": ["0"] * n, "PolicyTerm": ["20"] * n, "PremiumTerm": ["20"] * n,
        "LastUpdatedTs": ["2024-01-01"] * n,
        # Two source ids for the same human, which is what resolution must find.
        "OwnerCustomerId": ["C-1", "C-1", "C-2", "C-2", "C-3", "C-3"],
        "OwnerName": ["John Smith", "SMITH, JOHN", "Jane Doe", "Jane Doe",
                      "Priya Patel", "Priya Patel"],
        "OwnerDOB": ["1980-05-01"] * 2 + ["1975-01-01"] * 2 + ["1990-09-09"] * 2,
        "OwnerGender": ["M"] * 6, "OwnerEmail": ["j@x.com"] * 2 + ["d@x.com"] * 2 + ["p@x.com"] * 2,
        "OwnerPhone": ["020 7946 0958"] * 6,
        "OwnerAddress1": ["12 High St"] * 6, "OwnerAddress2": [""] * 6,
        "OwnerCity": ["London"] * 6, "OwnerPostcode": ["SW1A 1AA"] * 6,
        "OwnerCountry": ["GB"] * 6, "OwnerOccupation": ["Engineer"] * 6,
        "InsuredCustomerId": ["C-1", "C-1", "C-2", "C-2", "C-3", "C-3"],
        "InsuredName": ["John Smith"] * 2 + ["Jane Doe"] * 2 + ["Priya Patel"] * 2,
        "InsuredDOB": ["1980-05-01"] * 2 + ["1975-01-01"] * 2 + ["1990-09-09"] * 2,
        "InsuredGender": ["M"] * 6,
        "InsuredEmail": ["j@x.com"] * 2 + ["d@x.com"] * 2 + ["p@x.com"] * 2,
        "InsuredPhone": ["020 7946 0958"] * 6,
        "InsuredAddress1": ["12 High St"] * 6, "InsuredAddress2": [""] * 6,
        "InsuredCity": ["London"] * 6, "InsuredPostcode": ["SW1A 1AA"] * 6,
        "InsuredCountry": ["GB"] * 6, "InsuredOccupation": ["Engineer"] * 6,
        "AgentCode": ["AGT-1"] * 6, "AgentName": ["Jane Broker"] * 6,
        "AgentEmail": ["b@x.com"] * 6, "AgentPhone": ["+44 20 1111 2222"] * 6,
    })

    # Measured as a delta rather than a total: the store is shared with other
    # tests and with whatever a developer left behind, and asserting on a global
    # count really asserts that nothing else has ever run.
    before = conn.execute("SELECT count(*) FROM mdm.person WHERE is_current").fetchone()[0]

    result = run_pipeline(conn, raw, mapping, trust=TRUST)

    assert result.policies == 6
    assert result.source_identities > 0
    assert result.golden_persons > 0
    # Resolution must collapse something: the same party appears under two
    # namespaces and across several policies.
    assert result.golden_persons < result.source_identities
    assert result.writes["person"]["inserted"] == result.golden_persons
    assert result.xref_rows > 0

    after = conn.execute("SELECT count(*) FROM mdm.person WHERE is_current").fetchone()[0]
    assert after - before == result.golden_persons


def test_pipeline_can_run_without_writing(conn) -> None:
    """Dry-run for tuning: report the outcome without touching the store."""
    import pathlib

    from cmdm.ingest.mapping import load_mapping
    from cmdm.pipeline import run_pipeline

    repo = pathlib.Path(__file__).resolve().parent.parent
    mapping = load_mapping(repo / "src" / "cmdm" / "mappings" / "life_admin.toml")
    empty = pl.DataFrame({c: [] for c in mapping.source_columns}, schema={
        c: pl.String for c in mapping.source_columns
    })
    result = run_pipeline(conn, empty, mapping, write=False)
    assert result.writes == {}
