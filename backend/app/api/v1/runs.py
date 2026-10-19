"""GET /api/v1/runs + GET /api/v1/runs/export — the agent_runs usage-ledger read surface (wave U5, B8).

WHY this file exists: `agent_runs` has carried the accounting truth (tokens split/used,
`cost_usd NUMERIC(14,4)`, attempt, session, `usage_json`) since migration 0002, and the spend truth
became COMPLETE when U3/H-03 landed (cycle runs write ledger rows —
`celery_app/tasks/daily_cycle.py:203-246`) — but the ledger had NO read surface (BEAT B8: "the runs
ledger exists in the database … but has no read surface").  This file is that read; the read itself
lives in `app/services/run_service.py` (a NEW file on purpose — `task_service.py` is lane P3's
territory).

FROZEN CONTRACT (quoted verbatim from the U5 brief; source: build/ui-reverse/wave-u2/backend-audit/
BACKEND-AUDIT.md lines 582-597, P4 — Usage ledger read + export (B8)):

  - `GET /api/v1/runs?task_id=&agent_type=&status=&from=&to=&limit=&offset=` — rows:
    `{id, task_id, task_title, agent_type, run_type, status, attempt, started_at, ended_at,
    duration_secs, tokens_input, tokens_output, tokens_used, cost_usd, cost_source, pricing_version}`
    where `cost_source`/`pricing_version` are read from `usage_json` (`accounting.RunUsage.as_dict`
    :98-117 writes them) and unmeasured stays `null`. Export: `GET /api/v1/runs/export?format=csv|json`
    streaming the same rows (row count must equal the filtered `total` — B8's declared-total rule;
    return `total` in the list response's headers or a wrapper object).

Wave-U5 frozen interfaces (WAVE-PLAN.md:56-57): runs router: `app/api/v1/runs.py`, object `router`,
APIRouter(dependencies=[verify_api_key]); paths GET /runs and GET /runs/export (P2 includes it).

THE SHAPE (frozen; `tests/unit/test_u5_runs.py` pins it key by key)

    200 {"items": [<row>, ...], "total": N}
      <row> — EXACTLY: id, task_id, task_title, agent_type, run_type, status, attempt,
              started_at, ended_at, duration_secs, tokens_input, tokens_output, tokens_used,
              cost_usd, cost_source, pricing_version
      `total` — the FULL filtered count, NOT the page size; `items` is the requested page.
    newest first: started_at DESC, id DESC as tiebreak; NULL-started_at rows sort LAST on both
    dialects (the explicit `started_at IS NULL` key — see run_service's docstring).

PROVENANCE — every field comes from a real source; nothing is invented here:
  * `task_title` <- `LEFT OUTER JOIN tasks` on `agent_runs.task_id` — null for runs with no task
    (cycle runs), which still serve.
  * `cost_source` / `pricing_version` <- the row's stored `usage_json` (cast to Text, parsed per
    row with documented junk rules — a malformed row must not refuse the feed; the ORM JSON
    attribute raises while fetching, measured).  `null` = unmeasured; never `""`, never 0.
  * `cost_usd` <- the NUMERIC(14,4) column, served as `float | None` — the same convention
    `tasks.py` uses for the per-task sums (`schemas/task.py:52-63`); NULL means UNMEASURED, and a
    measured 0 is served as 0.
  * `tokens_*`, `duration_secs`, `attempt`, `started_at`, `ended_at`, `status`, `run_type` <- the
    columns, exactly as stored (the read never re-derives).

EXPORT — `GET /api/v1/runs/export?format=csv|json` (default `csv`):
  * Same filters as the list (task_id, agent_type, status, from, to); NO limit/offset — the export
    is the FULL filtered set (B8's declared-total rule: the export's row count equals the same
    filter's total).
  * CSV = a header row (the frozen field names, in order) + one row per record; a null cell is an
    EMPTY cell (CSV has no null type — documented, never a fabricated `0`); datetimes are
    ISO-8601, exactly the values served by the list.
  * JSON = the same wrapper object (`{"items": [...], "total": N}`) with `total == len(items)`.
  * Both carry `X-Polsia-Row-Count: N` — the declared count, verified equal to the served rows
    BEFORE the first byte (see STREAMING below).
  * `format` is normalized (case-insensitive); anything else refuses by NAME: `E_VALIDATION`
    (422) naming the value, with the supported set in the message and context.

STREAMING — what "streaming" honestly means in this stack (refuted by measurement, see the lane's
raw probe): a DB cursor streamed straight through the response generator would start the response
BEFORE the read finished, so a mid-stream failure could neither become a named refusal nor verify
any declared count, and the client would receive a truncated body under a 200.  So the export
MATERIALIZES the verified row set first (inside `refusal_guard()`, so every read failure still
surfaces as the coded refusal with no body sent), cross-checks `len(rows) == total`, and then
streams the ALREADY-DECIDED bytes in chunks — chunked emission is real (bounded buffering per
chunk), the honesty of the count is guaranteed before the first byte leaves.

HONEST FAILURE: reads run inside `refusal_guard()`; a broken source surfaces as the named E_DB
refusal (503 envelope + `X-Polsia-Error-Code`), never as a 200 whose empty page reads like "no
runs".  A count/rows mismatch refuses the same way, naming both numbers.

AUTH: the same `dependencies=[Depends(verify_api_key)]` every sibling v1 router carries
(`channels.py:52`, `notifications.py:82-84`).
"""
from __future__ import annotations

