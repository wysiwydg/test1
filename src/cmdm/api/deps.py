"""Shared request dependencies.

Extracted into their own module so that the API routes and the console routes
resolve the *same* connection and authentication functions. Two copies of an
authentication path is one place too many for an authorization bug to hide.

The extraction is also what makes the annotations work. With
``from __future__ import annotations`` every annotation is a string that FastAPI
evaluates against the defining module's globals; dependencies passed in as
function parameters are not in those globals, so the framework cannot resolve
them and treats them as query parameters instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Annotated

import psycopg
from fastapi import Depends, Header, HTTPException

from cmdm.db.engine import connect
from cmdm.governance.rbac import (
    AccessDenied,
    Action,
    Principal,
    authenticate,
    authorize,
    log_access,
)

__all__ = ["get_connection", "get_principal", "require", "ConnectionDep", "PrincipalDep"]


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
) -> Principal:
    """Resolve the caller.

    An unauthenticated request yields the anonymous principal, which holds no
    roles -- so it fails every authorization check rather than falling through
    to a permissive default.
    """
    return authenticate(conn, x_api_key)


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require(principal: Principal, action: str, conn: psycopg.Connection) -> None:
    """Authorize, logging the denial.

    A refused request is logged as DENIED. Failed access attempts are the first
    thing an incident review looks for, and a system that only logs successes
    cannot show them.
    """
    try:
        authorize(principal, action)
    except AccessDenied as exc:
        log_access(conn, principal, Action.DENIED, detail={"attempted": action})
        raise HTTPException(status_code=403, detail=str(exc)) from None
