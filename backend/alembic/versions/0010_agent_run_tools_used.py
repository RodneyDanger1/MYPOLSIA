"""agent_runs.tools_used — persist the runtime's tool evidence (V22/L1)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-19

WHY (the V21 finding, closed): the DeepSeek runtime writes `envelope["tools_used"]` for a
tool-enabled run (`app/agents/deepseek_runtime.py:1225`) — names only, bounded there — but the
persistence boundary dropped it: `accounting.RunUsage` had no field, `usage_from_envelope` never
read the key, `base_agent.RUN_META_KEYS` omitted it and `agent_runs` had no column.  So the run
record could not answer "which tools did this run call?", and the detail page honestly said
"not recorded".  This revision adds the column the writer (`agent_tasks._apply_run_usage`) now
fills from that envelope chain.

THE THREE STATES THE COLUMN MUST KEEP (the reason it is NULLABLE with NO default):

  * NULL  — NEVER RECORDED: a legacy row written before this revision, a mock-mode run, or a run
            with no tools at all (the envelope has no key).  Never backfilled: there is no source
            of truth for a past run's tool list, and inventing `[]` would be a fabricated fact.
  * `[]`  — RECORDED AND NONE: tools were available and the model called none (a measured zero,
            exactly the `tokens_used = 0` convention).
  * names — the recorded tool names, bounded by `accounting.bounded_tools_used` (50 entries; an
            overflowing list ends with an honest truncation marker).

ADDITIVE + REVERSIBLE:
  * upgrade: one `op.add_column` of a nullable JSON column.  SQLite accepts
    `ALTER TABLE agent_runs ADD COLUMN tools_used JSON` with no table rebuild (a nullable column
    needs no default); PostgreSQL gets JSONB via the same `with_variant` the ORM uses
    (`app/models/types.py:json_type`), so `create_all` and the migration describe the same type.
  * downgrade: drops exactly that one column and touches nothing else.  SQLite ≥ 3.35 and
    PostgreSQL both support `ALTER TABLE ... DROP COLUMN`; the rows themselves are untouched, so
    downgrading loses only the tool evidence written since the upgrade (documented, not silent).
    No data backfill is attempted in either direction.
"""
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the nullable tool-evidence column — nothing else."""
    op.add_column(
        "agent_runs",
        sa.Column(
            "tools_used",
            # the same type choice as app/models/types.py, so SQLite tests and PostgreSQL agree
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Remove exactly the column `upgrade()` added; no row is deleted and nothing else changes."""
    op.drop_column("agent_runs", "tools_used")
