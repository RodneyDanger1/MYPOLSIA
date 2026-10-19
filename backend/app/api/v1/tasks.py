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
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.api.v1.totals import TOTAL_HEADER, declared_total
from app.core.errors import E_NOT_FOUND, ERROR_HINTS
from app.models.task import Task
from app.schemas.task import TaskCreate, TaskResponse, TaskStatusUpdate
from app.services.task_service import (
    TaskRunTotals,
    create_task,
    get_task,
    get_task_run_totals,
    list_tasks,
    task_completed_at,
    update_task_status,
)

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(verify_api_key)])


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
