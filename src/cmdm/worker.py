"""The worker: what actually drains the queue.

Accepting a batch and processing it are deliberately separate. The API validates
a file synchronously, lands it, enqueues a job and returns — everything after
that belongs here.

    work_queue ──claim(SKIP LOCKED)──> process_batch ──> run_pipeline ──> golden

Three entry points, because they are three different operational shapes:

*   :func:`process_batch` — one batch, one transaction. The unit of work.
*   :func:`drain` — claim and process what is queued, then stop. What a console
    button and a cron job both want.
*   :func:`serve` — the supervisor loop. What a long-running worker process runs.

Each job is its own transaction and its own connection. A worker that shares one
transaction across jobs turns any single bad batch into a rollback of every
batch it had already finished, which is precisely the failure the queue exists
to contain.

Reprocessing is safe and is meant to be used. Landing is content-addressed, the
golden writer is SCD-2 with a record hash that excludes audit columns, and the
crosswalk upserts — so running a batch twice writes no second version of
anything that did not change.
"""

from __future__ import annotations

import argparse
import logging
import pathlib
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import polars as pl
import psycopg
from psycopg.rows import dict_row

from cmdm.db.queue import QUEUE_STANDARDIZE, Job, WorkQueue
from cmdm.ingest.mapping import SourceMapping, load_mapping
from cmdm.ingest.shred import PASSTHROUGH_COLUMNS
from cmdm.pipeline import PipelineResult, run_pipeline

RECORD_ID_COLUMN = PASSTHROUGH_COLUMNS[0]

__all__ = [
    "MAPPINGS_DIR",
    "load_batch_frame",
    "mapping_for_batch",
    "process_batch",
    "drain",
    "serve",
    "mine_rules",
    "main",
]

log = logging.getLogger("cmdm.worker")

MAPPINGS_DIR = pathlib.Path(__file__).resolve().parent / "mappings"

#: How long a worker sleeps when the queue is empty. Short enough that a batch
#: submitted from the console is picked up while the operator is still looking at
#: the page, long enough that an idle worker is not a busy poll.
IDLE_SLEEP_SECONDS = 2.0


def mapping_for_batch(conn: psycopg.Connection, batch_id: uuid.UUID) -> SourceMapping:
    """Load the mapping a batch was accepted under.

    Read from the batch row rather than passed in by the caller: a worker
    picking a job off the queue has only an id, and guessing the mapping from
    the source system breaks the moment one system has two feeds.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT mapping_name FROM mdm.ingest_batch WHERE batch_id = %s",
            (batch_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise LookupError(f"no such batch {batch_id}")

    path = (MAPPINGS_DIR / f"{row['mapping_name']}.toml").resolve()
    if path.parent != MAPPINGS_DIR or not path.exists():
        raise LookupError(
            f"batch {batch_id} was accepted under mapping {row['mapping_name']!r}, "
            "which is no longer present"
        )
    return load_mapping(path)


def load_batch_frame(
    conn: psycopg.Connection, batch_id: uuid.UUID, mapping: SourceMapping
) -> pl.DataFrame:
    """Rebuild the source-shaped frame from the landing zone.

    The pipeline reads the batch back out of ``source_record`` rather than
    keeping the uploaded file around, so processing is replayable from the
    database alone and a re-run cannot silently use a different file than the
    one that was accepted.

    The schema is declared rather than inferred. A column null on every row of
    this batch infers as Polars ``Null``, and every downstream string kernel
    then fails on a batch that happens to have one empty column.

    ``source_record_id`` rides along beside the mapped columns. It is what lets
    a surviving value name the record it came from, and — because it is stable
    across runs where row order is not — what makes survivorship break a tie the
    same way every time.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT source_record_id, payload FROM mdm.source_record "
            "WHERE source_batch_id = %s ORDER BY source_record_id",
            (str(batch_id),),
        )
        rows = cur.fetchall()

    columns = [*mapping.source_columns, RECORD_ID_COLUMN]
    if not rows:
        return pl.DataFrame(schema={c: pl.String for c in columns})

    return pl.DataFrame(
        [
            {
                **{
                    c: (None if r["payload"].get(c) is None else str(r["payload"][c]))
                    for c in mapping.source_columns
                },
                RECORD_ID_COLUMN: str(r["source_record_id"]),
            }
            for r in rows
        ],
        schema={c: pl.String for c in columns},
    )


