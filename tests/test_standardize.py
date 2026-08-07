"""Tests for the hybrid standardization pipeline.

The property that matters most here is the loop closing: a pattern the model
handled becomes a deterministic rule, and the AI share of an identical batch
falls as a result. That is asserted end to end rather than inferred from the
parts, because every individual piece can work while the loop still fails to
close — which is exactly what happened during development, twice.
"""

from __future__ import annotations

import polars as pl
import pytest

from cmdm.model.ids import uuid7
from cmdm.standardize.agent import (
    _name_extract_pattern,
    _token_shape,
    mine_and_propose,
    mine_signatures,
    propose_rules,
    shadow_evaluate,
)
from cmdm.standardize.ai import (
    HeuristicStandardizer,
    OnnxStandardizer,
    StandardizationRequest,
    get_standardizer,
)
from cmdm.standardize.gate import CHECKS, apply_gate, checks_for, gate_summary
from cmdm.standardize.pipeline import standardize
from cmdm.standardize.rules import (
    InvalidRule,
    RuleKind,
    RuleState,
    StandardizationRule,
    apply_rules,
    approve_rule,
    insert_rule,
    list_rules,
    load_active_rules,
    record_shadow_result,
    reject_rule,
)


def person_frame(names: list[str], party_types: list[str] | None = None) -> pl.DataFrame:
    """A minimal Person-shaped frame carrying the columns the gate reads.

    Mirrors what the shredder produces, so gate behaviour here matches gate
    behaviour on real batches. Built to survive an empty name list, since the
    pipeline must handle an empty batch and the fixture would otherwise be the
    thing that could not.
    """
    from cmdm.ingest import normalize as N

    frame = pl.DataFrame({"full_name": names}, schema={"full_name": pl.String})
    frame = frame.with_columns(
        N.normalize_full_name(pl.col("full_name")).alias("full_name_normalized")
    ).with_columns(
        N.name_tokens(pl.col("full_name_normalized")).alias("name_tokens"),
        N.name_phonetic_key(pl.col("full_name_normalized")).alias("name_phonetic_key"),
    )
    frame = frame.with_columns(
        pl.Series("party_type", party_types, dtype=pl.String)
        if party_types is not None
        else N.detect_party_type(pl.col("name_tokens")).alias("party_type")
    )

    two = (pl.col("name_tokens").list.len() == 2) & (pl.col("party_type") == "PERSON")
    return frame.with_columns(
        # list.get with null_on_oob, because a one-token name has no index 1 and
        # the gate must be able to judge exactly that record.
        pl.when(two)
        .then(pl.col("name_tokens").list.get(0, null_on_oob=True))
        .alias("given_name_derived"),
        pl.when(two)
        .then(pl.col("name_tokens").list.get(1, null_on_oob=True))
        .alias("surname_derived"),
        pl.lit(None, dtype=pl.String).alias("middle_name_derived"),
        pl.when(two).then(pl.lit(0.75)).otherwise(pl.lit(0.0)).alias("name_parse_confidence"),
        pl.lit("RULE_BASED").alias("name_parse_method"),
    )


# ---------------------------------------------------------------------------
# Gate
# ---------------------------------------------------------------------------


def test_clean_two_token_name_passes_the_gate() -> None:
    gated = apply_gate(person_frame(["John Smith"])).collect()
    assert gated["gate_passed"][0]


def test_three_token_name_fails_the_parse_check() -> None:
    gated = apply_gate(person_frame(["John Michael Smith"])).collect()
    assert not gated["gate_passed"][0]
    assert "name_parsed_cleanly" in gated["failed_checks"][0].to_list()


def test_gate_names_every_failing_check() -> None:
    """The AI prompt and the miner both key off which check failed."""
    gated = apply_gate(person_frame(["Bob99 Smith Jones Extra Names Here"])).collect()
    failed = set(gated["failed_checks"][0].to_list())
    assert "name_no_digits" in failed


def test_organization_is_exempt_from_person_only_checks() -> None:
    """A check that does not apply must not push a record to a model."""
    frame = person_frame(["ACME"], party_types=["ORGANIZATION"])
    gated = apply_gate(frame).collect()
    assert "name_token_count" not in gated["failed_checks"][0].to_list()


