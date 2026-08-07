"""Tests for identity resolution.

The properties asserted here are the ones that make a matcher trustworthy rather
than merely functional: missing data must not read as disagreement, a veto must
overrule any score, transitivity must close, and one bad edge must be visible
rather than silently unioning two clusters.
"""

from __future__ import annotations

import datetime as dt

import polars as pl
import pytest

from cmdm.resolve.blocking import BlockingKey, block_pairs
from cmdm.resolve.clustering import cluster_pairs
from cmdm.resolve.crossencoder import (
    NICKNAMES,
    FeatureCrossEncoder,
    OnnxCrossEncoder,
    classify_grey_zone,
)
from cmdm.resolve.pipeline import resolve
from cmdm.resolve.scoring import (
    AUTO_MATCH_THRESHOLD,
    AUTO_REJECT_THRESHOLD,
    COMPARATORS,
    Zone,
    score_pairs,
)


def parties(rows: list[dict]) -> pl.DataFrame:
    """Build a party frame, filling every column the comparators read."""
    template = {
        "person_id": None, "full_name_normalized": "", "name_sorted_key": "",
        "name_phonetic_key": "", "given_name_derived": None, "surname_derived": None,
        "email_normalized": None, "phone_e164": None, "national_id_hash": None,
        "date_of_birth": None, "postal_code": None, "address_key": None,
        "gender": None, "party_type": "PERSON",
    }
    filled = [{**template, **r} for r in rows]
    return pl.DataFrame(filled, schema={
        "person_id": pl.String, "full_name_normalized": pl.String,
        "name_sorted_key": pl.String, "name_phonetic_key": pl.String,
        "given_name_derived": pl.String, "surname_derived": pl.String,
        "email_normalized": pl.String, "phone_e164": pl.String,
        "national_id_hash": pl.String, "date_of_birth": pl.Date,
        "postal_code": pl.String, "address_key": pl.String,
        "gender": pl.String, "party_type": pl.String,
    })


def pair(left: str, right: str) -> pl.DataFrame:
    return pl.DataFrame({"left_id": [left], "right_id": [right]})


# ---------------------------------------------------------------------------
# Blocking
# ---------------------------------------------------------------------------


def test_blocking_pairs_records_sharing_a_key() -> None:
    frame = parties([
        {"person_id": "a", "email_normalized": "x@y.com"},
        {"person_id": "b", "email_normalized": "x@y.com"},
        {"person_id": "c", "email_normalized": "other@y.com"},
    ])
    pairs, _ = block_pairs(frame)
    assert pairs.height == 1
    assert set(pairs.row(0)[:2]) == {"a", "b"}


def test_blocking_emits_each_unordered_pair_once() -> None:
    frame = parties([
        {"person_id": str(i), "email_normalized": "x@y.com"} for i in range(4)
    ])
    pairs, _ = block_pairs(frame)
    assert pairs.height == 6  # 4 choose 2
    assert (pairs["left_id"] < pairs["right_id"]).all()


def test_blocking_never_pairs_a_record_with_itself() -> None:
    frame = parties([{"person_id": "a", "email_normalized": "x@y.com"}])
    pairs, _ = block_pairs(frame)
    assert pairs.height == 0


def test_null_and_blank_keys_form_no_block() -> None:
    """Every record missing an email must not land in one enormous block.

    This is the classic way a blocking pass silently becomes quadratic.
    """
    frame = parties([
        {"person_id": "a", "email_normalized": None},
        {"person_id": "b", "email_normalized": None},
        {"person_id": "c", "email_normalized": ""},
    ])
    pairs, _ = block_pairs(frame, keys=[BlockingKey("email", "email_normalized")])
    assert pairs.height == 0


def test_oversized_blocks_are_dropped_and_reported() -> None:
    """One huge block would exhaust memory before the scorer saw it."""
    frame = parties([
        {"person_id": str(i), "name_sorted_key": "SAME"} for i in range(50)
    ])
    pairs, report = block_pairs(
        frame, keys=[BlockingKey("name", "name_sorted_key")], max_block=10
    )
    assert pairs.height == 0
    assert report.oversized_blocks
    assert report.dropped_pairs == 50 * 49 // 2


