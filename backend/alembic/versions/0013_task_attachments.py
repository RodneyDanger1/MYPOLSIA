"""task_attachments — image attachments on a task (V25/L1: vision input)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-19

WHY (V25 PLAN item 1): Polsia reads screenshots/charts; "beat this landing page" is table stakes.
Before this revision there was nowhere to put an image: `tasks` has no attachment channel, the
`data` volume has no attachment directory, and the DeepSeek request builder only ever sent a
plain-string user message.  This revision adds the ROW; the blob lives on the data volume under
`<ATTACHMENTS_DIR>/task-<task_id>/<uuid4>.<ext>` (see `app/services/task_attachment_service.py`
for the caps, the magic-byte sniffing and the traversal-proof path rule).

COLUMNS (all NOT NULL except `created_at`, which defaults to the database clock):

    id            INTEGER PK
    task_id       INTEGER NOT NULL, FK -> tasks.id ON DELETE CASCADE
    filename      VARCHAR(255) NOT NULL   -- the operator's file name, DISPLAY ONLY
    stored_name   VARCHAR(128) NOT NULL   -- server-generated `<uuid4>.<ext>`, the storage locator
    mime          VARCHAR(50)  NOT NULL   -- the SNIFFED type (image/png|jpeg|webp), never the claim
    size_bytes    INTEGER      NOT NULL   -- bytes written, measured after the sniff
    sha256        VARCHAR(64)  NOT NULL   -- digest of the stored bytes at upload time
    created_at    DATETIME/TIMESTAMP WITH TIME ZONE, server default now()

WHY `stored_name` EXISTS IN ADDITION TO `filename`: the brief's `<uuid>.<ext>` storage rule needs
a recorded locator.  Deriving the path from `filename` (operator-controlled) is exactly the
path-traversal class this table must not have, and deriving it from `sha256` would re-hash every
GET.  The service re-validates the name as one generated component and re-resolves it inside the
task directory, so the column is a locator, not an authority.

INDEX: `ix_task_attachments_task_id` — both readers (`list_attachments`, and the aggregate that
enforces the 4-per-task / 8-MiB-per-task caps) filter on `task_id`; without it every upload scan
would read the whole table.

CASCADE: an attachment is a property of its task.  A deleted task must not leave an orphaned row
(or a blob with nothing pointing at it).  SQLite only enforces this with `PRAGMA foreign_keys=ON`
(the migration round-trip test enables it; PostgreSQL enforces it always).

ADDITIVE + REVERSIBLE:
  * upgrade: one `op.create_table` + one `op.create_index`.  No existing table, column, row or
    constraint is touched.
  * downgrade: `op.drop_index` then `op.drop_table`.  The BLOBS are not deleted by a schema
    downgrade (a migration does not get to destroy operator data); an operator who downgrades
    then deletes `task_attachments` leaves files under the attachments dir, which the README's
    data-volume note covers.  Re-running `upgrade` recreates the table byte-for-byte; the rows
    are not restored, and nothing pretends they were.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "task_attachments"
INDEX = "ix_task_attachments_task_id"


def upgrade() -> None:
    """Create the attachments table and its task_id index — nothing else."""
    op.create_table(
        "task_attachments",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("stored_name", sa.String(length=128), nullable=False),
        sa.Column("mime", sa.String(length=50), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(INDEX, TABLE, ["task_id"], unique=False)
    print(
        f"[alembic 0013] created {TABLE} (task_id FK CASCADE; filename/stored_name/mime/"
        f"size_bytes/sha256 NOT NULL; {INDEX})"
    )


def downgrade() -> None:
    """Drop exactly the index and table `upgrade()` created; no row elsewhere is touched."""
    op.drop_index(INDEX, table_name=TABLE)
    op.drop_table(TABLE)
