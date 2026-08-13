"""Tests for the work queue and the landing zone.

These run against real PostgreSQL. The properties under test — disjoint claims
across competing consumers, partial unique indexes as ON CONFLICT arbiters,
atomic land-and-enqueue — are database behaviour, and testing them against
anything other than the real engine would test the mock instead.
"""

from __future__ import annotations

import collections
import concurrent.futures as cf
import pathlib

import polars as pl
import psycopg
import pytest

from cmdm.db.queue import QUEUE_STANDARDIZE, JobState, WorkQueue, processing
from cmdm.ingest.landing import (
    ValidationIssue,
    accept_batch,
    land_batch,
    validate_batch,
)
from cmdm.ingest.mapping import load_mapping

REPO = pathlib.Path(__file__).resolve().parent.parent
LIFE_ADMIN = REPO / "src" / "cmdm" / "mappings" / "life_admin.toml"


@pytest.fixture(scope="module")
def mapping():
    return load_mapping(LIFE_ADMIN)


@pytest.fixture()
def qname() -> str:
    """A queue name unique to this test.

    Queue tests must not assume the table is globally empty. Other tests, other
    suites and a real deployment all share it, and a test that asserts on total
    depth is really asserting that nothing else has ever run.
    """
    from cmdm.model.ids import uuid7

    return f"test-{uuid7()}"


@pytest.fixture()
def raw(mapping) -> pl.DataFrame:
    """A tiny well-formed batch matching the life-admin mapping."""
    n = 4
    base = {
        "PolicyNumber": [f"POL-{i:06d}" for i in range(n)],
        "ProductCode": ["TL10"] * n, "ProductName": ["Term Life"] * n,
        "ProductLine": ["LIFE"] * n, "PlanCode": ["P1"] * n,
        "Status": ["In Force"] * n, "IssuingCompany": ["ACME"] * n,
        "BranchCode": ["BR1"] * n, "Channel": ["BROKER"] * n,
        "IssueState": ["GB"] * n, "UwClass": ["STANDARD"] * n,
        "PaymentMethod": ["DD"] * n, "PremiumFrequency": ["M"] * n,
        "ApplicationDate": ["2020-01-01"] * n, "IssueDate": ["2020-02-01"] * n,
        "EffectiveDate": ["2020-03-01"] * n, "MaturityDate": ["2040-03-01"] * n,
        "TerminationDate": [""] * n, "PaidToDate": ["2021-03-01"] * n,
        "SumAssured": ["100000"] * n, "AnnualPremium": ["1200"] * n,
        "ModalPremium": ["100"] * n, "AccountValue": ["0"] * n,
        "PolicyTerm": ["20"] * n, "PremiumTerm": ["20"] * n,
        "LastUpdatedTs": ["2024-01-01"] * n,
        "OwnerCustomerId": [f"C-{i}" for i in range(n)],
        "OwnerName": ["John Smith", "Jane Doe", "The Patel Trust", "Ann Lee"],
        "OwnerDOB": ["1980-05-01"] * n, "OwnerGender": ["M"] * n,
        "OwnerEmail": ["a@b.com"] * n, "OwnerPhone": ["020 7946 0958"] * n,
        "OwnerAddress1": ["12 High St"] * n, "OwnerAddress2": [""] * n,
        "OwnerCity": ["London"] * n, "OwnerPostcode": ["SW1A 1AA"] * n,
        "OwnerCountry": ["GB"] * n, "OwnerOccupation": ["Engineer"] * n,
        "InsuredCustomerId": [f"C-{i}" for i in range(n)],
        "InsuredName": ["John Smith", "Jane Doe", "Priya Patel", "Ann Lee"],
        "InsuredDOB": ["1980-05-01"] * n, "InsuredGender": ["M"] * n,
        "InsuredEmail": ["a@b.com"] * n, "InsuredPhone": ["020 7946 0958"] * n,
        "InsuredAddress1": ["12 High St"] * n, "InsuredAddress2": [""] * n,
        "InsuredCity": ["London"] * n, "InsuredPostcode": ["SW1A 1AA"] * n,
        "InsuredCountry": ["GB"] * n, "InsuredOccupation": ["Engineer"] * n,
        "AgentCode": ["AGT-1"] * n, "AgentName": ["Jane Broker"] * n,
        "AgentEmail": ["j@b.com"] * n, "AgentPhone": ["+44 20 1111 2222"] * n,
    }
    return pl.DataFrame(base)


