"""Tests for the worker and for the three consoles as a person uses them.

The consoles are exercised over HTTP rather than by calling the handlers,
because most of what makes a console work is not in the handler: authentication
by cookie rather than by header, the redirect that sends an unauthenticated
browser to a form it can act on, the multipart upload, and the fact that
pressing the process button twice must not corrupt anything.

Unlike the rest of the suite these tests commit. They have to: a worker claims
its job on one connection and does the work on another, and a console request is
served by the pool rather than by the test's connection. So this module clears
the store around itself instead of relying on the shared rollback fixture.
"""

from __future__ import annotations

import io
import pathlib
import re
import uuid

import polars as pl
import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
SAMPLE = REPO / "data" / "life_admin_sample.csv"

#: Enough rows to produce a grey zone without paying for the whole extract.
SAMPLE_ROWS = 600

TABLES = """
    mdm.person, mdm.policy, mdm.relationship,
    mdm.person_master, mdm.policy_master,
    mdm.person_xref, mdm.policy_xref, mdm.attribute_provenance,
    mdm.match_pair, mdm.resolution_run, mdm.match_audit,
    mdm.source_record, mdm.ingest_batch, mdm.work_queue,
    mdm.standardization_exception, mdm.standardization_rule,
    mdm.steward_action
"""


@pytest.fixture(autouse=True)
def _hash_key(monkeypatch):
    """Identifier hashing is keyed; the pipeline refuses to run without one."""
    monkeypatch.setenv("CMDM_ID_HASH_KEY", "test-key-not-for-production")


def _clear(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn) as connection:
        connection.execute(f"TRUNCATE {TABLES} CASCADE")
        connection.commit()


@pytest.fixture
def store(migrated: str):
    """A committed connection over an empty store, cleared again afterwards."""
    import psycopg

    _clear(migrated)
    connection = psycopg.connect(migrated)
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()
        _clear(migrated)


@pytest.fixture
def mapping():
    from cmdm.ingest.mapping import load_mapping

    return load_mapping(REPO / "src" / "cmdm" / "mappings" / "life_admin.toml")


@pytest.fixture
def sample() -> pl.DataFrame:
    if not SAMPLE.exists():  # pragma: no cover - environment dependent
        pytest.skip("run `python -m scripts.generate_sample_data` first")
    return pl.read_csv(SAMPLE, infer_schema_length=0).head(SAMPLE_ROWS)


@pytest.fixture
def landed(store, mapping, sample):
    """A batch accepted, landed, queued and committed."""
    from cmdm.ingest.landing import accept_batch

    batch_id, report, enqueued = accept_batch(
        store, sample, mapping, origin="TEST", filename="sample.csv",
        submitted_by="pytest",
    )
    assert report.accepted and enqueued
    store.commit()
    return batch_id


@pytest.fixture
def processed(store, landed):
    """That batch, run through the pipeline and committed."""
    from cmdm.worker import process_batch

    result = process_batch(store, landed)
    store.commit()
    return result


# ---------------------------------------------------------------------------
# Worker
# ---------------------------------------------------------------------------


def test_the_batch_records_which_mapping_read_it(store, landed, mapping):
    """A worker holding only a batch id has to be able to load the mapping.

    It recorded the source system instead, so every batch named a mapping file
    that does not exist and nothing could reprocess one.
    """
    from cmdm.worker import mapping_for_batch

    assert mapping_for_batch(store, landed).source_system == mapping.source_system


def test_process_batch_produces_golden_records(store, landed, processed):
    assert processed.golden_persons > 0
    state = store.execute(
        "SELECT state FROM mdm.ingest_batch WHERE batch_id = %s", (landed,)
    ).fetchone()[0]
    assert state == "COMPLETED"


