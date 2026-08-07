"""Tests for the vectorized ingestion layer.

The normalization tests are written as behaviour statements rather than golden
strings wherever possible: what matters is not that a name normalizes to one
exact form, but that two spellings of the same person *collide* and two
different people do not. A test asserting the exact output would fail on any
harmless rule change; a test asserting the collision fails only when the
property that matters breaks.
"""

from __future__ import annotations

import pathlib

import polars as pl
import pytest

from cmdm.ingest import normalize as N
from cmdm.ingest.mapping import (
    FieldMapping,
    SourceMapping,
    Transform,
    load_mapping,
    parse_mapping,
)
from cmdm.ingest.shred import POLICY_SCOPED_COLUMNS, collapse_parties, shred
from cmdm.model.enums import PartyRole

REPO = pathlib.Path(__file__).resolve().parent.parent
LIFE_ADMIN = REPO / "src" / "cmdm" / "mappings" / "life_admin.toml"


def col(values: list[str | None]) -> pl.DataFrame:
    return pl.DataFrame({"x": values}, schema={"x": pl.String})


def apply(values: list[str | None], expr: pl.Expr) -> list:
    return col(values).select(expr.alias("out"))["out"].to_list()


# ---------------------------------------------------------------------------
# Text and name normalization
# ---------------------------------------------------------------------------


def test_normalize_text_folds_accents_and_case() -> None:
    assert apply(["José  Peña"], N.normalize_text(pl.col("x"))) == ["JOSE PENA"]


def test_normalize_text_expands_rather_than_drops_eszett() -> None:
    """STRASSE and STRAßE must not block apart."""
    out = apply(["Straße", "STRASSE"], N.normalize_text(pl.col("x")))
    assert out[0] == out[1] == "STRASSE"


def test_normalize_text_splits_on_punctuation_rather_than_deleting_it() -> None:
    """Double-barrelled names must tokenize into their halves.

    Other sources routinely record the two parts separately, so fusing them
    into one token would block the same person apart.
    """
    assert apply(["Smith-Jones"], N.normalize_text(pl.col("x"))) == ["SMITH JONES"]


def test_normalize_full_name_strips_stacked_honorifics_and_suffixes() -> None:
    out = apply(["Mr Dr John Smith Jr PhD"], N.normalize_full_name(pl.col("x")))
    assert out == ["JOHN SMITH"]


def test_name_prefix_and_suffix_are_retained_not_discarded() -> None:
    """A JR/SR difference is evidence of two people, so it must survive."""
    norm = N.normalize_text(pl.col("x"))
    assert apply(["Dr John Smith Jr"], N.extract_name_prefix(norm)) == ["DR"]
    assert apply(["Dr John Smith Jr"], N.extract_name_suffix(norm)) == ["JR"]


def test_sorted_key_collides_reordered_names() -> None:
    """The most common disagreement between feeds is name order."""
    frame = col(["John Michael Smith", "SMITH, JOHN MICHAEL", "Mr John Michael Smith"])
    out = frame.with_columns(n=N.normalize_full_name(pl.col("x"))).with_columns(
        t=N.name_tokens(pl.col("n"))
    ).with_columns(k=N.name_sorted_key(pl.col("t")))
    assert out["k"].n_unique() == 1


def test_sorted_key_still_separates_different_people() -> None:
    frame = col(["John Smith", "Jane Smith"])
    out = frame.with_columns(n=N.normalize_full_name(pl.col("x"))).with_columns(
        t=N.name_tokens(pl.col("n"))
    ).with_columns(k=N.name_sorted_key(pl.col("t")))
    assert out["k"].n_unique() == 2


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Katherine Phillips", "Catherine Filips"),
        ("Smith", "Smyth"),
        ("Jon Kowalski", "John Kovalski"),
        ("Steven Clarke", "Stephen Clark"),
    ],
)
def test_phonetic_key_collides_sound_alike_spellings(left: str, right: str) -> None:
    """Recall on transcription errors is the whole purpose of this key."""
    frame = col([left, right]).with_columns(n=N.normalize_full_name(pl.col("x")))
    keys = frame.select(N.name_phonetic_key(pl.col("n")).alias("k"))["k"].to_list()
    assert keys[0] == keys[1], f"{left!r} and {right!r} produced {keys}"