def process_batch(
    conn: psycopg.Connection,
    batch_id: uuid.UUID,
    *,
    mapping: SourceMapping | None = None,
) -> PipelineResult:
    """Run one landed batch through the pipeline.

    The caller owns the transaction, as with :func:`cmdm.pipeline.run_pipeline`,
    and for the same reason: the golden versions, the crosswalk, the provenance
    and the match ledger are one atomic statement about the batch.

    Marks the landed rows processed and the batch COMPLETED. Both are inside the
    caller's transaction, so a failed run leaves the batch claimable again rather
    than recorded as done.
    """
    mapping = mapping or mapping_for_batch(conn, batch_id)
    raw = load_batch_frame(conn, batch_id, mapping)

    result = run_pipeline(conn, raw, mapping, batch_id=batch_id)

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE mdm.source_record SET is_processed = true "
            "WHERE source_batch_id = %s AND NOT is_processed",
            (str(batch_id),),
        )
        cur.execute(
            "UPDATE mdm.ingest_batch SET state = 'COMPLETED', completed_at = now() "
            "WHERE batch_id = %s",
            (batch_id,),
        )
    return result


def _handle(conn: psycopg.Connection, job: Job) -> dict[str, Any]:
    """Run one job's payload. The only place a job kind is interpreted."""
    batch_id = job.payload.get("batch_id")
    if not batch_id:
        raise ValueError(f"job {job.job_id} carries no batch_id")
    return process_batch(conn, uuid.UUID(batch_id)).as_dict()


def drain(
    connect: Callable[[], psycopg.Connection],
    *,
    queue_name: str = QUEUE_STANDARDIZE,
    limit: int = 1,
    lease_seconds: int = 900,
    worker: str | None = None,
) -> list[dict[str, Any]]:
    """Claim and process up to ``limit`` jobs, then return.

    Takes a connection *factory*, not a connection: each job needs its own
    transaction, and a caller handing in one open connection would get all of
    them committed or rolled back together.

    A failing job is failed on the queue — where the retry schedule and the dead
    letter are — and does not stop the ones after it. A batch that cannot be
    processed is a batch-shaped problem; treating it as a worker-shaped one takes
    down the whole feed.
    """
    outcomes: list[dict[str, Any]] = []

    for _ in range(limit):
        with connect() as claim_conn:
            claimed = WorkQueue(claim_conn).claim(
                queue_name, batch=1, lease_seconds=lease_seconds, worker=worker
            )
            claim_conn.commit()
        if not claimed:
            break
        job = claimed[0]

        with connect() as conn:
            try:
                result = _handle(conn, job)
            except Exception as exc:
                # The failure must be recorded and the work must not be. Both
                # are in this transaction, so the rollback comes first and the
                # bookkeeping is written after it — recording the failure before
                # rolling back would roll the record back too, and the job would
                # come round again with no attempt counted and no error stored.
                conn.rollback()
                WorkQueue(conn).fail(job.job_id, f"{type(exc).__name__}: {exc}")
                _mark_batch_failed(conn, job, exc)
                conn.commit()
                log.exception("job %s failed", job.job_id)
                outcomes.append(
                    {"job_id": str(job.job_id), "ok": False, "error": str(exc)}
                )
                continue

            WorkQueue(conn).complete(job.job_id, result=result)
            conn.commit()
            outcomes.append({"job_id": str(job.job_id), "ok": True, **result})

    return outcomes


def _mark_batch_failed(
    conn: psycopg.Connection, job: Job, exc: BaseException
) -> None:
    """Record on the batch why it did not process.

    The queue already knows, but an operator looking at a batch should not have
    to know the queue exists to find out what happened to their file.
    """
    batch_id = job.payload.get("batch_id")
    if not batch_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE mdm.ingest_batch SET state = 'FAILED', last_error = %s "
            "WHERE batch_id = %s",
            (f"{type(exc).__name__}: {exc}"[:2000], uuid.UUID(batch_id)),
        )


