"""vision_descriptions — vision memory: one stored description per (attachment bytes, model)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-19

WHY (V27 PLAN, L4): V26 made `POST /api/v1/vision/describe` work on demand, but every press paid
the model again and nothing was kept — the operator could not see what the model had said about
an image a minute ago, and re-describing the same screenshot was a second identical bill.  This
revision adds the ROW that remembers the measurement.  It is the only migration in V27.

COLUMNS:

    id             INTEGER PK
    attachment_id  INTEGER NOT NULL, FK -> task_attachments.id ON DELETE CASCADE
    task_id        INTEGER NOT NULL, FK -> tasks.id           ON DELETE CASCADE
    sha256         VARCHAR(64) NOT NULL   -- the attachment BYTES digest (the cache key half)
    description    TEXT        NOT NULL   -- the model's sentence, exactly as served
    model          VARCHAR(120) NULL      -- the model id the provider actually served
    tokens         INTEGER      NULL      -- measured prompt+completion total; NULL = unmeasured
    created_at     DATETIME/TIMESTAMP WITH TIME ZONE, NOT NULL, server default now()

WHY `sha256` IS COPIED FROM THE ATTACHMENT ROW: the digest is already measured at upload time
(`task_attachments.sha256`, the bytes that were sniffed and stored).  Re-hashing inside the
describe path would read up to 5 MiB a second time to learn a fact the row already carries, so
the cache key is the recorded digest.  It is NOT globally unique: the history of one task may
hold several rows for one image (a different model, a re-measure after a provider change), and
the brief explicitly keeps that history.  `ix_vision_descriptions_sha256` is the lookup index;
`ix_vision_descriptions_task_id` serves the newest-first history read.  Both are non-unique.

WHY TWO CASCADES: a description is a property of the attachment AND of the task.  Deleting the
task must remove its images' descriptions (no orphan rows pointing at a vanished task), and
deleting one attachment must remove only that attachment's descriptions (the task's other
images keep theirs).  SQLite enforces this only with `PRAGMA foreign_keys=ON` (the round-trip
test enables it and proves both legs at the engine level); PostgreSQL enforces it always.

WHY `tokens` IS NULLABLE WITH NO DEFAULT: NULL is "the provider did not report usage", a
measured `0` is a real measurement of zero tokens.  The service stores None, never a
fabricated 0 (the repo's NULL-vs-zero rule, the same one 0014/0015 applied to money).

ADDITIVE + REVERSIBLE:
  * upgrade: one `op.create_table` + two `op.create_index`.  No existing table, column, row or
    constraint is touched.
  * downgrade: drop the two indexes, then the table.  Only descriptions are lost — the
    attachments and tasks they described are untouched.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "vision_descriptions"
SHA_INDEX = "ix_vision_descriptions_sha256"
TASK_INDEX = "ix_vision_descriptions_task_id"


def upgrade() -> None:
    """Create the memory table and its two lookup indexes — nothing else."""
    # Literal, not the TABLE constant: the parity pin
    # (tests/parity/test_contract_parity.py::test_orm_tables_match_the_migration_exactly)
    # parses `op.create_table("<name>"` from every revision; a constant would hide this table
    # from the drift check. The constant still names the indexes/drop below.
    op.create_table("vision_descriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("attachment_id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("tokens", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["attachment_id"], ["task_attachments.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["task_id"], ["tasks.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(SHA_INDEX, TABLE, ["sha256"], unique=False)
    op.create_index(TASK_INDEX, TABLE, ["task_id"], unique=False)
    print(
        f"[alembic 0016] created {TABLE} (attachment_id/task_id FKs CASCADE; sha256/description "
        f"NOT NULL; model/tokens nullable; {SHA_INDEX}, {TASK_INDEX})"
    )


def downgrade() -> None:
    """Drop exactly the indexes and table `upgrade()` created; no row elsewhere is touched."""
    op.drop_index(TASK_INDEX, table_name=TABLE)
    op.drop_index(SHA_INDEX, table_name=TABLE)
    op.drop_table(TABLE)
