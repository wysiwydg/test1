"""Access control, PII masking and the audit trail.

Three controls that only work together.

**RBAC** decides what a caller may do. Roles are coarse and few on purpose: a
model with fifty roles is one nobody can reason about, and its failure mode is
that everybody ends up holding the broadest one.

**Masking** decides what a caller may see. This is the control that matters most
in an MDM system specifically, because a golden record is the most complete
profile of a person the organisation holds — assembled from feeds that each held
much less. Masking is driven by the ``pii`` class already declared on every field
in the registry, so a new attribute is protected by declaring what it is rather
than by remembering to add it to a list.

**The access log** records what actually happened, reads included. Most systems
log mutations and call themselves audited; the question asked after an incident
is who *looked* at a person's record, and a mutation log cannot answer it.

Secrets are compared in constant time and stored only as hashes. A database dump
must not yield working credentials.
"""

from __future__ import annotations

import hmac
import secrets
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import polars as pl
import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from cmdm.model.enums import PiiClass
from cmdm.model.fields import EntitySpec
from cmdm.model.ids import keyed_identifier_hash, uuid7

__all__ = [
    "Role",
    "Action",
    "Principal",
    "Permission",
    "ROLE_PERMISSIONS",
    "AccessDenied",
    "authenticate",
    "authorize",
    "create_principal",
    "mask_frame",
    "maskable_columns",
    "log_access",
    "record_steward_action",
    "MASK",
]

#: What a masked value renders as. A fixed token rather than a null, so a
#: consumer can tell "you may not see this" from "we do not have this" -- those
#: are different facts and conflating them makes a masked export look like a
#: data quality problem.
MASK = "***"


class Role:
    VIEWER = "VIEWER"
    OPERATOR = "OPERATOR"
    STEWARD = "STEWARD"
    INGESTOR = "INGESTOR"
    ADMIN = "ADMIN"


class Action:
    READ = "READ"
    SEARCH = "SEARCH"
    DUPLICATE_CHECK = "DUPLICATE_CHECK"
    SUBMIT = "SUBMIT"
    MERGE = "MERGE"
    SPLIT = "SPLIT"
    OVERRIDE = "OVERRIDE"
    APPROVE_RULE = "APPROVE_RULE"
    ERASE = "ERASE"
    EXPORT = "EXPORT"
    DENIED = "DENIED"


class Permission:
    """What a role may do, beyond the action list.

    ``UNMASK`` is separate from read access deliberately. Most people who need
    to look at customer data need to know a record exists and what policies it
    holds; far fewer need the date of birth and the national identifier. Making
    it a distinct permission means the common case can be granted freely.
    """

    UNMASK = "UNMASK"


#: Role to permitted actions. Read as data rather than encoded in scattered
#: checks, so the whole policy is one table a reviewer can read.
ROLE_PERMISSIONS: dict[str, frozenset[str]] = {
    Role.VIEWER: frozenset({Action.READ, Action.SEARCH}),
    Role.OPERATOR: frozenset({
        Action.READ, Action.SEARCH, Action.DUPLICATE_CHECK, Permission.UNMASK,
    }),
    Role.STEWARD: frozenset({
        Action.READ, Action.SEARCH, Action.DUPLICATE_CHECK, Action.MERGE,
        Action.SPLIT, Action.OVERRIDE, Action.APPROVE_RULE, Action.EXPORT,
        Permission.UNMASK,
    }),
    Role.INGESTOR: frozenset({Action.SUBMIT, Action.DUPLICATE_CHECK}),
    Role.ADMIN: frozenset({
        Action.READ, Action.SEARCH, Action.DUPLICATE_CHECK, Action.SUBMIT,
        Action.MERGE, Action.SPLIT, Action.OVERRIDE, Action.APPROVE_RULE,
        Action.ERASE, Action.EXPORT, Permission.UNMASK,
    }),
}


class AccessDenied(PermissionError):
    """The caller may not perform this action."""