def serve(
    connect: Callable[[], psycopg.Connection],
    *,
    queue_name: str = QUEUE_STANDARDIZE,
    idle_sleep: float = IDLE_SLEEP_SECONDS,
    max_jobs: int | None = None,
    reap_every: int = 30,
) -> int:
    """Run until interrupted, claiming and processing.

    Returns the number of jobs handled. ``max_jobs`` bounds the loop so the same
    function is usable as a test and as a daemon.

    Expired leases are reaped periodically rather than every pass. Reaping is a
    write against the whole queue and doing it per iteration makes an idle worker
    the busiest writer in the database.
    """
    handled = 0
    since_reap = 0

    while max_jobs is None or handled < max_jobs:
        remaining = None if max_jobs is None else max_jobs - handled
        outcomes = drain(
            connect, queue_name=queue_name, limit=min(4, remaining or 4)
        )
        handled += len(outcomes)

        since_reap += 1
        if since_reap >= reap_every:
            since_reap = 0
            with connect() as conn:
                returned = WorkQueue(conn).reap_expired(queue_name)
                conn.commit()
            if returned:
                log.warning("returned %d expired job(s) to %s", returned, queue_name)

        if not outcomes:
            if max_jobs is not None:
                break
            time.sleep(idle_sleep)

    return handled


# ---------------------------------------------------------------------------
# Rule miner
# ---------------------------------------------------------------------------


def mine_rules(
    conn: psycopg.Connection,
    *,
    source_system: str | None = None,
    mapping: SourceMapping | None = None,
    min_evidence: int | None = None,
) -> list[dict[str, Any]]:
    """Mine the exception log and propose deterministic rules.

    Separate from the worker loop and deliberately not on the ingest path. Mining
    reads the whole exception corpus, so running it per batch would do the same
    global scan once per file and propose the same rules repeatedly.

    Shadow evaluation needs two corpora — what a rule claims to fix, and what it
    must not disturb — and both are built here by re-shredding the landed records
    and splitting them on the gate. Reconstructed from the landing zone rather
    than cached, because the corpus a rule is judged against must be the data as
    it stands now, not as it stood when the exception was logged.

    Proposes only. Every rule this produces sits in SHADOW until a steward
    approves it in the console, which is the point of the loop.
    """
    from cmdm.ingest.shred import shred
    from cmdm.standardize.agent import MIN_EVIDENCE, mine_and_propose
    from cmdm.standardize.gate import GATE_PASSED_COLUMN, apply_gate, checks_for

    mapping = mapping or _only_mapping(conn, source_system)
    raw = _landed_frame(conn, mapping, source_system=source_system)
    if raw.height == 0:
        return []

    persons = shred(raw, mapping)["person"]
    gated = apply_gate(persons, checks_for(persons.columns)).collect()

    results = mine_and_propose(
        conn,
        target_corpus=gated.filter(~pl.col(GATE_PASSED_COLUMN)),
        regression_corpus=gated.filter(pl.col(GATE_PASSED_COLUMN)),
        min_evidence=min_evidence or MIN_EVIDENCE,
    )
    return [
        {
            "rule_id": str(r.rule_id),
            "rule_name": r.rule_name,
            "matched": r.matched,
            "fixed": r.fixed,
            "regressions": r.regressions,
        }
        for r in results
    ]


def _only_mapping(
    conn: psycopg.Connection, source_system: str | None
) -> SourceMapping:
    """Resolve the mapping to mine against.

    With one mapping installed the answer is unambiguous and asking for it is
    ceremony. With several it is not, and guessing would mine one feed's
    exceptions against another feed's corpus.
    """
    if source_system:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT mapping_name FROM mdm.ingest_batch WHERE source_system = %s "
                "ORDER BY submitted_at DESC LIMIT 1",
                (source_system,),
            )
            row = cur.fetchone()
        if row:
            return load_mapping(MAPPINGS_DIR / f"{row['mapping_name']}.toml")

    available = sorted(MAPPINGS_DIR.glob("*.toml"))
    if len(available) != 1:
        raise LookupError(
            f"{len(available)} mappings installed; name a source system so the "
            "corpus and the exceptions come from the same feed"
        )
    return load_mapping(available[0])