def test_placeholder_names_fail() -> None:
    gated = apply_gate(person_frame(["UNKNOWN"], party_types=["PERSON"])).collect()
    assert "name_not_placeholder" in gated["failed_checks"][0].to_list()


def test_real_surname_containing_a_placeholder_substring_passes() -> None:
    """The placeholder check is anchored, so NASH is not NA."""
    gated = apply_gate(person_frame(["Peter Nash"])).collect()
    assert "name_not_placeholder" not in gated["failed_checks"][0].to_list()


def test_checks_for_skips_checks_whose_columns_are_absent() -> None:
    """A batch with no address columns must not fail every address check."""
    names = {c.name for c in checks_for(["full_name_normalized", "name_phonetic_key"])}
    assert "address_split" not in names
    assert "name_present" in names


def test_gate_on_a_frame_with_no_applicable_checks_passes_everything() -> None:
    frame = pl.DataFrame({"unrelated": [1, 2]})
    gated = apply_gate(frame).collect()
    assert gated["gate_passed"].all()


def test_gate_summary_reports_per_check_counts() -> None:
    """'3% failed' is a number; 'failed address_split' is an action."""
    gated = apply_gate(person_frame(["John Michael Smith", "Jane Doe"])).collect()
    summary = gate_summary(gated)
    assert summary.filter(pl.col("check") == "name_parsed_cleanly")["failures"][0] == 1


def test_gate_summary_of_empty_frame_is_empty() -> None:
    assert gate_summary(apply_gate(person_frame([])).collect()).height == 0


def test_every_declared_check_has_a_reason() -> None:
    """A gate failure a steward cannot interpret is not actionable."""
    for check in CHECKS:
        assert len(check.reason) > 20, check.name


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def test_rewrite_rule_applies() -> None:
    rule = StandardizationRule(
        rule_id=uuid7(), field_name="full_name_normalized", rule_name="strip_junk",
        pattern=r"\s+X{3,}$", replacement="",
    )
    frame = pl.DataFrame({"full_name_normalized": ["JOHN SMITH XXXX"]})
    out = apply_rules(frame, [rule]).collect()
    assert out["full_name_normalized"][0] == "JOHN SMITH"


def test_extract_rule_populates_components() -> None:
    rule = StandardizationRule(
        rule_id=uuid7(), field_name="full_name_normalized", rule_name="w_i_w",
        pattern=r"^(?P<given_name_derived>[A-Z']+)\s+[A-Z]\s+(?P<surname_derived>[A-Z']+)$",
        rule_kind=RuleKind.EXTRACT,
        target_fields=("given_name_derived", "surname_derived"),
    )
    frame = person_frame(["Emma A Phillips"])
    out = apply_rules(frame, [rule]).collect()
    assert out["given_name_derived"][0] == "EMMA"
    assert out["surname_derived"][0] == "PHILLIPS"


def test_extract_rule_does_not_overwrite_an_existing_value() -> None:
    """A learned rule fills gaps; it must never override the built-in pass."""
    rule = StandardizationRule(
        rule_id=uuid7(), field_name="full_name_normalized", rule_name="greedy",
        pattern=r"^(?P<given_name_derived>[A-Z]+)\s+(?P<surname_derived>[A-Z]+)$",
        rule_kind=RuleKind.EXTRACT,
        target_fields=("given_name_derived", "surname_derived"),
    )
    frame = person_frame(["John Smith"]).with_columns(
        pl.lit("ORIGINAL").alias("given_name_derived")
    )
    out = apply_rules(frame, [rule]).collect()
    assert out["given_name_derived"][0] == "ORIGINAL"


def test_rules_are_skipped_when_their_columns_are_absent() -> None:
    """A rule learned from one feed must not break a feed shaped differently."""
    rule = StandardizationRule(
        rule_id=uuid7(), field_name="missing_column", rule_name="r", pattern="x",
    )
    frame = pl.DataFrame({"other": ["a"]})
    assert apply_rules(frame, [rule]).collect().equals(frame)


def test_only_where_restricts_a_rule_to_a_subset() -> None:
    rule = StandardizationRule(
        rule_id=uuid7(), field_name="v", rule_name="upper", pattern="a", replacement="Z",
    )
    frame = pl.DataFrame({"v": ["a", "a"], "apply": [True, False]})
    out = apply_rules(frame, [rule], only_where=pl.col("apply")).collect()
    assert out["v"].to_list() == ["Z", "a"]


