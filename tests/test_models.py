"""Tests for the model registry.

The two local models were the last thing here that could change without a
record. A standardization rule has to be mined, shadow-tested and approved by a
named steward before it may rewrite a single field; a *model* that rewrites the
same fields — and decides which parties are one person — was selected by an
environment variable pointing at a file.

Most of what follows is about the refusals, because the refusals are the
feature. A registry that records what somebody chose is bookkeeping; one that
declines to run a model nobody measured is a control.
"""

from __future__ import annotations

import pytest

from cmdm.models import (
    ModelKind,
    ModelState,
    active_model,
    artifact_digest,
    list_models,
    promote_model,
    record_evaluation,
    register_model,
    retire_model,
)

METRICS = {"precision": 0.94, "recall": 0.78, "f1": 0.85}


@pytest.fixture
def candidate(conn):
    """A registered model that has not been evaluated."""
    model_id = register_model(
        conn, kind=ModelKind.CROSS_ENCODER, model_name="minilm-pair",
        version="1.0",
    )
    return model_id


# ---------------------------------------------------------------------------
# The refusals
# ---------------------------------------------------------------------------


def test_a_model_cannot_be_promoted_without_evidence(conn, candidate) -> None:
    """The whole point. Promoting on a hunch is the behaviour being replaced."""
    with pytest.raises(ValueError, match="never been evaluated"):
        promote_model(conn, candidate, promoted_by="arthur", note="looks fine")


def test_promotion_requires_an_approver_and_a_reason(conn, candidate) -> None:
    record_evaluation(conn, candidate, metrics=METRICS, evaluated_on="bench")

    with pytest.raises(ValueError, match="approver and a reason"):
        promote_model(conn, candidate, promoted_by="", note="fine")
    with pytest.raises(ValueError, match="approver and a reason"):
        promote_model(conn, candidate, promoted_by="arthur", note="")


def test_the_database_refuses_an_unreviewed_active_model(conn, candidate) -> None:
    """Not only the Python path. A check constraint holds the line against
    anything that writes to this table — a migration, a script, psql."""
    import psycopg

    with pytest.raises(psycopg.errors.CheckViolation):
        conn.execute(
            "UPDATE mdm.model_version SET state = 'ACTIVE' WHERE model_id = %s",
            (candidate,),
        )


def test_only_one_model_per_kind_can_be_active(conn) -> None:
    """Two active standardizers is not a configuration, it is a race between
    whichever loads first."""
    first = register_model(conn, kind=ModelKind.STANDARDIZER,
                           model_name="a", version="1")
    second = register_model(conn, kind=ModelKind.STANDARDIZER,
                            model_name="b", version="1")
    for model in (first, second):
        record_evaluation(conn, model, metrics=METRICS, evaluated_on="bench")

    promote_model(conn, first, promoted_by="arthur", note="first")
    promote_model(conn, second, promoted_by="arthur", note="better")

    active = [m for m in list_models(conn) if m.state == ModelState.ACTIVE]
    assert len(active) == 1 and active[0].model_name == "b"


# ---------------------------------------------------------------------------
# The lifecycle
# ---------------------------------------------------------------------------


def test_evaluation_moves_a_candidate_to_shadow(conn, candidate) -> None:
    """Measured is not the same as trusted: a model can improve recall and cost
    precision, and which of those matters is not a decision code can make."""
    record_evaluation(conn, candidate, metrics=METRICS, evaluated_on="bench")
    model = next(m for m in list_models(conn) if m.model_id == candidate)
    assert model.state == ModelState.SHADOW
    assert model.metrics["precision"] == 0.94


def test_promotion_retires_the_model_it_replaces(conn) -> None:
    first = register_model(conn, kind=ModelKind.CROSS_ENCODER,
                           model_name="a", version="1")
    second = register_model(conn, kind=ModelKind.CROSS_ENCODER,
                            model_name="b", version="1")
    for model in (first, second):
        record_evaluation(conn, model, metrics=METRICS, evaluated_on="bench")
    promote_model(conn, first, promoted_by="arthur", note="first")
    promote_model(conn, second, promoted_by="arthur", note="second")

    states = {m.model_name: m.state for m in list_models(conn)}
    assert states == {"a": ModelState.RETIRED, "b": ModelState.ACTIVE}


def test_a_retired_model_is_kept_not_deleted(conn, candidate) -> None:
    """Every decision it made still names it. A row that vanished would leave
    those decisions pointing at a model the store has never heard of."""
    record_evaluation(conn, candidate, metrics=METRICS, evaluated_on="bench")
    promote_model(conn, candidate, promoted_by="arthur", note="ship it")
    retire_model(conn, candidate)

    assert any(m.model_id == candidate for m in list_models(conn))
    assert active_model(conn, ModelKind.CROSS_ENCODER) is None