@dataclass(frozen=True, slots=True)
class Principal:
    """An authenticated caller."""

    principal_id: uuid.UUID
    subject: str
    roles: tuple[str, ...]
    kind: str = "SERVICE"
    display_name: str | None = None

    @property
    def permissions(self) -> frozenset[str]:
        """Union of everything the caller's roles allow."""
        out: set[str] = set()
        for role in self.roles:
            out |= ROLE_PERMISSIONS.get(role, frozenset())
        return frozenset(out)

    def may(self, action: str) -> bool:
        return action in self.permissions

    @property
    def may_unmask(self) -> bool:
        return Permission.UNMASK in self.permissions


#: Anonymous caller, used when a request carries no credentials. Holds no roles,
#: so every authorize() call against it fails -- the default is denial rather
#: than a permissive fallback nobody notices.
ANONYMOUS = Principal(
    principal_id=uuid.UUID(int=0), subject="anonymous", roles=(), kind="SERVICE"
)


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------

#: How many leading characters of a key are stored in the clear. Enough to find
#: the candidate row with an index; not enough to be useful to an attacker.
KEY_PREFIX_LENGTH = 8


def _hash_secret(secret: str) -> str:
    """Hash an API key under the deployment's secret.

    Reuses the keyed identifier hash so there is one hashing primitive in the
    system rather than a second one that might be weaker.
    """
    return keyed_identifier_hash(secret, id_type="API_KEY")


def create_principal(
    conn: psycopg.Connection,
    subject: str,
    roles: Sequence[str],
    *,
    kind: str = "SERVICE",
    display_name: str | None = None,
    secret: str | None = None,
) -> tuple[Principal, str | None]:
    """Register a caller and return it with its generated secret.

    The secret is returned exactly once and never stored in the clear; only its
    hash and a short prefix are kept. If it is lost it must be rotated, which is
    the correct behaviour rather than an inconvenience to design around.
    """
    unknown = set(roles) - set(ROLE_PERMISSIONS)
    if unknown:
        raise ValueError(f"unknown roles {sorted(unknown)}; valid: {sorted(ROLE_PERMISSIONS)}")
    if not roles:
        raise ValueError("a principal with no roles can do nothing; assign at least one")

    principal_id = uuid7()
    generated = None
    secret_hash = secret_prefix = None
    if kind == "SERVICE":
        generated = secret or secrets.token_urlsafe(32)
        secret_hash = _hash_secret(generated)
        secret_prefix = generated[:KEY_PREFIX_LENGTH]

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mdm.principal
                (principal_id, kind, subject, display_name, roles, secret_hash, secret_prefix)
            VALUES (%s, %s::mdm.principal_kind, %s, %s, %s::mdm.role_name[], %s, %s)
            """,
            (principal_id, kind, subject, display_name, list(roles),
             secret_hash, secret_prefix),
        )

    return (
        Principal(
            principal_id=principal_id, subject=subject, roles=tuple(roles),
            kind=kind, display_name=display_name,
        ),
        generated,
    )


def authenticate(conn: psycopg.Connection, secret: str | None) -> Principal:
    """Resolve an API key to a principal.

    Returns :data:`ANONYMOUS` rather than raising when the key is absent or
    wrong, so the caller decides what an unauthenticated request means. The
    comparison is constant-time: a fast rejection on the first differing byte
    leaks which prefix was close.
    """
    if not secret:
        return ANONYMOUS

    prefix = secret[:KEY_PREFIX_LENGTH]
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            -- roles is cast to text[]: psycopg has no loader registered for an
            -- array of a custom enum type and hands it back as the raw literal
            -- '{VIEWER}', which tuple() then splits into characters.
            SELECT principal_id, subject, roles::text[] AS roles, kind,
                   display_name, secret_hash
            FROM mdm.principal
            WHERE secret_prefix = %s AND is_active
              AND (expires_at IS NULL OR expires_at > now())
            """,
            (prefix,),
        )
        candidates = cur.fetchall()

    presented = _hash_secret(secret)
    for row in candidates:
        if row["secret_hash"] and hmac.compare_digest(row["secret_hash"], presented):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE mdm.principal SET last_seen_at = now() WHERE principal_id = %s",
                    (row["principal_id"],),
                )
            return Principal(
                principal_id=row["principal_id"], subject=row["subject"],
                roles=tuple(row["roles"]), kind=row["kind"],
                display_name=row["display_name"],
            )
    return ANONYMOUS