def test_a_pair_found_by_several_keys_appears_once() -> None:
    frame = parties([
        {"person_id": "a", "email_normalized": "x@y.com", "phone_e164": "+1"},
        {"person_id": "b", "email_normalized": "x@y.com", "phone_e164": "+1"},
    ])
    pairs, _ = block_pairs(frame)
    assert pairs.height == 1
    assert "+" in pairs["blocking_keys"][0]


def test_blocking_reports_reduction_ratio() -> None:
    frame = parties([
        {"person_id": str(i), "email_normalized": f"{i % 3}@y.com"} for i in range(30)
    ])
    _, report = block_pairs(frame)
    assert report.reduction_ratio > 1.0


def test_blocking_skips_keys_whose_column_is_absent() -> None:
    frame = pl.DataFrame({"person_id": ["a", "b"], "email_normalized": ["x", "x"]})
    pairs, _ = block_pairs(frame)
    assert pairs.height == 1


def test_blocking_on_a_single_record_is_empty() -> None:
    pairs, report = block_pairs(parties([{"person_id": "a"}]))
    assert pairs.height == 0


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------


def test_identical_records_score_into_auto_match() -> None:
    frame = parties([
        {"person_id": "a", "full_name_normalized": "JOHN SMITH",
         "name_sorted_key": "JOHN SMITH", "email_normalized": "j@x.com",
         "date_of_birth": dt.date(1980, 1, 1)},
        {"person_id": "b", "full_name_normalized": "JOHN SMITH",
         "name_sorted_key": "JOHN SMITH", "email_normalized": "j@x.com",
         "date_of_birth": dt.date(1980, 1, 1)},
    ])
    scored, report = score_pairs(pair("a", "b"), frame)
    assert scored["zone"][0] == Zone.AUTO_MATCH
    assert scored["score"][0] >= AUTO_MATCH_THRESHOLD


def test_unrelated_records_score_into_auto_reject() -> None:
    frame = parties([
        {"person_id": "a", "full_name_normalized": "JOHN SMITH",
         "name_sorted_key": "JOHN SMITH", "email_normalized": "j@x.com"},
        {"person_id": "b", "full_name_normalized": "PRIYA PATEL",
         "name_sorted_key": "PATEL PRIYA", "email_normalized": "p@z.com"},
    ])
    scored, _ = score_pairs(pair("a", "b"), frame)
    assert scored["zone"][0] == Zone.AUTO_REJECT


def test_missing_data_is_not_treated_as_disagreement() -> None:
    """Sparse records are where duplicates hide.

    A comparator returning 0.0 for missing input would push every sparse pair
    towards auto-reject, which is exactly backwards.
    """
    complete = parties([
        {"person_id": "a", "full_name_normalized": "JOHN SMITH",
         "name_sorted_key": "JOHN SMITH", "email_normalized": "j@x.com",
         "date_of_birth": dt.date(1980, 1, 1), "postal_code": "SW1", "gender": "MALE"},
        {"person_id": "b", "full_name_normalized": "JOHN SMITH",
         "name_sorted_key": "JOHN SMITH", "email_normalized": "j@x.com",
         "date_of_birth": dt.date(1980, 1, 1), "postal_code": "SW1", "gender": "MALE"},
    ])
    sparse = parties([
        {"person_id": "a", "full_name_normalized": "JOHN SMITH",
         "name_sorted_key": "JOHN SMITH"},
        {"person_id": "b", "full_name_normalized": "JOHN SMITH",
         "name_sorted_key": "JOHN SMITH"},
    ])
    full_score = score_pairs(pair("a", "b"), complete)[0]["score"][0]
    sparse_score = score_pairs(pair("a", "b"), sparse)[0]["score"][0]
    assert sparse_score == pytest.approx(full_score, abs=0.2)
    assert sparse_score >= AUTO_REJECT_THRESHOLD


