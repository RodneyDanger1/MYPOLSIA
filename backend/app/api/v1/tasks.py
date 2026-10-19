"""GET/POST/PATCH /api/v1/tasks.

`list_tasks` is deliberately index-shaped: `(created_at DESC, id DESC)` with an
explicit LIMIT/OFFSET, so the page is a bounded backward index scan instead of a
sort of the whole table (measured: 63.771 ms -> 1.271 ms on 200k rows once the
`(created_at DESC)` index exists — `matrix-s200k.json`).

Unknown `agent_type` is 422 (the service raises `ValueError`; a bad value is the
caller's input, not an internal fault).  A missing task is 404 with the `E_NOT_FOUND`
code in the body.

`PATCH /{task_id}/status` (T12/D20) is the typed write the `TaskStatusUpdate` model
documents: it calls the SAME `task_service.update_task_status` the worker uses, so the
service's `VALID_TASK_STATUSES` remains the single authority over the value set and an
unknown status is refused by name with the accepted values in the context (E_VALIDATION ->
422, via `refusal_guard`).  `result_summary`/`error_message` are only written when the body
supplies them.  The row is committed by `get_db` on success, like every other write route.

`POST /{task_id}/run` (V24/L1) is the missing verb between "created" and "executed": it loads
the row through `task_service.get_runnable_task` (404 `E_NOT_FOUND`; any status but `pending`
is `E_TASK_NOT_RUNNABLE` with the CURRENT status in context), then hands the id to the SAME
Celery task the scheduler uses (`run_agent_task.delay`).  The broker call is blocking, so it
runs through `asyncio.to_thread` exactly like `agents.py`'s trigger — a dead broker must not
stall the event loop while it climbs redis-py's retry ladder.  A broker refusal is a named
503 `E_TASK_NOT_RUNNABLE` and the row is LEFT HONEST: this route never marks anything
`in_progress`; only the worker's atomic `claim_task` may do that after a real delivery.

IMAGE ATTACHMENTS (V25/L1) — the vision-input surface, in this module and a sibling router:

    POST   /api/v1/tasks/{task_id}/attachments   multipart `file` -> 201 metadata
    GET    /api/v1/tasks/{task_id}/attachments   -> [metadata], upload order
    GET    /api/v1/attachments/{attachment_id}   -> the bytes, sniffed content type
    DELETE /api/v1/attachments/{attachment_id}   -> 204 (blob + row removed)

The upload refuses BY NAME: `E_VALIDATION` (not PNG/JPEG/WebP by magic bytes, > 5 MiB, > 4 per
task, > 8 MiB total), `E_NOT_FOUND` (unknown task / attachment), `E_CONFIG` (the data volume is
not writable) and `E_UPSTREAM` (the row exists but its blob is gone/no longer the recorded type).
Every route is owner-keyed by the router dependency; the byte response carries
`X-Content-Type-Options: nosniff` and an `inline` disposition with a QUOTED name, and the path is
derived from the row's server-generated `stored_name` — never from the operator's file name.
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.api.v1.totals import TOTAL_HEADER, declared_total
from app.core.errors import E_NOT_FOUND, E_TASK_NOT_RUNNABLE, ERROR_HINTS
from app.models.task import Task
from app.schemas.task import (
    TaskAttachmentResponse,
    TaskCreate,
    TaskResponse,
    TaskStatusUpdate,
)
from app.services import task_attachment_service as attachment_service
from app.services.task_service import (
    TaskRunTotals,
    create_task,
    get_runnable_task,
    get_task,
    get_task_run_totals,
    list_tasks,
    task_completed_at,
    update_task_status,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(verify_api_key)])


class TaskRunDispatchResponse(BaseModel):
    """The frozen V24/L1 wire shape of a run dispatch (202) — nothing is claimed here.

    `status` is the literal `"dispatched"`: the broker accepted ONE message for this row. It
    deliberately says nothing about the task's own status, which only the worker's claim moves
    (`pending` -> `in_progress`); a client must re-read the task to learn that.
    """

    task_id: int
    status: str


def _fill_truth(response: TaskResponse, task: Task, totals: TaskRunTotals | None) -> TaskResponse:
    """The wave-U2 truth fields on ONE response — the single fill every construction site uses.

    `completed_at` is read off the row (`task_service.task_completed_at`: only a `completed` task
    has one — a failed task did not complete).  The cost/token sums come from the runs-ledger read
    handed in; `totals is None` means the task has NO runs and every sum stays null — unmeasured is
    never zero (the frozen contract; see `schemas/task.py`).  Mutating the validated model is
    deliberate: the four fields are declared on `TaskResponse` and this is the one place they are
    set from real rows, so a site that skipped this helper would serve the defined-but-never-called
    defect — which the U2 tests pin per site.
    """
    response.completed_at = task_completed_at(task)
    if totals is not None:
        response.cost_usd = float(totals.cost_usd) if totals.cost_usd is not None else None
        response.tokens_in = totals.tokens_in
        response.tokens_out = totals.tokens_out
    return response


async def _task_response(db: AsyncSession, task: Task) -> TaskResponse:
    """One task -> one response, truth fields filled (used by the detail, create and patch sites)."""
    totals = await get_task_run_totals(db, [task.id])
    return _fill_truth(TaskResponse.model_validate(task), task, totals.get(task.id))


async def _task_responses(db: AsyncSession, tasks: list[Task]) -> list[TaskResponse]:
    """A page of tasks -> responses, truth fields filled from ONE grouped ledger read."""
    totals = await get_task_run_totals(db, [task.id for task in tasks])
    return [_fill_truth(TaskResponse.model_validate(task), task, totals.get(task.id)) for task in tasks]


@router.get("", response_model=list[TaskResponse])
async def list_tasks_endpoint(
    response: Response,
    status_filter: str | None = Query(default=None, alias="status"),
    agent_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[TaskResponse]:
    """The task list, newest first.

    V20/L1 (card L2-07): `X-Polsia-Total` declares the FULL filtered count (the same `WHERE`,
    without LIMIT/OFFSET) so a full page is distinguishable from a truncated one.  The body stays
    a bare array — every existing client parses it as one.
    """
    with refusal_guard():
        tasks = await list_tasks(db, status=status_filter, agent_type=agent_type, limit=limit, offset=offset)
        responses = await _task_responses(db, tasks)
        conditions = []
        if status_filter is not None:
            conditions.append(Task.status == status_filter)
        if agent_type is not None:
            conditions.append(Task.agent_type == agent_type)
        total = await declared_total(db, Task, conditions, what="tasks total")
    response.headers[TOTAL_HEADER] = str(total)
    return responses


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
async def create_task_endpoint(payload: TaskCreate, db: AsyncSession = Depends(get_db)) -> TaskResponse:
    with refusal_guard():
        try:
            task = await create_task(
                db,
                title=payload.title,
                agent_type=payload.agent_type,
                description=payload.description,
                priority=payload.priority,
                source=payload.source,
                scheduled_date=payload.scheduled_date,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail={
                    "code": "E_AGENT_UNKNOWN",
                    "message": str(exc),
                    "how_to_fix": "use an agent_type from task_service.VALID_AGENT_TYPES",
                    "context": {"agent_type": payload.agent_type},
                },
            ) from exc
        response = await _task_response(db, task)
    return response


@router.get("/{task_id}", response_model=TaskResponse)
async def get_task_endpoint(task_id: int, db: AsyncSession = Depends(get_db)) -> TaskResponse:
    with refusal_guard():
        task = await get_task(db, task_id)
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": E_NOT_FOUND,
                "message": f"task {task_id} does not exist",
                "how_to_fix": ERROR_HINTS[E_NOT_FOUND],
                "context": {"task_id": task_id},
            },
        )
    with refusal_guard():
        return await _task_response(db, task)


@router.post(
    "/{task_id}/run",
    response_model=TaskRunDispatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def run_task_endpoint(
    task_id: int, db: AsyncSession = Depends(get_db)
) -> TaskRunDispatchResponse:
    """Dispatch ONE existing pending task to the worker queue (V24/L1, frozen interface).

    Order of operations, each deliberate:
      1. `get_runnable_task` loads the row and refuses by NAME before anything is dispatched
         (404 `E_NOT_FOUND`, or 409 `E_TASK_NOT_RUNNABLE` with the current status in context);
      2. `.delay()` runs in a thread (`asyncio.to_thread`) — it is a blocking broker call;
      3. a broker refusal answers 503 `E_TASK_NOT_RUNNABLE` and this route writes NOTHING to
         the row: `pending` + no claim fields is the honest state of "no worker was told".
    The 202 body names the row and the dispatch fact, never a task status the worker has not
    written yet.
    """
    with refusal_guard():
        task = await get_runnable_task(db, task_id)
    dispatched_id = int(task.id)

    try:
        # The import is lazy for the same reason `agents.py` keeps it lazy: importing the Celery
        # task module pulls in the whole worker stack, and the API must boot without a broker.
        from celery_app.tasks.agent_tasks import run_agent_task

        # `.delay()` is BLOCKING (redis-py's connection retry ladder). Called directly from an
        # async handler it stalls the uvicorn event loop for the whole ladder — measured in this
        # repo's e2e (agents.py:93-99). `to_thread` keeps the loop free.
        await asyncio.to_thread(run_agent_task.delay, dispatched_id)
    except Exception as exc:  # broker unavailable: NO delivery was accepted for this row
        logger.warning("failed to dispatch task %s", dispatched_id, exc_info=True)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": E_TASK_NOT_RUNNABLE,
                "message": (
                    f"task {dispatched_id} could not be handed to a worker: the broker did not "
                    f"accept the dispatch ({exc.__class__.__name__})"
                ),
                "how_to_fix": (
                    "check REDIS_URL and that the broker/worker are running, then run the task "
                    "again; the row was left pending and untouched"
                ),
                "context": {
                    "task_id": dispatched_id,
                    "status": "pending",
                    "reason": exc.__class__.__name__,
                },
            },
        ) from exc

    return TaskRunDispatchResponse(task_id=dispatched_id, status="dispatched")


@router.patch("/{task_id}/status", response_model=TaskResponse)
async def update_task_status_endpoint(
    task_id: int, payload: TaskStatusUpdate, db: AsyncSession = Depends(get_db)
) -> TaskResponse:
    """Typed status write (T12/D20).

    The service is the authority: an unknown status raises its named `E_VALIDATION` refusal
    (422 through `refusal_guard`, with the accepted value set in the context), and a missing
    task is the same `E_NOT_FOUND` envelope the read routes answer.  Nothing is written when
    either refuses — `update_task_status` validates BEFORE it touches the row, and `get_db`
    rolls the session back on the raised refusal.
    """
    with refusal_guard():
        task = await update_task_status(
            db,
            task_id,
            payload.status,
            result_summary=payload.result_summary,
            error_message=payload.error_message,
        )
    if task is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "code": E_NOT_FOUND,
                "message": f"task {task_id} does not exist",
                "how_to_fix": ERROR_HINTS[E_NOT_FOUND],
                "context": {"task_id": task_id},
            },
        )
    with refusal_guard():
        return await _task_response(db, task)


# ── V25/L1: task image attachments (vision input) ────────────────────────────────────────────
# A SECOND router for the id-addressed attachment surface (`/api/v1/attachments/{id}`), so the
# byte route cannot collide with `/tasks/{task_id}` and each router keeps ONE prefix.  Both are
# owner-keyed; `main.py` includes this one next to `tasks.router`.

attachments_router = APIRouter(
    prefix="/attachments", tags=["tasks"], dependencies=[Depends(verify_api_key)]
)


def _task_404(task_id: int) -> HTTPException:
    """The named 404 every task-addressed route answers for an unknown id."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": E_NOT_FOUND,
            "message": f"task {task_id} does not exist",
            "how_to_fix": ERROR_HINTS[E_NOT_FOUND],
            "context": {"task_id": task_id},
        },
    )


