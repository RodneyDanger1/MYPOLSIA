from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, field_validator

from app.schemas.common import TimestampedORM, UtcDatetime

logger = logging.getLogger(__name__)

#: The `tasks.metadata` keys the morning cycle persists for one planned task
#: (`celery_app/tasks/daily_cycle.py:619-628` writes the first five flat on the bag; the second
#: pass at :673-682 adds `depends_on_ids`, and :700-705 records `depends_on_cycle` for a stranded
#: node it released as a root).  `TaskResponse.plan` mirrors exactly these keys — nothing is
#: renamed, and a bag holding NONE of them is not a plan (`plan` is null, not an empty edge).
PLAN_METADATA_KEYS: tuple[str, ...] = (
    "plan_day",
    "depends_on_titles",
    "depends_on_ids",
    "depends_on_cycle",
    "depends_on_unresolved",
    "side_effect",
    "acceptance",
    "estimate_usd",
    "estimate_seconds",
)

#: The `tasks.metadata` key a parked run's required fingerprints are recorded under
#: (`celery_app/tasks/agent_tasks.py:553` `APPROVAL_REQUIRED_KEY`, written at :1094).
APPROVAL_REQUIRED_KEY = "approval_required"


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=512)
    agent_type: str
    description: str | None = None
    priority: int = 3
    source: str = "dashboard"
    scheduled_date: datetime | None = None


