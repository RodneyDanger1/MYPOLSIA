"""Strict wire-parameter types for date/time query inputs (S4-F5 / decision #10, wave V15).

WHY this module exists: the routes typed their query params as bare `datetime`/`date`, and
pydantic's default parser reads NUMERIC input as an epoch — `?from=1.5` silently became
`1970-01-01T00:00:01.5Z` and the route answered 200 for a filter the caller never asked for
(measured at b17d7b0: `TypeAdapter(datetime).validate_python("1.5")` accepts).  Decision #10 is
LOCKED "strict": admit ISO-8601 or an INTEGER epoch only, and refuse floats/numeric junk BEFORE
any driver sees them, naming the rule and the offending input.

Where it applies (owner map E1): `api/v1/runs.py` `from`/`to` on the list AND its export twin,
and `api/v1/dashboard.py` `report_date`.  Non-numeric junk is passed through so pydantic's own
named refusal reports it (the pre-existing `?from=abc` 422 shape is unchanged, V14 S4-F2).

Dates take ISO-8601 (`YYYY-MM-DD`) only: epoch-day integers were NEVER accepted for a `date`
here (pydantic refuses ints for dates), so this validator keeps that door closed with a NAMED
message instead of silently widening what a date param accepts.
"""
from __future__ import annotations

import re
from datetime import UTC, date, datetime
from typing import Annotated, Any

from pydantic import BeforeValidator

#: A pure integer (optionally signed) — the ONLY numeric form a timestamp may take.
_INTEGER_RE = re.compile(r"[+-]?\d+")

#: "Looks numeric" but is not an integer (floats, `.5`, `5.`, exponents) — refused BY NAME.
_NUMERIC_NON_INTEGER_RE = re.compile(r"[+-]?(?:\d+\.\d*|\.\d+|\d+[eE][+-]?\d+|\d+\.\d*[eE][+-]?\d+)")

#: The only digit-shaped input a `date` param may carry is ... none (ISO-8601 or nothing).
_ANY_NUMERIC_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)([eE][+-]?\d+)?")


def _strict_instant(value: Any) -> Any:
    """`datetime` input: ISO-8601 string or integer epoch (seconds) only; floats refuse."""
    if value is None or isinstance(value, datetime):
        return value
    if isinstance(value, bool):
        raise ValueError(
            f"non-timestamp input {value!r} refused; pass ISO-8601 (e.g. 2026-10-01T00:00:00Z) "
            "or an INTEGER epoch (seconds)"
        )
    if isinstance(value, int):
        return datetime.fromtimestamp(value, tz=UTC)
    if isinstance(value, float):
        raise ValueError(
            f"non-integer timestamp {value!r} refused; pass ISO-8601 or an INTEGER epoch "
            "(seconds) — fractional epochs are not accepted"
        )
    if isinstance(value, str):
        stripped = value.strip()
        if _INTEGER_RE.fullmatch(stripped):
            return datetime.fromtimestamp(int(stripped), tz=UTC)
        if _NUMERIC_NON_INTEGER_RE.fullmatch(stripped):
            raise ValueError(
                f"non-integer timestamp {value!r} refused; pass ISO-8601 or an INTEGER epoch "
                "(seconds) — fractional epochs are not accepted"
            )
        return value  # ISO-8601 for pydantic to parse (or its own named refusal for junk)
    raise ValueError(
        f"{type(value).__name__} is not a timestamp; pass ISO-8601 or an INTEGER epoch (seconds)"
    )


def _strict_date(value: Any) -> Any:
    """`date` input: ISO-8601 `YYYY-MM-DD` only; every numeric form refuses by name."""
    if value is None or isinstance(value, date):
        return value
    if isinstance(value, bool):
        raise ValueError(f"non-date input {value!r} refused; pass an ISO-8601 date (YYYY-MM-DD)")
    if isinstance(value, (int, float)) or (
        isinstance(value, str) and _ANY_NUMERIC_RE.fullmatch(value.strip())
    ):
        raise ValueError(
            f"non-ISO date {value!r} refused; dates take ISO-8601 (YYYY-MM-DD) only — epoch "
            "numbers are not accepted for dates"
        )
    if isinstance(value, str):
        return value  # ISO-8601 (or junk: pydantic's own named refusal)
    raise ValueError(f"{type(value).__name__} is not a date; pass an ISO-8601 date (YYYY-MM-DD)")


#: Use inside a route signature — the Query/alias metadata must ride IN the annotation (FastAPI
#: 0.115 drops Annotated validators when a `Query(...)` OBJECT is the default; measured):
#:     x: Annotated[StrictInstant, Query(alias="from")] = None
StrictInstant = Annotated[datetime | None, BeforeValidator(_strict_instant)]
#: Same for `report_date`-style params:
#:     d: Annotated[StrictDate, Query()] = None
StrictDate = Annotated[date | None, BeforeValidator(_strict_date)]
