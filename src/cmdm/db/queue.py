"""Postgres-backed work queue.

Ingestion is decoupled from processing by this queue, and the queue lives in the
same database as the landing zone on purpose. Accepting a batch means two facts
must become true together: the raw records are durably landed, and something is
obliged to process them. With the queue in Postgres those are one transaction,
so a batch can never be accepted-but-lost — the failure mode where an API
returns 202, the broker publish fails, and the data sits in a landing table that
nothing will ever read.

Claiming uses ``FOR UPDATE SKIP LOCKED``, which is Postgres's built-in answer to
competing consumers: each worker locks the rows it takes and other workers step
over them without blocking. No advisory locks, no leader election, no separate
broker to run, secure and monitor.

Throughput is far past what a batch MDM feed needs — thousands of claims per
second on modest hardware, against feeds that arrive hourly or nightly. If
streaming CDC later becomes a first-class requirement rather than an optional
one, a log-structured broker earns its place; until then this does not.

Reliability properties:

*   **At-least-once.** A worker that dies mid-job leaves the row claimed; the
    lease expires and another worker retries. Handlers must be idempotent, which
    the pipeline achieves through content-addressed landing.
*   **Bounded retries with backoff.** A job that keeps failing lands in DEAD
    rather than cycling forever and starving the queue.
*   **Visibility deadlines**, not just locks, so a crashed worker's jobs return
    to the queue without operator intervention.
"""

from __future__ import annotations

import datetime as dt
import os
import socket
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from cmdm.model.ids import uuid7

__all__ = [
    "JobState",
    "Job",
    "WorkQueue",
    "QUEUE_INGEST",
    "QUEUE_STANDARDIZE",
    "QUEUE_RESOLVE",
    "QUEUE_SURVIVE",
    "worker_identity",
]

#: Queue names, one per pipeline stage. Separate queues rather than one queue
#: with a type column, so a backlog in resolution cannot starve ingestion and
#: each stage's workers can be scaled independently.
QUEUE_INGEST = "ingest"
QUEUE_STANDARDIZE = "standardize"
QUEUE_RESOLVE = "resolve"
QUEUE_SURVIVE = "survive"


class JobState:
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    DONE = "DONE"
    FAILED = "FAILED"
    DEAD = "DEAD"


def worker_identity() -> str:
    """Stable-ish identifier for the claiming process.

    Recorded on the claimed row so that a stuck job can be traced to a host and
    process rather than merely being observed to be stuck.
    """
    return f"{socket.gethostname()}:{os.getpid()}"


@dataclass(frozen=True, slots=True)
class Job:
    """A claimed unit of work."""

    job_id: uuid.UUID
    queue_name: str
    payload: dict[str, Any]
    attempts: int
    max_attempts: int
    created_at: dt.datetime

    @property
    def is_last_attempt(self) -> bool:
        """True when failing this attempt will move the job to DEAD.

        Handlers use this to decide whether to write a partial result or a
        diagnostic before the job stops being retried.
        """
        return self.attempts >= self.max_attempts


