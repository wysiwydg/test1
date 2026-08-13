"""Tests for the entity dashboard and the record export.

The export has one property that matters more than the rest: a source system
must be able to join the file to what it already holds. That means the same
rows, the same grain, the same delivered values -- plus ids. A file that is
correct but unjoinable is a file nobody uses.
"""

from __future__ import annotations

import csv
import io
import pathlib

import polars as pl
import pytest

from cmdm.export import (
    browse_entity,
    entity_names,
    entity_overview,
    export_entity,
    export_source_shaped,
    source_shaped_columns,
)
from cmdm.governance.rbac import MASK, Principal, Role
from cmdm.model.ids import uuid7

REPO = pathlib.Path(__file__).resolve().parent.parent
SAMPLE = REPO / "data" / "life_admin_sample.csv"
ROWS = 400


ADMIN = Principal(uuid7(), "admin@x", (Role.ADMIN,))
VIEWER = Principal(uuid7(), "viewer@x", (Role.VIEWER,))


@pytest.fixture
def mapping():
    from cmdm.ingest.mapping import load_mapping

    return load_mapping(REPO / "src" / "cmdm" / "mappings" / "life_admin.toml")


@pytest.fixture
def empty_landing(conn):
    """A landing zone holding only what this test puts there.

    The source-shaped export is defined over everything landed for a source
    system, so a test that asserts a row count has to know what was landed.
    The truncate is inside the test's transaction and rolls back with it, so a
    developer's working database survives the run intact.
    """
    conn.execute("TRUNCATE mdm.source_record CASCADE")
    return conn


@pytest.fixture
def loaded(conn, empty_landing, mapping):
    """A processed batch, inside the test's rolled-back transaction."""
    if not SAMPLE.exists():  # pragma: no cover - environment dependent
        pytest.skip("run `python -m scripts.generate_sample_data` first")

    from cmdm.ingest.landing import accept_batch
    from cmdm.worker import process_batch

    raw = pl.read_csv(SAMPLE, infer_schema_length=0).head(ROWS)
    batch_id, report, enqueued = accept_batch(
        conn, raw, mapping, origin="TEST", filename="s.csv", submitted_by="pytest",
    )
    assert report.accepted and enqueued
    process_batch(conn, batch_id)
    return raw


def read(stream) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO("".join(stream))))


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------


def test_the_dashboard_counts_all_three_entities(conn, loaded) -> None:
    totals = entity_overview(conn)
    assert totals["policies"] > 0
    assert totals["persons"] > 0
    assert totals["relationships"] > 0


def test_the_dashboard_offers_every_declared_entity() -> None:
    """A console that shows two of three entities describes a different model."""
    assert set(entity_names()) == {"policy", "person", "relationship"}


def test_browsing_pages_without_repeating_or_skipping(conn, loaded) -> None:
    first, total = browse_entity(conn, "policy", page=0, page_size=10)
    second, _ = browse_entity(conn, "policy", page=1, page_size=10)
    assert len(first) == 10 and len(second) == 10
    assert total >= 20
    assert not {r["policy_id"] for r in first} & {r["policy_id"] for r in second}


# ---------------------------------------------------------------------------
# Entity export
# ---------------------------------------------------------------------------


def test_entity_export_has_a_row_per_current_record(conn, loaded) -> None:
    rows = read(export_entity(conn, "policy", principal=ADMIN))
    total = conn.execute(
        "SELECT count(*) FROM mdm.policy WHERE is_current"
    ).fetchone()[0]
    assert len(rows) == total


def test_entity_export_masks_for_a_viewer(conn, loaded) -> None:
    """An export must not be the way around masking."""
    revealed = read(export_entity(conn, "person", principal=ADMIN))
    masked = read(export_entity(conn, "person", principal=VIEWER))

    assert any(r["full_name"] not in ("", MASK) for r in revealed)
    assert all(r["full_name"] in ("", MASK) for r in masked)
    # Non-PII is untouched, so the file is still useful.
    assert any(r["party_type"] for r in masked)


def test_entity_export_flattens_arrays(conn, loaded) -> None:
    """A loader should not have to strip Python quoting to read a list."""
    rows = read(export_entity(conn, "person", principal=ADMIN))
    values = [r["name_tokens"] for r in rows if r["name_tokens"]]
    assert values, "expected at least one record with name tokens"
    assert not any(v.startswith("[") or v.startswith("{") for v in values)
    assert any("|" in v for v in values), "a multi-token name should stay readable"


def test_browsing_masks_for_a_viewer(conn, loaded) -> None:
    """A browser is a read like any other. Showing on a page what the search
    and the export both withhold would be the way around masking."""
    revealed, _ = browse_entity(conn, "person", principal=ADMIN, page_size=20)
    masked, _ = browse_entity(conn, "person", principal=VIEWER, page_size=20)

    assert any(r["full_name"] not in (None, MASK) for r in revealed)
    assert all(r["full_name"] in (None, MASK) for r in masked)
    # Non-PII is untouched, or the page tells the reader nothing.
    assert all(r["party_type"] for r in masked)
    assert [r["person_id"] for r in revealed] == [r["person_id"] for r in masked]