def test_conflicting_dob_vetoes_an_otherwise_perfect_match() -> None:
    """A veto must overrule any score, not merely subtract from it."""
    frame = parties([
        {"person_id": "a", "full_name_normalized": "JOHN SMITH",
         "name_sorted_key": "JOHN SMITH", "email_normalized": "j@x.com",
         "date_of_birth": dt.date(1980, 1, 1)},
        {"person_id": "b", "full_name_normalized": "JOHN SMITH",
         "name_sorted_key": "JOHN SMITH", "email_normalized": "j@x.com",
         "date_of_birth": dt.date(1955, 6, 30)},
    ])
    scored, _ = score_pairs(pair("a", "b"), frame)
    assert scored["vetoed"][0]
    assert scored["zone"][0] == Zone.AUTO_REJECT


def test_person_and_organization_never_match() -> None:
    frame = parties([
        {"person_id": "a", "full_name_normalized": "SMITH", "party_type": "PERSON",
         "name_sorted_key": "SMITH", "email_normalized": "s@x.com"},
        {"person_id": "b", "full_name_normalized": "SMITH", "party_type": "ORGANIZATION",
         "name_sorted_key": "SMITH", "email_normalized": "s@x.com"},
    ])
    scored, _ = score_pairs(pair("a", "b"), frame)
    assert scored["zone"][0] == Zone.AUTO_REJECT


def test_near_identical_dates_are_not_vetoed() -> None:
    """Transposed digits are common in hand-keyed dates."""
    frame = parties([
        {"person_id": "a", "date_of_birth": dt.date(1980, 1, 1),
         "full_name_normalized": "JOHN SMITH", "name_sorted_key": "JOHN SMITH"},
        {"person_id": "b", "date_of_birth": dt.date(1980, 1, 2),
         "full_name_normalized": "JOHN SMITH", "name_sorted_key": "JOHN SMITH"},
    ])
    scored, _ = score_pairs(pair("a", "b"), frame)
    assert not scored["vetoed"][0]


def test_score_is_bounded() -> None:
    frame = parties([
        {"person_id": "a", "full_name_normalized": "X", "name_sorted_key": "X"},
        {"person_id": "b", "full_name_normalized": "Y", "name_sorted_key": "Y"},
    ])
    scored, _ = score_pairs(pair("a", "b"), frame)
    assert 0.0 <= scored["score"][0] <= 1.0


def test_per_comparator_scores_are_retained() -> None:
    """A composite that cannot be broken down is reportable, not explainable."""
    frame = parties([
        {"person_id": "a", "email_normalized": "j@x.com",
         "full_name_normalized": "A", "name_sorted_key": "A"},
        {"person_id": "b", "email_normalized": "j@x.com",
         "full_name_normalized": "B", "name_sorted_key": "B"},
    ])
    scored, _ = score_pairs(pair("a", "b"), frame)
    assert "cmp_email" in scored.columns
    assert scored["cmp_email"][0] == 1.0


def test_grey_zone_sits_between_the_thresholds() -> None:
    frame = parties([
        {"person_id": "a", "full_name_normalized": "JOHN SMITH",
         "name_sorted_key": "JOHN SMITH", "name_phonetic_key": "JHN SMT",
         "postal_code": "SW1"},
        {"person_id": "b", "full_name_normalized": "JOHN SMITH",
         "name_sorted_key": "JOHN SMITH", "name_phonetic_key": "JHN SMT",
         "postal_code": "NW3"},
    ])
    scored, _ = score_pairs(pair("a", "b"), frame)
    score = scored["score"][0]
    if scored["zone"][0] == Zone.GREY:
        assert AUTO_REJECT_THRESHOLD <= score < AUTO_MATCH_THRESHOLD


def test_scoring_an_empty_pair_frame() -> None:
    empty = pl.DataFrame(schema={"left_id": pl.String, "right_id": pl.String})
    scored, report = score_pairs(empty, parties([{"person_id": "a"}]))
    assert scored.height == 0
    assert report.pairs == 0


def test_scoring_raises_when_no_comparator_can_run() -> None:
    frame = pl.DataFrame({"person_id": ["a", "b"]})
    with pytest.raises(ValueError, match="no comparator"):
        score_pairs(pair("a", "b"), frame)