def _attachment_404(attachment_id: int) -> HTTPException:
    """The named 404 for an unknown attachment id (the row does not exist)."""
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail={
            "code": E_NOT_FOUND,
            "message": f"attachment {attachment_id} does not exist",
            "how_to_fix": ERROR_HINTS[E_NOT_FOUND],
            "context": {"attachment_id": attachment_id},
        },
    )


@router.post(
    "/{task_id}/attachments",
    response_model=TaskAttachmentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def upload_task_attachment_endpoint(
    task_id: int,
    file: UploadFile = File(..., description="a PNG, JPEG or WebP image, at most 5 MB"),
    db: AsyncSession = Depends(get_db),
) -> TaskAttachmentResponse:
    """Attach ONE image to an existing task (V25/L1).

    Order matters: the task is checked FIRST (an upload to a missing task is `E_NOT_FOUND`, not
    a file written for a row that never existed), then the body is read under the 5 MiB cap, then
    the service sniffs the bytes and applies the per-task caps BEFORE writing anything.  A 201
    means the blob is on the data volume and the row is persisted — `get_db` commits after the
    response is built, and the service removes the blob again if the row fails to land.
    """
    with refusal_guard():
        task = await get_task(db, task_id)
        if task is None:
            raise _task_404(task_id)
        data = await attachment_service.read_upload(file)
        row = await attachment_service.save_attachment(
            db,
            task_id=task_id,
            filename=file.filename or "image",
            data=data,
            declared_mime=file.content_type,
        )
        return TaskAttachmentResponse.model_validate(row)


@router.get("/{task_id}/attachments", response_model=list[TaskAttachmentResponse])
async def list_task_attachments_endpoint(
    task_id: int, db: AsyncSession = Depends(get_db)
) -> list[TaskAttachmentResponse]:
    """This task's images, oldest first — metadata only; the bytes have their own route."""
    with refusal_guard():
        task = await get_task(db, task_id)
        if task is None:
            raise _task_404(task_id)
        rows = await attachment_service.list_attachments(db, task_id)
        return [TaskAttachmentResponse.model_validate(row) for row in rows]


@attachments_router.get(
    "/{attachment_id}",
    response_class=Response,
    responses={
        200: {"content": {"image/png": {}, "image/jpeg": {}, "image/webp": {}}},
        404: {"description": "E_NOT_FOUND"},
        502: {"description": "E_UPSTREAM — the row exists, its blob is gone"},
    },
)
async def get_attachment_endpoint(
    attachment_id: int, db: AsyncSession = Depends(get_db)
) -> Response:
    """Serve ONE attachment's exact bytes with the SNIFFED content type (V25/L1).

    `stored_name` is server-generated and re-validated by the service, so the path this reads can
    never contain `..`, a separator or a symlink escape.  A row whose blob vanished is
    `E_UPSTREAM` (502), never an empty 200: a caller must be able to tell "no image" from
    "the image is gone".  `nosniff` stops a browser from reinterpreting the bytes as anything
    other than the recorded image type.
    """
    with refusal_guard():
        row = await attachment_service.get_attachment(db, attachment_id)
        if row is None:
            raise _attachment_404(attachment_id)
        data = await attachment_service.read_attachment_bytes(row)
        disposition = f'inline; filename="{quote((row.filename or "image")[:255])}"'
        return Response(
            content=data,
            media_type=row.mime,
            headers={
                "Content-Disposition": disposition,
                "Content-Length": str(len(data)),
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "private, max-age=60",
            },
        )


@attachments_router.delete("/{attachment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attachment_endpoint(
    attachment_id: int, db: AsyncSession = Depends(get_db)
) -> Response:
    """Remove ONE attachment: the blob and the row together, or neither (V25/L1)."""
    with refusal_guard():
        row = await attachment_service.get_attachment(db, attachment_id)
        if row is None:
            raise _attachment_404(attachment_id)
        await attachment_service.delete_attachment(db, row)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
