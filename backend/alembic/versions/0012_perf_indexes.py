"""perf indexes for the hot read filters (V24/L6)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-19

WHY (V24/L6 brief item 3 — missing indexes).  Every index below is a filter/ORDER BY that a
shipped route or the scheduler really runs, and every one of them was measured WITHOUT a usable
index on the migrated schema (`pre/schema_probe.db`, 0011):

    SELECT * FROM chat_messages ORDER BY created_at DESC, id DESC LIMIT 50
        -> SCAN chat_messages + USE TEMP B-TREE FOR ORDER BY
           (the table had NO index beyond its primary key; `chat_service.list_history` issues
           exactly this on every `/api/v1/chat` load)
    SELECT * FROM support_inbox_items ORDER BY created_at DESC, id DESC LIMIT 20
        -> SCAN support_inbox_items + USE TEMP B-TREE FOR ORDER BY
           (`support_inbox_service.list_items`, the owner's support page)
    SELECT * FROM support_inbox_items WHERE status='pending'
        ORDER BY created_at DESC, id DESC LIMIT 20
        -> SEARCH ... USING ix_support_inbox_items_status (status=?) + USE TEMP B-TREE
           (the status-filtered variant of the same page: the single-column status index could
           not serve the ordering)
    SELECT * FROM invoices WHERE status='paid' ORDER BY id DESC LIMIT 100
        -> SCAN invoices
           (`invoice_service.list_invoices`; `invoices` had only the UNIQUE(number) index)
    SELECT detail FROM activity_log
        WHERE agent_type='<planner>' AND action='proposed_candidates'
        ORDER BY created_at DESC, id DESC LIMIT 1
        -> SEARCH ... USING ix_activity_log_agent_type + USE TEMP B-TREE FOR ORDER BY
           (`planner_service.status`/`confirm`, read on every planner page load; the existing
           single-column agent_type index narrows but cannot order)

WHAT IS ADDED (additive only; no column, constraint or row is touched):

    table                  index                                          columns
    chat_messages          ix_chat_messages_created_at                    (created_at, id)
    support_inbox_items    ix_support_inbox_items_created_at              (created_at, id)
    support_inbox_items    ix_support_inbox_items_status_created_at       (status, created_at, id)
    invoices               ix_invoices_status_id                          (status, id)
    activity_log           ix_activity_log_agent_action_created           (agent_type, action, created_at)

The `id` column is included where it is the query's tie-break, so the ORDER BY is satisfied by
the index itself (no temp b-tree).  The two existing single-column indexes on
`support_inbox_items`/`activity_log` are LEFT IN PLACE: they still serve reads this revision
does not own, and an additive migration does not get to remove them.

DELIBERATELY NOT ADDED (checked, not forgotten):
  * `tasks(status, created_at)` / `agent_runs.task_id` / `activity_log(created_at)` — the brief's
    examples — ALREADY EXIST (`ix_tasks_status_created_at` from 0001/0002,
    `ix_agent_runs_task_id` from 0002, `ix_activity_log_created_at` + `ix_activity_log_tail` from
    0001; verified in `pre/schema_probe_indexes.txt`).
  * `auto_mode_sessions` — the expired-window scan reads a table that holds one row per bounded
    Auto Mode window (6 rows in the bench DB; at most a handful in production).  An index there
    would be decoration, not speed.

ADDITIVE + REVERSIBLE:
  * upgrade: five `op.create_index` calls.  SQLite and PostgreSQL both accept the plain
    `CREATE INDEX` form (no expression, no partial predicate), so no dialect branch is needed.
  * downgrade: `op.drop_index` for exactly those five names.  No row, column or other index is
    affected; re-running `upgrade` recreates them byte-for-byte.
"""
from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: (index name, table, columns) — ONE list so upgrade and downgrade cannot drift.
PERF_INDEXES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("ix_chat_messages_created_at", "chat_messages", ("created_at", "id")),
    ("ix_support_inbox_items_created_at", "support_inbox_items", ("created_at", "id")),
    (
        "ix_support_inbox_items_status_created_at",
        "support_inbox_items",
        ("status", "created_at", "id"),
    ),
    ("ix_invoices_status_id", "invoices", ("status", "id")),
    (
        "ix_activity_log_agent_action_created",
        "activity_log",
        ("agent_type", "action", "created_at"),
    ),
)


def upgrade() -> None:
    """Create the five missing indexes — nothing else changes."""
    for name, table, columns in PERF_INDEXES:
        op.create_index(name, table, list(columns), unique=False)


def downgrade() -> None:
    """Drop exactly the five indexes `upgrade()` created; data and other indexes are untouched."""
    for name, table, _columns in reversed(PERF_INDEXES):
        op.drop_index(name, table_name=table)