# ---------------------------------------------------------------------------
# Validation (no database needed)
# ---------------------------------------------------------------------------


def test_valid_batch_is_accepted(raw, mapping) -> None:
    assert validate_batch(raw, mapping).accepted


def test_empty_batch_is_rejected(raw, mapping) -> None:
    report = validate_batch(raw.head(0), mapping)
    assert not report.accepted
    assert [i.code for i in report.errors] == ["EMPTY_BATCH"]


def test_missing_column_is_reported_by_name(raw, mapping) -> None:
    """A renamed column is the commonest feed breakage; naming it is the fix."""
    report = validate_batch(raw.drop("OwnerName"), mapping)
    assert not report.accepted
    assert "OwnerName" in report.errors[0].message


def test_missing_columns_do_not_cascade(raw, mapping) -> None:
    """One missing column must not emit an error per downstream check."""
    report = validate_batch(raw.drop("OwnerName", "OwnerDOB"), mapping)
    assert len(report.errors) == 1


def test_entirely_empty_party_block_is_an_error(raw, mapping) -> None:
    """An empty block means the mapping points at the wrong columns."""
    broken = raw.with_columns(pl.lit("").alias("InsuredName"))
    report = validate_batch(broken, mapping)
    assert "EMPTY_PARTY_BLOCK" in {i.code for i in report.errors}


def test_unparseable_dates_are_reported_with_samples(raw, mapping) -> None:
    broken = raw.with_columns(pl.lit("31/31/9999").alias("EffectiveDate"))
    report = validate_batch(broken, mapping)
    issue = next(i for i in report.issues if i.code == "DATE_FORMAT_MISMATCH")
    assert issue.sample, "an error a human must act on needs example values"


def test_a_date_of_birth_column_is_checked_too(raw, mapping) -> None:
    """DOB lives in a party block, and the check only covered policy dates.

    It is the date that matters most: date_of_birth is both a comparator and a
    veto, so a feed whose format changed weakens matching across the whole batch
    while the API reports the file as clean.
    """
    broken = raw.with_columns(pl.lit("31/31/9999").alias("OwnerDOB"))
    report = validate_batch(broken, mapping)
    issue = next(
        i for i in report.issues
        if i.code == "DATE_FORMAT_MISMATCH" and i.column == "OwnerDOB"
    )
    assert "OWNER" in issue.message


def test_a_day_first_to_month_first_switch_is_caught(raw, mapping) -> None:
    """The failure the check exists for, and the one 50% could not see.

    Under month-first only the days above 12 fail to parse, so on a real feed
    roughly two thirds still come through -- silently, and as the wrong date.
    """
    # Month-first. Only 02/01 has a day the configured day-first format accepts.
    month_first = ["02/13/1980", "02/20/1980", "02/25/1980", "02/01/1980"]
    swapped = raw.with_columns(
        pl.Series("OwnerDOB", month_first[: raw.height])
    )
    report = validate_batch(swapped, mapping)
    issue = next(
        i for i in report.issues
        if i.code == "DATE_FORMAT_MISMATCH" and i.column == "OwnerDOB"
    )
    assert "OWNER" in issue.message
    assert issue.sample, "an error a human must act on needs example values"


def test_the_validator_uses_the_mapping_formats_not_the_defaults(mapping) -> None:
    """A check looser than the pipeline it guards is worse than none.

    The module default list holds both %d/%m/%Y and %m/%d/%Y, so validating
    against it accepted a month-first column that the shredder -- which does
    pass the mapping's formats -- then nulled.
    """
    from cmdm.ingest.normalize import _DATE_FORMATS, parse_date

    assert "%m/%d/%Y" in _DATE_FORMATS
    assert "%m/%d/%Y" not in mapping.date_formats

    frame = pl.DataFrame({"d": ["02/13/1980"]})
    lenient = frame.select(parse_date(pl.col("d")).alias("x"))["x"][0]
    strict = frame.select(
        parse_date(pl.col("d"), tuple(mapping.date_formats)).alias("x")
    )["x"][0]
    assert lenient is not None, "the default list accepts month-first"
    assert strict is None, "the mapping's formats do not"