def test_invalid_regex_is_refused_at_proposal_time() -> None:
    with pytest.raises(InvalidRule, match="invalid regex"):
        StandardizationRule(
            rule_id=uuid7(), field_name="f", rule_name="bad", pattern="([unclosed"
        ).validate()


def test_unreviewably_long_pattern_is_refused() -> None:
    """An unreviewable rule cannot be approved, so it must not reach a queue."""
    with pytest.raises(InvalidRule, match="reviewed"):
        StandardizationRule(
            rule_id=uuid7(), field_name="f", rule_name="long", pattern="a" * 600
        ).validate()


def test_nested_quantifier_is_refused() -> None:
    with pytest.raises(InvalidRule, match="nested quantifier"):
        StandardizationRule(
            rule_id=uuid7(), field_name="f", rule_name="evil", pattern=r"(a+)+"
        ).validate()


def test_extract_rule_targets_must_have_capture_groups() -> None:
    """A target with no group would silently write nothing."""
    with pytest.raises(InvalidRule, match="capture group"):
        StandardizationRule(
            rule_id=uuid7(), field_name="f", rule_name="x", pattern=r"^(?P<a>\w+)$",
            rule_kind=RuleKind.EXTRACT, target_fields=("a", "b"),
        ).validate()


def test_extract_rule_must_name_targets() -> None:
    with pytest.raises(InvalidRule, match="no target fields"):
        StandardizationRule(
            rule_id=uuid7(), field_name="f", rule_name="x", pattern=r"(?P<a>\w+)",
            rule_kind=RuleKind.EXTRACT,
        ).validate()


# ---------------------------------------------------------------------------
# Mining
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "shape"),
    [
        ("JOHN SMITH", "W-W"),
        ("EMMA A PHILLIPS", "W-I-W"),
        ("JOHN VAN BERG", "W-P-W"),
        ("JOHN DE LA CRUZ", "W-P-P-W"),
    ],
)
def test_token_shape(value: str, shape: str) -> None:
    assert _token_shape(value) == shape


def test_particle_shapes_produce_patterns_that_bind_the_particle_to_the_surname() -> None:
    """VAN DER BERG is one surname, not a middle name and a surname."""
    pattern, targets = _name_extract_pattern("W-P-W")
    assert "middle_name_derived" not in targets
    import re

    match = re.match(pattern, "JOHN VAN BERG")
    assert match and match.group("surname_derived") == "VAN BERG"


def test_particle_patterns_do_not_match_plain_words() -> None:
    """Otherwise the general W-W-W rule shadows the specific particle rule."""
    import re

    pattern, _ = _name_extract_pattern("W-P-W")
    assert re.match(pattern, "JOHN MICHAEL SMITH") is None


def test_shapes_with_numbers_produce_no_name_rule() -> None:
    """A value containing digits is not a name and must not be parsed as one."""
    assert _name_extract_pattern("W-N-W") is None


def test_single_token_shape_produces_no_rule() -> None:
    assert _name_extract_pattern("W") is None


def test_mine_signatures_requires_recurrence() -> None:
    """A pattern seen three times is a coincidence, not a rule."""
    exceptions = pl.DataFrame({
        "field_name": ["full_name"] * 3,
        "failed_check": ["name_parsed_cleanly"] * 3,
        "deterministic_value": ["A B C", "D E F", "G H I"],
    })
    assert mine_signatures(exceptions, min_evidence=25).height == 0
    assert mine_signatures(exceptions, min_evidence=3).height == 1


def test_mine_signatures_on_empty_input() -> None:
    assert mine_signatures(pl.DataFrame()).height == 0


def test_proposals_are_only_made_for_checks_with_a_builder() -> None:
    """A name-splitting rule for an address failure is worse than nothing."""
    signatures = pl.DataFrame({
        "field_name": ["address_line1"], "failed_check": ["address_split"],
        "shape": ["W-W-W"], "party_type": ["PERSON"],
        "evidence_count": [100], "sample": [["a"]],
    })
    assert propose_rules(signatures) == []


# ---------------------------------------------------------------------------
# Shadow evaluation
# ---------------------------------------------------------------------------