class TaskResponse(TimestampedORM):
    """`priority`/`status` are NULLABLE in the frozen schema (`models/task.py`; `0001`'s
    `CREATE TABLE tasks` carries `server_default`s but no `NOT NULL`, and on PostgreSQL
    `CHECK(NULL)` passes — `ck_tasks_status` cannot police a NULL).  Declaring them required turned
    ONE schema-legal NULL row (a raw-SQL/ops writer is an accepted path, the same premise R26/R31
    accepted for ads) into a bare, codeless 500 for the WHOLE list (L1-11).  `None` means "not
    recorded" and is served as null — never a fabricated `3`/"pending"; the tasks page renders the
    dash / gray badge for it.
    """

    id: int
    title: str
    description: str | None = None
    agent_type: str
    priority: int | None = None
    status: str | None = None
    source: str | None = None
    scheduled_date: UtcDatetime | None = None
    result_summary: str | None = None
    error_message: str | None = None

    # ── wave U2 truth fields (ADDITIVE, all nullable, default None) ───────────────────────────
    # The tasks page needed two facts the API could not answer: when a task finished, and what it
    # cost.  Both are measured from rows that already exist (`agent_runs` carries the ledger; the
    # schema has no `completed_at` column), so nothing here changes the DDL or the wire shape of
    # the existing fields — an old client deserializing these models sees exactly its old keys plus
    # four nullables.
    #
    #   * `completed_at` — set ONLY when `status == "completed"`, then it is the completion
    #     timestamp `task_service.task_completed_at` derives (`updated_at`, the column the repo's own
    #     completion writer stamps — `update_task_status`).  A failed task did not complete: its
    #     `completed_at` stays null rather than wearing a synonym for `updated_at`.
    #   * `cost_usd` — the SUM of this task's `agent_runs.cost_usd` (NUMERIC(14,4) at rest; served as
    #     a float like every other `*_usd` response field in this repo).  NULL when the task has no
    #     runs — unmeasured is never zero.  A measured zero (runs exist, cost recorded as 0) sums to
    #     0.0 and IS served.
    #   * `tokens_in` / `tokens_out` — the SUMs of `agent_runs.tokens_input` / `tokens_output`, same
    #     NULL discipline (no runs, or no value ever recorded, => null).
    #
    # Filled at EVERY construction site (all four live in `app/api/v1/tasks.py` — recon:
    # `grep -rn "TaskResponse" backend/app`): a field defined here but left unpopulated at a site
    # with runs behind it is the defined-but-never-called defect class this wave exists to kill.
    completed_at: UtcDatetime | None = None
    cost_usd: float | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None

    # ── S7-R1 / A5-02: the plan provenance + the parked-approval flag (ADDITIVE, computed) ────
    # The bag is read by its ORM ATTRIBUTE name and EXCLUDED from the wire.  It must NOT be
    # declared as `metadata`: on a SQLAlchemy declarative instance `task.metadata` IS the class's
    # `MetaData` object, pydantic's `from_attributes` lookup finds it, and the response raises
    # ValidationError on EVERY row (measured by the A5 seat; `models/task.py:93-99` explains why
    # the attribute cannot carry the column's name).  Nor is the whole bag safe to serve:
    # `metadata.approval_token` is the operator's CREDENTIAL (`task_service.py:347`,
    # `agent_tasks.approval_token_from_metadata`).  So three computed fields expose exactly the
    # shapes a client renders, and nothing else — the raw dict and the token stay off the wire.
    metadata_json: dict[str, Any] | None = Field(default=None, exclude=True, repr=False)

    @field_validator("metadata_json", mode="before")
    @classmethod
    def _the_bag_must_be_an_object(cls, value: Any) -> Any:
        """`tasks.metadata` is a JSON column with no CHECK — a raw writer can put ANY JSON there.

        A list/string/number is not a bag: it carries no plan and no parked approval, so it is
        read as `None` (the plan is null and nothing is pending — both TRUE of that row) instead
        of raising ValidationError and 500-ing the WHOLE task list for one schema-legal row, the
        L1-11 defect class this repo has already paid for once.  The drop is named in the log,
        never silent.
        """
        if value is None or isinstance(value, dict):
            return value
        logger.warning(
            "code=task_metadata_not_an_object: tasks.metadata holds %s for this row; serving no "
            "plan and no pending approval (the bag is not an object)", type(value).__name__,
        )
        return None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def plan(self) -> dict[str, Any] | None:
        """The plan edge the orchestrator persisted on this row, or null for a task without one.

        Mirrors the persisted keys (never renames them): `depends_on` is the RESOLVED DAG edge
        (`depends_on_ids` -> ints the dependency release reads), `depends_on_cycle` says the plan
        was not a DAG and this node was released as a root with its edges dropped, and the rest
        are the TaskSpec fields the model stated.  A key the model never stated is served as
        null — its absence is a fact, and a fabricated `0`/`[]` would state something else.
        """
        bag = self.metadata_json or {}
        if not any(key in bag for key in PLAN_METADATA_KEYS):
            return None
        ids = bag.get("depends_on_ids")
        cycle = bag.get("depends_on_cycle")
        return {
            "depends_on": [
                dep for dep in ids
                if isinstance(dep, int) and not isinstance(dep, bool)
            ] if isinstance(ids, list) else [],
            "depends_on_cycle": isinstance(cycle, list) and bool(cycle),
            "depends_on_titles": bag.get("depends_on_titles"),
            "depends_on_unresolved": bag.get("depends_on_unresolved"),
            "plan_day": bag.get("plan_day"),
            "side_effect": bag.get("side_effect"),
            "acceptance": bag.get("acceptance"),
            "estimate_usd": bag.get("estimate_usd"),
            "estimate_seconds": bag.get("estimate_seconds"),
        }

    @computed_field  # type: ignore[prop-decorator]
    @property
    def approval_required(self) -> bool:
        """True when the row carries a PENDING approval (a non-empty parked fingerprint list).

        `False` is a measured answer here, not an unknown one: a row whose run never parked
        anything recorded no `approval_required` key at all, and an EMPTY list is a park the
        approvals channel already cleared.  The flag is derived from the same list
        `approval_fingerprints` serves, so the two can never disagree.
        """
        return bool(self.approval_fingerprints)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def approval_fingerprints(self) -> list[str]:
        """The fingerprints an operator mints a token for (`POST /api/v1/approvals`), in order.

        Narrowed to the ONE value the Approve control needs: an entry that is not an object
        carrying a non-empty string fingerprint is dropped (never coerced into a token the
        server would then reject), duplicates collapse the way `approval_requirements` dedupes
        them, and the rest of the parked record — the action, ref, reason — is not served here.
        """
        raw = (self.metadata_json or {}).get(APPROVAL_REQUIRED_KEY)
        if not isinstance(raw, list):
            return []
        found: list[str] = []
        for item in raw:
            value = item.get("fingerprint") if isinstance(item, dict) else None
            if isinstance(value, str) and value.strip():
                found.append(value.strip())
        return list(dict.fromkeys(found))


