"""Database access: connections, migrations and the work queue.

No ORM. The write paths here are bulk COPY, set-based merges and a queue claim
built on ``FOR UPDATE SKIP LOCKED``; SQL expresses all three more clearly than
any mapping layer would.
"""

from cmdm.db.engine import (
    apply_migrations,
    close_pool,
    connect,
    dsn_from_env,
    pool,
    transaction,
)
from cmdm.db.queue import (
    QUEUE_INGEST,
    QUEUE_RESOLVE,
    QUEUE_STANDARDIZE,
    QUEUE_SURVIVE,
    Job,
    JobState,
    WorkQueue,
    processing,
)

__all__ = [
    "apply_migrations", "close_pool", "connect", "dsn_from_env", "pool", "transaction",
    "Job", "JobState", "WorkQueue", "processing",
    "QUEUE_INGEST", "QUEUE_STANDARDIZE", "QUEUE_RESOLVE", "QUEUE_SURVIVE",
]