def test_shadow_evaluation_counts_what_a_rule_fixes() -> None:
    rule = propose_rules(pl.DataFrame({
        "field_name": ["full_name"], "failed_check": ["name_parsed_cleanly"],
        "shape": ["W-I-W"], "party_type": ["PERSON"],
        "evidence_count": [50], "sample": [["A B C"]],
    }))[0]
    target = person_frame(["Emma A Phillips", "John M Smith"])
    result = shadow_evaluate(rule, target, person_frame([]))
    assert result.fixed == 2
    assert result.safe


def test_shadow_evaluation_catches_a_regression() -> None:
    """Measuring only what a rule fixes approves rules that break more.

    Uses a REWRITE rule, because that is where regressions actually arise: an
    EXTRACT rule coalesces into empty columns and structurally cannot overwrite
    a value the deterministic pass established.
    """
    destructive = StandardizationRule(
        rule_id=uuid7(), field_name="surname_derived", rule_name="destructive",
        pattern=r"^.*$", replacement="",
    )
    regression = person_frame(["John Smith", "Jane Doe"])
    result = shadow_evaluate(destructive, person_frame([]), regression)
    assert result.regressions > 0
    assert not result.safe


def test_extract_rules_cannot_regress_an_established_value() -> None:
    """The coalesce in compile_rule is what makes machine-authored rules safe."""
    rule = StandardizationRule(
        rule_id=uuid7(), field_name="full_name_normalized", rule_name="overlapping",
        pattern=r"^(?P<given_name_derived>[A-Z]+)\s+(?P<surname_derived>[A-Z]+)$",
        rule_kind=RuleKind.EXTRACT,
        target_fields=("given_name_derived", "surname_derived"),
    )
    result = shadow_evaluate(rule, person_frame([]), person_frame(["John Smith"]))
    assert result.regressions == 0


def test_a_rule_that_fixes_nothing_is_not_safe() -> None:
    rule = StandardizationRule(
        rule_id=uuid7(), field_name="full_name_normalized", rule_name="noop",
        pattern=r"^ZZZZZ$", replacement="",
    )
    result = shadow_evaluate(rule, person_frame(["John Michael Smith"]), person_frame([]))
    assert result.fixed == 0
    assert not result.safe


# ---------------------------------------------------------------------------
# AI fallback
# ---------------------------------------------------------------------------


def test_heuristic_splits_a_middle_initial_name() -> None:
    result = HeuristicStandardizer().standardize([
        StandardizationRequest("full_name", "Emma A Phillips", "EMMA A PHILLIPS",
                               ("name_parsed_cleanly",))
    ])[0]
    assert result.components["given_name_derived"] == "EMMA"
    assert result.components["surname_derived"] == "PHILLIPS"
    assert result.confidence >= 0.9


def test_heuristic_binds_particles_into_the_surname() -> None:
    result = HeuristicStandardizer().standardize([
        StandardizationRequest("full_name", "Jan van der Berg", "JAN VAN DER BERG",
                               ("name_parsed_cleanly",))
    ])[0]
    assert result.components["surname_derived"] == "VAN DER BERG"


def test_heuristic_inverts_a_comma_separated_name() -> None:
    result = HeuristicStandardizer().standardize([
        StandardizationRequest("full_name", "SMITH, JOHN MICHAEL", None,
                               ("name_parsed_cleanly",))
    ])[0]
    assert result.components["given_name_derived"] == "JOHN"
    assert result.components["surname_derived"] == "SMITH"


def test_heuristic_recovers_a_phone_from_a_field_with_an_extension() -> None:
    result = HeuristicStandardizer().standardize([
        StandardizationRequest("phone_raw", "020 7946 0958 x221", None, ("phone_syntax",),
                               context={"default_country_code": "44"})
    ])[0]
    assert result.value == "+442079460958"


def test_heuristic_splits_fused_address_tokens() -> None:
    result = HeuristicStandardizer().standardize([
        StandardizationRequest("address_line1", "12AHighStreet", None,
                               ("address_has_number",))
    ])[0]
    assert "12" in (result.value or "")


def test_heuristic_returns_one_result_per_request() -> None:
    requests = [
        StandardizationRequest("full_name", n, None, ("name_parsed_cleanly",))
        for n in ["A B", "C D E", ""]
    ]
    assert len(HeuristicStandardizer().standardize(requests)) == 3