def test_a_clean_extract_raises_no_date_warnings(raw, mapping) -> None:
    """The threshold has to sit above real feeds or it is noise."""
    report = validate_batch(raw, mapping)
    assert not [i for i in report.issues if i.code == "DATE_FORMAT_MISMATCH"]


def test_duplicate_policy_numbers_warn_but_do_not_block(raw, mapping) -> None:
    """A full extract concatenated with a delta is normal, not a fault."""
    doubled = pl.concat([raw, raw])
    report = validate_batch(doubled, mapping)
    assert report.accepted
    assert "DUPLICATE_POLICY_NUMBERS" in {i.code for i in report.warnings}


def test_warnings_alone_do_not_block_a_batch(raw, mapping) -> None:
    sparse = raw.with_columns(
        pl.when(pl.int_range(pl.len()) < 1).then(pl.col("InsuredName")).otherwise(pl.lit(""))
        .alias("InsuredName")
    )
    report = validate_batch(sparse, mapping)
    assert report.accepted
    assert report.warnings


def test_validation_report_serializes_for_storage(raw, mapping) -> None:
    payload = validate_batch(raw, mapping).as_dict()
    assert set(payload) >= {"row_count", "accepted", "issues"}


def test_validation_issue_serializes() -> None:
    issue = ValidationIssue("ERROR", "X", "msg", column="c", row_count=2, sample=("a",))
    assert issue.as_dict()["sample"] == ["a"]


# ---------------------------------------------------------------------------
# Queue
# ---------------------------------------------------------------------------


def test_enqueue_and_claim_roundtrip(conn, qname) -> None:
    q = WorkQueue(conn)
    job_id = q.enqueue(qname, {"hello": "world"})
    claimed = q.claim(qname, batch=10)
    assert [j.job_id for j in claimed] == [job_id]
    assert claimed[0].payload == {"hello": "world"}
    assert claimed[0].attempts == 1


def test_claim_returns_nothing_when_queue_is_empty(conn) -> None:
    assert WorkQueue(conn).claim("no-such-queue") == []


def test_claimed_job_is_not_claimed_twice(conn, qname) -> None:
    q = WorkQueue(conn)
    q.enqueue(qname, {})
    assert len(q.claim(qname)) == 1
    assert q.claim(qname) == []


def test_dedupe_key_blocks_a_second_live_job(conn, qname) -> None:
    """A retried API call must not queue the same batch twice."""
    q = WorkQueue(conn)
    assert q.enqueue(qname, {}, dedupe_key="k") is not None
    assert q.enqueue(qname, {}, dedupe_key="k") is None


def test_dedupe_key_frees_up_once_the_job_is_done(conn, qname) -> None:
    """Deliberate reprocessing of a batch must remain possible."""
    q = WorkQueue(conn)
    first = q.enqueue(qname, {}, dedupe_key="k")
    q.claim(qname)
    q.complete(first)
    assert q.enqueue(qname, {}, dedupe_key="k") is not None


def test_null_dedupe_keys_do_not_collide(conn, qname) -> None:
    q = WorkQueue(conn)
    assert q.enqueue(qname, {}) is not None
    assert q.enqueue(qname, {}) is not None


def test_failure_schedules_a_retry(conn, qname) -> None:
    q = WorkQueue(conn)
    q.enqueue(qname, {}, max_attempts=3)
    job = q.claim(qname)[0]
    assert q.fail(job.job_id, "boom") == JobState.FAILED


def test_repeated_failure_buries_the_job(conn, qname) -> None:
    """One poison message must not occupy a worker forever."""
    q = WorkQueue(conn)
    q.enqueue(qname, {}, max_attempts=2)
    state = None
    for _ in range(2):
        # Backoff pushes visible_at forward, so make the job claimable again.
        conn.execute("UPDATE mdm.work_queue SET visible_at = now() - interval '1 hour'")
        job = q.claim(qname)[0]
        state = q.fail(job.job_id, "boom", backoff_base_seconds=0)
    assert state == JobState.DEAD
    assert q.claim(qname) == [], "a dead job must not be claimed again"