def test_the_browser_only_shows_columns_that_exist(conn, loaded) -> None:
    """Guards the browser against registry drift: a renamed column should fail
    loudly here, not vanish from the page and read as missing data."""
    for entity in entity_names():
        rows, total = browse_entity(conn, entity, page=0, page_size=3)
        assert total > 0, f"no {entity} rows to browse"
        assert rows and len(rows[0]) == 10


# ---------------------------------------------------------------------------
# Source-shaped export -- the one that has to be joinable
# ---------------------------------------------------------------------------


def test_source_export_returns_the_delivered_grain(conn, loaded, mapping) -> None:
    """One row out per row delivered. Not per golden record, per row."""
    rows = read(export_source_shaped(conn, mapping, principal=ADMIN))
    landed = conn.execute("SELECT count(*) FROM mdm.source_record").fetchone()[0]
    assert len(rows) == landed == ROWS


def test_source_export_keeps_the_delivered_columns_and_values(
    conn, loaded, mapping
) -> None:
    rows = read(export_source_shaped(conn, mapping, principal=ADMIN))
    for column in mapping.source_columns:
        assert column in rows[0], f"{column} missing from the hand-back file"

    delivered = {r["PolicyNumber"]: r for r in loaded.to_dicts()}
    exported = {r["PolicyNumber"]: r for r in rows}
    sample = next(iter(exported))
    assert exported[sample]["OwnerName"] == delivered[sample]["OwnerName"]


def test_source_export_appends_an_id_per_role_and_the_policy(
    conn, loaded, mapping
) -> None:
    rows = read(export_source_shaped(conn, mapping, principal=ADMIN))
    for column in source_shaped_columns(mapping):
        assert column in rows[0]
    assert "PolicyMdmId" in rows[0]
    assert "OwnerMdmId" in rows[0]

    resolved = [r for r in rows if r["PolicyMdmId"]]
    assert len(resolved) == len(rows), "every delivered policy should resolve"


def test_one_source_key_maps_to_exactly_one_mdm_id(conn, loaded, mapping) -> None:
    """The whole point. If a customer id can carry two MDM ids the file is
    worse than useless -- it would split the customer downstream."""
    rows = read(export_source_shaped(conn, mapping, principal=ADMIN))
    seen: dict[str, set[str]] = {}
    for row in rows:
        key = row["OwnerCustomerId"]
        if key:
            seen.setdefault(key, set()).add(row["OwnerMdmId"])
    ambiguous = {k: v for k, v in seen.items() if len(v) > 1}
    assert not ambiguous, f"source keys mapped to several MDM ids: {ambiguous}"


def test_an_unresolved_row_is_kept_with_an_empty_id(
    conn, empty_landing, mapping
) -> None:
    """A hand-back file that silently drops rows cannot be reconciled against
    what was sent."""
    from cmdm.ingest.landing import land_batch
    from cmdm.model.ids import uuid7 as new_id

    # Keys nothing has ever resolved, so the rows are genuinely unknown to the
    # crosswalk rather than merely absent from this batch.
    raw = pl.read_csv(SAMPLE, infer_schema_length=0).head(3).with_columns(
        ("UNRESOLVED-" + pl.col("PolicyNumber")).alias("PolicyNumber"),
        ("UNRESOLVED-" + pl.col("OwnerCustomerId")).alias("OwnerCustomerId"),
    )
    land_batch(conn, raw, mapping, batch_id=new_id())

    rows = read(export_source_shaped(conn, mapping, principal=ADMIN))
    assert len(rows) == 3, "landed rows must survive into the file"
    assert all(r["PolicyMdmId"] == "" for r in rows), "nothing was processed"
    assert all(r["OwnerMdmId"] == "" for r in rows)
    # The delivered values are still there, so the row can be chased down.
    assert all(r["PolicyNumber"].startswith("UNRESOLVED-") for r in rows)


def test_source_export_masks_for_a_viewer(conn, loaded, mapping) -> None:
    rows = read(export_source_shaped(conn, mapping, principal=VIEWER))
    assert all(r["OwnerName"] in ("", MASK) for r in rows)
    # The policy number is not PII and must survive, or the file cannot be
    # joined to anything at all.
    assert all(r["PolicyNumber"] for r in rows)
    assert all(r["PolicyMdmId"] for r in rows)


def test_golden_values_replace_the_delivered_ones(conn, loaded, mapping) -> None:
    from cmdm.export import clear_golden_cache

    clear_golden_cache()
    try:
        delivered = read(export_source_shaped(conn, mapping, principal=ADMIN))
        golden = read(
            export_source_shaped(conn, mapping, principal=ADMIN, values="golden")
        )
    finally:
        clear_golden_cache()

    assert len(delivered) == len(golden)
    # The ids are the same either way; only the attribute values move.
    assert [r["OwnerMdmId"] for r in delivered] == [r["OwnerMdmId"] for r in golden]
    changed = sum(
        1 for a, b in zip(delivered, golden, strict=True)
        if a["OwnerName"] != b["OwnerName"]
    )
    assert changed > 0, "survivorship should have cleaned at least one name"


def test_an_unknown_values_mode_is_refused(conn, mapping) -> None:
    with pytest.raises(ValueError, match="as-delivered"):
        list(export_source_shaped(conn, mapping, principal=ADMIN, values="whatever"))