def test_phonetic_key_still_separates_unrelated_names() -> None:
    """A blocking key that collides everything is worthless."""
    frame = col(["John Smith", "Priya Patel", "Ahmed Nguyen"]).with_columns(
        n=N.normalize_full_name(pl.col("x"))
    )
    keys = frame.select(N.name_phonetic_key(pl.col("n")).alias("k"))["k"].to_list()
    assert len(set(keys)) == 3


def test_phonetic_key_is_order_insensitive() -> None:
    frame = col(["John Smith", "Smith John"]).with_columns(
        n=N.normalize_full_name(pl.col("x"))
    )
    keys = frame.select(N.name_phonetic_key(pl.col("n")).alias("k"))["k"].to_list()
    assert keys[0] == keys[1]


def test_name_tokens_drops_empty_tokens() -> None:
    """A stray separator must not shift the sorted key."""
    out = col(["John   Smith"]).with_columns(n=N.normalize_full_name(pl.col("x"))).select(
        N.name_tokens(pl.col("n")).alias("t")
    )["t"].to_list()
    assert out == [["JOHN", "SMITH"]]


# ---------------------------------------------------------------------------
# Party type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("John Smith", "PERSON"),
        ("The Smith Family Trust", "TRUST"),
        ("Estate of Mary O'Brien", "ESTATE"),
        ("ACME Insurance Brokers Pty Ltd", "ORGANIZATION"),
        ("Smith Holdings Limited", "ORGANIZATION"),
        ("", "UNKNOWN"),
    ],
)
def test_detect_party_type(name: str, expected: str) -> None:
    """Trusts and estates outrank the general organization vocabulary."""
    out = col([name]).with_columns(n=N.normalize_full_name(pl.col("x"))).with_columns(
        t=N.name_tokens(pl.col("n"))
    ).select(N.detect_party_type(pl.col("t")).alias("p"))["p"].to_list()
    assert out == [expected]


def test_org_tokens_match_whole_tokens_not_substrings() -> None:
    """INCLEDON must not be read as INC."""
    out = col(["Mary Incledon"]).with_columns(n=N.normalize_full_name(pl.col("x"))).with_columns(
        t=N.name_tokens(pl.col("n"))
    ).select(N.detect_party_type(pl.col("t")).alias("p"))["p"].to_list()
    assert out == ["PERSON"]


# ---------------------------------------------------------------------------
# Contact details
# ---------------------------------------------------------------------------


def test_normalize_email_rejects_non_addresses() -> None:
    """Placeholders must be nulled, not kept.

    A blocking key built from "N/A" would collide every record carrying one
    into a single enormous block, which is how a blocking pass goes quadratic.
    """
    out = apply(["N/A", "not-an-email", "", None, "A@B.co"], N.normalize_email(pl.col("x")))
    assert out == [None, None, None, None, "a@b.co"]


def test_normalize_email_does_not_fold_provider_aliases() -> None:
    """Dot- and plus-folding is provider-specific and wrong for most domains.

    At a corporate mail server a.smith@ and asmith@ are routinely two people.
    """
    out = apply(["a.smith@corp.com", "asmith@corp.com"], N.normalize_email(pl.col("x")))
    assert out[0] != out[1]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("+44 20 7946 0958", "+442079460958"),
        ("0044 20 7946 0958", "+442079460958"),
        ("020 7946 0958", "+442079460958"),
        ("(020) 7946-0958", "+442079460958"),
    ],
)
def test_normalize_phone_reaches_the_same_e164(raw: str, expected: str) -> None:
    assert apply([raw], N.normalize_phone(pl.col("x"), "44")) == [expected]


def test_normalize_phone_nulls_values_too_short_to_be_numbers() -> None:
    assert apply(["1234", "x", ""], N.normalize_phone(pl.col("x"), "44")) == [None, None, None]


# ---------------------------------------------------------------------------
# Policy number
# ---------------------------------------------------------------------------


def test_policy_number_variants_normalize_together() -> None:
    out = apply(
        ["POL-00001234", "pol 1234", "POL1234", "  pol-1234  "],
        N.normalize_policy_number(pl.col("x")),
    )
    assert len(set(out)) == 1, out