def test_comparator_weights_are_positive() -> None:
    for comparator in COMPARATORS:
        assert comparator.weight > 0, comparator.name


# ---------------------------------------------------------------------------
# Cross-encoder
# ---------------------------------------------------------------------------


def test_nickname_lexicon_resolves_the_archetypal_pairs() -> None:
    assert NICKNAMES["BOB"] == "ROBERT"
    assert NICKNAMES["KATE"] == "KATHERINE"


def test_feature_encoder_scores_a_nickname_pair_above_a_stranger_pair() -> None:
    """Bob/Robert is what the lexical comparators structurally cannot see."""
    encoder = FeatureCrossEncoder()
    nickname = pl.DataFrame({
        "l_given_name_derived": ["BOB"], "r_given_name_derived": ["ROBERT"],
        "l_surname_derived": ["SMITH"], "r_surname_derived": ["SMITH"],
        "score": [0.6],
    })
    stranger = pl.DataFrame({
        "l_given_name_derived": ["BOB"], "r_given_name_derived": ["PRIYA"],
        "l_surname_derived": ["SMITH"], "r_surname_derived": ["PATEL"],
        "score": [0.6],
    })
    assert encoder.score(nickname)[0] > encoder.score(stranger)[0]


def test_feature_encoder_returns_one_score_per_row() -> None:
    frame = pl.DataFrame({
        "l_given_name_derived": ["A", "B"], "r_given_name_derived": ["A", "C"],
        "l_surname_derived": ["X", "Y"], "r_surname_derived": ["X", "Z"],
        "score": [0.6, 0.6],
    })
    assert len(FeatureCrossEncoder().score(frame)) == 2


def test_feature_encoder_scores_are_bounded() -> None:
    frame = pl.DataFrame({
        "l_given_name_derived": ["BOB"], "r_given_name_derived": ["ROBERT"],
        "l_surname_derived": ["SMITH"], "r_surname_derived": ["SMITH"],
        "score": [1.0],
    })
    score = FeatureCrossEncoder().score(frame)[0]
    assert 0.0 <= score <= 1.0


def test_feature_encoder_handles_an_empty_frame() -> None:
    assert len(FeatureCrossEncoder().score(pl.DataFrame())) == 0


def test_classify_records_a_verdict_for_every_grey_pair() -> None:
    """Rejections matter: a production duplicate is diagnosed by asking why."""
    frame = parties([
        {"person_id": "a", "given_name_derived": "BOB", "surname_derived": "SMITH"},
        {"person_id": "b", "given_name_derived": "ROBERT", "surname_derived": "SMITH"},
    ])
    grey = pair("a", "b").with_columns(pl.lit(0.6).alias("score"))
    scored, decisions = classify_grey_zone(grey, frame)
    assert len(decisions) == 1
    assert decisions[0].decision in ("MATCH", "NO_MATCH")
    assert decisions[0].model_name


def test_classify_on_an_empty_grey_zone() -> None:
    empty = pl.DataFrame(schema={"left_id": pl.String, "right_id": pl.String})
    scored, decisions = classify_grey_zone(empty, parties([{"person_id": "a"}]))
    assert decisions == []


def test_missing_cross_encoder_model_fails_loudly(tmp_path) -> None:
    with pytest.raises(FileNotFoundError, match="cross-encoder model not found"):
        OnnxCrossEncoder(tmp_path / "absent.onnx")