import csv
import io
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.api.params import StrictInstant
from app.core.errors import E_VALIDATION, PolsiaError
from app.core.timeutil import iso_utc
from app.schemas.common import UtcDatetime
from app.services.run_service import RunFilters, RunRowData, export_runs, list_runs

router = APIRouter(prefix="/runs", tags=["runs"], dependencies=[Depends(verify_api_key)])

#: List-page bounds.  The export is NEVER capped by these — it serves the full filtered set.
DEFAULT_LIMIT = 50
MAX_LIMIT = 500

#: The export formats the contract names, in the order the brief lists them (the default is csv).
SUPPORTED_EXPORT_FORMATS = ("csv", "json")

#: How many rows-worth of CSV bytes are emitted per streamed chunk (bounded buffering).
CSV_CHUNK_ROWS = 200
#: JSON payload chunks are sliced at this many bytes.
JSON_CHUNK_BYTES = 64 * 1024


class RunRow(BaseModel):
    """One `agent_runs` row — EXACTLY the frozen field set, nothing more, nothing less.

    Every nullable field is `null` when UNMEASURED — served as `null`, never as 0 / "".
    """

    id: int
    task_id: int | None
    task_title: str | None
    agent_type: str
    run_type: str | None
    status: str | None
    attempt: int | None
    started_at: UtcDatetime | None
    ended_at: UtcDatetime | None
    duration_secs: float | None
    tokens_input: int | None
    tokens_output: int | None
    tokens_used: int | None
    cost_usd: float | None
    cost_source: str | None
    pricing_version: str | None


class RunsResponse(BaseModel):
    """The frozen wrapper object: `total` is the FULL filtered count, never the page size."""

    items: list[RunRow]
    total: int


#: The CSV header, DERIVED from the row model so it cannot drift from the wire contract.
CSV_COLUMNS: tuple[str, ...] = tuple(RunRow.model_fields)


def _normalized_instant(value: datetime | None) -> datetime | None:
    """A query instant as aware UTC.  Naive input is read as UTC — the clock the ledger stores."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _filters_from(
    *,
    task_id: int | None,
    agent_type: str | None,
    status: str | None,
    from_instant: datetime | None,
    to_instant: datetime | None,
) -> RunFilters:
    """Build the frozen filter set (both routes share it — one filter builder, one truth)."""
    return RunFilters(
        task_id=task_id,
        agent_type=agent_type,
        status=status,
        from_instant=_normalized_instant(from_instant),
        to_instant=_normalized_instant(to_instant),
    )


def _row_model(row: RunRowData) -> RunRow:
    """`RunRowData` -> wire row.  `cost_usd` renders the repo's float convention; pydantic
    converts the at-rest Decimal."""
    return RunRow(
        id=row.id,
        task_id=row.task_id,
        task_title=row.task_title,
        agent_type=row.agent_type,
        run_type=row.run_type,
        status=row.status,
        attempt=row.attempt,
        started_at=row.started_at,
        ended_at=row.ended_at,
        duration_secs=row.duration_secs,
        tokens_input=row.tokens_input,
        tokens_output=row.tokens_output,
        tokens_used=row.tokens_used,
        cost_usd=row.cost_usd,
        cost_source=row.cost_source,
        pricing_version=row.pricing_version,
    )


def _refuse_unknown_format(requested: str) -> PolsiaError:
    """The named refusal for an unknown export format (E_VALIDATION, per the frozen contract)."""
    return PolsiaError(
        E_VALIDATION,
        f"unknown export format {requested!r}",
        how_to_fix=f"pass format= one of {', '.join(SUPPORTED_EXPORT_FORMATS)}",
        context={"format": requested, "supported": list(SUPPORTED_EXPORT_FORMATS)},
    )


def _csv_cell(value: object) -> str:
    """One CSV cell.  `None` -> empty cell (CSV has no null; documented, never a fake 0)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        # V19/L2: the export promises "exactly the values served by the list" — the list
        # serves `...Z` (UtcDatetime), so the CSV cell must use the same UTC spelling.
        return iso_utc(value)
    return str(value)