def test_unhandled_field_is_reported_not_guessed() -> None:
    result = HeuristicStandardizer().standardize([
        StandardizationRequest("occupation", "x", None, ("whatever",))
    ])[0]
    assert not result.resolved


def test_prompt_key_is_stable_and_input_sensitive() -> None:
    """A decision must be replayable against the input that produced it."""
    a = StandardizationRequest("full_name", "X", None, ("c",))
    b = StandardizationRequest("full_name", "X", None, ("c",))
    c = StandardizationRequest("full_name", "Y", None, ("c",))
    assert a.prompt_key() == b.prompt_key() != c.prompt_key()


def test_get_standardizer_defaults_to_the_heuristic(monkeypatch) -> None:
    monkeypatch.delenv("CMDM_STANDARDIZER_MODEL", raising=False)
    assert get_standardizer().name == "heuristic-reference"


def test_missing_onnx_model_fails_loudly(tmp_path) -> None:
    """A deployment that believes it runs a model must not quietly not."""
    with pytest.raises(FileNotFoundError, match="ONNX model not found"):
        OnnxStandardizer(tmp_path / "absent.onnx")


def test_onnx_standardizer_loads_a_real_model(tmp_path) -> None:
    """Exercises the real ONNX Runtime path, not a mock of it."""
    pytest.importorskip("onnx")
    pytest.importorskip("onnxruntime")
    import onnx
    from onnx import TensorProto, helper

    node = helper.make_node("Identity", ["input"], ["output"])
    graph = helper.make_graph(
        [node], "identity",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [None, 2])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [None, 2])],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 9
    path = tmp_path / "identity.onnx"
    onnx.save(model, path)

    standardizer = OnnxStandardizer(path)
    assert standardizer.input_names == ["input"]
    assert standardizer.output_names == ["output"]