def test_feature_encoder_runs_a_real_onnx_model(tmp_path) -> None:
    """Exercises ONNX Runtime, not a mock of it."""
    pytest.importorskip("onnx")
    import numpy as np
    import onnx
    from onnx import TensorProto, helper

    weights = helper.make_tensor(
        "w", TensorProto.FLOAT, [7, 1], np.full(7, 1.0 / 7, dtype=np.float32).tolist()
    )
    node = helper.make_node("MatMul", ["features", "w"], ["output"])
    graph = helper.make_graph(
        [node], "scorer",
        [helper.make_tensor_value_info("features", TensorProto.FLOAT, [None, 7])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [None, 1])],
        initializer=[weights],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 9
    path = tmp_path / "scorer.onnx"
    onnx.save(model, path)

    encoder = FeatureCrossEncoder(path)
    frame = pl.DataFrame({
        "l_given_name_derived": ["BOB"], "r_given_name_derived": ["ROBERT"],
        "l_surname_derived": ["SMITH"], "r_surname_derived": ["SMITH"],
        "score": [0.6],
    })
    assert len(encoder.score(frame)) == 1


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def test_transitive_closure_merges_a_chain() -> None:
    """A=B and B=C implies A=C even though A and C were never compared."""
    pairs = pl.DataFrame({"left_id": ["a", "b"], "right_id": ["b", "c"]})
    result = cluster_pairs(pairs, ["a", "b", "c"])
    masters = set(result.assignments["master_id"])
    assert len(masters) == 1
    assert result.cluster_count == 1


def test_records_in_no_pair_still_receive_a_master_id() -> None:
    """A party with no duplicates is a cluster of one, not an omission."""
    pairs = pl.DataFrame({"left_id": ["a"], "right_id": ["b"]})
    result = cluster_pairs(pairs, ["a", "b", "lonely"])
    assert result.assignments.height == 3
    assert result.cluster_count == 2


def test_master_id_assignment_is_deterministic() -> None:
    """A re-run over identical edges must reproduce the same master ids."""
    pairs = pl.DataFrame({"left_id": ["b"], "right_id": ["c"]})
    first = cluster_pairs(pairs, ["a", "b", "c"]).assignments.sort("person_id")
    second = cluster_pairs(pairs, ["c", "b", "a"]).assignments.sort("person_id")
    assert first.equals(second)


def test_master_id_is_the_smallest_member() -> None:
    pairs = pl.DataFrame({"left_id": ["b"], "right_id": ["c"]})
    result = cluster_pairs(pairs, ["b", "c"])
    assert set(result.assignments["master_id"]) == {"b"}


def test_disjoint_clusters_stay_separate() -> None:
    pairs = pl.DataFrame({"left_id": ["a", "c"], "right_id": ["b", "d"]})
    result = cluster_pairs(pairs, ["a", "b", "c", "d"])
    assert result.cluster_count == 2


def test_oversized_clusters_are_flagged() -> None:
    """A cluster of hundreds is a bad edge, not a family."""
    ids = [f"p{i}" for i in range(40)]
    pairs = pl.DataFrame({"left_id": ids[:-1], "right_id": ids[1:]})
    result = cluster_pairs(pairs, ids, suspicious_size=25)
    assert result.suspicious_clusters
    assert result.largest_cluster == 40


def test_clustering_with_no_records() -> None:
    empty = pl.DataFrame(schema={"left_id": pl.String, "right_id": pl.String})
    assert cluster_pairs(empty, []).cluster_count == 0


def test_collapse_ratio_reports_merging() -> None:
    pairs = pl.DataFrame({"left_id": ["a"], "right_id": ["b"]})
    result = cluster_pairs(pairs, ["a", "b"])
    assert result.collapse_ratio == 2.0


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def test_resolve_end_to_end_merges_duplicates() -> None:
    frame = parties([
        {"person_id": "a", "full_name_normalized": "JOHN SMITH",
         "name_sorted_key": "JOHN SMITH", "email_normalized": "j@x.com",
         "date_of_birth": dt.date(1980, 1, 1)},
        {"person_id": "b", "full_name_normalized": "SMITH JOHN",
         "name_sorted_key": "JOHN SMITH", "email_normalized": "j@x.com",
         "date_of_birth": dt.date(1980, 1, 1)},
        {"person_id": "c", "full_name_normalized": "PRIYA PATEL",
         "name_sorted_key": "PATEL PRIYA", "email_normalized": "p@z.com",
         "date_of_birth": dt.date(1990, 5, 5)},
    ])
    clusters, pairs, report = resolve(frame)
    masters = clusters.assignments
    a = masters.filter(pl.col("person_id") == "a")["master_id"][0]
    b = masters.filter(pl.col("person_id") == "b")["master_id"][0]
    c = masters.filter(pl.col("person_id") == "c")["master_id"][0]
    assert a == b
    assert c != a


