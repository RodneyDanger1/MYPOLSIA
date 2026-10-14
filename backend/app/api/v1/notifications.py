"""GET /api/v1/notifications — the bell's read surface (waves U2 + U4; frozen contract).

The U1 shell ships a notifications bell that has to say "this build has no notifications
endpoint" (`frontend/src/components/layout/Topbar.tsx`); this route is what lets it stop saying
that.  The contract is deliberately derived, never stored: there is NO notifications table and NO
migration — every item is a projection of an EXISTING row (a task row, or — wave U4/B7 — a
stale-claim sweep's own activity row).

THE SHAPE (frozen, both sides briefed with the same text)

    200 {"items": [...], "count": N}   — items newest first, at most 20, window = last 48 hours.
    each item: id (stable string), kind (task_completed | task_failed | awaiting_approval |
               run_requeued), title (the task's title — a run_requeued title is
               "Stalled run requeued: task #<id>", naming the recovered task id),
               task_id (int), created_at (ISO-8601).

KIND MAPPING (a status outside the three produces no notification):
    completed         -> task_completed, created_at = the completion timestamp
    failed            -> task_failed
    awaiting_approval -> awaiting_approval (the approvals channel parked it for the owner)

RUN_REQUEUED — the SECOND projection (wave U4/B7: the hung-run watchdog, visible).  Every
`activity_log` row with action='stale_claims_requeued' inside the window (the ONE batched row per
sweep that `task_service.requeue_stale_claims` has written since U3/H-02 — its
`detail.requeued_ids` carries every task the sweep recovered on the requeue branch) contributes
ONE `run_requeued` item PER entry of `detail.requeued_ids`:
    id = `activity:<activity_row_id>:<task_id>:run_requeued`, title = "Stalled run requeued:
    task #<task_id>", task_id = that id, created_at = the row's own timestamp.
  * The row is NEVER joined to `tasks`: it records the recovery EVENT — a task row that no longer
    exists must not un-record it (the item is served regardless of the task's current state; the
    frontend's `?task=N` deep link is the honest no-op for such an id).
  * `failed_ids` are deliberately NOT projected: that branch of the sweep flips the task row to
    `failed`, which the task-derived `task_failed` item already reports (a second item would
    double-report one event).
  * `detail` is read as TEXT and parsed per row, on purpose (measured): the ORM JSON column's
    result processor raises `json.JSONDecodeError` while FETCHING when one at-rest row holds
    malformed bytes — the whole feed would refuse over one bad row.  Rules (documented, never a
    silent catch): detail not parseable / not a dict / `requeued_ids` not a list -> the row
    projects nothing; a list element that is not a real int (bool/str/float/None/...) -> that
    element is dropped, the honest ids beside it still serve; duplicate ids inside one row -> one
    item (the contract is one item per task id).  Nothing is ever fabricated.

TIMESTAMP — `task_service.task_terminal_at_expr()`: `coalesce(tasks.updated_at, tasks.created_at)`,
the completion/park instant the repo's own writer stamps (`update_task_status` sets
`updated_at = utcnow()` at every transition).  The window bound is computed as a UTC instant and
adapted to the active dialect (`as_bound_instant`), so SQLite (naive UTC text) and PostgreSQL
(timestamptz) select the same rows; the served `created_at` is the column value — the same
timestamp convention every other read surface serves.  Window and ORDER BY share the one
expression, so a row is never filtered on one clock and ordered by another; the activity rows are
bound by the SAME `since` instant (one clock for both projections).

STABLE id — a pure function of the SOURCE row: `task:<task_id>:<kind>` for status-derived items,
`activity:<activity_row_id>:<task_id>:run_requeued` for run_requeued items.  Two requests inside
the window return the same identifier for the same notification (a client may key on it), and it
changes only when the row itself changes (a task changes kind).

MERGE — the two projections merge into ONE feed: sorted newest-first by the items' `created_at`
(a STABLE sort — ties keep construction order: task items in their SQL `(at DESC, id DESC)` order,
then activity items row-newest-first with ids ascending as the producer wrote them), then capped
at 20 TOTAL.  `count` is the served length.

Auth: the same `dependencies=[Depends(verify_api_key)]` every sibling v1 router carries.
"""
from __future__ import annotations

import json
from datetime import timedelta

