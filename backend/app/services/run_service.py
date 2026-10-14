"""agent_runs ledger READ — wave U5 lane P4 (B8): the ONE filtered read both /runs and /runs/export serve.

WHY this module exists: `agent_runs` has carried the accounting truth (tokens split/used,
`cost_usd NUMERIC(14,4)`, attempt, `cli_session_id`, `usage_json`) since migration 0002, and the spend
truth became COMPLETE the day U3/H-03 landed — cycle runs write ledger rows
(`celery_app/tasks/daily_cycle.py:203-246`).  The ledger had NO read surface at all (BEAT B8); this
module is that read and `app/api/v1/runs.py` is its wire form.  It is a NEW file on purpose:
`task_service.py` is lane P3's territory (WAVE-PLAN.md:30-32).

THE RULE THIS MODULE SERVES (B8's declared-total rule): a declared row count must equal what is
served.  `count_runs` and `fetch_run_rows` run the SAME filter over the SAME table; `export_runs`
cross-checks `len(rows) == count` and REFUSES BY NAME on a mismatch instead of shipping a
complete-looking export that is not complete — a write landing between the two statements is exactly
the case where a confident declared count would be a lie.

SEMANTICS (frozen by the P4 outline; the identical text documents `app/api/v1/runs.py`):

  * Filters: `task_id`, `agent_type`, `status` (equality) and `from`/`to` instants on `started_at`,
    INCLUSIVE on both ends.  A NULL `started_at` cannot fall inside a window (SQL three-valued
    logic) and is honestly excluded.
  * `from`/`to` bounds are adapted to the ACTIVE DIALECT with `as_bound_instant` (the
    notifications.py pattern — `app/api/v1/notifications.py:139-140`): SQLite keeps
    `DateTime(timezone=True)` as NAIVE UTC text while PostgreSQL keeps `timestamptz`, so an aware
    bound must be rendered naive for SQLite or the window silently selects zero rows
    (`app/core/timeutil.py:93-123`).
  * Ordering: newest-first by `started_at`, then `id` DESC.  `started_at` is nullable; the leading
    `started_at IS NULL` key pins NULL rows LAST on BOTH dialects (PostgreSQL's `DESC` defaults to
    NULLS FIRST, SQLite sorts NULLs last under `DESC` — the explicit key removes the divergence;
    measured in the lane's raw probe).
  * `cost_source` / `pricing_version` are read from the row's stored `usage_json` — there are no
    columns for them.  The column is read as TEXT (`cast(..., Text)`) and parsed PER ROW because
    the ORM JSON result processor RAISES while FETCHING on one malformed at-rest row (measured:
    `json.JSONDecodeError` — one bad row would refuse the whole feed).  Junk rules, never silent:
    `usage_json` NULL / unparseable / non-dict -> both fields null; a value that is not a
    non-empty string -> null.  Unmeasured stays null, never `""`.
  * `cost_usd` passes through as the at-rest `Decimal` (NUMERIC(14,4)); the wire model renders the
    repo's float convention (`app/schemas/task.py:52-63`).  Every measurement is served EXACTLY as
    stored: the read never re-derives a half-measured sum, and an unmeasured column stays null
    (the writers' F-10/R8 discipline is the one definition of that rule).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import ColumnElement, Text, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_DB, ERROR_HINTS, PolsiaError
from app.core.timeutil import as_bound_instant
from app.models.task import AgentRun, Task
from app.services.query_helpers import fetch_rows, fetch_scalar


@dataclass(frozen=True)
class RunFilters:
    """The frozen filter set.  Instants are aware UTC (the route normalizes before calling);
    the service renders them for the active dialect."""

    task_id: int | None = None
    agent_type: str | None = None
    status: str | None = None
    from_instant: datetime | None = None
    to_instant: datetime | None = None


@dataclass(frozen=True)
class RunRowData:
    """One ledger row, exactly as stored.  `cost_usd` stays `Decimal` here (the at-rest type);
    `cost_source`/`pricing_version` come from `usage_json` and are `None` when unmeasured."""

    id: int
    task_id: int | None
    task_title: str | None
    agent_type: str
    run_type: str | None
    status: str | None
    attempt: int | None
    started_at: datetime | None
    ended_at: datetime | None
    duration_secs: float | None
    tokens_input: int | None
    tokens_output: int | None
    tokens_used: int | None
    cost_usd: Decimal | None
    cost_source: str | None
    pricing_version: str | None


def _nonempty_str(value: Any) -> str | None:
    """A stored attribution value or None.  `""` is the writer's "unset" marker
    (`RunUsage.as_dict` writes `cost_source or None`), so an empty string is NOT served."""
    if isinstance(value, str) and value:
        return value
    return None


def _usage_attribution(raw: str | None) -> tuple[str | None, str | None]:
    """`(cost_source, pricing_version)` from a row's raw `usage_json` text — junk reads as null.

    Tolerant per row on purpose (measured; see the module docstring): the feed must not refuse
    over one malformed at-rest row, and it must never fabricate an attribution.
    """
    if not raw:
        return None, None
    try:
        payload = json.loads(raw)
    except ValueError:
        return None, None
    if not isinstance(payload, dict):
        return None, None
    return _nonempty_str(payload.get("cost_source")), _nonempty_str(payload.get("pricing_version"))


def _run_conditions(filters: RunFilters, target: object) -> list[ColumnElement[bool]]:
    """The ONE condition list both the count and the row read run — they cannot drift apart."""
    conditions: list[ColumnElement[bool]] = []
    if filters.task_id is not None:
        conditions.append(AgentRun.task_id == filters.task_id)
    if filters.agent_type is not None:
        conditions.append(AgentRun.agent_type == filters.agent_type)
    if filters.status is not None:
        conditions.append(AgentRun.status == filters.status)
    if filters.from_instant is not None:
        conditions.append(AgentRun.started_at >= as_bound_instant(filters.from_instant, target))
    if filters.to_instant is not None:
        conditions.append(AgentRun.started_at <= as_bound_instant(filters.to_instant, target))
    return conditions


async def count_runs(db: AsyncSession, *, filters: RunFilters) -> int:
    """The FULL filtered row count — the number the wrapper's `total` declares (`E_DB` on failure)."""
    stmt = select(func.count()).select_from(AgentRun).where(*_run_conditions(filters, db))
    total = await fetch_scalar(db, stmt, what="runs total")
    if total is None:
        # `SELECT count(*)` always yields one row; a None here means the driver broke its own
        # contract — refuse loudly rather than serve a fabricated 0.
        raise PolsiaError(
            E_DB,
            "runs count query returned no row",
            how_to_fix=ERROR_HINTS[E_DB],
            context={"query": "runs total"},
        )
    return int(total)