def test_onnx_without_tokenizer_raises_rather_than_degrading(tmp_path) -> None:
    pytest.importorskip("onnx")
    import onnx
    from onnx import TensorProto, helper

    node = helper.make_node("Identity", ["input"], ["output"])
    graph = helper.make_graph(
        [node], "identity",
        [helper.make_tensor_value_info("input", TensorProto.FLOAT, [None, 2])],
        [helper.make_tensor_value_info("output", TensorProto.FLOAT, [None, 2])],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 9
    path = tmp_path / "identity.onnx"
    onnx.save(model, path)

    with pytest.raises(RuntimeError, match="tokenizer"):
        OnnxStandardizer(path).standardize([
            StandardizationRequest("full_name", "x", None, ())
        ])


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


def test_pipeline_sends_only_gate_failures_to_the_model() -> None:
    """The economic argument for the whole design."""
    frame = person_frame(["John Smith", "Jane Doe", "Emma A Phillips"])
    _, report = standardize(frame, log=False)
    assert report.total_records == 3
    assert report.ai_requests == 1


def test_pipeline_leaves_a_clean_batch_untouched_by_the_model() -> None:
    frame = person_frame(["John Smith", "Jane Doe"])
    _, report = standardize(frame, log=False)
    assert report.ai_requests == 0
    assert report.ai_share == 0.0


def test_pipeline_handles_an_empty_frame() -> None:
    _, report = standardize(person_frame([]), log=False)
    assert report.total_records == 0
    assert report.ai_share == 0.0


def test_pipeline_reports_deterministic_and_final_pass_separately() -> None:
    """Reporting only the post-AI number hides what the fast path is doing."""
    frame = person_frame(["John Smith", "Emma A Phillips"])
    _, report = standardize(frame, log=False)
    assert report.gate_passed_deterministic == 1
    assert report.gate_passed == 2


def test_pipeline_marks_model_supplied_components_as_such() -> None:
    """A model guess must not be weighted like a reviewed deterministic split."""
    frame = person_frame(["Emma A Phillips"])
    out, _ = standardize(frame, log=False)
    assert out["name_parse_method"][0] == "LLM_FALLBACK"
    assert out["name_parse_confidence"][0] < 0.75


def test_report_serializes_for_storage() -> None:
    _, report = standardize(person_frame(["John Smith"]), log=False)
    assert set(report.as_dict()) >= {"total_records", "ai_share", "check_failures"}


# ---------------------------------------------------------------------------
# The loop, end to end
# ---------------------------------------------------------------------------


def test_rule_lifecycle_requires_shadow_evaluation_and_review(conn) -> None:
    """The approval gate is a database constraint, not a convention."""
    rule = StandardizationRule(
        rule_id=uuid7(), field_name="full_name_normalized", rule_name="lifecycle_test",
        pattern=r"^(?P<given_name_derived>[A-Z]+)\s+(?P<surname_derived>[A-Z]+)$",
        rule_kind=RuleKind.EXTRACT,
        target_fields=("given_name_derived", "surname_derived"),
    )
    insert_rule(conn, rule)

    # Not yet shadow-tested: approval must not take.
    assert approve_rule(conn, rule.rule_id, reviewer="s") is False
    assert load_active_rules(conn) == []

    record_shadow_result(conn, rule.rule_id, matched=10, fixed=10, regressions=0, report={})
    assert approve_rule(conn, rule.rule_id, reviewer="s", note="ok") is True
    assert [r.rule_name for r in load_active_rules(conn)] == ["lifecycle_test"]


def test_database_refuses_an_active_rule_that_regressed(conn) -> None:
    """The gate must survive a refactor of the promotion code."""
    import psycopg

    rule = StandardizationRule(
        rule_id=uuid7(), field_name="f", rule_name="regressing", pattern="x",
    )
    insert_rule(conn, rule)
    record_shadow_result(conn, rule.rule_id, matched=5, fixed=5, regressions=3, report={})
    with pytest.raises(psycopg.errors.CheckViolation):
        approve_rule(conn, rule.rule_id, reviewer="s")


def test_rejected_rules_are_kept_not_deleted(conn) -> None:
    """Otherwise the miner re-proposes the same bad idea forever."""
    rule = StandardizationRule(
        rule_id=uuid7(), field_name="f", rule_name="rejected_rule", pattern="x",
    )
    insert_rule(conn, rule)
    assert reject_rule(conn, rule.rule_id, reviewer="s", note="no") is True
    assert insert_rule(conn, rule) is None
    assert [r.rule_name for r in list_rules(conn, RuleState.REJECTED)] == ["rejected_rule"]


def test_duplicate_proposal_is_dropped(conn) -> None:
    rule = StandardizationRule(
        rule_id=uuid7(), field_name="f", rule_name="dupe", pattern="x",
    )
    assert insert_rule(conn, rule) is not None
    assert insert_rule(conn, rule) is None


def test_learning_loop_reduces_the_ai_share(conn) -> None:
    """The property the whole design exists for.

    Same batch, twice. Between the runs the miner proposes rules from the logged
    exceptions, shadow evaluation confirms they break nothing, and a steward
    approves. The second run must send strictly fewer records to the model.
    """
    # Isolate from any rule or exception another run left behind. Rolled back
    # with the rest of the test transaction.
    conn.execute("DELETE FROM mdm.standardization_rule")
    conn.execute("DELETE FROM mdm.standardization_exception")

    names = (
        ["John Smith", "Jane Doe"] * 40                 # clean, two tokens
        + ["Emma A Phillips", "Robert J Brown"] * 30    # W-I-W
        + ["John Michael Smith", "Mary Anne Jones"] * 30  # W-W-W
    )
    frame = person_frame(names)

    _, first = standardize(frame, conn=conn, batch_id=None)
    assert first.ai_requests > 0, "the fixture must actually exercise the fallback"

    target = frame.filter(pl.col("given_name_derived").is_null())
    regression = frame.filter(pl.col("given_name_derived").is_not_null())
    results = mine_and_propose(
        conn, target_corpus=target, regression_corpus=regression, min_evidence=10
    )
    assert results, "recurring shapes must produce proposals"
    assert all(r.regressions == 0 for r in results)

    for rule in list_rules(conn, RuleState.SHADOW):
        approve_rule(conn, rule.rule_id, reviewer="steward")

    _, second = standardize(frame, conn=conn, batch_id=None)

    assert second.ai_requests < first.ai_requests
    assert second.gate_passed_deterministic > first.gate_passed_deterministic
    assert second.rules_fixed > 0


def test_mining_pass_with_no_exceptions_proposes_nothing(conn) -> None:
    conn.execute("DELETE FROM mdm.standardization_exception")
    assert mine_and_propose(
        conn, target_corpus=person_frame([]), regression_corpus=person_frame([])
    ) == []
