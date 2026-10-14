"""Chat + scheduled tasks + invoices, and the owner profile/readiness columns — ONE 0008 for the wave

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-14

Wave V8 ("Chat & Automation") freezes ONE revision 0008 (`WAVE-PLAN.md`, "Frozen interface — CHAT"
and "Frozen interface — SCHEDULES"; plan section 7 C2 lists the contents).  The names below are the
frozen interfaces the other lanes import — never renamed here:

**A. NEW table `chat_messages`** (C1).  The owner <-> Polsia conversation; the class is
`app.models.chat.ChatMessage` and `create_all` describes the same table.  `role` is a named CHECK
(`ck_chat_messages_role`) over ('owner','polsia'); `intent` is the parsed exchange JSON (the pending
create-task proposal rides the owner row's intent until /chat/confirm consumes it — C1's shape);
`task_id` is a nullable FK to `tasks.id` (most exchanges create nothing).

**B. NEW table `task_schedules`** (C2).  Owner-defined recurring task prompts; the class is
`app.models.schedule.TaskSchedule`.  `cadence` is the named CHECK ('once','daily','weekdays',
'weekly'), `hour` the company-local hour (0-23), `weekday` 0-6 Monday=0 (NULL for cadences that do
not use it).  `enabled` is NOT NULL server default FALSE — pause is `enabled=false` and the cadence
tick SKIPS a disabled row.  `next_run_at` is recomputed from the company-tz wall clock each run
(never +24h — DST-safe); children are created JUST-IN-TIME by the tick, never materialized ahead.

**C. NEW table `invoices`** (V9 consumes; V8 lands the schema only — drift ledger V8-D2 is why
`main.py` does not import an invoices router).  The class is `app.models.invoice.Invoice`.  `number`
is UNIQUE (`INV-YYYY-NNN`, assigned by V9's service under a transaction; the unique constraint is
the backstop).  Money is `NUMERIC(12, 2)` and stays NULL when unmeasured (A-T17/T18); `line_items`
is JSON v0 — itemized rows as JSON, no child table.  `status` is the named CHECK ('draft','sent',
'paid','void','overdue').

**D. `company_config` += 13 columns** (V8's owner profile + sender identity + readiness):
`cycle_report_email` (FALSE), six NOT NULL text fields with server default ''
(`sender_legal_name`, `sender_postal_address`, `owner_skills`, `owner_goal`, `risk_tolerance`,
`market_locale`), `owner_hours_per_week` / `owner_budget_usd` (NULLABLE — NULL is "no value"),
`channels_ready` (JSON, server default '[]'), `confirmed_idea` (JSON, NULLABLE), `checklist_state`
(JSON, server default '{}') and `dispatch_paused` (NOT NULL, server default FALSE — C5's pause
feature rides this column).  Every NOT NULL column carries a SERVER default, which is what lets
SQLite ALTER TABLE ... ADD COLUMN accept it AND materialises a value for every EXISTING row (a bare
NOT NULL add would abort on a non-empty table — the 0007 doctrine).

**Why no `batch_alter_table`** — the same measured reasons 0003/0007 recorded: this stack re-renders
VIRTUAL generated columns empty in batch mode and a rename leaves the stored CREATE quoted.  Nothing
here needs a table rebuild: 0002:218-234 is the shipped precedent for plain `op.add_column` on both
dialects, and 0004 for a plain `op.create_table`; SQLite's own rule is the other half — a column
ADDed to an existing table cannot carry a non-constant default, and a NOT NULL column needs a
default (the constant defaults above are exactly what both dialects accept).  No generated columns
anywhere (the batch-mode trap the plan §7 C2 names) — keep it that way.

`downgrade()` drops exactly what `upgrade()` created — the 13 columns in reverse, then the three
tables (reverse creation order) — and touches nothing else.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _company_columns() -> list[tuple[str, sa.Column[sa.types.TypeEngine[object]]]]:
    """Fresh `Column` objects per call — a Column instance is consumed by the Table it joins.

    (The round-trip tests run `upgrade()` twice in one process; module-level Column objects would
    be reused across those calls — the 0007 reason.)
    """
    return [
        (
            "cycle_report_email",
            sa.Column(
                "cycle_report_email", sa.Boolean(), server_default=sa.false(), nullable=False
            ),
        ),
        (
            "sender_legal_name",
            sa.Column("sender_legal_name", sa.Text(), server_default="", nullable=False),
        ),
        (
            "sender_postal_address",
            sa.Column("sender_postal_address", sa.Text(), server_default="", nullable=False),
        ),
        ("owner_skills", sa.Column("owner_skills", sa.Text(), server_default="", nullable=False)),
        (
            "owner_hours_per_week",
            sa.Column("owner_hours_per_week", sa.Integer(), nullable=True),
        ),
        (
            "owner_budget_usd",
            sa.Column("owner_budget_usd", sa.Numeric(precision=10, scale=2), nullable=True),
        ),
        ("owner_goal", sa.Column("owner_goal", sa.Text(), server_default="", nullable=False)),
        (
            "risk_tolerance",
            sa.Column("risk_tolerance", sa.Text(), server_default="", nullable=False),
        ),
        (
            "channels_ready",
            sa.Column("channels_ready", sa.JSON(), server_default="[]", nullable=False),
        ),
        ("market_locale", sa.Column("market_locale", sa.Text(), server_default="", nullable=False)),
        ("confirmed_idea", sa.Column("confirmed_idea", sa.JSON(), nullable=True)),
        (
            "checklist_state",
            sa.Column("checklist_state", sa.JSON(), server_default="{}", nullable=False),
        ),
        (
            "dispatch_paused",
            sa.Column("dispatch_paused", sa.Boolean(), server_default=sa.false(), nullable=False),
        ),
    ]


def upgrade() -> None:
    # ── A. chat_messages (C1's frozen table) ────────────────────────────────
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "role",
            sa.Enum(
                "owner",
                "polsia",
                name="ck_chat_messages_role",
                native_enum=False,
                create_constraint=True,
                length=50,
            ),
            nullable=False,
        ),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("intent", sa.JSON(), nullable=True),
        sa.Column("task_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    print(
        "[alembic 0008] created chat_messages (role CHECK owner|polsia; intent JSON NULL; "
        "task_id FK tasks NULL; created_at NOT NULL default now())"
    )

    # ── B. task_schedules (C2's frozen table) ───────────────────────────────
    op.create_table(
        "task_schedules",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "cadence",
            sa.Enum(
                "once",
                "daily",
                "weekdays",
                "weekly",
                name="ck_task_schedules_cadence",
                native_enum=False,
                create_constraint=True,
                length=50,
            ),
            nullable=False,
        ),
        sa.Column("hour", sa.Integer(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    print(
        "[alembic 0008] created task_schedules (cadence CHECK once|daily|weekdays|weekly; hour "
        "0-23 NOT NULL; enabled FALSE; next_run_at/last_run_at NULL; children JUST-IN-TIME)"
    )

    # ── C. invoices (V8 schema; V9's service/routes consume) ────────────────
    op.create_table(
        "invoices",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("number", sa.Text(), nullable=False),
        sa.Column("bill_to", sa.JSON(), nullable=True),
        sa.Column("issue_date", sa.Date(), nullable=False),
        sa.Column("due_date", sa.Date(), nullable=False),
        sa.Column("net_days", sa.Integer(), server_default="30", nullable=False),
        sa.Column("currency", sa.Text(), server_default="USD", nullable=False),
        sa.Column("subtotal", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("tax", sa.Numeric(precision=12, scale=2), server_default="0", nullable=True),
        sa.Column("total", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column(
            "status",
            sa.Enum(
                "draft",
                "sent",
                "paid",
                "void",
                "overdue",
                name="ck_invoices_status",
                native_enum=False,
                create_constraint=True,
                length=50,
            ),
            nullable=False,
        ),
        sa.Column("payment_link", sa.Text(), nullable=True),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("late_fee_note", sa.Text(), nullable=True),
        sa.Column("line_items", sa.JSON(), nullable=True),
        sa.UniqueConstraint("number"),
        sa.PrimaryKeyConstraint("id"),
    )
    print(
        "[alembic 0008] created invoices (number UNIQUE; money NUMERIC(12,2) NULL-able; status "
        "CHECK draft|sent|paid|void|overdue; line_items JSON v0)"
    )

    # ── D. company_config += the owner profile / sender / readiness columns ─
    for name, column in _company_columns():
        op.add_column("company_config", column)
        print(
            f"[alembic 0008] added company_config.{name} "
            "(server default materialises EXISTING rows; NULL = unset where nullable)"
        )


def downgrade() -> None:
    """Drop exactly what `upgrade()` created: the 13 columns (reverse), then the three tables."""
    for name, _column in reversed(_company_columns()):
        op.drop_column("company_config", name)
        print(f"[alembic 0008] dropped company_config.{name}")

    op.drop_table("invoices")
    print("[alembic 0008] dropped invoices")
    op.drop_table("task_schedules")
    print("[alembic 0008] dropped task_schedules")
    op.drop_table("chat_messages")
    print("[alembic 0008] dropped chat_messages")
