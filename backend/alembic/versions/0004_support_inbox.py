"""the customer-support inbox store: `support_inbox_items` (R17)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-13

WHY (RD12 receipt §7 REQUEST 2, verbatim): "An operator/API surface to ingest items
(route into the support-inbox table of some later wave) — needs the schema decision (new
table + migration + a re-pin of `test_models_schema_frozen.py` DDL sha) that this lane
deliberately did not make."  This is that table.  RD12 shipped the consumer (a CS task's
`metadata.inbox_item` reaches the prompt, quarantined); with no producer, the 3h
`run_email_sweep` degraded honestly to title-only tasks.  From this revision on the sweep
attaches every pending item as its own deduped CS task (`celery_app/tasks/agent_tasks.py`,
`_email_inbox_sweep`) and `app/api/v1/support.py` is the owner's submit/list surface.

A NEW table — plain `op.create_table`, no batch dance.  The 0003 trap (`batch_alter_table`
re-rendering generated columns / quoting the stored CREATE) applies to ALTERING existing
tables; nothing here touches an existing table.  The DDL this revision creates is
reconciled byte-for-byte against the ORM's `create_all` output (same columns, same
inline UNIQUE, same two indexes — evidence in `build/R17-inbox/raw/`), and the frozen
pin `tests/unit/test_models_schema_frozen.py` is re-pinned in the same commit
(17 tables -> 18; DDL sha old -> new; both recorded in the lane receipt).

`downgrade()` drops exactly what `upgrade()` created (indexes first, then the table) and
touches nothing else — proven by the tmp-SQLite round-trip
(`tests/unit/test_support_inbox_migration.py`: head -> -1 -> head, sqlite_master diff is
exactly this table's rows).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "support_inbox_items",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("provider_message_id", sa.String(length=255), nullable=False),
        sa.Column("from_address", sa.String(length=255), nullable=False),
        sa.Column("subject", sa.String(length=512), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=50), server_default="pending", nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        # constraint ORDER matters for DDL evidence: the ORM's `unique=True` lands before the
        # table-level FK, so the same order here keeps the stored CREATE byte-identical to
        # ORM create_all (probe: build/R17-inbox/raw/probe_ddl_parity.txt).
        sa.UniqueConstraint("provider_message_id"),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
    )
    op.create_index("ix_support_inbox_items_status", "support_inbox_items", ["status"])
    op.create_index("ix_support_inbox_items_task_id", "support_inbox_items", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_support_inbox_items_task_id", table_name="support_inbox_items")
    op.drop_index("ix_support_inbox_items_status", table_name="support_inbox_items")
    op.drop_table("support_inbox_items")
