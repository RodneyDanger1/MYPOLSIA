"""Shared query plumbing for the service layer.

Two rules this module exists to enforce:

1. **A read that fails is a named refusal.**  `fetch_*` translate a driver/SQLAlchemy
   failure into `PolsiaError(E_DB, ...)` so a route can never turn "the database is
   unreachable" into `[]` (an empty list that looks like "no rows").
2. **Dialect choices are made in one place.**  `agent_rollup_expr` picks the string
   aggregation each engine actually has (SQLite `group_concat`, PostgreSQL
   `string_agg`) so no call site has to know.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import Select, distinct, func, literal
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_DB, ERROR_HINTS, PolsiaError

logger = logging.getLogger(__name__)


def json_or_none(raw: Any) -> Any:
    """A JSON column read as TEXT, parsed tolerantly: junk is `None`, never a 500.

    Why this exists: SQLAlchemy's JSON result processor raises `JSONDecodeError` WHILE FETCHING
    when one at-rest row holds bytes that are not JSON (a raw/ops writer can leave them).  One
    such row used to refuse the WHOLE feed with an uncoded 500 — `run_service` and
    `notifications` already avoid it by casting the column to Text, and this is the shared
    per-row parse for the reads that followed (V24/L6: admin reports + the activity feed).

    Rules: NULL -> None; a dict/list (PostgreSQL JSONB already decoded) -> itself; text ->
    `json.loads`; anything that fails -> None plus a WARNING naming the bytes.  The caller
    decides whether the parsed shape is acceptable (a detail must be an object, a list must be
    a list).  `None` means "not measured / not readable", NEVER an invented stand-in.
    """
    if raw is None:
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, (str, bytes, bytearray)):
        logger.warning("ignoring JSON at rest of unexpected type %s", type(raw).__name__)
        return None
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        logger.warning("ignoring malformed JSON at rest: %.60r", raw)
        return None


def db_refusal(what: str, exc: BaseException) -> PolsiaError:
    """The single shape of "we could not read X"."""
    return PolsiaError(
        E_DB,
        f"could not read {what}: {type(exc).__name__}: {exc}",
        how_to_fix=ERROR_HINTS[E_DB],
        context={"query": what},
    )


async def fetch_rows(db: AsyncSession, stmt: Select, *, what: str) -> list[Any]:
    """All rows of a multi-column select, or `E_DB`."""
    try:
        return list((await db.execute(stmt)).all())
    except SQLAlchemyError as exc:
        raise db_refusal(what, exc) from exc


async def fetch_scalars(db: AsyncSession, stmt: Select, *, what: str) -> list[Any]:
    """All first-column values of a select, or `E_DB`."""
    try:
        return list((await db.execute(stmt)).scalars().all())
    except SQLAlchemyError as exc:
        raise db_refusal(what, exc) from exc


async def fetch_scalar(db: AsyncSession, stmt: Select, *, what: str) -> Any:
    """First column of the first row (None when empty), or `E_DB`."""
    try:
        return (await db.execute(stmt)).scalar()
    except SQLAlchemyError as exc:
        raise db_refusal(what, exc) from exc


async def fetch_one(db: AsyncSession, stmt: Select, *, what: str) -> Any:
    """At most one row, or `E_DB`.

    `MultipleResultsFound` is *not* swallowed: a query that promised one row and
    returned several is a bug worth seeing, and it is re-raised as `E_DB` with the
    original exception chained.
    """
    try:
        return (await db.execute(stmt)).one_or_none()
    except SQLAlchemyError as exc:
        raise db_refusal(what, exc) from exc


def agent_rollup_expr(dialect: str, column: Any):
    """Comma-joined DISTINCT values of `column` — one expression per engine.

    SQLite and PostgreSQL spell "aggregate the distinct values into one string"
    differently, and neither name exists on the other engine:
    `group_concat(DISTINCT x)` vs `string_agg(DISTINCT x, ',')`.  Both were
    compile-checked for the other dialect and run for real on SQLite
    (`probe_fragments.py`).
    """
    if dialect == "postgresql":
        return func.string_agg(distinct(column), literal(","))
    return func.group_concat(distinct(column))
