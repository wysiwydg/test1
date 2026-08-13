"""Shared request dependencies.

Sits at the package root rather than inside ``cmdm.api`` deliberately. Both the
API routes and the console routes need these, and homing them in either package
makes the other import it -- which is a circular dependency that only appears to
work because the import is deferred to call time. A shared dependency belongs
below both of its consumers, not inside one of them.

Extracting them also means the API and the console resolve the *same* connection
and authentication functions. Two copies of an authentication path is one place
too many for an authorization bug to hide.

The extraction is also what makes the annotations work. With
``from __future__ import annotations`` every annotation is a string that FastAPI
evaluates against the defining module's globals; dependencies passed in as
function parameters are not in those globals, so the framework cannot resolve
them and treats them as query parameters instead.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from typing import Annotated

import psycopg
from fastapi import Cookie, Depends, Header, HTTPException

from cmdm.db.engine import connect
from cmdm.governance.rbac import (
    AccessDenied,
    Action,
    Principal,
    authenticate,
    authorize,
    log_access,
)

__all__ = [
    "get_connection",
    "get_principal",
    "require",
    "ConnectionDep",
    "PrincipalDep",
    "SESSION_COOKIE",
]

#: Where the console keeps the caller's key. A browser cannot set an
#: ``X-API-Key`` header on a plain navigation, so without this the consoles are
#: reachable only from curl -- which is not a UI.
#:
#: It holds the API key itself rather than a minted session token. That is a
#: bearer credential in a cookie, and it is defensible only with the flags the
#: login route sets: ``HttpOnly`` keeps it out of any script on the page, and
#: ``SameSite=Lax`` is what stops another site POSTing to /console/steward/decide
#: with the operator's cookie attached. A deployment that already has SSO should
#: put it in front of this and map the assertion to a principal instead.
SESSION_COOKIE = "cmdm_session"


def get_connection() -> Iterator[psycopg.Connection]:
    """One pooled connection per request, committed on success."""
    with connect() as conn:
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise


ConnectionDep = Annotated[psycopg.Connection, Depends(get_connection)]


def get_principal(
    conn: ConnectionDep,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    session: Annotated[str | None, Cookie(alias=SESSION_COOKIE)] = None,
) -> Principal:
    """Resolve the caller.

    Header first, cookie second. A machine caller that sends a key means that
    key even if a stale console cookie is riding along on the same connection,
    and the reverse -- letting a cookie win -- would let a browser session
    silently answer for an integration.

    An unauthenticated request yields the anonymous principal, which holds no
    roles -- so it fails every authorization check rather than falling through
    to a permissive default.
    """
    return authenticate(conn, x_api_key or session)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require(principal: Principal, action: str, conn: psycopg.Connection) -> None:
    """Authorize, logging the denial.

    A refused request is logged as DENIED. Failed access attempts are the first
    thing an incident review looks for, and a system that only logs successes
    cannot show them.

    The denial is written on its own connection, not the request's. The request
    is about to fail, and :func:`get_connection` rolls back on exception -- so
    logging the refusal on the same transaction rolls the record of the refusal
    back with it, which is how this originally recorded nothing at all. An audit
    entry for a rejected attempt must not be undone by the rejection.
    """
    try:
        authorize(principal, action)
    except AccessDenied as exc:
        _log_denial(principal, action)
        raise HTTPException(status_code=403, detail=str(exc)) from None


def _log_denial(principal: Principal, action: str) -> None:
    """Record a refused attempt, independently of the request that failed.

    Best-effort by design: a database that cannot record the denial must not
    turn a 403 into a 500, because that hands the caller a different answer
    depending on whether the audit write succeeded -- and the difference is
    itself information about the system.
    """
    try:
        with connect() as audit:
            log_access(audit, principal, Action.DENIED, detail={"attempted": action})
            audit.commit()
    except Exception:  # pragma: no cover - audit must not mask the 403
        logging.getLogger(__name__).exception(
            "could not record DENIED for %s on %s", principal.subject, action
        )