def test_reprocessing_a_batch_changes_nothing(store, landed, processed):
    """The ingestion console offers a process button. Pressing it twice is not
    an error and must not manufacture a second version of every record."""
    from cmdm.worker import process_batch

    second = process_batch(store, landed)

    assert second.golden_persons == processed.golden_persons
    assert second.writes["person"]["inserted"] == 0
    assert second.writes["person"]["changed"] == 0
    assert second.writes["policy"]["changed"] == 0


def test_backfill_reruns_every_landed_batch(migrated, store, landed):
    """The upgrade path. A release that computes something the last one did not
    has to be able to fill in the gap from the landing zone, without anybody
    re-uploading a file they already sent."""
    import psycopg

    from cmdm.worker import _backfill

    store.commit()

    def connect_pool():
        return psycopg.connect(migrated)

    assert _backfill(connect_pool) == 0

    with psycopg.connect(migrated) as check:
        persons = check.execute(
            "SELECT count(*) FROM mdm.person WHERE is_current"
        ).fetchone()[0]
        edges = check.execute(
            "SELECT count(*) FROM mdm.relationship WHERE is_current"
        ).fetchone()[0]
        keys = check.execute("SELECT count(*) FROM mdm.policy_xref").fetchone()[0]
    assert persons and edges and keys, "backfill produced nothing from a landed batch"


def test_backfill_twice_writes_nothing_the_second_time(migrated, store, processed):
    """It is offered as the thing to run after an update, so it will be run
    twice by somebody who is not sure whether the first one worked."""
    import psycopg

    from cmdm.worker import _backfill

    store.commit()

    def connect_pool():
        return psycopg.connect(migrated)

    _backfill(connect_pool)
    with psycopg.connect(migrated) as check:
        before = check.execute("SELECT count(*) FROM mdm.person").fetchone()[0]

    _backfill(connect_pool)
    with psycopg.connect(migrated) as check:
        after = check.execute("SELECT count(*) FROM mdm.person").fetchone()[0]

    assert after == before, "a second backfill manufactured new versions"


def test_backfill_on_an_empty_landing_zone_is_not_an_error(migrated):
    """Running it on a fresh install should say so, not fail."""
    import psycopg

    from cmdm.worker import _backfill

    _clear(migrated)
    assert _backfill(lambda: psycopg.connect(migrated)) == 0


def test_the_landed_frame_carries_the_record_id(store, landed, mapping):
    """Survivorship needs it to name a winner and to break ties the same way
    twice."""
    from cmdm.worker import RECORD_ID_COLUMN, load_batch_frame

    frame = load_batch_frame(store, landed, mapping)
    assert RECORD_ID_COLUMN in frame.columns
    assert frame[RECORD_ID_COLUMN].null_count() == 0


def test_drain_claims_and_completes_the_queued_job(migrated, store, landed):
    import psycopg

    from cmdm.worker import drain

    outcomes = drain(lambda: psycopg.connect(migrated), limit=1)
    assert len(outcomes) == 1 and outcomes[0]["ok"]

    state = store.execute(
        "SELECT state FROM mdm.work_queue WHERE dedupe_key = %s",
        (f"batch:{landed}",),
    ).fetchone()[0]
    assert state == "DONE"


def test_a_failing_job_does_not_take_the_worker_down(migrated, store):
    """A batch that cannot be processed is a batch-shaped problem."""
    import psycopg

    from cmdm.db.queue import QUEUE_STANDARDIZE, WorkQueue
    from cmdm.worker import drain

    WorkQueue(store).enqueue(
        QUEUE_STANDARDIZE, {"batch_id": str(uuid.uuid4())}, dedupe_key="bogus"
    )
    store.commit()

    outcomes = drain(lambda: psycopg.connect(migrated), limit=1)
    assert len(outcomes) == 1
    assert outcomes[0]["ok"] is False

    state, attempts = store.execute(
        "SELECT state, attempts FROM mdm.work_queue WHERE dedupe_key = 'bogus'"
    ).fetchone()
    assert state == "FAILED" and attempts == 1