def _csv_row(row: RunRow) -> list[str]:
    """The cells of one row in the frozen column order."""
    return [_csv_cell(getattr(row, name)) for name in CSV_COLUMNS]


async def _csv_chunks(rows: list[RunRow]) -> AsyncIterator[bytes]:
    """Header + one line per row, emitted in bounded chunks over the VERIFIED row set."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for index, row in enumerate(rows, start=1):
        writer.writerow(_csv_row(row))
        if index % CSV_CHUNK_ROWS == 0:
            yield buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate(0)
    remainder = buffer.getvalue()
    if remainder:
        yield remainder.encode("utf-8")


async def _json_chunks(payload: dict) -> AsyncIterator[bytes]:
    """The wrapper object as UTF-8 JSON, emitted in bounded byte chunks."""
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for start in range(0, len(data), JSON_CHUNK_BYTES):
        yield data[start : start + JSON_CHUNK_BYTES]


@router.get("", response_model=RunsResponse)
async def list_runs_route(
    task_id: int | None = Query(None),
    agent_type: str | None = Query(None),
    status: str | None = Query(None),
    from_instant: Annotated[StrictInstant, Query(alias="from")] = None,
    to_instant: Annotated[StrictInstant, Query(alias="to")] = None,
    limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> RunsResponse:
    """The filtered runs ledger page + the FULL filtered count, newest first (B8)."""
    filters = _filters_from(
        task_id=task_id, agent_type=agent_type, status=status,
        from_instant=from_instant, to_instant=to_instant,
    )
    with refusal_guard():
        rows, total = await list_runs(db, filters=filters, limit=limit, offset=offset)
    return RunsResponse(items=[_row_model(row) for row in rows], total=total)


@router.get("/export")
async def export_runs_route(
    format: str = Query("csv"),
    task_id: int | None = Query(None),
    agent_type: str | None = Query(None),
    status: str | None = Query(None),
    from_instant: Annotated[StrictInstant, Query(alias="from")] = None,
    to_instant: Annotated[StrictInstant, Query(alias="to")] = None,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """CSV/JSON export of the FULL filtered row set (B8: row count == the same filter's total)."""
    requested = format.strip().lower()
    if requested not in SUPPORTED_EXPORT_FORMATS:
        raise _refuse_unknown_format(format)

    filters = _filters_from(
        task_id=task_id, agent_type=agent_type, status=status,
        from_instant=from_instant, to_instant=to_instant,
    )
    # The read + the declared-count verification happen BEFORE the response object exists: any
    # refusal is still a clean coded response and NOTHING has been streamed (module docstring).
    with refusal_guard():
        rows, total = await export_runs(db, filters=filters)

    headers = {
        # The declared count.  `export_runs` verified it equals the row set about to be served.
        "X-Polsia-Row-Count": str(total),
        "Content-Disposition": f'attachment; filename="runs.{requested}"',
    }
    if requested == "csv":
        return StreamingResponse(
            _csv_chunks([_row_model(row) for row in rows]),
            media_type="text/csv",
            headers=headers,
        )
    payload = {
        "items": [model.model_dump(mode="json") for model in map(_row_model, rows)],
        "total": total,
    }
    return StreamingResponse(
        _json_chunks(payload), media_type="application/json", headers=headers
    )
