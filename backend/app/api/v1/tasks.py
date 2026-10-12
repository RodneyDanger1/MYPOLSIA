"""GET/POST /api/v1/tasks.

`list_tasks` is deliberately index-shaped: `(created_at DESC, id DESC)` with an
explicit LIMIT/OFFSET, so the page is a bounded backward index scan instead of a
sort of the whole table (measured: 63.771 ms -> 1.271 ms on 200k rows once the
`(created_at DESC)` index exists — `matrix-s200k.json`).

Unknown `agent_type` is 422 (the service raises `ValueError`; a bad value is the
caller's input, not an internal fault).  A missing task is 404 with the `E_NOT_FOUND`
code in the body.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.errors import E_NOT_FOUND, ERROR_HINTS
from app.schemas.task import TaskCreate, TaskResponse
from app.services.task_service import create_task, get_task, list_tasks

router = APIRouter(prefix="/tasks", tags=["tasks"], dependencies=[Depends(verify_api_key)])


@router.get("", response_model=list[TaskResponse])
async def list_tasks_endpoint(
    status_filter: str | None = Query(default=None, alias="status"),
    agent_type: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[TaskResponse]:
    with refusal_guard():
        tasks = await list_tasks(db, status=status_filter, agent_type=agent_type, limit=limit, offset=offset)
    return [TaskResponse.model_validate(task) for task in tasks]


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
    return TaskResponse.model_validate(task)


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
    return TaskResponse.model_validate(task)
