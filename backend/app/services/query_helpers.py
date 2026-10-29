"""Shared query plumbing for the service layer.

Two rules this module exists to enforce:

1. **A read that fails is a named refusal.**  `fetch_*` translate a driver/SQLAlchemy
   failure into `PolsiaError(E_DB, ...)` so a route can never turn "the database is
   unreachable" into `[]` (an empty list that looks like "no rows").
2. **Dialect choices are made in one place.**  `agent_rollup_expr` picks the string
   aggregation each engine actually has (SQLite `group_concat`, PostgreSQL
   `string_agg`) so no call site has to know.

W8-LD-03 (wave 9) adds the third rule — **caller input is not a database outage**:

* a DRIVER DATA/RANGE failure (`invalid input syntax for type integer`, `value out of int32
  range`, `numeric field overflow`, `date/time field value out of range`) is the caller's value,
  so it is refused as `E_VALIDATION` (422) with a `how_to_fix`, never as `E_DB` (503) blaming the
  database.  Measured live before this change: `GET /api/v1/tasks/9223372036854775808` answered
  **503 `E_DB`** on PostgreSQL;
* the client-facing message NEVER carries the server's compiled statement.  SQLAlchemy appends
  `[SQL: SELECT tasks.id, …] [parameters: …]` to the driver's exception, and the wave-8 close
  probe captured exactly that text on the wire (a schema disclosure, and useless to the caller).
  `_redact_sql` strips it from the wire message; the full text goes to the server log, where an
  operator can act on it and the caller cannot mine it;
* a GENUINE outage (connection refused, missing table, timeout) keeps `E_DB` 503 — the
  classification only moves failures the driver itself calls a data/range problem.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import Select, distinct, func, literal
from sqlalchemy.exc import DataError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_DB, E_VALIDATION, ERROR_HINTS, PolsiaError

logger = logging.getLogger(__name__)

#: Driver exception CLASS NAMES that mean "the value is not usable for that column".  Matched by
#: name (not import) because the asyncpg names live in a package this app must not require at
#: import time (SQLite-only unit runs, and `asyncpg` is lazy-imported repo-wide).
DATA_ERROR_TYPES = frozenset(
    {
        "DataError",
        "InvalidTextRepresentationError",
        "NumericValueOutOfRangeError",
        "DatatypeMismatchError",
        "StringDataRightTruncationError",
        "InvalidDatetimeFormatError",
        "DatetimeFieldOverflowError",
    }
)

#: Case-insensitive phrases the DRIVER puts in the message of a data/range failure.  Deliberately
#: narrow: "invalid input syntax"/"invalid input for query argument" (not every "invalid input"),
#: and the numeric/time overflow family — a bare-string match on "invalid" would reclassify genuine
#: driver failures as caller mistakes.
DATA_ERROR_MARKERS = (
    "invalid input syntax",
    "invalid input for query argument",
    "value out of int",
    "out of range",
    "numeric field overflow",
    "invalidtextrepresentation",
    "numericvalueoutofrange",
    "stringdatarighttruncation",
    "datatypemismatch",
    "date/time field value out of range",
)

#: SQLAlchemy's own statement/parameter echo — never echoed to a client.
_SQL_ECHO_MARKERS = ("[SQL:", "[parameters:", "[cached since", "(Background on this error at:")


def _redact_sql(text: str) -> str:
    """`text` without SQLAlchemy's compiled statement, bound parameters or doc link.

    The cut is at the FIRST echo marker: everything a driver says about the failure itself comes
    before it, and the statement (with its column list) comes after.  Never raises.
    """
    if not text:
        return ""
    redacted = text
    for marker in _SQL_ECHO_MARKERS:
        index = redacted.find(marker)
        if index != -1:
            redacted = redacted[:index]
    return redacted.strip()


def _exception_chain(exc: BaseException, *, depth: int = 4) -> list[BaseException]:
    """`exc` plus the exceptions it wraps (`orig`, `__cause__`, `__context__`), bounded."""
    seen: list[BaseException] = []
    current: BaseException | None = exc
    for _ in range(max(1, depth)):
        if current is None or any(current is item for item in seen):
            break
        seen.append(current)
        wrappers = [
            getattr(current, "__cause__", None),
            getattr(current, "__context__", None),
            getattr(current, "orig", None),
        ]
        current = next((item for item in wrappers if isinstance(item, BaseException)), None)
    return seen


def is_data_error(exc: BaseException) -> bool:
    """Is this a driver DATA/RANGE failure (caller input) rather than a database outage?

    Two independent legs, because neither alone is trustworthy: the exception's class (the
    DB-API's own mapping, `sqlalchemy.exc.DataError` and the asyncpg data-error family) and the
    driver's message (`invalid input syntax`, `out of range`, `numeric field overflow`, …).
    Walking the wrapper chain matters: SQLAlchemy raises `DBAPIError` around the driver's own
    `DataError`, so the class test must look at `orig` and the cause chain too.
    """
    for candidate in _exception_chain(exc):
        if isinstance(candidate, DataError):
            return True
        if type(candidate).__name__ in DATA_ERROR_TYPES:
            return True
        haystack = str(candidate).lower()
        if any(marker in haystack for marker in DATA_ERROR_MARKERS):
            return True
    return False


def db_refusal(what: str, exc: BaseException) -> PolsiaError:
    """The single shape of "we could not read X" — and of "X is not a value we can use".

    A data/range failure is `E_VALIDATION` (422): the caller sent a value the column cannot hold,
    which is the same class as any other request mistake and must not read as "the database is
    broken" (it sent users to retry a request that can never succeed, and it made a 503 pager fire
    on a malformed URL).  Every other failure stays `E_DB` 503.

    Both messages are SQL-free: the compiled statement, the bound parameters and the
    "Background on this error" link are stripped, and the FULL driver text is logged server-side
    with the query name so the operator loses nothing (`_redact_sql`).
    """
    driver_text = f"{type(exc).__name__}: {exc}"
    if is_data_error(exc):
        logger.warning(
            "data/range error reading %s (classified E_VALIDATION, not E_DB): %s",
            what,
            driver_text,
        )
        return PolsiaError(
            E_VALIDATION,
            f"{what} is not a value this database column can hold: the driver rejected it as a "
            "data/range error (an out-of-range integer, a malformed date/number, or a value too "
            "long for the column)",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={
                "query": what,
                "exception": type(exc).__name__,
                "class": "data_or_range",
                "detail": _redact_sql(driver_text),
            },
        )
    logger.warning("database read failed for %s: %s", what, driver_text)
    return PolsiaError(
        E_DB,
        f"could not read {what}: {_redact_sql(driver_text)}",
        how_to_fix=ERROR_HINTS[E_DB],
        # W9: the outage pin contract stays the shipped {"query": name}; the driver class and
        # full text are logged server-side (a richer context would be a wire change).
        context={"query": what},
    )
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
    if isinstance(raw, dict | list):
        return raw
    if not isinstance(raw, str | bytes | bytearray):
        logger.warning("ignoring JSON at rest of unexpected type %s", type(raw).__name__)
        return None
    try:
        return json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        logger.warning("ignoring malformed JSON at rest: %.60r", raw)
        return None


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
