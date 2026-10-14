"""GET /api/v1/notifications — the bell's read surface (wave U2, frozen contract).

The U1 shell ships a notifications bell that has to say "this build has no notifications
endpoint" (`frontend/src/components/layout/Topbar.tsx`); this route is what lets it stop saying
that.  The contract is deliberately derived, never stored: there is NO notifications table and NO
migration — every item is a projection of an EXISTING task row.

THE SHAPE (frozen, both sides briefed with the same text)

    200 {"items": [...], "count": N}   — items newest first, at most 20, window = last 48 hours.
    each item: id (stable string), kind (task_completed | task_failed | awaiting_approval),
               title (the task's title), task_id (int), created_at (ISO-8601).

KIND MAPPING (a status outside the three produces no notification):
    completed         -> task_completed, created_at = the completion timestamp
    failed            -> task_failed
    awaiting_approval -> awaiting_approval (the approvals channel parked it for the owner)

TIMESTAMP — `task_service.task_terminal_at_expr()`: `coalesce(tasks.updated_at, tasks.created_at)`,
the completion/park instant the repo's own writer stamps (`update_task_status` sets
`updated_at = utcnow()` at every transition).  The window bound is computed as a UTC instant and
adapted to the active dialect (`as_bound_instant`), so SQLite (naive UTC text) and PostgreSQL
(timestamptz) select the same rows; the served `created_at` is the column value — the same
timestamp convention every other read surface serves.  Window and ORDER BY share the one
expression, so a row is never filtered on one clock and ordered by another.

STABLE id — `task:<task_id>:<kind>`, a pure function of the row: two requests inside the window
return the same identifier for the same notification (a client may key on it), and it changes
only when the row itself changes kind.

Auth: the same `dependencies=[Depends(verify_api_key)]` every sibling v1 router carries.
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.database import utcnow
from app.core.timeutil import as_bound_instant
from app.models.task import Task, TaskStatus
from app.schemas.task import NotificationItem, NotificationKind, NotificationsResponse
from app.services.query_helpers import fetch_rows
from app.services.task_service import task_terminal_at_expr

router = APIRouter(
    prefix="/notifications", tags=["notifications"], dependencies=[Depends(verify_api_key)]
)

#: The frozen contract's bounds.  Named constants (not literals in the statement) so the tests and
#: the route read the same numbers.
NOTIFICATIONS_LIMIT = 20
NOTIFICATIONS_WINDOW_HOURS = 48

#: task status -> notification kind, over the frozen vocabulary (`schemas.task.NotificationKind`).
#: `pending`/`in_progress` are not events the bell reports, and `cancelled`/`rejected` rows are the
#: tasks page's own record — a status absent from this mapping contributes no item.
KIND_BY_STATUS: dict[TaskStatus, NotificationKind] = {
    TaskStatus.COMPLETED: "task_completed",
    TaskStatus.FAILED: "task_failed",
    TaskStatus.AWAITING_APPROVAL: "awaiting_approval",
}


@router.get("", response_model=NotificationsResponse)
async def list_notifications(db: AsyncSession = Depends(get_db)) -> NotificationsResponse:
    """The newest 20 task-derived notifications inside the last 48 hours, newest first."""
    at = task_terminal_at_expr()
    since = as_bound_instant(utcnow() - timedelta(hours=NOTIFICATIONS_WINDOW_HOURS), db)
    stmt = (
        select(Task.id, Task.title, Task.status, at.label("at"))
        .where(Task.status.in_(list(KIND_BY_STATUS)), at >= since)
        .order_by(at.desc(), Task.id.desc())
        .limit(NOTIFICATIONS_LIMIT)
    )
    with refusal_guard():
        rows = await fetch_rows(db, stmt, what="notifications")

    items: list[NotificationItem] = []
    for task_id, title, status, at_value in rows:
        # The status arrives as the Enum MEMBER on a typed column select (and as a plain string
        # under a raw one); `TaskStatus(...)` normalises both to the member before the lookup.
        kind = KIND_BY_STATUS[TaskStatus(str(status))]
        items.append(
            NotificationItem(
                id=f"task:{int(task_id)}:{kind}",
                kind=kind,
                title=str(title),
                task_id=int(task_id),
                created_at=at_value,
            )
        )
    # `count` is served explicitly and equals the items length by construction — the same list that
    # is serialized is the one that was counted.
    return NotificationsResponse(items=items, count=len(items))