def _row_to_data(row: Any) -> RunRowData:
    """One SQL row tuple -> `RunRowData` (see the docstring for what is converted and why)."""
    (
        run_id,
        task_id,
        task_title,
        agent_type,
        run_type,
        run_status,
        attempt,
        started_at,
        ended_at,
        duration_secs,
        tokens_input,
        tokens_output,
        tokens_used,
        cost_usd,
        usage_raw,
    ) = row
    cost_source, pricing_version = _usage_attribution(usage_raw)
    return RunRowData(
        id=int(run_id),
        task_id=None if task_id is None else int(task_id),
        task_title=None if task_title is None else str(task_title),
        agent_type=str(agent_type),
        run_type=None if run_type is None else str(run_type),
        status=None if run_status is None else str(run_status),
        attempt=None if attempt is None else int(attempt),
        started_at=started_at,
        ended_at=ended_at,
        duration_secs=None if duration_secs is None else float(duration_secs),
        tokens_input=None if tokens_input is None else int(tokens_input),
        tokens_output=None if tokens_output is None else int(tokens_output),
        tokens_used=None if tokens_used is None else int(tokens_used),
        # Money discipline (A-T17): a Decimal passes through; anything else crosses via
        # `Decimal(str(v))` — never binary-float arithmetic (the `_as_column_decimal` rule).
        cost_usd=None
        if cost_usd is None
        else (cost_usd if isinstance(cost_usd, Decimal) else Decimal(str(cost_usd))),
        cost_source=cost_source,
        pricing_version=pricing_version,
    )


async def fetch_run_rows(
    db: AsyncSession,
    *,
    filters: RunFilters,
    limit: int | None,
    offset: int = 0,
) -> list[RunRowData]:
    """The filtered rows, newest-first.  `limit=None` means the FULL filtered set (export path)."""
    stmt = (
        select(
            AgentRun.id,
            AgentRun.task_id,
            Task.title,
            AgentRun.agent_type,
            AgentRun.run_type,
            AgentRun.status,
            AgentRun.attempt,
            AgentRun.started_at,
            AgentRun.ended_at,
            AgentRun.duration_secs,
            AgentRun.tokens_input,
            AgentRun.tokens_output,
            AgentRun.tokens_used,
            AgentRun.cost_usd,
            # Deliberately TEXT, not the ORM JSON attribute: the JSON result processor raises
            # WHILE FETCHING on one malformed at-rest row (measured), which would refuse the
            # whole feed over one bad row.  See the module docstring's junk rules.
            cast(AgentRun.usage_json, Text),
        )
        .select_from(AgentRun)
        # LEFT OUTER JOIN: cycle runs (task_id NULL) have no title and MUST still serve.
        .outerjoin(Task, Task.id == AgentRun.task_id)
        .where(*_run_conditions(filters, db))
        # `started_at IS NULL` first: NULL rows last on BOTH dialects; then newest-first, id as
        # the stable tiebreaker.
        .order_by(AgentRun.started_at.is_(None), AgentRun.started_at.desc(), AgentRun.id.desc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    if offset:
        stmt = stmt.offset(offset)
    rows = await fetch_rows(db, stmt, what="runs")
    return [_row_to_data(row) for row in rows]


async def list_runs(
    db: AsyncSession, *, filters: RunFilters, limit: int, offset: int
) -> tuple[list[RunRowData], int]:
    """The list page + the FULL filtered count (the wrapper's `total`, never the page size)."""
    total = await count_runs(db, filters=filters)
    rows = await fetch_run_rows(db, filters=filters, limit=limit, offset=offset)
    return rows, total


async def export_runs(db: AsyncSession, *, filters: RunFilters) -> tuple[list[RunRowData], int]:
    """ALL filtered rows + the declared count — verified equal, or the export refuses by name.

    B8's declared-total rule, enforced: the count and the rows come from the same filter built by
    the same builder, and a mismatch (only reachable when a write lands between the two
    statements) is a NAMED `E_DB` refusal carrying both numbers — never a truncated export under
    a confident header.
    """
    total = await count_runs(db, filters=filters)
    rows = await fetch_run_rows(db, filters=filters, limit=None, offset=0)
    if len(rows) != total:
        raise PolsiaError(
            E_DB,
            f"runs export refused: declared total={total} but {len(rows)} row(s) read",
            how_to_fix="retry the export; the ledger changed between the count and the read",
            context={"declared_total": total, "rows_read": len(rows)},
        )
    return rows, total