def test_resolve_records_every_pair_including_rejections(monkeypatch) -> None:
    frame = parties([
        {"person_id": "a", "name_sorted_key": "K", "full_name_normalized": "JOHN SMITH"},
        {"person_id": "b", "name_sorted_key": "K", "full_name_normalized": "PRIYA PATEL"},
    ])
    _, pairs, _ = resolve(frame)
    assert pairs.height == 1
    assert "final_decision" in pairs.columns


def test_resolve_on_a_single_party() -> None:
    clusters, pairs, report = resolve(parties([{"person_id": "a"}]))
    assert report.candidate_pairs == 0
    assert clusters.assignments.height == 1


def test_resolve_on_an_empty_frame() -> None:
    clusters, _, report = resolve(parties([]))
    assert report.records == 0


def test_report_serializes() -> None:
    frame = parties([
        {"person_id": "a", "name_sorted_key": "K"},
        {"person_id": "b", "name_sorted_key": "K"},
    ])
    _, _, report = resolve(frame)
    payload = report.as_dict()
    assert set(payload) >= {"candidate_pairs", "auto_match", "grey", "clusters"}


def test_only_accepted_edges_reach_the_graph() -> None:
    """One wrong edge silently unions two clusters; rejections must not merge."""
    frame = parties([
        {"person_id": "a", "name_sorted_key": "K", "full_name_normalized": "JOHN SMITH",
         "date_of_birth": dt.date(1980, 1, 1)},
        {"person_id": "b", "name_sorted_key": "K", "full_name_normalized": "PRIYA PATEL",
         "date_of_birth": dt.date(1950, 1, 1)},
    ])
    clusters, _, _ = resolve(frame)
    assert clusters.cluster_count == 2


def test_resolution_run_is_persisted(conn) -> None:
    frame = parties([
        {"person_id": "11111111-1111-1111-1111-111111111111",
         "name_sorted_key": "K", "full_name_normalized": "JOHN SMITH",
         "email_normalized": "j@x.com"},
        {"person_id": "22222222-2222-2222-2222-222222222222",
         "name_sorted_key": "K", "full_name_normalized": "JOHN SMITH",
         "email_normalized": "j@x.com"},
    ])
    _, _, report = resolve(frame, conn=conn)
    runs = conn.execute(
        "SELECT count(*) FROM mdm.resolution_run WHERE run_id = %s", (report.run_id,)
    ).fetchone()[0]
    stored = conn.execute(
        "SELECT count(*) FROM mdm.match_pair WHERE run_id = %s", (report.run_id,)
    ).fetchone()[0]
    assert runs == 1
    assert stored == 1


def test_persisted_pair_ordering_constraint_holds(conn) -> None:
    """left < right is enforced so a pair cannot be stored twice, both ways."""
    frame = parties([
        {"person_id": "22222222-2222-2222-2222-222222222222",
         "name_sorted_key": "K", "email_normalized": "j@x.com"},
        {"person_id": "11111111-1111-1111-1111-111111111111",
         "name_sorted_key": "K", "email_normalized": "j@x.com"},
    ])
    _, _, report = resolve(frame, conn=conn)
    row = conn.execute(
        "SELECT left_person_id::text, right_person_id::text FROM mdm.match_pair "
        "WHERE run_id = %s", (report.run_id,)
    ).fetchone()
    assert row[0] < row[1]


def test_thresholds_are_stored_with_the_run(conn) -> None:
    """A decision is only interpretable alongside its configuration."""
    frame = parties([
        {"person_id": "11111111-1111-1111-1111-111111111111", "name_sorted_key": "K"},
        {"person_id": "22222222-2222-2222-2222-222222222222", "name_sorted_key": "K"},
    ])
    _, _, report = resolve(frame, conn=conn, auto_match=0.9, auto_reject=0.4)
    row = conn.execute(
        "SELECT auto_match_threshold, auto_reject_threshold FROM mdm.resolution_run "
        "WHERE run_id = %s", (report.run_id,)
    ).fetchone()
    assert row == (0.9, 0.4)