def test_policy_number_keeps_the_alphabetic_prefix() -> None:
    """Different books distinguished only by prefix must not collide."""
    out = apply(["POL-1234", "ABC-1234"], N.normalize_policy_number(pl.col("x")))
    assert out[0] != out[1]


def test_policy_number_strips_leading_zeros_only_from_the_numeric_tail() -> None:
    assert apply(["POL0001234"], N.normalize_policy_number(pl.col("x"))) == ["POL1234"]


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------


def test_address_key_survives_unit_and_street_number_reordering() -> None:
    """The same door written two ways must block together.

    This is the case that a leading-number key gets wrong, which is why the key
    uses the sorted set of all numeric tokens.
    """
    frame = pl.DataFrame({
        "l1": ["Flat 2, 12 High Street", "12 HIGH ST APT 2"],
        "pc": ["SW1A 1AA", "sw1a1aa"],
    })
    out = frame.with_columns(n=N.normalize_address(pl.col("l1"))).with_columns(
        k=N.address_key(pl.col("n"), pl.col("pc"))
    )
    assert out["k"][0] == out["k"][1]


def test_address_key_separates_different_doors_on_one_street() -> None:
    frame = pl.DataFrame({"l1": ["12 High Street", "14 High Street"], "pc": ["SW1A 1AA"] * 2})
    out = frame.with_columns(n=N.normalize_address(pl.col("l1"))).with_columns(
        k=N.address_key(pl.col("n"), pl.col("pc"))
    )
    assert out["k"][0] != out["k"][1]


def test_address_key_is_null_without_a_number_or_postcode() -> None:
    """A key that is present-but-empty would form one giant block."""
    frame = pl.DataFrame({"l1": ["No Number Lane", "12 High St"], "pc": ["2000", ""]})
    out = frame.with_columns(n=N.normalize_address(pl.col("l1"))).with_columns(
        k=N.address_key(pl.col("n"), pl.col("pc"))
    )
    assert out["k"].to_list() == [None, None]


def test_address_normalization_folds_thoroughfare_types() -> None:
    frame = pl.DataFrame({"l1": ["12 High Street", "12 HIGH ST"]})
    out = frame.select(N.normalize_address(pl.col("l1")).alias("n"))
    assert out["n"][0] == out["n"][1]


# ---------------------------------------------------------------------------
# Enums and dates
# ---------------------------------------------------------------------------


def test_normalize_enum_maps_known_values_and_defaults_the_rest() -> None:
    """An unmapped value must surface as UNKNOWN, not stop the pipeline."""
    out = apply(
        ["In Force", "LAPSED", "brand new status", None],
        N.normalize_enum(pl.col("x"), {"IN_FORCE": "INFORCE", "LAPSED": "LAPSED"}),
    )
    assert out == ["INFORCE", "LAPSED", "UNKNOWN", "UNKNOWN"]


def test_parse_date_accepts_each_configured_format() -> None:
    out = apply(["2024-03-01", "01/03/2024", "20240301"], N.parse_date(pl.col("x")))
    assert len(set(out)) == 1


def test_parse_date_nulls_junk_instead_of_failing_the_batch() -> None:
    assert apply(["nonsense"], N.parse_date(pl.col("x"))) == [None]


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------


def test_life_admin_mapping_loads() -> None:
    mapping = load_mapping(LIFE_ADMIN)
    assert mapping.source_system == "LIFE_ADMIN"
    assert {p.role for p in mapping.parties} == {
        PartyRole.OWNER, PartyRole.INSURED, PartyRole.AGENT
    }


def test_mapping_rejects_unknown_canonical_field() -> None:
    with pytest.raises(ValueError, match="not a field of Policy"):
        parse_mapping({
            "source_system": "S",
            "policy": {"policy_number": "P", "nonexistent_field": "X"},
            "party": [{"role": "OWNER", "key_field": "K", "key_kind": "OWNER_CUSTOMER_ID",
                       "fields": {"full_name": "N"}}],
        })


def test_mapping_rejects_derived_field_as_a_target() -> None:
    """A source cannot supply a value the pipeline computes."""
    with pytest.raises(ValueError, match="derived"):
        parse_mapping({
            "source_system": "S",
            "policy": {"policy_number": "P"},
            "party": [{"role": "OWNER", "key_field": "K", "key_kind": "OWNER_CUSTOMER_ID",
                       "fields": {"full_name": "N", "name_phonetic_key": "X"}}],
        })


