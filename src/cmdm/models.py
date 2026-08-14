"""Which model is allowed to run, on what evidence, and who said so.

The two local models were the last thing in this system that could change
without a record. A standardization *rule* has to be mined, shadow-tested
against a regression set and approved by a steward with a written reason before
it may touch a record. A model that rewrites the same fields — and decides which
parties are the same person — was selected by an environment variable pointing
at a file. Swapping it was a deployment action with no measurement, no approval
and no trace.

This closes that. A model version moves through the states a rule moves through,
and the database refuses an ACTIVE model that was never evaluated:

    CANDIDATE ──evaluate──> SHADOW ──promote──> ACTIVE
                                                   │
                                                RETIRED

Two properties are worth stating because they are what make the record useful
rather than decorative.

**Evidence is measured, not asserted.** Promotion requires metrics produced by
running the candidate over a benchmark whose answer key is known — the same
harness ``worker evaluate`` uses. A model registered with numbers somebody typed
in is a model nobody measured.

**The artifact is fingerprinted.** A file on disk can change after it was
evaluated, and then the model running is not the model that was approved. The
digest recorded at registration is what makes that detectable instead of
assumed.

What this module does *not* do is load models. ``standardize.ai`` and
``resolve.crossencoder`` already know how; this decides which one they are
handed, so the two concerns stay apart.
"""

from __future__ import annotations

import hashlib
import json
import pathlib
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.rows import dict_row

from cmdm.model.ids import uuid7

__all__ = [
    "ModelKind",
    "ModelState",
    "RegisteredModel",
    "register_model",
    "record_evaluation",
    "promote_model",
    "retire_model",
    "active_model",
    "list_models",
    "artifact_digest",
]


class ModelKind:
    STANDARDIZER = "STANDARDIZER"
    CROSS_ENCODER = "CROSS_ENCODER"


class ModelState:
    CANDIDATE = "CANDIDATE"
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


@dataclass(frozen=True, slots=True)
class RegisteredModel:
    """One registered model version, as the store holds it."""

    model_id: str
    kind: str
    model_name: str
    version: str
    state: str
    artifact_path: str | None = None
    artifact_sha256: str | None = None
    metrics: dict[str, Any] | None = None
    evaluated_on: str | None = None
    promoted_by: str | None = None
    promotion_note: str | None = None

    @property
    def runnable(self) -> bool:
        """Whether the artifact is still the one that was evaluated.

        A model with no artifact is a reference implementation and is always
        runnable. One with an artifact is runnable only while the file on disk
        still hashes to what was registered.
        """
        if not self.artifact_path:
            return True
        path = pathlib.Path(self.artifact_path)
        if not path.exists():
            return False
        return artifact_digest(path) == self.artifact_sha256


def artifact_digest(path: str | pathlib.Path) -> str:
    """SHA-256 of a model file, read in chunks.

    A model artifact is tens or hundreds of megabytes; reading it whole to hash
    it would make registering one a memory event.
    """
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def register_model(
    conn: psycopg.Connection,
    *,
    kind: str,
    model_name: str,
    version: str,
    artifact_path: str | pathlib.Path | None = None,
    registered_by: str = "system",
) -> str:
    """Record a model version as a candidate. It cannot run yet.

    Registration is deliberately inert: it says a model exists and fingerprints
    it, nothing more. Making it usable takes an evaluation and an approval,
    which is the whole point of the table.
    """
    digest = artifact_digest(artifact_path) if artifact_path else None
    model_id = str(uuid7())

    conn.execute(
        """
        INSERT INTO mdm.model_version
            (model_id, kind, model_name, version, state, artifact_path,
             artifact_sha256, registered_by)
        VALUES (%s, %s, %s, %s, 'CANDIDATE', %s, %s, %s)
        ON CONFLICT (kind, model_name, version) DO UPDATE
           SET artifact_path   = EXCLUDED.artifact_path,
               artifact_sha256 = EXCLUDED.artifact_sha256
        """,
        (model_id, kind, model_name, version,
         str(artifact_path) if artifact_path else None, digest, registered_by),
    )
    row = conn.execute(
        "SELECT model_id::text FROM mdm.model_version "
        "WHERE kind = %s AND model_name = %s AND version = %s",
        (kind, model_name, version),
    ).fetchone()
    return row[0]