class WorkQueue:
    """Claim-and-complete queue over a single Postgres table.

    The connection is supplied rather than owned. Enqueueing has to be able to
    join the caller's transaction — that is the entire point of putting the
    queue here — so this class must never open its own connection for a write.
    """

    def __init__(self, conn: psycopg.Connection) -> None:
        self._conn = conn

    # -- producing ---------------------------------------------------------

    def enqueue(
        self,
        queue_name: str,
        payload: dict[str, Any],
        *,
        priority: int = 100,
        max_attempts: int = 5,
        delay_seconds: float = 0.0,
        dedupe_key: str | None = None,
    ) -> uuid.UUID | None:
        """Add one job, in the caller's transaction.

        ``dedupe_key`` makes enqueueing idempotent: a second job with the same
        key while the first is still pending or running is dropped and ``None``
        is returned. This is what stops a retried API call, or a file delivered
        twice, from queueing the same batch twice.

        Returns the job id, or ``None`` when the job was deduplicated away.
        """
        job_id = uuid7()
        visible_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=delay_seconds)

        with self._conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO mdm.work_queue
                    (job_id, queue_name, payload, priority, state, attempts,
                     max_attempts, visible_at, dedupe_key)
                VALUES (%s, %s, %s, %s, 'PENDING', 0, %s, %s, %s)
                ON CONFLICT (queue_name, dedupe_key)
                    WHERE dedupe_key IS NOT NULL AND state IN ('PENDING', 'RUNNING')
                    DO NOTHING
                RETURNING job_id
                """,
                (job_id, queue_name, Jsonb(payload), priority, max_attempts,
                 visible_at, dedupe_key),
            )
            row = cur.fetchone()
        return job_id if row else None

    # -- consuming ---------------------------------------------------------

    def claim(
        self,
        queue_name: str,
        *,
        batch: int = 1,
        lease_seconds: int = 300,
        worker: str | None = None,
    ) -> list[Job]:
        """Claim up to ``batch`` jobs.

        ``FOR UPDATE SKIP LOCKED`` is what makes competing consumers work
        without coordination: rows another worker has locked are skipped rather
        than waited on, so N workers get N disjoint sets in one round trip.

        The lease is a deadline written into the row, not merely a held lock. A
        worker whose process dies releases its database lock immediately, but
        the job must not become claimable the instant that happens — the work
        may still be in flight elsewhere. The visibility deadline gives it a
        defined window instead.
        """
        worker = worker or worker_identity()
        now = dt.datetime.now(dt.UTC)
        lease_until = now + dt.timedelta(seconds=lease_seconds)

        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                WITH claimed AS (
                    SELECT job_id
                    FROM mdm.work_queue
                    WHERE queue_name = %s
                      AND state IN ('PENDING', 'FAILED')
                      AND visible_at <= %s
                    ORDER BY priority ASC, visible_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                )
                UPDATE mdm.work_queue q
                SET state = 'RUNNING',
                    attempts = q.attempts + 1,
                    locked_by = %s,
                    locked_at = %s,
                    visible_at = %s,
                    updated_at = %s
                FROM claimed
                WHERE q.job_id = claimed.job_id
                RETURNING q.job_id, q.queue_name, q.payload, q.attempts,
                          q.max_attempts, q.created_at
                """,
                (queue_name, now, batch, worker, now, lease_until, now),
            )
            rows = cur.fetchall()

        return [
            Job(
                job_id=r["job_id"],
                queue_name=r["queue_name"],
                payload=r["payload"] or {},
                attempts=r["attempts"],
                max_attempts=r["max_attempts"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    def complete(self, job_id: uuid.UUID, *, result: dict[str, Any] | None = None) -> None:
        """Mark a job done.

        Completed jobs are retained rather than deleted. The queue doubles as
        the processing audit trail — "when was this batch handled, by which
        worker, after how many attempts" — and a reaper trims it on a retention
        schedule instead of the hot path paying for the delete.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE mdm.work_queue
                SET state = 'DONE', result = %s, locked_by = NULL,
                    completed_at = now(), updated_at = now()
                WHERE job_id = %s
                """,
                (Jsonb(result) if result is not None else None, job_id),
            )

    def fail(
        self,
        job_id: uuid.UUID,
        error: str,
        *,
        backoff_base_seconds: float = 2.0,
    ) -> str:
        """Record a failure and schedule a retry, or bury the job.

        Backoff is exponential in the attempt count. A job that has exhausted
        its attempts moves to DEAD and stops being claimed, so one poison
        message cannot occupy a worker forever — and stays in the table, because
        the whole point of a dead-letter state is that somebody can look at it.

        Returns the resulting state, so a caller can log or alert on burial.
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                UPDATE mdm.work_queue
                SET state = CASE WHEN attempts >= max_attempts
                                 THEN 'DEAD'::mdm.job_state
                                 ELSE 'FAILED'::mdm.job_state END,
                    last_error = %s,
                    locked_by = NULL,
                    visible_at = now() + (%s * power(%s, attempts)) * interval '1 second',
                    updated_at = now()
                WHERE job_id = %s
                RETURNING state
                """,
                (error[:4000], 1.0, backoff_base_seconds, job_id),
            )
            row = cur.fetchone()
        return row["state"] if row else JobState.DEAD

    def reap_expired(self, queue_name: str) -> int:
        """Return jobs whose lease expired to the pending pool.

        This is what makes a crashed worker self-healing. Without it, a job
        claimed by a process that never comes back stays RUNNING forever and is
        invisible to both the queue and whoever is waiting for the result.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                UPDATE mdm.work_queue
                SET state = CASE WHEN attempts >= max_attempts
                                 THEN 'DEAD'::mdm.job_state
                                 ELSE 'FAILED'::mdm.job_state END,
                    last_error = coalesce(last_error, '') || ' [lease expired]',
                    locked_by = NULL,
                    updated_at = now()
                WHERE queue_name = %s AND state = 'RUNNING' AND visible_at <= now()
                """,
                (queue_name,),
            )
            return cur.rowcount

    # -- observing ---------------------------------------------------------

    def depth(self, queue_name: str) -> dict[str, int]:
        """Count jobs by state.

        Exposed for the observability stage: queue depth and dead-letter count
        are the two numbers that say whether the pipeline is keeping up.
        """
        with self._conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                """
                SELECT state, count(*) AS n
                FROM mdm.work_queue WHERE queue_name = %s GROUP BY state
                """,
                (queue_name,),
            )
            return {r["state"]: r["n"] for r in cur.fetchall()}

    def oldest_pending_age_seconds(self, queue_name: str) -> float | None:
        """Age of the oldest claimable job.

        A better staleness signal than depth alone: a queue can be shallow and
        still be stuck if the one job in it has been waiting for an hour.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                """
                SELECT extract(epoch FROM now() - min(created_at))
                FROM mdm.work_queue
                WHERE queue_name = %s AND state IN ('PENDING', 'FAILED')
                  AND visible_at <= now()
                """,
                (queue_name,),
            )
            row = cur.fetchone()
        return float(row[0]) if row and row[0] is not None else None


@contextmanager
def processing(queue: WorkQueue, job: Job) -> Iterator[dict[str, Any]]:
    """Run a handler, completing or failing the job from its outcome.

    The result dict the caller populates is written to the job row on success.
    On exception the job is failed with the traceback summary and the exception
    is re-raised, so a worker loop can decide whether to keep going — swallowing
    it here would turn a systemic failure into a silent one.
    """
    result: dict[str, Any] = {}
    try:
        yield result
    except Exception as exc:
        queue.fail(job.job_id, f"{type(exc).__name__}: {exc}")
        raise
    else:
        queue.complete(job.job_id, result=result)