def test_mapping_requires_a_policy_number() -> None:
    with pytest.raises(ValueError, match="policy_number"):
        parse_mapping({
            "source_system": "S", "policy": {"product_code": "P"},
            "party": [{"role": "OWNER", "key_field": "K", "key_kind": "K",
                       "fields": {"full_name": "N"}}],
        })


def test_mapping_requires_a_name_on_every_party_block() -> None:
    """A party with no name can be neither resolved nor reviewed."""
    with pytest.raises(ValueError, match="full_name"):
        parse_mapping({
            "source_system": "S", "policy": {"policy_number": "P"},
            "party": [{"role": "OWNER", "key_field": "K", "key_kind": "K", "fields": {}}],
        })


def test_mapping_rejects_duplicate_role_and_sequence() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        parse_mapping({
            "source_system": "S", "policy": {"policy_number": "P"},
            "party": [
                {"role": "INSURED", "key_field": "A", "key_kind": "K",
                 "fields": {"full_name": "N1"}},
                {"role": "INSURED", "key_field": "B", "key_kind": "K",
                 "fields": {"full_name": "N2"}},
            ],
        })


def test_mapping_allows_a_repeated_role_with_distinct_sequence() -> None:
    mapping = parse_mapping({
        "source_system": "S", "policy": {"policy_number": "P"},
        "party": [
            {"role": "INSURED", "key_field": "A", "key_kind": "K",
             "fields": {"full_name": "N1"}, "role_sequence": 1},
            {"role": "INSURED", "key_field": "B", "key_kind": "K",
             "fields": {"full_name": "N2"}, "role_sequence": 2},
        ],
    })
    assert len(mapping.parties) == 2


def test_field_mapping_rejects_unknown_transform() -> None:
    with pytest.raises(ValueError, match="unknown transform"):
        FieldMapping(canonical="policy_number", source="P", transform="nope")


def test_field_mapping_requires_exactly_one_value_origin() -> None:
    with pytest.raises(ValueError, match="either"):
        FieldMapping(canonical="x")
    with pytest.raises(ValueError, match="mutually exclusive"):
        FieldMapping(canonical="x", source="a", literal="b")


def test_enum_transform_requires_a_vocabulary() -> None:
    with pytest.raises(ValueError, match="vocabulary"):
        FieldMapping(canonical="policy_status", source="S", transform=Transform.ENUM)


def test_missing_columns_are_reported_by_name() -> None:
    mapping = load_mapping(LIFE_ADMIN)
    missing = mapping.missing_columns(["PolicyNumber"])
    assert "OwnerName" in missing
    assert "PolicyNumber" not in missing


