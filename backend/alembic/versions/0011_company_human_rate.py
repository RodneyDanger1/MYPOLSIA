"""company_config.human_hourly_rate — the owner's human hourly rate (V23/L3)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-19

WHY (COMPLETENESS §7 item 11): `GET /api/v1/finance/planning` hard-coded
`human_rate = None` (finance.py, `build_planning_view`), so `margin_fraction` could never be
computed and the margin template served `—` forever with the reason "no human hourly rate is
configured anywhere".  There was no column to configure either.  This revision adds the storage;
`PUT /config` accepts it (`ge=0`, `null` clears) and the planning view reads it.

THE THREE STATES THE COLUMN KEEPS (the reason it is NULLABLE with NO default):

  * NULL  — NO RATE WAS EVER SET.  The planning view serves `human_rate: null` plus its reason
            (updated to say "set it on Finance"), and margin stays null.  Never backfilled: there
            is no source of truth for a past rate, and inventing one would fabricate a margin.
  * 0     — A MEASURED ZERO-HOURLY RATE is representable (the write bound is `ge=0`); it is a
            reading, not an absence.  (The margin math then values human hours at zero — the
            owner's explicit statement — never a hidden default.)
  * > 0   — the stored rate the owner entered on the Finance page.

ADDITIVE + REVERSIBLE:
  * upgrade: one `op.add_column` of a nullable `sa.Float()` (DOUBLE PRECISION on PostgreSQL,
    FLOAT on SQLite) with no default.  SQLite accepts `ALTER TABLE company_config ADD COLUMN
    human_hourly_rate FLOAT` with no table rebuild; existing rows become NULL — the honest
    "no rate" state, not a fabricated 0.
  * downgrade: drops exactly that one column and touches nothing else.  SQLite >= 3.35 and
    PostgreSQL both support `ALTER TABLE ... DROP COLUMN`; rows survive.  The rate values written
    since the upgrade are lost with the column (documented, not silent); there is nowhere else
    they could live.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable human-rate column — nothing else."""
    op.add_column("company_config", sa.Column("human_hourly_rate", sa.Float(), nullable=True))


def downgrade() -> None:
    """Remove exactly the column `upgrade()` added; no row is deleted and nothing else changes."""
    op.drop_column("company_config", "human_hourly_rate")