def authorize(principal: Principal, action: str) -> None:
    """Raise unless the caller may perform the action."""
    if not principal.may(action):
        raise AccessDenied(
            f"{principal.subject!r} holds roles {list(principal.roles)}, which do not "
            f"permit {action}"
        )


# ---------------------------------------------------------------------------
# Masking
# ---------------------------------------------------------------------------


def maskable_columns(
    spec: EntitySpec, *, minimum: PiiClass = PiiClass.DIRECT
) -> tuple[str, ...]:
    """Columns to mask for a caller without the unmask permission.

    Driven by the registry's declared PII class, so an attribute added later is
    protected by declaring what it is rather than by anyone remembering to
    update a list here.
    """
    tiers = {
        PiiClass.NONE: 0, PiiClass.INDIRECT: 1,
        PiiClass.DIRECT: 2, PiiClass.SENSITIVE: 3,
    }
    threshold = tiers[minimum]
    return tuple(f.name for f in spec.fields if tiers[f.pii] >= threshold)


def mask_frame(
    frame: pl.DataFrame,
    spec: EntitySpec,
    principal: Principal,
    *,
    minimum: PiiClass = PiiClass.DIRECT,
) -> tuple[pl.DataFrame, bool]:
    """Mask PII columns unless the caller is permitted to see them.

    Returns the frame and whether PII was revealed, which the access log
    records — "this call returned 4,000 unmasked dates of birth" is the event
    worth alerting on, and it cannot be reconstructed later.

    Masked columns are replaced rather than dropped. A consumer needs to know
    the attribute exists and is withheld; silently omitting it makes a masked
    response indistinguishable from a sparse record.
    """
    if principal.may_unmask:
        return frame, True

    targets = [c for c in maskable_columns(spec, minimum=minimum) if c in frame.columns]
    if not targets:
        return frame, False

    return (
        frame.with_columns([
            pl.when(pl.col(c).is_null())
            .then(None)
            .otherwise(pl.lit(MASK))
            .alias(c)
            for c in targets
        ]),
        False,
    )


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------


def log_access(
    conn: psycopg.Connection,
    principal: Principal,
    action: str,
    *,
    entity_name: str | None = None,
    entity_id: uuid.UUID | str | None = None,
    record_count: int = 1,
    pii_revealed: bool = False,
    request_id: str | None = None,
    client_ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one access event.

    Deliberately narrow and cheap: this is on the hot path of every API call, so
    it must not become a join or a trigger over the golden tables. The table
    itself refuses UPDATE and DELETE, so an audit trail a compromised
    application account can rewrite is not what this is.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mdm.access_log
                (principal_id, subject, action, entity_name, entity_id,
                 record_count, pii_revealed, request_id, client_ip, detail)
            VALUES (%s, %s, %s::mdm.access_action, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                None if principal.principal_id == uuid.UUID(int=0) else principal.principal_id,
                principal.subject, action, entity_name,
                str(entity_id) if entity_id else None, record_count, pii_revealed,
                request_id, client_ip, Jsonb(detail) if detail else None,
            ),
        )


def record_steward_action(
    conn: psycopg.Connection,
    principal: Principal,
    action: str,
    *,
    entity_name: str,
    reason: str,
    entity_id: uuid.UUID | str | None = None,
    related_id: uuid.UUID | str | None = None,
    before: dict[str, Any] | None = None,
    after: dict[str, Any] | None = None,
) -> uuid.UUID:
    """Record a manual intervention with its before and after state.

    Separate from the access log so that "what did a human change" is one query
    rather than a filter over everything the system did. The reason is mandatory
    and the database rejects a blank one: a manual override with no stated
    reason is the one nobody can defend later.
    """
    if not reason or len(reason.strip()) <= 3:
        raise ValueError("a steward action needs a substantive reason")

    action_id = uuid7()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mdm.steward_action
                (action_id, action, principal_id, subject, entity_name, entity_id,
                 related_id, before_value, after_value, reason)
            VALUES (%s, %s::mdm.access_action, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                action_id, action,
                None if principal.principal_id == uuid.UUID(int=0) else principal.principal_id,
                principal.subject, entity_name,
                str(entity_id) if entity_id else None,
                str(related_id) if related_id else None,
                Jsonb(before) if before else None,
                Jsonb(after) if after else None,
                reason,
            ),
        )
    return action_id