def test_no_active_model_is_a_normal_state(conn) -> None:
    """The default. With nothing promoted both AI paths fall back to their
    reference implementations, which is what makes the system runnable with no
    model artifact present at all."""
    assert active_model(conn, ModelKind.STANDARDIZER) is None
    assert active_model(conn, ModelKind.CROSS_ENCODER) is None


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------


def test_an_artifact_is_fingerprinted_at_registration(conn, tmp_path) -> None:
    artifact = tmp_path / "model.onnx"
    artifact.write_bytes(b"pretend this is a quantised transformer")

    model_id = register_model(
        conn, kind=ModelKind.STANDARDIZER, model_name="local", version="1",
        artifact_path=artifact,
    )
    model = next(m for m in list_models(conn) if m.model_id == model_id)
    assert model.artifact_sha256 == artifact_digest(artifact)
    assert model.runnable


def test_a_changed_artifact_stops_being_runnable(conn, tmp_path) -> None:
    """The file on disk changed after somebody approved it, so what would run
    is not what was approved. Detected rather than assumed."""
    artifact = tmp_path / "model.onnx"
    artifact.write_bytes(b"the evaluated model")
    model_id = register_model(
        conn, kind=ModelKind.STANDARDIZER, model_name="local", version="1",
        artifact_path=artifact,
    )

    artifact.write_bytes(b"something else entirely")
    model = next(m for m in list_models(conn) if m.model_id == model_id)
    assert not model.runnable


def test_a_missing_artifact_stops_being_runnable(conn, tmp_path) -> None:
    artifact = tmp_path / "model.onnx"
    artifact.write_bytes(b"here for now")
    model_id = register_model(
        conn, kind=ModelKind.CROSS_ENCODER, model_name="local", version="1",
        artifact_path=artifact,
    )
    artifact.unlink()

    model = next(m for m in list_models(conn) if m.model_id == model_id)
    assert not model.runnable


def test_a_reference_implementation_needs_no_artifact(conn) -> None:
    """Both reference engines are code, not files, and must not be treated as
    broken for having nothing on disk."""
    model_id = register_model(
        conn, kind=ModelKind.CROSS_ENCODER, model_name="feature-cross-encoder",
        version="builtin",
    )
    model = next(m for m in list_models(conn) if m.model_id == model_id)
    assert model.artifact_path is None and model.runnable


# ---------------------------------------------------------------------------
# What actually runs
# ---------------------------------------------------------------------------


def test_the_registry_outranks_the_environment_variable(conn, tmp_path,
                                                        monkeypatch) -> None:
    """An environment variable is a deployment detail nobody reviewed. It must
    not quietly displace a model somebody measured and signed for."""
    from cmdm.standardize.ai import get_standardizer

    monkeypatch.setenv("CMDM_STANDARDIZER_MODEL", str(tmp_path / "from-env.onnx"))

    artifact = tmp_path / "approved.onnx"
    artifact.write_bytes(b"approved")
    model_id = register_model(
        conn, kind=ModelKind.STANDARDIZER, model_name="approved", version="1",
        artifact_path=artifact,
    )
    record_evaluation(conn, model_id, metrics=METRICS, evaluated_on="bench")
    promote_model(conn, model_id, promoted_by="arthur", note="measured")

    # Loading a fake ONNX file fails inside onnxruntime, which is the proof the
    # approved artifact was the one selected: the env path was never opened.
    with pytest.raises(Exception) as raised:
        get_standardizer(conn=conn)
    assert "from-env" not in str(raised.value)


def test_an_approved_model_whose_artifact_changed_is_refused(
    conn, tmp_path
) -> None:
    """Refusing is the correct failure. Loading it would run a model that was
    never approved in the state it is now in."""
    from cmdm.standardize.ai import get_standardizer

    artifact = tmp_path / "approved.onnx"
    artifact.write_bytes(b"evaluated")
    model_id = register_model(
        conn, kind=ModelKind.STANDARDIZER, model_name="approved", version="1",
        artifact_path=artifact,
    )
    record_evaluation(conn, model_id, metrics=METRICS, evaluated_on="bench")
    promote_model(conn, model_id, promoted_by="arthur", note="measured")
    artifact.write_bytes(b"tampered")

    with pytest.raises(RuntimeError, match="has changed since it was evaluated"):
        get_standardizer(conn=conn)


def test_with_nothing_promoted_the_reference_engine_runs(conn) -> None:
    from cmdm.resolve.crossencoder import FeatureCrossEncoder, get_cross_encoder
    from cmdm.standardize.ai import HeuristicStandardizer, get_standardizer

    assert isinstance(get_standardizer(conn=conn), HeuristicStandardizer)
    assert isinstance(get_cross_encoder(conn), FeatureCrossEncoder)