# ---------------------------------------------------------------------------
# The match ledger
# ---------------------------------------------------------------------------


def test_resolution_decisions_reach_the_ledger(store, processed):
    """It stayed empty because the pipeline passed no connection through, and
    the steward queue reads this table."""
    assert store.execute("SELECT count(*) FROM mdm.match_pair").fetchone()[0] > 0


def test_the_ledger_keeps_both_id_spaces(store, processed):
    row = store.execute(
        """
        SELECT left_source_identity, right_source_identity,
               left_person_id, right_person_id
        FROM mdm.match_pair WHERE zone = 'AUTO_MATCH' LIMIT 1
        """
    ).fetchone()
    if row is None:  # pragma: no cover - depends on the sample
        pytest.skip("no auto-matched pair in this sample")
    left_identity, right_identity, left_person, right_person = row
    assert left_identity != right_identity
    # An auto-match merged the two sides, so both landed in one golden person —
    # which is why the ledger cannot be keyed on the golden ids.
    assert left_person == right_person


def test_a_steward_decision_is_read_by_the_next_run(store, landed, processed):
    """Otherwise the review is spent again on every run and nothing changes."""
    from cmdm.resolve import STEWARD, load_steward_decisions
    from cmdm.worker import process_batch

    pair = store.execute(
        "SELECT pair_id, left_source_identity, right_source_identity "
        "FROM mdm.match_pair WHERE zone = 'GREY' AND final_decision <> 'MATCH' "
        "LIMIT 1"
    ).fetchone()
    if pair is None:  # pragma: no cover - depends on the sample
        pytest.skip("no undecided grey pair in this sample")

    store.execute(
        "UPDATE mdm.match_pair SET final_decision = 'MATCH', decided_by = %s "
        "WHERE pair_id = %s",
        (STEWARD, pair[0]),
    )
    assert load_steward_decisions(store)[(pair[1], pair[2])] == "MATCH"

    second = process_batch(store, landed)
    assert second.resolution.steward_overrides >= 1


def test_a_steward_merge_actually_merges(store, landed, processed):
    from cmdm.resolve import STEWARD
    from cmdm.worker import process_batch

    rejected = store.execute(
        "SELECT pair_id FROM mdm.match_pair "
        "WHERE zone = 'GREY' AND final_decision <> 'MATCH' LIMIT 5"
    ).fetchall()
    if not rejected:  # pragma: no cover - depends on the sample
        pytest.skip("no undecided grey pair in this sample")

    store.execute(
        "UPDATE mdm.match_pair SET final_decision = 'MATCH', decided_by = %s "
        "WHERE pair_id = ANY(%s)",
        (STEWARD, [r[0] for r in rejected]),
    )
    after = process_batch(store, landed)
    assert after.golden_persons < processed.golden_persons


# ---------------------------------------------------------------------------
# Consoles
# ---------------------------------------------------------------------------


@pytest.fixture
def console(migrated, monkeypatch):
    """A TestClient and a sign-in helper, wired to the test database."""
    from fastapi.testclient import TestClient

    from cmdm.governance.rbac import Role, create_principal

    monkeypatch.setenv("CMDM_DSN", migrated)
    from cmdm.db import engine

    engine.close_pool()

    import psycopg

    secrets = {}
    with psycopg.connect(migrated) as setup:
        setup.execute("DELETE FROM mdm.principal WHERE subject LIKE 'console-%'")
        for role in (Role.VIEWER, Role.INGESTOR, Role.STEWARD, Role.ADMIN):
            _, secret = create_principal(setup, f"console-{role.lower()}@x.com", [role])
            secrets[role] = secret
        setup.commit()

    from cmdm.api.app import create_app

    with TestClient(create_app()) as test_client:

        def sign_in(role: str):
            test_client.cookies.clear()
            response = test_client.post(
                "/console/login", data={"key": secrets[role]}, follow_redirects=False
            )
            assert response.status_code == 303
            return test_client

        yield sign_in

    engine.close_pool()
    with psycopg.connect(migrated) as teardown:
        teardown.execute("DELETE FROM mdm.principal WHERE subject LIKE 'console-%'")
        teardown.commit()