from fastapi import APIRouter, Depends
from sqlalchemy import Text, cast, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.database import utcnow
from app.core.timeutil import as_bound_instant
from app.models.report import ActivityLog
from app.models.task import Task, TaskStatus
from app.schemas.task import NotificationItem, NotificationKind, NotificationsResponse
from app.services.query_helpers import fetch_rows
from app.services.task_service import task_terminal_at_expr

router = APIRouter(
    prefix="/notifications", tags=["notifications"], dependencies=[Depends(verify_api_key)]
)

#: The frozen contract's bounds.  Named constants (not literals in the statement) so the tests and
#: the route read the same numbers.  Both projections share them: the cap applies to the MERGED
#: feed, the window to both queries.
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

#: The activity action the second projection reads (wave U4/B7).  The producer is
#: `task_service.requeue_stale_claims` (U3/H-02): ONE batched row per sweep, whose
#: `detail.requeued_ids` carries every task the sweep recovered on the requeue branch.
STALE_CLAIMS_REQUEUED_ACTION = "stale_claims_requeued"


def _requeued_ids_from_detail(detail: object) -> list[int]:
    """The task ids a `stale_claims_requeued` row's `detail` really carries — junk dropped.

    Tolerant per row on purpose (each rule has a planted hostile case in
    `tests/unit/test_u4_notifications_activity.py`): a detail that cannot yield task ids projects
    nothing (never a crash, never a fabricated id), and one bad element must not hide the honest
    ids beside it.  Duplicates collapse — the contract is ONE item per task id.  `bool` is
    rejected explicitly (it is an `int` subclass and JSON `true` is not a task id).
    """
    if isinstance(detail, str):
        # Raw-SQL / ops writers can leave any bytes here; the ORM writer always serializes.
        try:
            detail = json.loads(detail)
        except ValueError:
            return []
    if not isinstance(detail, dict):
        return []
    raw = detail.get("requeued_ids")
    if not isinstance(raw, list):
        return []
    ids: list[int] = []
    for value in raw:
        if isinstance(value, bool) or not isinstance(value, int):
            continue
        if value not in ids:
            ids.append(value)
    return ids


@router.get("", response_model=NotificationsResponse)
async def list_notifications(db: AsyncSession = Depends(get_db)) -> NotificationsResponse:
    """The newest 20 notifications inside the last 48 hours, newest first — both projections."""
    at = task_terminal_at_expr()
    since = as_bound_instant(utcnow() - timedelta(hours=NOTIFICATIONS_WINDOW_HOURS), db)
    stmt = (
        select(Task.id, Task.title, Task.status, at.label("at"))
        .where(Task.status.in_(list(KIND_BY_STATUS)), at >= since)
        .order_by(at.desc(), Task.id.desc())
        .limit(NOTIFICATIONS_LIMIT)
    )
    # `cast(..., Text)` is deliberate and measured (docstring §RUN_REQUEUED): the JSON column's
    # result processor raises WHILE FETCHING on one malformed row; as text, one bad row projects
    # nothing and every honest item still serves.
    activity_stmt = (
        select(ActivityLog.id, cast(ActivityLog.detail, Text), ActivityLog.created_at)
        .where(
            ActivityLog.action == STALE_CLAIMS_REQUEUED_ACTION,
            ActivityLog.created_at >= since,
        )
        .order_by(ActivityLog.created_at.desc(), ActivityLog.id.desc())
        .limit(NOTIFICATIONS_LIMIT)
    )
    with refusal_guard():
        rows = await fetch_rows(db, stmt, what="notifications")
        activity_rows = await fetch_rows(db, activity_stmt, what="notifications activity")

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

    for row_id, detail, created_at in activity_rows:
        # A NULL `created_at` cannot pass the `>= since` filter (SQL three-valued logic); the
        # guard keeps the item's stamp a real instant even if a dialect ever surprises.
        if created_at is None:
            continue
        for requeued_id in _requeued_ids_from_detail(detail):
            items.append(
                NotificationItem(
                    id=f"activity:{int(row_id)}:{requeued_id}:run_requeued",
                    kind="run_requeued",
                    title=f"Stalled run requeued: task #{requeued_id}",
                    task_id=requeued_id,
                    created_at=created_at,
                )
            )

    # One feed, newest first — stable, so each source keeps its own order for equal instants.
    items.sort(key=lambda item: item.created_at, reverse=True)
    items = items[:NOTIFICATIONS_LIMIT]
    # `count` is served explicitly and equals the items length by construction — the same list
    # that is serialized is the one that was counted.
    return NotificationsResponse(items=items, count=len(items))
