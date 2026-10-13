"""the `rejected` task status + the ad-campaign identity index (D2/T03, D18/T19)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-13

Two at-rest identity jobs in one revision, both driven by an owner ruling:

**A. `tasks.status` gains `rejected` (D2).**  The live product's task states are
Queued / In Progress / Completed / **Rejected** / Failed, where Rejected means "refused
before execution — nothing charged".  Ours had the other four; a budget/policy refusal at
the dispatch choke point was recorded as `failed`, which reads as a crash instead of a
refusal.  The value set lives in `app/models/task.py` (`TaskStatus` -> SQLAlchemy
`Enum(native_enum=False, create_constraint=True, name="ck_tasks_status")`, so a fresh
`create_all` schema carries the CHECK on every backend), and `task_service.VALID_TASK_STATUSES`
is derived from the same enum — this migration is the at-rest half:

* PostgreSQL: `DROP CONSTRAINT IF EXISTS ck_tasks_status` then re-add the SAME name with the
  SAME list ORDER and `'rejected'` appended.  Added VALIDATED (not NOT VALID): the new set is
  a strict superset of the old one, so validation over existing rows provably succeeds, and
  0005's doctrine is that a CHECK should end up validated.  A row that somehow violates it
  aborts the migration naming the constraint — the honest outcome (0005's own rule).
* SQLite: nothing to do — 0002:329-333 states the doctrine this revision follows: "SQLite
  cannot ADD CONSTRAINT to an existing table; the value sets are enforced on PostgreSQL and
  asserted by the code paths that write them".  Batch mode's 12-step rebuild is specifically
  not used here: `agent_runs.task_id` and `support_inbox_items.task_id` reference `tasks`, and
  the Alembic manual warns "batch table operations **do not work** with foreign keys that
  enforce referential integrity ... the target table is dropped".  Fresh SQLite databases get
  the CHECK from `create_all` (measured: a bogus value now raises `IntegrityError ... CHECK
  constraint failed`); migrated ones keep the writer-side assertion that has been shipped
  since L1-09/L2-02.

`downgrade()` re-adds the OLD six-value CHECK on PostgreSQL as **NOT VALID** (0002's own idiom,
0002:325-328): rows with `status='rejected'` may exist, and a VALIDATED re-add would refuse to
install itself — blocking the downgrade instead of preserving it.  NOT VALID still enforces the
old set for every subsequent write, so the downgraded schema is not weaker than 0005's.

**B. `ad_campaigns` gets its identity index (D18/T19).**  `ads_service.record_ads_run` dedupes
campaigns on the provider's own id — `(platform, external_id)` — but nothing at rest enforced
it (its own docstring: "`ad_campaigns` carries NO unique index ... so this SELECT-first read IS
the guard — there is no constraint to catch"), so two overlapping writers (Celery
`--concurrency=4`; a beat run and a manual trigger) both miss the SELECT and both insert, and
every later run then reads an AMBIGUOUS campaign for a metric.  The fix is the same DB-owned
invariant 0005 shipped for the run-artifact keys: a PARTIAL UNIQUE index

  `uq_ad_campaigns_platform_external_id` UNIQUE(platform, external_id) WHERE external_id IS NOT NULL

SQLite (partial-index doc: "A partial index definition may include the UNIQUE keyword ... requires
every entry in the index to be unique") and PostgreSQL ("When the WHERE clause is present, a
partial index is created") both honour it, and in both dialects NULLs are distinct, so legacy rows
without a provider id stay legal.  Rows with NO provider id are deliberately NOT covered: their
identity is `(platform, name)` — a string a human edits — and a unique index on a display name
would refuse a legitimate rename.  The `(platform, name)` fallback therefore stays a code-level
dedupe, exactly as it shipped.

PRE-CLEAN FIRST (0005's idiom — keep `MIN(id)` per key).  This is a DESTRUCTIVE step and it is
documented as such: for one (platform, external_id) the OLDEST campaign row survives and every
later exact duplicate is dropped.  The rows removed are counted and printed for the operator's
log.  The SQL (portable, no window functions):

    DELETE FROM ad_campaigns
     WHERE platform IS NOT NULL AND external_id IS NOT NULL
       AND id NOT IN (
             SELECT MIN(id) FROM ad_campaigns
              WHERE platform IS NOT NULL AND external_id IS NOT NULL
              GROUP BY platform, external_id)

The duplicate pre-clean is NOT reversible by `downgrade()`; dropping the index restores the 0005
schema shape.
"""
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The SHIPPED `ck_tasks_status` value set, order preserved from 0002:90's literal, with `rejected`
#: appended.  Duplicated here deliberately: a migration is a frozen snapshot of the schema at its
#: point in history and must not import `app.models` (the ORM moves; the DDL this revision ran once
#: does not).  `app/models/task.py::TaskStatus` is the same list by construction — the pin in
#: tests/unit/test_t03_rejected_status.py reads THIS literal and compares.
_OLD_STATUS_VALUES: tuple[str, ...] = (
    "pending", "in_progress", "completed", "failed", "cancelled", "awaiting_approval",
)
_NEW_STATUS_VALUES: tuple[str, ...] = _OLD_STATUS_VALUES + ("rejected",)