# ---------------------------------------------------------------------------
# Shredding
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def raw_batch() -> pl.DataFrame:
    """A small batch exercising the shapes the shredder must handle."""
    return pl.DataFrame({
        "PolicyNumber": ["POL-0001", "pol 2", "POL-0001"],
        "ProductCode": ["TL10", "WL01", "TL10"],
        "ProductName": ["Term Life", "Whole Life", "Term Life"],
        "ProductLine": ["LIFE", "LIFE", "LIFE"],
        "PlanCode": ["P1", "P2", "P1"],
        "Status": ["In Force", "LAPSED", "In Force"],
        "IssuingCompany": ["ACME"] * 3,
        "BranchCode": ["BR1"] * 3,
        "Channel": ["BROKER"] * 3,
        "IssueState": ["GB"] * 3,
        "UwClass": ["STANDARD"] * 3,
        "PaymentMethod": ["DD"] * 3,
        "PremiumFrequency": ["M", "A", "M"],
        "ApplicationDate": ["2020-01-01"] * 3,
        "IssueDate": ["2020-02-01"] * 3,
        "EffectiveDate": ["2020-03-01"] * 3,
        "MaturityDate": ["2040-03-01"] * 3,
        "TerminationDate": ["", "2022-01-01", ""],
        "PaidToDate": ["2021-03-01"] * 3,
        "SumAssured": ["100000.00", "£250,000.00", "100000.00"],
        "AnnualPremium": ["1200.50", "£2,400.00", "1200.50"],
        "ModalPremium": ["100.04", "2400.00", "100.04"],
        "AccountValue": ["0", "0", "0"],
        "PolicyTerm": ["20", "25", "20"],
        "PremiumTerm": ["20", "25", "20"],
        "LastUpdatedTs": ["2024-01-01"] * 3,
        "OwnerCustomerId": ["C-1", "C-2", "C-1"],
        "OwnerName": ["Mr John Smith", "The Patel Family Trust", "SMITH, JOHN"],
        "OwnerDOB": ["1980-05-01", "", "1980-05-01"],
        "OwnerGender": ["M", "", "M"],
        "OwnerEmail": ["john.smith@example.com", "", "john.smith@example.com"],
        "OwnerPhone": ["020 7946 0958", "", "020 7946 0958"],
        "OwnerAddress1": ["12 High Street", "1 Mill Road", "Flat 2, 12 High Street"],
        "OwnerAddress2": ["", "", ""],
        "OwnerCity": ["London", "Leeds", "London"],
        "OwnerPostcode": ["SW1A 1AA", "LS1 1AA", "sw1a1aa"],
        "OwnerCountry": ["GB"] * 3,
        "OwnerOccupation": ["Engineer", "", "Engineer"],
        "InsuredCustomerId": ["C-1", "C-3", "C-1"],
        "InsuredName": ["John Smith", "Priya Patel", "John Smith"],
        "InsuredDOB": ["1980-05-01", "1990-01-01", "1980-05-01"],
        "InsuredGender": ["M", "F", "M"],
        "InsuredEmail": ["", "priya@example.com", ""],
        "InsuredPhone": ["", "07700 900123", ""],
        "InsuredAddress1": ["12 High Street", "5 Church Lane", "12 High Street"],
        "InsuredAddress2": ["", "", ""],
        "InsuredCity": ["London", "Bristol", "London"],
        "InsuredPostcode": ["SW1A 1AA", "BS1 1AA", "SW1A 1AA"],
        "InsuredCountry": ["GB"] * 3,
        "InsuredOccupation": ["Engineer", "Nurse", "Engineer"],
        "AgentCode": ["AGT-1", "AGT-1", "AGT-2"],
        "AgentName": ["Jane Doe", "Jane Doe", "Acme Brokers Ltd"],
        "AgentEmail": ["jane@b.com", "jane@b.com", "info@acme.com"],
        "AgentPhone": ["+44 20 1111 2222"] * 3,
    })


@pytest.fixture(scope="module")
def mapping() -> SourceMapping:
    return load_mapping(LIFE_ADMIN)


@pytest.fixture(scope="module")
def shredded(raw_batch: pl.DataFrame, mapping: SourceMapping) -> dict[str, pl.DataFrame]:
    return shred(raw_batch, mapping)


def test_shred_produces_three_frames(shredded: dict[str, pl.DataFrame]) -> None:
    assert set(shredded) == {"policy", "person", "relationship"}


def test_shred_deduplicates_repeated_policy_rows(shredded: dict[str, pl.DataFrame]) -> None:
    """A full extract concatenated with a delta must not double the policy."""
    assert shredded["policy"].height == 2


def test_shred_emits_one_edge_per_party_per_policy(shredded: dict[str, pl.DataFrame]) -> None:
    """Three roles over three raw rows, before policy dedup, is nine edges."""
    assert shredded["relationship"].height == 9
    assert set(shredded["relationship"]["role"].unique()) == {"OWNER", "INSURED", "AGENT"}


def test_shred_collapses_parties_to_distinct_source_identities(
    shredded: dict[str, pl.DataFrame],
) -> None:
    person = shredded["person"]
    keys = person.select(
        pl.struct("source_system", "source_key_kind", "source_party_key").n_unique()
    ).item()
    assert person.height == keys


def test_collapsed_person_drops_policy_scoped_columns(
    shredded: dict[str, pl.DataFrame],
) -> None:
    """A collapsed party has no single role; asserting one would be false."""
    for column in POLICY_SCOPED_COLUMNS:
        assert column not in shredded["person"].columns