def test_the_ingestion_console_offers_the_installed_mappings(console):
    from cmdm.governance.rbac import Role

    page = console(Role.INGESTOR).get("/console/ingest")
    assert page.status_code == 200
    assert "life_admin" in page.text


def test_uploading_through_the_console_lands_and_queues(console, store, sample):
    from cmdm.governance.rbac import Role

    csv = io.BytesIO()
    sample.head(50).write_csv(csv)
    client = console(Role.INGESTOR)
    response = client.post(
        "/console/ingest/upload",
        data={"mapping_name": "life_admin"},
        files={"file": ("console.csv", csv.getvalue(), "text/csv")},
    )
    assert response.status_code == 200

    page = client.get("/console/ingest").text
    assert "console.csv" in page
    assert "Process 1 queued batch(es) now" in page


def test_the_process_button_runs_the_batch(console, store, landed):
    from cmdm.governance.rbac import Role

    client = console(Role.INGESTOR)
    client.post("/console/ingest/process")

    state = store.execute(
        "SELECT state FROM mdm.ingest_batch WHERE batch_id = %s", (landed,)
    ).fetchone()[0]
    assert state == "COMPLETED"


def test_the_console_refuses_a_mapping_outside_the_mappings_directory(console):
    from cmdm.governance.rbac import Role

    response = console(Role.INGESTOR).post(
        "/console/ingest/upload",
        data={"mapping_name": "../../../etc/passwd"},
        files={"file": ("x.csv", b"a,b\n1,2\n", "text/csv")},
    )
    assert response.status_code == 404


def test_a_viewer_cannot_reach_the_ingestion_console(console):
    from cmdm.governance.rbac import Role

    assert console(Role.VIEWER).get("/console/ingest").status_code == 403


def test_the_steward_queue_shows_the_pairs_the_run_could_not_decide(
    console, store, processed
):
    from cmdm.governance.rbac import Role

    page = console(Role.STEWARD).get("/console/steward")
    assert page.status_code == 200
    assert "Nothing in the grey zone" not in page.text
    assert 'name="pair_id"' in page.text


def test_deciding_a_pair_records_how_and_who(console, store, processed):
    from cmdm.governance.rbac import Role

    client = console(Role.STEWARD)
    page = client.get("/console/steward")
    match = re.search(r'name="pair_id" value="([0-9a-f-]+)"', page.text)
    assert match is not None
    pair_id = match.group(1)

    client.post(
        "/console/steward/decide",
        data={"pair_id": pair_id, "decision": "MERGE", "reason": "same person"},
    )

    decided_by = store.execute(
        "SELECT decided_by FROM mdm.match_pair WHERE pair_id = %s", (pair_id,)
    ).fetchone()[0]
    # How, not who. The next run finds overrides by querying for this literal,
    # which it cannot do if the column holds a different username per reviewer.
    assert decided_by == "STEWARD"

    action = store.execute(
        "SELECT subject, reason FROM mdm.steward_action WHERE entity_id = %s",
        (uuid.UUID(pair_id),),
    ).fetchone()
    assert action is not None
    assert action[0] == "console-steward@x.com"
    assert action[1] == "same person"

    assert "Decided by a steward" in client.get("/console/steward").text


def test_the_business_console_finds_a_processed_customer(console, store, processed):
    from cmdm.governance.rbac import Role

    name = store.execute(
        "SELECT full_name FROM mdm.person WHERE is_current AND full_name IS NOT NULL "
        "LIMIT 1"
    ).fetchone()[0]

    page = console(Role.ADMIN).get("/console", params={"q": name})
    assert page.status_code == 200
    assert "/console/person/" in page.text
