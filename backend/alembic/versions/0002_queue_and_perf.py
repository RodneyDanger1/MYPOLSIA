"""queue discipline + the measured index / money / integrity plan

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-11

Three groups of change, in one revision:

**A. Queue discipline (matches `app/models/task.py` exactly — names are the contract).**
`tasks.claimed_at / claimed_by / heartbeat_at / attempts / dedupe_key` with indexes
`ix_tasks_pending_queue`, `ix_tasks_created_at`, unique `ix_tasks_dedupe_key`;
`agent_runs.cli_session_id / tokens_input / tokens_output / usage_json` with indexes
`ix_agent_runs_task_id`, `ix_agent_runs_agent_type`, `ix_agent_runs_started_at`.
The upload had 17 tables, 6 UNIQUE constraints and **no index on `tasks` at all**
(lane 03 §18), so duplicate tasks were structurally permitted while Celery is
at-least-once.

**B. lane 06's measured plan** (`06-data-perf-business/DATA-PERF.md` §1.2/§3/§4):
the unindexed FK/filter columns; money `Float` → `Numeric(14,4)` with the exact
`USING round(x::numeric, 4)` clauses (float money is non-associative — measured
drift 4E-13 on a month of spend and a budget guard that trips the wrong way at
33.33 × 30); generated columns for the derived ad metrics in the **portable**
`CAST(x AS REAL)` form (the `::numeric` spelling compiles for both dialects and runs
only on PostgreSQL — measured `OperationalError: unrecognized token: ":"` on SQLite);
the idempotency uniques; FK `ON DELETE` behaviour; CHECK constraints; an append-only
trigger for `activity_log`; and a complete `downgrade()`.

**C. SQLite portability.** `alembic upgrade head` must also run against SQLite (the
unit-test dialect).  Every PG-only statement is dialect-guarded and prints
`[alembic 0002] SKIP (sqlite): <reason>` instead of failing silently or crashing.
0001 itself cannot render on SQLite (its `postgresql.JSONB()`/`ARRAY()` columns raise
`CompileError`), and 0001 is not this revision's file to edit, so the two SQLite
renderers below are registered at import time — alembic loads every revision module
before running any `upgrade()`, so they are in effect while 0001 runs.  PostgreSQL DDL
is byte-for-byte unaffected (same `with_variant` choice `app/models/types.py` makes).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.compiler import compiles

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# ── SQLite renderers for 0001's PostgreSQL-only column types ─────────────────
# Needed so the chain reaches this revision on SQLite.  PG output is unchanged.
@compiles(postgresql.JSONB, "sqlite")
def _sqlite_jsonb(element, compiler, **kw):  # noqa: ANN001, ARG001
    return "JSON"


@compiles(postgresql.ARRAY, "sqlite")
def _sqlite_array(element, compiler, **kw):  # noqa: ANN001, ARG001
    return "JSON"


# ── derived ad-metric expressions (data-perf §3.4, PORTABLE form) ────────────
# `round(<expr>, 6)` needs the PORTABLE form below (2026-10-13, live PostgreSQL finding): SQLite
# accepts an inner `CAST(x AS REAL)` division, but PostgreSQL has no round(double precision, int) —
# only round(numeric, int) — so the ROUND INPUT is cast to NUMERIC while the division stays REAL
# (an inner NUMERIC cast would INTEGER-divide on SQLite). The earlier REAL-only form upgraded every
# SQLite DB and died on the first real PostgreSQL one.
_CTR_EXPR = "CASE WHEN impressions > 0 THEN round(CAST(CAST(clicks AS REAL) / impressions AS NUMERIC), 6) ELSE 0 END"
_CPC_EXPR = "CASE WHEN clicks > 0 THEN round(CAST(CAST(spend_usd AS REAL) / clicks AS NUMERIC), 6) ELSE 0 END"
# roas needs a revenue number: `ad_metrics` had none, so 0001's roas column could only
# ever hold a value an agent invented.  `revenue_usd` (numeric, same unit as spend_usd)
# is added here and `roas` is derived from it — existing rows get 0, which is the honest
# value for a number that was never measured.
_ROAS_EXPR = (
    "CASE WHEN spend_usd > 0 THEN round(CAST(CAST(revenue_usd AS REAL) / CAST(spend_usd AS REAL) AS NUMERIC), 6) "
    "ELSE 0 END"
)
_RUNNABLE_AT_EXPR = "COALESCE(scheduled_date, created_at)"

_MONEY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("ad_campaigns", "daily_budget_usd"),
    ("ad_campaigns", "total_spent_usd"),
    ("ad_metrics", "spend_usd"),
    ("agent_runs", "cost_usd"),
)

# lane 06 §1.2 + §4.4: value sets are taken from the code and the UI, not from taste.
# `email_logs` additionally allows 'draft': SPEC-AGENTS §5.3 makes a sandboxed email a
# labelled draft row, and a CHECK that forbids the documented value would break the
# sandbox the moment SANDBOX_MODE=true records one.
CHECK_CONSTRAINTS: tuple[tuple[str, str, str], ...] = (
    ("ck_tasks_status", "tasks",
     "status IN ('pending','in_progress','completed','failed','cancelled','awaiting_approval')"),
    ("ck_agent_runs_status", "agent_runs",
     "status IN ('running','completed','failed','cancelled')"),
    ("ck_agent_runs_run_type", "agent_runs",
     "run_type IN ('task','cycle','manual')"),
    ("ck_activity_log_level", "activity_log",
     "level IN ('info','success','warning','error')"),
    ("ck_social_posts_status", "social_posts",
     "status IN ('draft','scheduled','published','failed')"),
    ("ck_prospects_status", "prospects",
     "status IN ('new','contacted','replied','converted','unsubscribed','bounced')"),
    ("ck_email_logs_status", "email_logs",
     "status IN ('draft','queued','sent','delivered','opened','replied','bounced','failed')"),
    ("ck_ad_campaigns_status", "ad_campaigns",
     "status IN ('active','paused','ended','draft')"),
    ("ck_stripe_events_status", "stripe_events",
     "status IN ('received','processed','failed','ignored')"),
    ("ck_expense_records_currency", "expense_records", "currency = lower(currency)"),
    ("ck_stripe_events_currency", "stripe_events", "currency = lower(currency)"),
    ("ck_expense_records_amount", "expense_records", "amount_cents > 0"),
    ("ck_ad_metrics_nonneg", "ad_metrics",
     "impressions >= 0 AND clicks >= 0 AND conversions >= 0 AND spend_usd >= 0 AND revenue_usd >= 0"),
    ("ck_ad_metrics_clicks_le_impressions", "ad_metrics", "clicks <= impressions"),
    ("ck_revenue_snapshots_nonneg", "revenue_snapshots",
     "mrr_cents >= 0 AND arr_cents >= 0 AND active_subscribers >= 0 AND stripe_balance_cents >= 0"),
)

# (index name, table, columns, kwargs) — frozen model indexes first, then lane 06's plan.
_INDEXES: tuple[tuple[str, str, tuple[str, ...], dict], ...] = (
    # ── frozen model: app/models/task.py must match these names exactly ──────
    ("ix_tasks_pending_queue", "tasks", ("status", "priority", "scheduled_date"), {}),
    ("ix_tasks_created_at", "tasks", ("created_at",), {}),
    ("ix_tasks_dedupe_key", "tasks", ("dedupe_key",), {"unique": True}),
    ("ix_agent_runs_task_id", "agent_runs", ("task_id",), {}),
    ("ix_agent_runs_agent_type", "agent_runs", ("agent_type",), {}),
    ("ix_agent_runs_started_at", "agent_runs", ("started_at",), {}),
    # ── lane 06 §1.2: the query set the API/cycle actually issue ─────────────
    ("ix_tasks_created_at_status", "tasks", ("created_at", "status"), {}),
    ("ix_tasks_status_created_at", "tasks", ("status", "created_at"),
     {"postgresql_ops": {"created_at": "DESC"}}),
    ("ix_tasks_pending_runnable", "tasks", ("priority", "runnable_at"),
     {"postgresql_where": sa.text("status = 'pending'"),
      "sqlite_where": sa.text("status = 'pending'")}),
    ("ix_activity_log_tail", "activity_log", ("created_at", "id"),
     {"postgresql_ops": {"created_at": "DESC", "id": "DESC"}}),
    ("ix_email_logs_follow_up_due", "email_logs", ("follow_up_due",),
     {"postgresql_where": sa.text("follow_up_due IS NOT NULL"),
      "sqlite_where": sa.text("follow_up_due IS NOT NULL")}),
    ("ix_email_logs_prospect_sent_at", "email_logs", ("prospect_id", "sent_at"),
     {"postgresql_ops": {"sent_at": "DESC"}}),
    ("ix_expense_records_date", "expense_records", ("date",), {}),
    ("ix_expense_records_category_date", "expense_records", ("category", "date"),
     {"postgresql_ops": {"date": "DESC"}}),
    ("ix_memory_entries_category_created_at", "memory_entries", ("category", "created_at"),
     {"postgresql_ops": {"created_at": "DESC"}}),
    ("ix_social_posts_created_at", "social_posts", ("created_at",),
     {"postgresql_ops": {"created_at": "DESC"}}),
    ("ix_social_posts_status_created_at", "social_posts", ("status", "created_at"),
     {"postgresql_ops": {"created_at": "DESC"}}),
    ("ix_prospects_created_at", "prospects", ("created_at",),
     {"postgresql_ops": {"created_at": "DESC"}}),
    ("ix_prospects_status_created_at", "prospects", ("status", "created_at"),
     {"postgresql_ops": {"created_at": "DESC"}}),
    ("ix_ad_metrics_date_campaign", "ad_metrics", ("date", "campaign_id"), {}),
    ("ix_agent_runs_agent_type_started_at", "agent_runs", ("agent_type", "started_at", "id"),
     {"postgresql_ops": {"started_at": "DESC", "id": "DESC"}}),
    # ── lane 06 §3.6: the two idempotency uniques that were missing ──────────
    ("uq_ad_metrics_campaign_date", "ad_metrics", ("campaign_id", "date"),
     {"unique": True}),
    ("ux_agent_runs_task_attempt", "agent_runs", ("task_id", "attempt"),
     {"unique": True, "postgresql_where": sa.text("task_id IS NOT NULL"),
      "sqlite_where": sa.text("task_id IS NOT NULL")}),
)


def _dialect_name() -> str:
    """Active dialect name — works online and in `--sql` (offline) mode."""
    try:
        return op.get_bind().dialect.name
    except Exception:  # noqa: BLE001 - offline mode exposes only the context dialect
        return op.get_context().dialect.name


def _is_pg() -> bool:
    return _dialect_name() == "postgresql"


def _skip(reason: str) -> None:
    """Report a statement SQLite cannot run.  Never silent, never a crash."""
    print(f"[alembic 0002] SKIP ({_dialect_name()}): {reason}")


def _money_preflight(bind) -> None:
    """Refuse the money ALTERs on values `numeric(14, 4)` cannot hold (M7).  PostgreSQL-only.

    The four ALTERs below convert Float columns with `USING round(x::numeric, 4)`, and
    `numeric(14, 4)` holds |v| < 10^10 (10 integer digits): the manual's edges are "if the
    number of digits to the left ... exceeds the declared precision, an error is raised"
    and "an infinity can only be stored in an unconstrained numeric column".  A legacy row
    with either aborts the ALTER mid-upgrade, leaving the operator to pick apart a failed
    migration — so the check runs FIRST, counts offenders per column, and raises naming
    table.column plus sample ids (`round(x::numeric, 4)` is the ALTER's own expression, so a
    value that merely ROUNDS out of range is caught too; the `x <> x` term also names float
    NaN explicitly rather than relying on numeric's NaN ordering).
    """
    offenders: list[str] = []
    for table, column in _MONEY_COLUMNS:
        rows = bind.exec_driver_sql(
            f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL AND ("
            f"abs(round({column}::numeric, 4)) >= 10000000000 OR {column} <> {column}) "
            f"ORDER BY id LIMIT 5"
        ).fetchall()
        if rows:
            sample = ", ".join(f"id={row[0]} value={row[1]!r}" for row in rows)
            offenders.append(f"{table}.{column}: [{sample}]")
    if offenders:
        raise RuntimeError(
            "alembic 0002: money pre-flight refuses this upgrade — numeric(14, 4) cannot "
            "store these values (overflow / Infinity / NaN). Correct the rows, then re-run: "
            + "; ".join(offenders)
        )


def upgrade() -> None:
    # ── 1. queue + accounting columns ───────────────────────────────────────
    op.add_column("tasks", sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("claimed_by", sa.String(120), nullable=True))
    op.add_column("tasks", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("tasks", sa.Column("attempts", sa.Integer(), server_default="0", nullable=True))
    op.add_column("tasks", sa.Column("dedupe_key", sa.String(255), nullable=True))

    op.add_column("agent_runs", sa.Column("cli_session_id", sa.String(120), nullable=True))
    op.add_column("agent_runs", sa.Column("tokens_input", sa.Integer(), nullable=True))
    op.add_column("agent_runs", sa.Column("tokens_output", sa.Integer(), nullable=True))
    # same type choice as app/models/types.py, so SQLite tests and PostgreSQL agree
    op.add_column(
        "agent_runs",
        sa.Column("usage_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
                  nullable=True),
    )
    # lane 06 §3.6: "a retried task must not create indistinguishable runs"
    op.add_column("agent_runs", sa.Column("attempt", sa.Integer(), nullable=True))

    # the revenue number `roas` never had (see _ROAS_EXPR)
    op.add_column(
        "ad_metrics",
        sa.Column("revenue_usd", sa.Numeric(14, 4), server_default="0", nullable=False),
    )

    # ── 2. money: Float → Numeric(14,4), exact USING clauses (data-perf §3.3) ─
    if _is_pg():
        # M7: name the rows numeric(14, 4) cannot hold BEFORE the ALTERs touch the columns.
        _money_preflight(op.get_bind())
        for table, column in _MONEY_COLUMNS:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} "
                f"TYPE numeric(14, 4) USING round({column}::numeric, 4)"
            )
    else:
        _skip(
            "money Float->Numeric(14,4): SQLite has no ALTER COLUMN TYPE and numbers are "
            "dynamically typed, so the conversion is a no-op there; the ORM keeps Float so "
            "the unit-test schema matches. PostgreSQL gets the exact USING clause."
        )

    # ── 3. generated columns ────────────────────────────────────────────────
    # tasks.runnable_at: lane 06 measured that the claim predicate with an OR can never
    # use a partial index (38.7 ms → 0.66 ms once the predicate is one sargable
    # comparison against this column).  PG stores it STORED; SQLite takes the documented
    # ADD COLUMN path, which only accepts VIRTUAL — same expression, same value.
    op.add_column(
        "tasks",
        sa.Column("runnable_at", sa.DateTime(timezone=True),
                  sa.Computed(_RUNNABLE_AT_EXPR, persisted=_is_pg())),
    )

    # ad_metrics.ctr/cpc/roas were plain Float columns any caller could overwrite — a
    # derived number that can disagree with its inputs.  PostgreSQL cannot turn an
    # existing column into a generated one, so drop and re-add: the values are recomputed
    # from (impressions, clicks, spend_usd, revenue_usd) for existing rows, and the three
    # columns become unwritable by construction (measured: "cannot INSERT into generated
    # column").
    for column in ("ctr", "cpc", "roas"):
        op.drop_column("ad_metrics", column)
    for column, expression in (("ctr", _CTR_EXPR), ("cpc", _CPC_EXPR), ("roas", _ROAS_EXPR)):
        op.add_column(
            "ad_metrics",
            sa.Column(column, sa.Numeric(14, 6), sa.Computed(expression, persisted=_is_pg())),
        )

    # ── 4. idempotency uniques ──────────────────────────────────────────────
    # A retried ads sync in the upload silently double-counted spend and nothing could
    # even detect it (no unique key on (campaign_id, date)).  Resolve exact duplicates
    # first — 0001 permitted them — keeping the lowest id; portable SQL, no `USING`.
    op.execute(
        "DELETE FROM ad_metrics WHERE id NOT IN "
        "(SELECT MIN(id) FROM ad_metrics GROUP BY campaign_id, date)"
    )

    # ── 5. indexes ──────────────────────────────────────────────────────────
    for name, table, columns, kwargs in _INDEXES:
        op.create_index(name, table, list(columns), **kwargs)

    # ── 6. FK ON DELETE behaviour (lane 06 §3.7) ────────────────────────────
    if _is_pg():
        op.execute(
            "ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_task_id_fkey, "
            "ADD CONSTRAINT agent_runs_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id) "
            "ON DELETE SET NULL"
        )
        op.execute(
            "ALTER TABLE email_logs DROP CONSTRAINT IF EXISTS email_logs_prospect_id_fkey, "
            "ADD CONSTRAINT email_logs_prospect_id_fkey FOREIGN KEY (prospect_id) "
            "REFERENCES prospects(id) ON DELETE RESTRICT"
        )
        op.execute(
            "ALTER TABLE email_logs DROP CONSTRAINT IF EXISTS email_logs_campaign_id_fkey, "
            "ADD CONSTRAINT email_logs_campaign_id_fkey FOREIGN KEY (campaign_id) "
            "REFERENCES email_campaigns(id) ON DELETE SET NULL"
        )
        op.execute(
            "ALTER TABLE ad_metrics DROP CONSTRAINT IF EXISTS ad_metrics_campaign_id_fkey, "
            "ADD CONSTRAINT ad_metrics_campaign_id_fkey FOREIGN KEY (campaign_id) "
            "REFERENCES ad_campaigns(id) ON DELETE CASCADE"
        )
    else:
        _skip(
            "FK ON DELETE (SET NULL / RESTRICT / CASCADE): SQLite cannot alter a foreign key "
            "of an existing table (12-step rebuild required) and does not enforce FKs unless "
            "PRAGMA foreign_keys=ON; the behaviour is a production (PostgreSQL) concern."
        )

    # ── 7. CHECK constraints ────────────────────────────────────────────────
    if _is_pg():
        for name, table, expression in CHECK_CONSTRAINTS:
            # NOT VALID: no table scan, enforcement still applies to new writes
            # (PostgreSQL docs: the constraint "will still be enforced against subsequent
            # inserts or updates").  VALIDATE happens in a later, quiet migration.
            op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expression}) NOT VALID")
    else:
        _skip(
            "CHECK constraints: SQLite cannot ADD CONSTRAINT to an existing table; the value "
            "sets are enforced on PostgreSQL and asserted by the code paths that write them."
        )

    # ── 8. activity_log becomes append-only (PostgreSQL) ────────────────────
    # The run ledger is the audit trail; an UPDATE could silently rewrite history.
    # DELETE stays allowed: the retention task is the one sanctioned deleter, and it runs
    # on a schedule with a bounded batch.
    if _is_pg():
        op.execute(
            """
            CREATE OR REPLACE FUNCTION polsia_activity_log_append_only() RETURNS trigger
            LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION
                  'activity_log is append-only: % is refused (polsia migration 0002)', TG_OP;
            END;
            $$
            """
        )
        op.execute("DROP TRIGGER IF EXISTS trg_activity_log_no_update ON activity_log")
        op.execute(
            "CREATE TRIGGER trg_activity_log_no_update BEFORE UPDATE ON activity_log "
            "FOR EACH ROW EXECUTE FUNCTION polsia_activity_log_append_only()"
        )
        op.execute("DROP TRIGGER IF EXISTS trg_activity_log_no_truncate ON activity_log")
        op.execute(
            "CREATE TRIGGER trg_activity_log_no_truncate BEFORE TRUNCATE ON activity_log "
            "FOR EACH STATEMENT EXECUTE FUNCTION polsia_activity_log_append_only()"
        )
    else:
        _skip(
            "activity_log append-only trigger: plpgsql trigger function is PostgreSQL-only "
            "(SQLite would need a different CREATE TRIGGER body)."
        )


def downgrade() -> None:
    """Reverse everything `upgrade()` did, in reverse order."""
    # ── 8. trigger ──────────────────────────────────────────────────────────
    if _is_pg():
        op.execute("DROP TRIGGER IF EXISTS trg_activity_log_no_truncate ON activity_log")
        op.execute("DROP TRIGGER IF EXISTS trg_activity_log_no_update ON activity_log")
        op.execute("DROP FUNCTION IF EXISTS polsia_activity_log_append_only()")
    else:
        _skip("activity_log trigger downgrade (never created on SQLite)")

    # ── 7. CHECK constraints ────────────────────────────────────────────────
    if _is_pg():
        for name, table, _expression in CHECK_CONSTRAINTS:
            op.execute(f"ALTER TABLE {table} DROP CONSTRAINT IF EXISTS {name}")
    else:
        _skip("CHECK constraint downgrade (never created on SQLite)")

    # ── 6. FK ON DELETE back to the 0001 default (NO ACTION) ────────────────
    if _is_pg():
        op.execute(
            "ALTER TABLE agent_runs DROP CONSTRAINT IF EXISTS agent_runs_task_id_fkey, "
            "ADD CONSTRAINT agent_runs_task_id_fkey FOREIGN KEY (task_id) REFERENCES tasks(id)"
        )
        op.execute(
            "ALTER TABLE email_logs DROP CONSTRAINT IF EXISTS email_logs_prospect_id_fkey, "
            "ADD CONSTRAINT email_logs_prospect_id_fkey FOREIGN KEY (prospect_id) "
            "REFERENCES prospects(id)"
        )
        op.execute(
            "ALTER TABLE email_logs DROP CONSTRAINT IF EXISTS email_logs_campaign_id_fkey, "
            "ADD CONSTRAINT email_logs_campaign_id_fkey FOREIGN KEY (campaign_id) "
            "REFERENCES email_campaigns(id)"
        )
        op.execute(
            "ALTER TABLE ad_metrics DROP CONSTRAINT IF EXISTS ad_metrics_campaign_id_fkey, "
            "ADD CONSTRAINT ad_metrics_campaign_id_fkey FOREIGN KEY (campaign_id) "
            "REFERENCES ad_campaigns(id)"
        )
    else:
        _skip("FK ON DELETE downgrade (never altered on SQLite)")

    # ── 5. indexes ──────────────────────────────────────────────────────────
    for name, table, _columns, _kwargs in reversed(_INDEXES):
        op.drop_index(name, table_name=table)

    # ── 4. idempotency uniques: dropped with the indexes above ──────────────
    # (uq_ad_metrics_campaign_date and ux_agent_runs_task_attempt are entries in
    # _INDEXES; the duplicate-resolution DELETE is not reversed — it removed rows that
    # the unique key required to be unique.)

    # ── 3. generated columns ────────────────────────────────────────────────
    for column, _expression in (("ctr", _CTR_EXPR), ("cpc", _CPC_EXPR), ("roas", _ROAS_EXPR)):
        op.drop_column("ad_metrics", column)
    for column, default in (("ctr", "0"), ("cpc", "0"), ("roas", "0")):
        # 0001's shape: a plain Float that any caller can write.  The values are gone
        # (they were derived); the column exists again for parity with 0001's schema.
        op.add_column(
            "ad_metrics",
            sa.Column(column, sa.Float(), server_default=default, nullable=True),
        )
    op.drop_column("tasks", "runnable_at")

    # ── 2. money back to Float (double precision) ───────────────────────────
    if _is_pg():
        for table, column in _MONEY_COLUMNS:
            op.execute(
                f"ALTER TABLE {table} ALTER COLUMN {column} "
                f"TYPE double precision USING {column}::double precision"
            )
    else:
        _skip("money downgrade: no ALTER COLUMN TYPE on SQLite (nothing was converted)")

    # ── 1. columns ──────────────────────────────────────────────────────────
    op.drop_column("ad_metrics", "revenue_usd")
    op.drop_column("agent_runs", "attempt")
    op.drop_column("agent_runs", "usage_json")
    op.drop_column("agent_runs", "tokens_output")
    op.drop_column("agent_runs", "tokens_input")
    op.drop_column("agent_runs", "cli_session_id")
    op.drop_column("tasks", "dedupe_key")
    op.drop_column("tasks", "attempts")
    op.drop_column("tasks", "heartbeat_at")
    op.drop_column("tasks", "claimed_by")
    op.drop_column("tasks", "claimed_at")