class TaskAttachmentResponse(TimestampedORM):
    """One image attached to a task (V25/L1).

    Metadata only — the bytes are served by `GET /api/v1/attachments/{id}`.  `stored_name` is
    deliberately absent: it is the server's storage locator and no client needs it.  `mime` is
    the SNIFFED type recorded at upload, `sha256` the digest of the stored bytes, and
    `created_at` the database's upload timestamp (the inherited `TimestampedORM` field).
    """

    id: int
    task_id: int
    filename: str
    mime: str
    size_bytes: int
    sha256: str


class TaskStatusUpdate(BaseModel):
    """The task status-change request body — now WIRED to `PATCH /api/v1/tasks/{task_id}/status`.

    fix-5 (2026-10-13) labelled this model zero-consumer: `update_task_status` was called by the
    worker and agent paths, never over HTTP (the L1-09/L2-02 fix asserts the value set at the
    SERVICE).  D20 ruled the model is the contract: the route now takes it as its typed body, so
    the write surface is documented and validated at the boundary instead of being an unwritten
    promise.  The vocabulary is NOT re-declared here — the service's `VALID_TASK_STATUSES`
    (`task_service.py`) stays the single authority, and a bad value is refused by name with the
    accepted set in the context.

    `result_summary`/`error_message` are assigned only when supplied (the service's rule), so a
    body that omits them cannot wipe a previously recorded message.
    """

    status: str
    result_summary: str | None = None
    error_message: str | None = None


class AgentStatusResponse(BaseModel):
    agent_type: str
    last_run_at: UtcDatetime | None = None
    last_run_status: str | None = None
    tasks_today: int = 0
    tasks_total: int = 0


class AgentTriggerResponse(BaseModel):
    status: str = "queued"
    task_id: int
    message: str


class AgentTriggerRequest(BaseModel):
    task_title: str | None = None
    description: str | None = None
    priority: int = 3


#: The frozen notification vocabulary (wave U2; extended wave U4/B7).  One Literal, one place: the
#: route maps task statuses onto the three task-derived strings and projects `run_requeued` from
#: the stale-claim sweep's own activity rows; pydantic refuses anything else at response-build
#: time — a new kind is a deliberate edit here, never a silent extra value on the wire.
#: U4/B7 old->new: Literal["task_completed", "task_failed", "awaiting_approval"] ->
#: Literal["task_completed", "task_failed", "awaiting_approval", "run_requeued"].
NotificationKind = Literal["task_completed", "task_failed", "awaiting_approval", "run_requeued"]


class NotificationItem(BaseModel):
    """One notification, DERIVED from an existing row — there is no notifications table.

    `id` is stable for as long as the SOURCE row is, and is a pure function of it:
    `task:<task_id>:<kind>` for status-derived items; `activity:<activity_row_id>:<task_id>:run_requeued`
    for the wave-U4 hung-run items (one per id in a `stale_claims_requeued` row's
    `detail.requeued_ids`) — two requests inside the window return the same identifier (the bell
    must be able to key its items).  `created_at` is the source row's own timestamp: a task's
    completion/park instant (see `services.task_service.task_terminal_at_expr`) or the sweep
    activity row's timestamp, serialized as ISO-8601.  A `run_requeued` title names the recovered
    task id ("Stalled run requeued: task #<id>").
    """

    id: str
    kind: NotificationKind
    title: str
    task_id: int
    created_at: UtcDatetime


class NotificationsResponse(BaseModel):
    """`count` is the items length — served explicitly so a client never has to trust its own
    parse of the array to know how many the server produced."""

    items: list[NotificationItem]
    count: int
