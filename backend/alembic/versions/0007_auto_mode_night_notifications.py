"""Auto Mode sessions + the night-task/notification settings — ONE 0007 for three plans

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-14

Three wave-U5 plans converged on "migration 0007" (P2 auto-mode, P3 night task, P5 owner email);
three migrations with one head would be a collision, so this ONE revision carries all of it,
exactly as `WAVE-PLAN.md` ("Collision resolution") freezes.  The names below are the frozen
interfaces the other lanes import — never renamed here:

**A. NEW table `auto_mode_sessions`** (P2).  One row per Auto Mode window; the class is
`app.models.auto_mode.AutoModeSession` and `create_all` describes the same table (the parity gate
`tests/parity/test_contract_parity.py::test_orm_tables_match_the_migration_exactly` reads this
file's `op.create_table(`).  A window, not a parent: deliberately NO task foreign key.  Money is
`NUMERIC(14, 4)` (A-T17/T18 doctrine) and stays NULL when unmeasured; `tasks_run` is NOT NULL
default 0; `started_at` carries `ix_auto_mode_sessions_started_at`.

**B. `company_config` += 8 columns** (P3 night task + P5 notifications): `night_task_enabled`
(FALSE), `night_task_limit` (3), `night_task_hour` (NULL = unset → the tick resolves 23:00),
`notification_email` (NULL = unconfigured) and the four `notify_on_*` toggles (FALSE).  Every
NOT NULL column carries a SERVER default, which is what lets SQLite `ALTER TABLE ... ADD COLUMN`
accept it AND materialises a sane value for every EXISTING row (a bare NOT NULL add would abort
on a non-empty table).  `night_task_hour`/`notification_email` are nullable on purpose: NULL is
"no value", the same contract `daily_cycle_hour` documents (`company_service.get_daily_cycle_hour`).

**Why no `batch_alter_table`** — the same measured reasons 0003 recorded: this stack (alembic
1.14 / SQLAlchemy 2.0.36 / aiosqlite) re-renders VIRTUAL generated columns empty in batch mode and
a rename leaves the stored CREATE quoted.  Nothing here needs a table rebuild: 0002:218-234 is the
shipped precedent for plain `op.add_column` on both dialects, and 0004 for a plain `op.create_table`.
SQLite's own rule is the other half of the story: a column ADDed to an existing table cannot carry
a non-constant default, and a NOT NULL column needs a default — constant server defaults above are
exactly what both dialects accept.

`downgrade()` drops exactly what `upgrade()` created — the 8 columns and (index first, then) the
new table — and touches nothing else; the round-trip is proven on a lab-DB copy in the lane receipt
(`build/ui-reverse/wave-u5/coder-p3-schema-night/`), and the chain pins move deliberately
(0006 -> 0007; recorded old -> new in the receipt).
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_NEW_TABLE_INDEX = "ix_auto_mode_sessions_started_at"


def _company_columns() -> list[tuple[str, sa.Column[sa.types.TypeEngine[object]]]]:
    """Fresh `Column` objects per call — a Column instance is consumed by the Table it joins.

    (The round-trip tests run `upgrade()` twice in one process; module-level Column objects would
    be reused across those calls.)
    """
    return [
        (
            "night_task_enabled",
            sa.Column("night_task_enabled", sa.Boolean(), server_default=sa.false(), nullable=False),
        ),
        (
            "night_task_limit",
            sa.Column("night_task_limit", sa.Integer(), server_default="3", nullable=False),
        ),
        ("night_task_hour", sa.Column("night_task_hour", sa.Integer(), nullable=True)),
        (
            "notification_email",
            sa.Column("notification_email", sa.String(length=255), nullable=True),
        ),
        (
            "notify_on_completed",
            sa.Column("notify_on_completed", sa.Boolean(), server_default=sa.false(), nullable=False),
        ),
        (
            "notify_on_failed",
            sa.Column("notify_on_failed", sa.Boolean(), server_default=sa.false(), nullable=False),
        ),
        (
            "notify_on_awaiting_approval",
            sa.Column(
                "notify_on_awaiting_approval", sa.Boolean(), server_default=sa.false(), nullable=False
            ),
        ),
        (
            "notify_on_run_requeued",
            sa.Column("notify_on_run_requeued", sa.Boolean(), server_default=sa.false(), nullable=False),
        ),
    ]


def upgrade() -> None:
    # ── A. auto_mode_sessions (P2's frozen table) ────────────────────────────
    op.create_table(
        "auto_mode_sessions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stop_reason", sa.String(length=50), nullable=True),
        sa.Column("tasks_run", sa.Integer(), server_default="0", nullable=False),
        sa.Column("spend_usd", sa.Numeric(precision=14, scale=4), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(_NEW_TABLE_INDEX, "auto_mode_sessions", ["started_at"])
    print(
        "[alembic 0007] created auto_mode_sessions (started_at/ends_at NOT NULL; stopped_at + "
        f"stop_reason nullable; tasks_run default 0; spend_usd NUMERIC(14,4) nullable) + {_NEW_TABLE_INDEX}"
    )

    # ── B. company_config += the night-task + notification settings ──────────
    for name, column in _company_columns():
        op.add_column("company_config", column)
        print(
            f"[alembic 0007] added company_config.{name} "
            "(server default materialises EXISTING rows; NULL = unset where nullable)"
        )


def downgrade() -> None:
    """Drop exactly what `upgrade()` created: the 8 columns, then the new table (index first)."""
    for name, _column in reversed(_company_columns()):
        op.drop_column("company_config", name)
        print(f"[alembic 0007] dropped company_config.{name}")

    op.drop_index(_NEW_TABLE_INDEX, table_name="auto_mode_sessions")
    op.drop_table("auto_mode_sessions")
    print("[alembic 0007] dropped auto_mode_sessions (index first, then the table)")
