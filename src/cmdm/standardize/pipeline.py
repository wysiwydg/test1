"""Standardization pipeline: the four stages wired together.

    raw records
        │
        ├── (a) vectorized deterministic pass ....... every record, column speed
        │        built-in kernels + ACTIVE learned rules
        │
        ├── (b) quality gate ....................... every record, one pass
        │        per-field checks, failures named
        │
        ├── (c) AI fallback ........................ gate failures only
        │        local model, per record, logged
        │
        └── (d) mining agent ....................... offline, over the log
                 recurring pattern -> proposed rule -> shadow -> human -> (a)

The arrow from (d) back to (a) is what makes this worth building. Work the model
does once becomes a deterministic rule, and the same input never reaches a model
again. The AI share of the batch is therefore a number that should fall over
time, and it is measured on every run rather than assumed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

import polars as pl
import psycopg
from psycopg.types.json import Jsonb

from cmdm.model.ids import uuid7
from cmdm.standardize.ai import (
    StandardizationRequest,
    Standardizer,
)
from cmdm.standardize.gate import (
    FAILED_CHECKS_COLUMN,
    GATE_PASSED_COLUMN,
    apply_gate,
    checks_for,
    gate_summary,
)
from cmdm.standardize.rules import StandardizationRule, apply_rules, load_active_rules

__all__ = ["StandardizationReport", "standardize", "log_exceptions"]

#: Fields the AI fallback is allowed to be asked about, and which gate failure
#: routes to which field. A check with no entry here fails the gate but does not
#: reach a model -- the record is flagged for a steward instead. That is
#: deliberate: not every quality problem has a machine answer, and sending one
#: to a model anyway produces a confident guess where "I don't know" was correct.
CHECK_TO_FIELD: dict[str, str] = {
    "name_token_count": "full_name",
    "name_parsed_cleanly": "full_name",
    "name_no_digits": "full_name",
    "address_has_number": "address_line1",
    "address_split": "address_line1",
    "phone_syntax": "phone_raw",
}


@dataclass(slots=True)
class StandardizationReport:
    """What one standardization run did."""

    batch_id: uuid.UUID | None = None
    total_records: int = 0
    #: Gate result after the deterministic pass and learned rules, before any
    #: model runs. Kept separate from the final count: reporting only the number
    #: after AI would hide how much work the deterministic path is doing, which
    #: is the one thing this design needs to be able to see.
    gate_passed_deterministic: int = 0
    gate_passed: int = 0
    ai_requests: int = 0
    ai_resolved: int = 0
    ai_latency_ms: int = 0
    rules_applied: int = 0
    rules_fixed: int = 0
    check_failures: dict[str, int] = field(default_factory=dict)
    model_name: str = "none"

    @property
    def ai_share(self) -> float:
        """Fraction of records that needed a model.

        The headline number for the whole design. It should fall as learned
        rules accumulate; if it does not, the mining loop is not working.
        """
        return self.ai_requests / self.total_records if self.total_records else 0.0

    @property
    def gate_pass_rate(self) -> float:
        return self.gate_passed / self.total_records if self.total_records else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "batch_id": str(self.batch_id) if self.batch_id else None,
            "total_records": self.total_records,
            "gate_passed_deterministic": self.gate_passed_deterministic,
            "gate_passed": self.gate_passed,
            "gate_pass_rate": round(self.gate_pass_rate, 4),
            "deterministic_pass_rate": round(
                self.gate_passed_deterministic / self.total_records, 4
            ) if self.total_records else 0.0,
            "ai_requests": self.ai_requests,
            "ai_resolved": self.ai_resolved,
            "ai_share": round(self.ai_share, 4),
            "ai_latency_ms": self.ai_latency_ms,
            "rules_applied": self.rules_applied,
            "rules_fixed": self.rules_fixed,
            "check_failures": self.check_failures,
            "model_name": self.model_name,
        }


def _requests_from_failures(
    failures: pl.DataFrame, default_country_code: str
) -> tuple[list[StandardizationRequest], list[int]]:
    """Build model requests from gated-out records.

    Returns the requests and the row indices they came from, so results can be
    written back positionally without a join.

    A record failing several checks produces one request for the highest-value
    field rather than one per check. Asking a model the same record three times
    costs three inferences to get one answer.
    """
    requests: list[StandardizationRequest] = []
    indices: list[int] = []

    for index, row in enumerate(failures.iter_rows(named=True)):
        checks = tuple(row.get(FAILED_CHECKS_COLUMN) or ())
        target_field = next(
            (CHECK_TO_FIELD[c] for c in checks if c in CHECK_TO_FIELD), None
        )
        if target_field is None or target_field not in failures.columns:
            continue

        raw = row.get(target_field)
        if raw is None or not str(raw).strip():
            continue

        requests.append(
            StandardizationRequest(
                field_name=target_field,
                raw_value=str(raw),
                deterministic_value=row.get(f"{target_field}_normalized")
                or row.get("full_name_normalized"),
                failed_checks=checks,
                context={
                    "party_type": row.get("party_type"),
                    "postal_code": row.get("postal_code"),
                    "country_code": row.get("country_code"),
                    "default_country_code": default_country_code,
                },
            )
        )
        indices.append(index)

    return requests, indices


def standardize(
    frame: pl.DataFrame,
    *,
    conn: psycopg.Connection | None = None,
    standardizer: Standardizer | None = None,
    rules: Sequence[StandardizationRule] | None = None,
    batch_id: uuid.UUID | None = None,
    default_country_code: str = "44",
    log: bool = True,
) -> tuple[pl.DataFrame, StandardizationReport]:
    """Run the full standardization pipeline over a shredded frame.

    Returns the standardized frame and a report. The frame keeps its
    ``failed_checks`` and ``gate_passed`` columns, recomputed after every stage,
    so a caller can see what remains unresolved rather than being told
    everything is fine.
    """
    report = StandardizationReport(batch_id=batch_id, total_records=frame.height)
    if frame.height == 0:
        return frame, report

    checks = checks_for(frame.columns)

    # -- (a) deterministic: built-in kernels have already run in the shredder;
    #        learned rules are applied here, restricted to records that failed
    #        the gate so an approved rule cannot perturb records already fine.
    if rules is None and conn is not None:
        rules = load_active_rules(conn)
    rules = list(rules or [])

    gated = apply_gate(frame, checks).collect()
    before_rules_passed = int(gated[GATE_PASSED_COLUMN].sum())

    if rules:
        report.rules_applied = len(rules)
        # only_where reads gate_passed, so the gate columns must still be
        # present when the rules run; they are recomputed from scratch after.
        ruled = apply_rules(gated, rules, only_where=~pl.col(GATE_PASSED_COLUMN))
        gated = apply_gate(
            ruled.drop([FAILED_CHECKS_COLUMN, GATE_PASSED_COLUMN]), checks
        ).collect()
        report.rules_fixed = int(gated[GATE_PASSED_COLUMN].sum()) - before_rules_passed

    # -- (b) gate
    report.gate_passed_deterministic = int(gated[GATE_PASSED_COLUMN].sum())
    report.gate_passed = report.gate_passed_deterministic
    summary = gate_summary(gated)
    report.check_failures = {
        r["check"]: int(r["failures"]) for r in summary.iter_rows(named=True)
    }

    # -- (c) AI fallback, on gate failures only
    failures = gated.filter(~pl.col(GATE_PASSED_COLUMN))
    # No engine handed in means "use whatever this store has approved". The
    # registry is consulted rather than defaulting straight to the reference
    # implementation, so promoting a model actually changes what runs.
    if standardizer is None:
        from cmdm.standardize.ai import get_standardizer

        standardizer = get_standardizer(conn=conn)
    report.model_name = standardizer.name

    if failures.height:
        requests, indices = _requests_from_failures(failures, default_country_code)
        report.ai_requests = len(requests)

        if requests:
            results = standardizer.standardize(requests)
            report.ai_resolved = sum(1 for r in results if r.resolved)
            report.ai_latency_ms = sum(r.latency_ms for r in results)

            gated = _write_back(gated, failures, indices, results)
            gated = apply_gate(
                gated.drop([FAILED_CHECKS_COLUMN, GATE_PASSED_COLUMN]), checks
            ).collect()
            report.gate_passed = int(gated[GATE_PASSED_COLUMN].sum())

            if log and conn is not None:
                log_exceptions(conn, failures, indices, requests, results, batch_id=batch_id)

    return gated, report


def _write_back(
    gated: pl.DataFrame,
    failures: pl.DataFrame,
    indices: Sequence[int],
    results: Sequence[Any],
) -> pl.DataFrame:
    """Apply model results to the frame.

    Joined on a synthetic row index rather than on any business key: the frame
    at this point may legitimately contain records that are not yet
    distinguishable from one another, which is the very problem the pipeline
    exists to fix.

    Model output only fills columns that are empty. A fallback correcting a
    value the deterministic pass established would silently override reviewed
    logic with a guess.
    """
    if not indices:
        return gated

    # Map position-in-failures back to position-in-gated.
    failure_positions = (
        gated.with_row_index("_row")
        .filter(~pl.col(GATE_PASSED_COLUMN))["_row"]
        .to_list()
    )

    components: dict[str, dict[int, str]] = {}
    for offset, result in zip(indices, results, strict=True):
        if not result.resolved:
            continue
        row_index = failure_positions[offset]
        for column, value in (result.components or {}).items():
            if value:
                components.setdefault(column, {})[row_index] = value

    if not components:
        return gated

    out = gated.with_row_index("_row")
    for column, mapping in components.items():
        if column not in out.columns:
            continue
        patch = pl.DataFrame(
            {"_row": list(mapping.keys()), "_patch": list(mapping.values())},
            schema={"_row": out.schema["_row"], "_patch": pl.String},
        )
        out = out.join(patch, on="_row", how="left").with_columns(
            pl.coalesce(pl.col(column), pl.col("_patch")).alias(column)
        ).drop("_patch")

    # The parse confidence must reflect that a model supplied the components,
    # so the matcher weights them accordingly rather than treating a model guess
    # as equal to a reviewed deterministic split.
    if "name_parse_method" in out.columns and "given_name_derived" in components:
        patched_rows = list(components.get("given_name_derived", {}))
        out = out.with_columns(
            pl.when(pl.col("_row").is_in(patched_rows))
            .then(pl.lit("LLM_FALLBACK"))
            .otherwise(pl.col("name_parse_method"))
            .alias("name_parse_method"),
            pl.when(pl.col("_row").is_in(patched_rows))
            .then(pl.lit(0.6))
            .otherwise(pl.col("name_parse_confidence"))
            .alias("name_parse_confidence"),
        )

    return out.drop("_row")


def log_exceptions(
    conn: psycopg.Connection,
    failures: pl.DataFrame,
    indices: Sequence[int],
    requests: Sequence[StandardizationRequest],
    results: Sequence[Any],
    *,
    batch_id: uuid.UUID | None = None,
) -> int:
    """Record every AI invocation.

    This is both the audit trail for the model path and the corpus the mining
    agent reads. A pattern is only worth a deterministic rule if it recurs, and
    recurrence is only visible if every exception is kept — so this is not
    optional instrumentation, it is the input to the loop.
    """
    if not requests:
        return 0

    rows = []
    for offset, request, result in zip(indices, requests, results, strict=True):
        source_record_id = None
        if "source_record_id" in failures.columns:
            source_record_id = failures["source_record_id"][offset]
        rows.append(
            (
                uuid7(), batch_id, source_record_id, request.field_name,
                request.raw_value, request.deterministic_value,
                list(request.failed_checks), result.value, result.confidence,
                result.model_name, result.model_version, request.prompt_key(),
                Jsonb({"components": result.components, "note": result.note}),
                result.latency_ms,
            )
        )

    with conn.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO mdm.standardization_exception
                (exception_id, batch_id, source_record_id, field_name, raw_value,
                 deterministic_value, failed_checks, ai_value, ai_confidence,
                 model_name, model_version, prompt_hash, model_output, latency_ms)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            rows,
        )
    return len(rows)