def record_evaluation(
    conn: psycopg.Connection,
    model_id: str,
    *,
    metrics: dict[str, Any],
    evaluated_on: str,
) -> None:
    """Attach measured quality to a candidate and move it to SHADOW.

    SHADOW means "measured, not yet trusted". It is a separate state from ACTIVE
    because the numbers being good is not the same as somebody having looked at
    them and accepted the trade — a model can improve recall and cost precision,
    and which of those matters is not a decision this code can make.
    """
    conn.execute(
        """
        UPDATE mdm.model_version
           SET metrics = %s::jsonb, evaluated_on = %s, evaluated_at = now(),
               state = CASE WHEN state = 'CANDIDATE' THEN 'SHADOW' ELSE state END
         WHERE model_id = %s
        """,
        (json.dumps(metrics, default=str), evaluated_on, model_id),
    )


def promote_model(
    conn: psycopg.Connection,
    model_id: str,
    *,
    promoted_by: str,
    note: str,
) -> None:
    """Make this the model its kind runs. Retires whichever held the slot.

    Requires a named approver and a reason. The database enforces the first
    through a check constraint; the second is required here because an approval
    with no reason is not reviewable later, which is the same standard the rule
    store holds.
    """
    if not promoted_by or not note:
        raise ValueError(
            "promotion needs an approver and a reason: an approval nobody signed "
            "and nobody explained cannot be reviewed six months from now"
        )

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT kind, state, metrics FROM mdm.model_version WHERE model_id = %s",
            (model_id,),
        )
        model = cur.fetchone()
        if model is None:
            raise LookupError(f"no such model {model_id}")
        if model["metrics"] is None:
            raise ValueError(
                "this model has never been evaluated. Run `worker models "
                "evaluate` against a benchmark extract before promoting it."
            )

        # One ACTIVE per kind, so the outgoing model is retired in the same
        # statement pair rather than left for a unique index to reject.
        cur.execute(
            "UPDATE mdm.model_version SET state = 'RETIRED' "
            "WHERE kind = %s AND state = 'ACTIVE'",
            (model["kind"],),
        )
        cur.execute(
            """
            UPDATE mdm.model_version
               SET state = 'ACTIVE', promoted_by = %s, promotion_note = %s,
                   promoted_at = now()
             WHERE model_id = %s
            """,
            (promoted_by, note, model_id),
        )


def retire_model(conn: psycopg.Connection, model_id: str) -> None:
    """Take a model out of service. Its decisions stay explainable.

    Nothing is deleted: every decision the model made still names it, and a
    registry row that vanished would leave those decisions pointing at a model
    the store has never heard of.
    """
    conn.execute(
        "UPDATE mdm.model_version SET state = 'RETIRED' WHERE model_id = %s",
        (model_id,),
    )


def active_model(conn: psycopg.Connection, kind: str) -> RegisteredModel | None:
    """The model this kind should be running, or None for the reference path.

    None is a legitimate answer and the default one. With no model promoted,
    both AI paths fall back to their reference implementations, which is what
    makes the system runnable with no model artifact present at all.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT model_id::text, kind, model_name, version, state,
                   artifact_path, artifact_sha256, metrics, evaluated_on,
                   promoted_by, promotion_note
            FROM mdm.model_version
            WHERE kind = %s AND state = 'ACTIVE'
            """,
            (kind,),
        )
        row = cur.fetchone()
    return RegisteredModel(**row) if row else None


def list_models(
    conn: psycopg.Connection, *, kind: str | None = None
) -> list[RegisteredModel]:
    """Every registered model, newest first."""
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            SELECT model_id::text, kind, model_name, version, state,
                   artifact_path, artifact_sha256, metrics, evaluated_on,
                   promoted_by, promotion_note
            FROM mdm.model_version
            WHERE (%s::text IS NULL OR kind = %s)
            ORDER BY registered_at DESC
            """,
            (kind, kind),
        )
        return [RegisteredModel(**row) for row in cur.fetchall()]