def test_relationship_retains_the_source_assertion(
    shredded: dict[str, pl.DataFrame],
) -> None:
    edges = shredded["relationship"]
    assert {"source_party_key", "source_key_kind", "role", "policy_number_normalized"} <= set(
        edges.columns
    )


def test_relationship_carries_no_surrogate_ids_yet(
    shredded: dict[str, pl.DataFrame],
) -> None:
    """Identity is resolved by the golden writer, not guessed by the shredder."""
    assert "from_person_id" not in shredded["relationship"].columns
    assert "to_policy_id" not in shredded["relationship"].columns


def test_collapse_prefers_the_more_complete_occurrence(
    shredded: dict[str, pl.DataFrame],
) -> None:
    """C-1 appears twice as owner; the occurrence with contact details wins."""
    owner = shredded["person"].filter(
        (pl.col("source_party_key") == "C-1")
        & (pl.col("source_key_kind") == "OWNER_CUSTOMER_ID")
    )
    assert owner.height == 1
    assert owner["email_normalized"][0] == "john.smith@example.com"
    assert owner["date_of_birth"][0] is not None


def test_owner_and_insured_namespaces_stay_separate(
    shredded: dict[str, pl.DataFrame],
) -> None:
    """The same literal key in two namespaces is two crosswalk entries.

    Collapsing them here would be probabilistic matching done in the wrong
    place. C-1 is both an OwnerCustomerId and an InsuredCustomerId; merging the
    two is the matching stage's job, with an audit trail.
    """
    c1 = shredded["person"].filter(pl.col("source_party_key") == "C-1")
    assert set(c1["source_key_kind"]) == {"OWNER_CUSTOMER_ID", "INSURED_CUSTOMER_ID"}


def test_shred_derives_the_columns_sources_cannot_supply(
    shredded: dict[str, pl.DataFrame],
) -> None:
    person = shredded["person"]
    for column in ("full_name_normalized", "name_sorted_key", "name_phonetic_key",
                   "name_initials", "party_type", "email_normalized", "phone_e164",
                   "address_normalized", "address_key"):
        assert column in person.columns, column


def test_shred_preserves_the_raw_name_alongside_the_normalized_one(
    shredded: dict[str, pl.DataFrame],
) -> None:
    person = shredded["person"]
    assert "Mr John Smith" in set(person["full_name"])
    assert "JOHN SMITH" in set(person["full_name_normalized"])


def test_shred_detects_a_trust_owner(shredded: dict[str, pl.DataFrame]) -> None:
    trust = shredded["person"].filter(pl.col("source_party_key") == "C-2")
    assert trust["party_type"][0] == "TRUST"


def test_money_is_decimal_after_shredding(shredded: dict[str, pl.DataFrame]) -> None:
    dtype = shredded["policy"].schema["annual_premium_amount"]
    assert isinstance(dtype, pl.Decimal)


def test_money_strips_currency_symbols_and_separators(
    shredded: dict[str, pl.DataFrame],
) -> None:
    values = {str(v) for v in shredded["policy"]["sum_assured_amount"].to_list()}
    assert any(v.startswith("250000") for v in values), values


def test_literal_mapping_populates_a_constant(shredded: dict[str, pl.DataFrame]) -> None:
    assert set(shredded["policy"]["currency_code"]) == {"GBP"}


def test_shred_reports_missing_columns_by_name(mapping: SourceMapping) -> None:
    with pytest.raises(ValueError, match="OwnerName"):
        shred(pl.DataFrame({"PolicyNumber": ["P1"]}), mapping)


def test_shred_accepts_a_lazyframe(raw_batch: pl.DataFrame, mapping: SourceMapping) -> None:
    assert shred(raw_batch.lazy(), mapping)["policy"].height == 2


def test_collapse_passes_through_unidentified_parties() -> None:
    """A party with no source key cannot be deterministically merged."""
    frame = pl.DataFrame({
        "source_system": ["S", "S"],
        "source_key_kind": ["OWNER_CUSTOMER_ID"] * 2,
        "source_party_key": ["", ""],
        "full_name": ["John Smith", "Jane Smith"],
        "date_of_birth": [None, None],
        "email_normalized": [None, None],
        "phone_e164": [None, None],
        "address_key": [None, None],
        "postal_code": [None, None],
    }).lazy()
    assert collapse_parties(frame).collect().height == 2