def _landed_frame(
    conn: psycopg.Connection,
    mapping: SourceMapping,
    *,
    source_system: str | None = None,
    limit: int = 200_000,
) -> pl.DataFrame:
    """Every landed row for a source, as the shredder wants it."""
    columns = list(mapping.source_columns)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT payload FROM mdm.source_record WHERE source_system = %s "
            "ORDER BY ingested_at DESC LIMIT %s",
            (source_system or mapping.source_system, limit),
        )
        payloads = [r["payload"] for r in cur.fetchall()]

    if not payloads:
        return pl.DataFrame(schema={c: pl.String for c in columns})
    return pl.DataFrame(
        [
            {c: (None if p.get(c) is None else str(p[c])) for c in columns}
            for p in payloads
        ],
        schema={c: pl.String for c in columns},
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def stale_key_kinds(conn: psycopg.Connection, mappings: list[SourceMapping]) -> dict[str, int]:
    """Key kinds in the crosswalk that no installed mapping declares any more.

    A key kind is part of a party's identity, so changing one in a mapping
    re-keys the crosswalk. Re-processing after such a change does not correct
    the old rows: it mints a *second* golden person under the new key and
    leaves the first behind, which doubles the affected customers rather than
    fixing them. The store has no way to notice on its own — nothing is
    violated, the identities are simply no longer the ones being written.

    So it is detected by comparing what is in the crosswalk against what the
    mappings now declare. Anything left over needs a rebuild, not a backfill.
    """
    declared = {p.key_kind for m in mappings for p in m.parties}
    rows = conn.execute(
        "SELECT source_key_kind, count(*) FROM mdm.person_xref "
        "WHERE is_active GROUP BY source_key_kind"
    ).fetchall()
    return {kind: n for kind, n in rows if kind not in declared}


def _rebuild(connect_pool, *, mappings: list[SourceMapping]) -> int:
    """Discard the derived store and rebuild it from the landing zone.

    The heavier of the two upgrade paths, and the one a re-keyed crosswalk
    needs. Everything it deletes is derived: golden versions, the crosswalks,
    provenance, the match ledger. The landing zone is untouched, and it holds
    the delivered bytes of every batch ever accepted — which is the reason it
    is immutable and never pruned. So this is a recomputation, not a data loss,
    and the ids it produces are the ids the current code would have produced had
    it been installed all along.

    Two things do not survive and cannot: golden ids already published
    downstream change, and steward decisions keyed on the old identities no
    longer apply. That is why it is not the default and why the updater asks
    before running it.
    """
    with connect_pool() as conn:
        landed = conn.execute("SELECT count(*) FROM mdm.source_record").fetchone()[0]
        if not landed:
            log.info("nothing to rebuild: the landing zone is empty")
            return 0

        # Prove the pipeline can read this landing zone *before* deleting
        # anything. A rebuild that truncates and then fails leaves an empty
        # store, and although the delivered bytes are still there and re-running
        # would fix it, "your golden store is now zero rows" is not a message to
        # put in front of somebody at the point where they are least sure what
        # they just did. The dry run does the whole thing except the write.
        batch = conn.execute(
            "SELECT b.batch_id FROM mdm.ingest_batch b "
            "WHERE EXISTS (SELECT 1 FROM mdm.source_record s "
            "              WHERE s.source_batch_id = b.batch_id::text) "
            "ORDER BY b.submitted_at DESC LIMIT 1"
        ).fetchone()
        if batch:
            try:
                mapping = mapping_for_batch(conn, batch[0])
                run_pipeline(
                    conn, load_batch_frame(conn, batch[0], mapping), mapping,
                    batch_id=batch[0], write=False,
                )
            except Exception as exc:
                conn.rollback()
                log.error(
                    "refusing to rebuild: the current pipeline cannot process "
                    "the most recent landed batch (%s: %s). Nothing was "
                    "deleted.", type(exc).__name__, exc
                )
                return 1
            conn.rollback()

        log.warning(
            "rebuilding the golden store from %s landed rows; published person "
            "and policy ids will change", f"{landed:,}"
        )
        conn.execute(
            "TRUNCATE mdm.relationship, mdm.person, mdm.policy, "
            "mdm.person_master, mdm.policy_master, mdm.person_xref, "
            "mdm.policy_xref, mdm.attribute_provenance, mdm.match_pair "
            "CASCADE"
        )
        conn.commit()

    code = _backfill(connect_pool)
    if code:
        return code

    with connect_pool() as conn:
        stale = stale_key_kinds(conn, mappings)
    if stale:  # pragma: no cover - only if a mapping was removed, not changed
        log.warning(
            "key kinds still present with no mapping that declares them: %s. "
            "A mapping file was removed rather than edited.", sorted(stale)
        )
    return 0


def _backfill(connect_pool) -> int:
    """Re-run every landed batch through the current pipeline.

    What an upgrade needs. A batch is processed once, by whatever version of the
    pipeline was installed that day; a version that computes something the old
    one did not — a new entity, a crosswalk that was declared and never
    written — leaves the store correct for what it holds and silent about what
    it never derived. Re-uploading the files would work and is the wrong answer:
    the bytes are already in the landing zone, which is the reason the landing
    zone is immutable and kept.

    Safe to run at any time, and safe to run twice. The pipeline is idempotent
    against its own output: an unchanged record is an unchanged record, not a
    new SCD-2 version. Each batch is its own transaction, so a failure part-way
    leaves the batches already done committed and reports which one broke.
    """
    # Selected by having landed rows rather than by batch state: the states are
    # an enum that grows between releases, and "there are bytes in the landing
    # zone to re-run" is the actual condition. A rejected batch never landed
    # any, so it is excluded by the same join that finds the others.
    with connect_pool() as conn:
        batches = [
            row[0] for row in conn.execute(
                "SELECT b.batch_id FROM mdm.ingest_batch b "
                "WHERE EXISTS (SELECT 1 FROM mdm.source_record s "
                "              WHERE s.source_batch_id = b.batch_id::text) "
                "ORDER BY b.submitted_at"
            ).fetchall()
        ]

    if not batches:
        log.info("nothing to backfill: no processed batches in the landing zone")
        return 0

    log.info("backfilling %d batch(es)", len(batches))
    # New rows and new versions are counted apart from rows that were already
    # right. On an upgrade the first is the point and the second is most of the
    # book; adding them together would report a number that looks like a
    # rewrite of the whole store on a run that changed nothing but the gaps.
    added = versioned = untouched = 0
    for index, batch_id in enumerate(batches, start=1):
        with connect_pool() as conn:
            result = process_batch(conn, batch_id)
            conn.commit()

        new = sum(w["inserted"] for w in result.writes.values())
        revised = sum(w["versioned"] for w in result.writes.values())
        same = sum(w["unchanged"] for w in result.writes.values())
        added, versioned, untouched = added + new, versioned + revised, untouched + same
        log.info(
            "  %d/%d %s: %s new, %s revised, %s unchanged, %s policy keys linked",
            index, len(batches), batch_id,
            f"{new:,}", f"{revised:,}", f"{same:,}", f"{result.policy_xref_rows:,}",
        )

    log.info(
        "backfill complete: %s new, %s revised, %s already correct",
        f"{added:,}", f"{versioned:,}", f"{untouched:,}",
    )
    _report_absent_evidence(connect_pool)
    return 0


def _report_absent_evidence(connect_pool) -> None:
    """Say why a feature produced nothing, when the reason is the data.

    A backfill can only derive what the delivered bytes support. Households are
    built from the relationship a source states at application, and batches
    landed before that column existed do not carry it -- so a store rebuilt
    entirely from old extracts comes back with zero households and no error
    anywhere, which reads as a broken feature rather than as an absent input.

    Worth a line precisely because everything here succeeded.
    """
    with connect_pool() as conn:
        stated, edges, households = conn.execute(
            "SELECT (SELECT count(*) FROM mdm.relationship "
            "         WHERE is_current AND stated_relationship IS NOT NULL),"
            "       (SELECT count(*) FROM mdm.relationship "
            "         WHERE is_current AND edge_kind = 'PARTY_POLICY'),"
            "       (SELECT count(DISTINCT household_id) FROM mdm.person "
            "         WHERE is_current AND household_id IS NOT NULL)"
        ).fetchone()

    if households:
        log.info("%s households derived", f"{households:,}")
    elif edges and not stated:
        log.warning(
            "no households derived: none of the %s landed party rows states how "
            "the parties are related. The extracts in the landing zone predate "
            "that column; households will appear when a file carrying it is "
            "submitted.", f"{edges:,}"
        )


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m cmdm.worker`` — the process the architecture calls a worker."""
    parser = argparse.ArgumentParser(
        prog="cmdm.worker",
        description="Drain the ingest queue, or mine standardization rules.",
    )
    parser.add_argument(
        "command", choices=("serve", "once", "mine", "backfill", "rebuild", "check"),
        nargs="?", default="serve",
    )
    parser.add_argument("--queue", default=QUEUE_STANDARDIZE)
    parser.add_argument(
        "--max-jobs", type=int, default=None,
        help="stop after this many jobs; omit to run until interrupted",
    )
    parser.add_argument("--source-system", default=None, help="mine: which feed")
    parser.add_argument(
        "--min-evidence", type=int, default=None,
        help="mine: occurrences a pattern needs before a rule is proposed",
    )
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    from cmdm.db.engine import connect as connect_pool

    if args.command == "mine":
        with connect_pool() as conn:
            proposed = mine_rules(
                conn,
                source_system=args.source_system,
                min_evidence=args.min_evidence,
            )
            conn.commit()
        for rule in proposed:
            log.info(
                "proposed %s: fixes %d, regressions %d",
                rule["rule_name"], rule["fixed"], rule["regressions"],
            )
        log.info("%d rule(s) proposed, awaiting approval in /console/rules",
                 len(proposed))
        return 0

    if args.command == "once":
        outcomes = drain(connect_pool, queue_name=args.queue, limit=args.max_jobs or 1)
        log.info("handled %d job(s)", len(outcomes))
        return 0 if all(o["ok"] for o in outcomes) else 1

    if args.command in ("backfill", "rebuild", "check"):
        installed = [load_mapping(p) for p in sorted(MAPPINGS_DIR.glob("*.toml"))]

        if args.command == "check":
            with connect_pool() as conn:
                stale = stale_key_kinds(conn, installed)
            if not stale:
                log.info("the crosswalk matches the installed mappings")
                return 0
            for kind, n in sorted(stale.items()):
                log.warning("%s: %s crosswalk rows, declared by no mapping",
                            kind, f"{n:,}")
            log.warning("run `worker rebuild` to re-key the store from the "
                        "landing zone; a backfill would duplicate these parties")
            return 1

        if args.command == "rebuild":
            return _rebuild(connect_pool, mappings=installed)

        with connect_pool() as conn:
            stale = stale_key_kinds(conn, installed)
        if stale:
            log.error(
                "refusing to backfill: %s carries crosswalk rows that no "
                "installed mapping declares. Backfilling would mint a second "
                "golden party for each of them rather than correcting the "
                "first. Run `worker rebuild` instead.", sorted(stale)
            )
            return 1
        return _backfill(connect_pool)

    log.info("worker started on queue %r", args.queue)
    try:
        handled = serve(connect_pool, queue_name=args.queue, max_jobs=args.max_jobs)
    except KeyboardInterrupt:
        log.info("interrupted")
        return 0
    log.info("handled %d job(s)", handled)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