_AD_CAMPAIGN_INDEX = "uq_ad_campaigns_platform_external_id"
_AD_CAMPAIGN_PREDICATE = "external_id IS NOT NULL"


def _dialect_name() -> str:
    """Active dialect name — works online and in `--sql` (offline) mode (0002/0005 idiom)."""
    try:
        return op.get_bind().dialect.name
    except Exception:  # noqa: BLE001 - offline mode exposes only the context dialect
        return op.get_context().dialect.name


def _status_check(values: tuple[str, ...]) -> str:
    return "status IN (" + ",".join(f"'{value}'" for value in values) + ")"


def _pre_clean_ad_campaigns(bind) -> int:
    """Drop exact duplicates on (platform, external_id), keeping the oldest row (`MIN(id)`).

    Portable SQL in 0005's exact shape (`NOT IN (SELECT MIN(id) ... GROUP BY ...)`); rows with a
    NULL key component are exempt, because the index is partial for exactly those.  Returns the
    number of rows removed (measured by before/after count, not the driver's rowcount).
    """
    before = bind.exec_driver_sql("SELECT COUNT(*) FROM ad_campaigns").scalar()
    bind.exec_driver_sql(
        "DELETE FROM ad_campaigns "
        "WHERE platform IS NOT NULL AND external_id IS NOT NULL AND id NOT IN "
        "(SELECT MIN(id) FROM ad_campaigns "
        " WHERE platform IS NOT NULL AND external_id IS NOT NULL "
        " GROUP BY platform, external_id)"
    )
    after = bind.exec_driver_sql("SELECT COUNT(*) FROM ad_campaigns").scalar()
    return int(before or 0) - int(after or 0)


def upgrade() -> None:
    # ── A. ck_tasks_status: add `rejected` ──────────────────────────────────
    if _dialect_name() == "postgresql":
        op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_tasks_status")
        op.execute(f"ALTER TABLE tasks ADD CONSTRAINT ck_tasks_status CHECK ({_status_check(_NEW_STATUS_VALUES)})")
        print(
            "[alembic 0006] ck_tasks_status re-created with "
            f"{len(_NEW_STATUS_VALUES)} values (added 'rejected'), validated"
        )
    else:
        print(
            f"[alembic 0006] SKIP ({_dialect_name()}): ck_tasks_status — SQLite cannot ADD "
            "CONSTRAINT to an existing table (0002:329-333); the value set is asserted by the "
            "writers (task_service.VALID_TASK_STATUSES) and carried by create_all on fresh "
            "databases."
        )

    # ── B. ad_campaigns identity index (D18/T19) ─────────────────────────────
    bind = op.get_bind()
    removed = _pre_clean_ad_campaigns(bind)
    print(
        f"[alembic 0006] pre-clean ad_campaigns: {removed} duplicate row(s) removed "
        "(kept MIN(id) per platform+external_id; the same provider campaign recorded twice)"
    )
    op.create_index(
        _AD_CAMPAIGN_INDEX,
        "ad_campaigns",
        ["platform", "external_id"],
        unique=True,
        postgresql_where=sa.text(_AD_CAMPAIGN_PREDICATE),
        sqlite_where=sa.text(_AD_CAMPAIGN_PREDICATE),
    )
    print(
        f"[alembic 0006] created {_AD_CAMPAIGN_INDEX} "
        f"(UNIQUE platform, external_id WHERE {_AD_CAMPAIGN_PREDICATE})"
    )


def downgrade() -> None:
    """Drop exactly what `upgrade()` created; the duplicate pre-clean is not reversible."""
    op.drop_index(_AD_CAMPAIGN_INDEX, table_name="ad_campaigns")
    print(f"[alembic 0006] dropped {_AD_CAMPAIGN_INDEX}")

    if _dialect_name() == "postgresql":
        # NOT VALID on the way back down: rows with status='rejected' may exist, and a validated
        # re-add would fail against them — blocking the downgrade instead of preserving it.
        # Enforcement for every subsequent write still applies (PostgreSQL: the constraint "will
        # still be enforced against subsequent inserts or updates", 0002:325-328).
        op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS ck_tasks_status")
        op.execute(
            f"ALTER TABLE tasks ADD CONSTRAINT ck_tasks_status "
            f"CHECK ({_status_check(_OLD_STATUS_VALUES)}) NOT VALID"
        )
        print("[alembic 0006] ck_tasks_status restored to the 0005 value set (NOT VALID)")
    else:
        print(
            f"[alembic 0006] SKIP ({_dialect_name()}): ck_tasks_status downgrade — nothing was "
            "re-created on SQLite."
        )