def test_expired_lease_returns_the_job(conn, qname) -> None:
    """A crashed worker's jobs must return without operator intervention."""
    q = WorkQueue(conn)
    q.enqueue(qname, {})
    q.claim(qname, lease_seconds=1)
    conn.execute("UPDATE mdm.work_queue SET visible_at = now() - interval '1 second'")
    assert q.reap_expired(qname) == 1
    conn.execute("UPDATE mdm.work_queue SET visible_at = now() - interval '1 hour'")
    assert len(q.claim(qname)) == 1


def test_priority_orders_the_claim(conn, qname) -> None:
    q = WorkQueue(conn)
    q.enqueue(qname, {"n": "low"}, priority=200)
    q.enqueue(qname, {"n": "high"}, priority=1)
    assert q.claim(qname)[0].payload["n"] == "high"


def test_processing_helper_completes_on_success(conn, qname) -> None:
    q = WorkQueue(conn)
    q.enqueue(qname, {})
    job = q.claim(qname)[0]
    with processing(q, job) as result:
        result["rows"] = 5
    assert q.depth(qname) == {"DONE": 1}


def test_processing_helper_fails_and_reraises(conn, qname) -> None:
    """Swallowing the exception would turn a systemic failure into a silent one."""
    q = WorkQueue(conn)
    q.enqueue(qname, {})
    job = q.claim(qname)[0]
    with pytest.raises(RuntimeError), processing(q, job):
        raise RuntimeError("handler blew up")
    assert q.depth(qname) == {"FAILED": 1}


def test_depth_and_age_report_queue_health(conn, qname) -> None:
    q = WorkQueue(conn)
    q.enqueue(qname, {})
    assert q.depth(qname) == {"PENDING": 1}
    assert q.oldest_pending_age_seconds(qname) is not None
    assert q.oldest_pending_age_seconds("empty-queue") is None


def test_competing_consumers_get_disjoint_jobs(migrated: str) -> None:
    """SKIP LOCKED is the whole reason this queue lives in Postgres.

    Uses real concurrent connections rather than the rollback fixture, since the
    behaviour under test only exists between separate transactions.
    """
    queue_name = "concurrency-test"
    total, workers = 300, 6

    with psycopg.connect(migrated) as setup:
        q = WorkQueue(setup)
        for i in range(total):
            q.enqueue(queue_name, {"n": i})
        setup.commit()

    def drain(worker_id: int) -> list:
        claimed = []
        with psycopg.connect(migrated) as c:
            q = WorkQueue(c)
            while True:
                jobs = q.claim(queue_name, batch=13, worker=f"w{worker_id}")
                c.commit()
                if not jobs:
                    return claimed
                claimed += [j.job_id for j in jobs]
                for j in jobs:
                    q.complete(j.job_id)
                c.commit()

    try:
        with cf.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(drain, range(workers)))

        claimed = [j for r in results for j in r]
        overlaps = [k for k, v in collections.Counter(claimed).items() if v > 1]
        assert len(claimed) == total
        assert overlaps == [], "two workers claimed the same job"
    finally:
        with psycopg.connect(migrated) as c:
            c.execute("DELETE FROM mdm.work_queue WHERE queue_name = %s", (queue_name,))
            c.commit()


# ---------------------------------------------------------------------------
# Landing
# ---------------------------------------------------------------------------


def test_land_batch_writes_rows(conn, raw, mapping) -> None:
    from cmdm.model.ids import uuid7

    batch_id = uuid7()
    assert land_batch(conn, raw, mapping, batch_id=batch_id) == raw.height


def test_landing_is_idempotent(conn, raw, mapping) -> None:
    """A re-run of the same batch must not duplicate landed rows."""
    from cmdm.model.ids import uuid7

    batch_id = uuid7()
    land_batch(conn, raw, mapping, batch_id=batch_id)
    assert land_batch(conn, raw, mapping, batch_id=batch_id) == 0


def test_landing_keeps_only_mapped_columns(conn, raw, mapping) -> None:
    """Unmapped columns are PII nothing reads but everything must protect."""
    from cmdm.model.ids import uuid7

    extra = raw.with_columns(pl.lit("secret").alias("UnmappedSensitiveColumn"))
    batch_id = uuid7()
    land_batch(conn, extra, mapping, batch_id=batch_id)
    row = conn.execute(
        "SELECT payload FROM mdm.source_record WHERE source_batch_id = %s LIMIT 1",
        (str(batch_id),),
    ).fetchone()
    assert "UnmappedSensitiveColumn" not in row[0]


def test_accept_batch_lands_and_enqueues_together(conn, raw, mapping) -> None:
    """The atomicity that justifies a Postgres-backed queue."""
    batch_id, report, enqueued = accept_batch(conn, raw, mapping, origin="TEST")
    assert report.accepted and enqueued

    landed = conn.execute(
        "SELECT count(*) FROM mdm.source_record WHERE source_batch_id = %s", (str(batch_id),)
    ).fetchone()[0]
    queued = conn.execute(
        "SELECT count(*) FROM mdm.work_queue WHERE queue_name = %s AND payload->>'batch_id' = %s",
        (QUEUE_STANDARDIZE, str(batch_id)),
    ).fetchone()[0]
    assert landed == raw.height
    assert queued == 1


def test_rejected_batch_lands_nothing_and_queues_nothing(conn, raw, mapping) -> None:
    batch_id, report, enqueued = accept_batch(conn, raw.head(0), mapping, origin="TEST")
    assert not report.accepted and not enqueued
    landed = conn.execute(
        "SELECT count(*) FROM mdm.source_record WHERE source_batch_id = %s", (str(batch_id),)
    ).fetchone()[0]
    assert landed == 0


def test_rejected_batch_is_still_recorded(conn, raw, mapping) -> None:
    """A rejected file must be diagnosable, so the attempt is kept."""
    batch_id, _, _ = accept_batch(conn, raw.head(0), mapping, origin="TEST")
    row = conn.execute(
        "SELECT state, validation_report FROM mdm.ingest_batch WHERE batch_id = %s",
        (batch_id,),
    ).fetchone()
    assert row[0] == "REJECTED"
    assert row[1]["issues"]


def test_identical_redelivery_is_ignored(conn, raw, mapping) -> None:
    """Feeds re-send far more often than anyone expects."""
    accept_batch(conn, raw, mapping, origin="TEST")
    _, report, enqueued = accept_batch(conn, raw, mapping, origin="TEST")
    assert not enqueued
    assert "DUPLICATE_BATCH" in {i.code for i in report.issues}


def test_redelivery_does_not_duplicate_landed_rows(conn, raw, mapping) -> None:
    accept_batch(conn, raw, mapping, origin="TEST")
    before = conn.execute("SELECT count(*) FROM mdm.source_record").fetchone()[0]
    accept_batch(conn, raw, mapping, origin="TEST")
    after = conn.execute("SELECT count(*) FROM mdm.source_record").fetchone()[0]
    assert before == after


def test_changed_file_from_same_source_is_a_new_batch(conn, raw, mapping) -> None:
    accept_batch(conn, raw, mapping, origin="TEST")
    changed = raw.with_columns(pl.lit("Changed Name").alias("OwnerName"))
    _, report, enqueued = accept_batch(conn, changed, mapping, origin="TEST")
    assert enqueued


def test_source_timestamp_falls_back_to_ingest_time(conn, raw, mapping) -> None:
    """A null here would drop the record out of every MOST_RECENT comparison."""
    from cmdm.model.ids import uuid7

    no_ts = raw.with_columns(pl.lit("").alias("LastUpdatedTs"))
    batch_id = uuid7()
    land_batch(conn, no_ts, mapping, batch_id=batch_id)
    nulls = conn.execute(
        "SELECT count(*) FROM mdm.source_record "
        "WHERE source_batch_id = %s AND source_timestamp IS NULL",
        (str(batch_id),),
    ).fetchone()[0]
    assert nulls == 0
