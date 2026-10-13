"""money exact at rest on SQLite too: the four USD columns become NUMERIC(14, 4)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-12

A-T17 closes the hole 0002 opened knowingly: 0002 converted the four money columns
(`ad_campaigns.daily_budget_usd`, `ad_campaigns.total_spent_usd`, `ad_metrics.spend_usd`,
`agent_runs.cost_usd`) to `NUMERIC(14, 4)` **PostgreSQL-only** and printed why it skipped
SQLite ("SQLite has no ALTER COLUMN TYPE ...; the ORM keeps Float so the unit-test schema
matches").  Two consequences survived that trade:

* the ORM still declared `Float`, so every SQLite test exercised a binary-float column --
  and a float column cannot round-trip a cent (`1.005` is `1.0049999999999999` binary-side);
* money crossed to the column through `task_service._as_column_float`, a lossy `float(...)`
  conversion, with a second, ungreppable site in `celery_app.tasks.agent_tasks`
  (`float(cost)`) writing around it.

**The rebuild is explicit, not `batch_alter_table` -- measured.**  A first draft used
alembic batch mode; on this stack (alembic 1.14 / SQLAlchemy 2.0.36 / aiosqlite) it failed
for two measured reasons:

1. `ad_metrics` carries three VIRTUAL generated columns (`ctr`, `cpc`, `roas`).  SQLAlchemy
   2.0.36 reflects their expressions as EMPTY, and the recreate renders the invalid
   `GENERATED ALWAYS AS () VIRTUAL` -> `sqlite3.OperationalError: near ")": syntax error`
   (traceback + statement echo: `build/T17-money/raw/alembic_upgrade_0003_attempt2_full.txt`).
2. every recreation path that ends in `ALTER TABLE ... RENAME` leaves the stored CREATE
   re-rendered by SQLite with a QUOTED name (`CREATE TABLE "ad_campaigns"` -- measured in
   the sync probe and in this revision's first draft), i.e. cosmetic drift against a chain
   whose DDL text is itself evidence (`raw/probe_batch_single_result.txt`).

So the rebuild never renames.  Per table, six statements: create `_t17_<t>` from the
measured text, copy the non-generated columns in, drop the old table, re-create `<t>` from
the measured text under its real (unquoted) name, copy back, drop the temp -- then recreate
the measured indexes.  The DDL texts are byte-for-byte the 0002-head `sqlite_master` texts
(dump archived at `raw/alembic_before_0003.sqlite_master_r2.txt`); only the four money type
tokens differ.  Row counts are checked before/after; a mismatch raises.
Values are preserved as written -- SQLite is dynamically typed; the declared NUMERIC(14, 4)
affinity plus SQLAlchemy's Decimal result processor make reads exact at scale 4 (measured:
`Decimal('1.005')` in => `Decimal('1.0050')` out, `SUM` is a `Decimal`; lane probe).

Per-column decisions, recorded not bulk-converted:

* all four money columns -- `NUMERIC(14, 4)`, the type 0002 already chose on PostgreSQL.
  The plan's sketch suggested `(12, 2)`; 0002's measured choice wins (the file beats the
  sketch): scale 4 carries sub-cent CLI cost estimates that `(12, 2)` cannot, and
  re-stating `(12, 2)` here would need a second PostgreSQL conversion -- duplicating
  0002's intent, which the plan forbids.
* `ad_metrics.ctr / cpc / roas` -- deliberately NOT converted: they are ratios, not money
  (the plan's own trap names this), and 0002 already re-derived them as generated columns.

PostgreSQL is a no-op here: 0002's `USING round(x::numeric, 4)` conversion is the recorded
decision and this revision must not duplicate it -- the guard prints the skip instead.
`downgrade()` reverses the rebuild with the same explicit dance (NUMERIC(14, 4) -> FLOAT);
the round-trip is proven in the lane receipt by re-dumping `sqlite_master`
before / after / after-downgrade.
"""
from typing import Sequence, Union

from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ── the measured DDL texts (0002-head sqlite_master dump; only the money types differ) ──────
#: upgrade: temp tables (`_t17_<t>`) carry the new NUMERIC(14, 4) types
_UPGRADE_TEMP_DDL: dict[str, str] = {
    'ad_campaigns': "CREATE TABLE _t17_ad_campaigns (\n\tid INTEGER NOT NULL, \n\tplatform VARCHAR(50) NOT NULL, \n\texternal_id VARCHAR(255), \n\tname VARCHAR(512) NOT NULL, \n\tgoal VARCHAR(255), \n\tstatus VARCHAR(50) DEFAULT 'active', \n\tdaily_budget_usd NUMERIC(14, 4) DEFAULT '0', \n\ttotal_spent_usd NUMERIC(14, 4) DEFAULT '0', \n\tcreated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), \n\tPRIMARY KEY (id)\n)",
    'ad_metrics': "CREATE TABLE _t17_ad_metrics (\n\tid INTEGER NOT NULL, \n\tcampaign_id INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\timpressions INTEGER DEFAULT '0', \n\tclicks INTEGER DEFAULT '0', \n\tconversions INTEGER DEFAULT '0', \n\tspend_usd NUMERIC(14, 4) DEFAULT '0', \n\trevenue_usd NUMERIC(14, 4) DEFAULT '0' NOT NULL, ctr NUMERIC(14, 6) GENERATED ALWAYS AS (CASE WHEN impressions > 0 THEN round(CAST(clicks AS REAL) / impressions, 6) ELSE 0 END) VIRTUAL, cpc NUMERIC(14, 6) GENERATED ALWAYS AS (CASE WHEN clicks > 0 THEN round(CAST(spend_usd AS REAL) / clicks, 6) ELSE 0 END) VIRTUAL, roas NUMERIC(14, 6) GENERATED ALWAYS AS (CASE WHEN spend_usd > 0 THEN round(CAST(revenue_usd AS REAL) / CAST(spend_usd AS REAL), 6) ELSE 0 END) VIRTUAL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(campaign_id) REFERENCES ad_campaigns (id)\n)",
    'agent_runs': "CREATE TABLE _t17_agent_runs (\n\tid INTEGER NOT NULL, \n\ttask_id INTEGER, \n\tagent_type VARCHAR(100) NOT NULL, \n\trun_type VARCHAR(100) DEFAULT 'task', \n\tstatus VARCHAR(50) DEFAULT 'running', \n\tinput_context JSON, \n\toutput JSON, \n\traw_log TEXT, \n\ttokens_used INTEGER, \n\tcost_usd NUMERIC(14, 4), \n\tduration_secs FLOAT, \n\tstarted_at DATETIME DEFAULT (CURRENT_TIMESTAMP), \n\tended_at DATETIME, cli_session_id VARCHAR(120), tokens_input INTEGER, tokens_output INTEGER, usage_json JSON, attempt INTEGER, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(task_id) REFERENCES tasks (id)\n)",
}

#: upgrade: the final re-creations use the same texts under the real (unquoted) names, so the
#: stored CREATE stays byte-faithful (no rename ever touches the final table)
_UPGRADE_FINAL_DDL: dict[str, str] = {
    'ad_campaigns': "CREATE TABLE ad_campaigns (\n\tid INTEGER NOT NULL, \n\tplatform VARCHAR(50) NOT NULL, \n\texternal_id VARCHAR(255), \n\tname VARCHAR(512) NOT NULL, \n\tgoal VARCHAR(255), \n\tstatus VARCHAR(50) DEFAULT 'active', \n\tdaily_budget_usd NUMERIC(14, 4) DEFAULT '0', \n\ttotal_spent_usd NUMERIC(14, 4) DEFAULT '0', \n\tcreated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), \n\tPRIMARY KEY (id)\n)",
    'ad_metrics': "CREATE TABLE ad_metrics (\n\tid INTEGER NOT NULL, \n\tcampaign_id INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\timpressions INTEGER DEFAULT '0', \n\tclicks INTEGER DEFAULT '0', \n\tconversions INTEGER DEFAULT '0', \n\tspend_usd NUMERIC(14, 4) DEFAULT '0', \n\trevenue_usd NUMERIC(14, 4) DEFAULT '0' NOT NULL, ctr NUMERIC(14, 6) GENERATED ALWAYS AS (CASE WHEN impressions > 0 THEN round(CAST(clicks AS REAL) / impressions, 6) ELSE 0 END) VIRTUAL, cpc NUMERIC(14, 6) GENERATED ALWAYS AS (CASE WHEN clicks > 0 THEN round(CAST(spend_usd AS REAL) / clicks, 6) ELSE 0 END) VIRTUAL, roas NUMERIC(14, 6) GENERATED ALWAYS AS (CASE WHEN spend_usd > 0 THEN round(CAST(revenue_usd AS REAL) / CAST(spend_usd AS REAL), 6) ELSE 0 END) VIRTUAL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(campaign_id) REFERENCES ad_campaigns (id)\n)",
    'agent_runs': "CREATE TABLE agent_runs (\n\tid INTEGER NOT NULL, \n\ttask_id INTEGER, \n\tagent_type VARCHAR(100) NOT NULL, \n\trun_type VARCHAR(100) DEFAULT 'task', \n\tstatus VARCHAR(50) DEFAULT 'running', \n\tinput_context JSON, \n\toutput JSON, \n\traw_log TEXT, \n\ttokens_used INTEGER, \n\tcost_usd NUMERIC(14, 4), \n\tduration_secs FLOAT, \n\tstarted_at DATETIME DEFAULT (CURRENT_TIMESTAMP), \n\tended_at DATETIME, cli_session_id VARCHAR(120), tokens_input INTEGER, tokens_output INTEGER, usage_json JSON, attempt INTEGER, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(task_id) REFERENCES tasks (id)\n)",
}

#: downgrade: the original FLOAT texts, restored byte-for-byte
_DOWNGRADE_TEMP_DDL: dict[str, str] = {
    'ad_campaigns': "CREATE TABLE _t17_ad_campaigns (\n\tid INTEGER NOT NULL, \n\tplatform VARCHAR(50) NOT NULL, \n\texternal_id VARCHAR(255), \n\tname VARCHAR(512) NOT NULL, \n\tgoal VARCHAR(255), \n\tstatus VARCHAR(50) DEFAULT 'active', \n\tdaily_budget_usd FLOAT DEFAULT '0', \n\ttotal_spent_usd FLOAT DEFAULT '0', \n\tcreated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), \n\tPRIMARY KEY (id)\n)",
    'ad_metrics': "CREATE TABLE _t17_ad_metrics (\n\tid INTEGER NOT NULL, \n\tcampaign_id INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\timpressions INTEGER DEFAULT '0', \n\tclicks INTEGER DEFAULT '0', \n\tconversions INTEGER DEFAULT '0', \n\tspend_usd FLOAT DEFAULT '0', \n\trevenue_usd NUMERIC(14, 4) DEFAULT '0' NOT NULL, ctr NUMERIC(14, 6) GENERATED ALWAYS AS (CASE WHEN impressions > 0 THEN round(CAST(clicks AS REAL) / impressions, 6) ELSE 0 END) VIRTUAL, cpc NUMERIC(14, 6) GENERATED ALWAYS AS (CASE WHEN clicks > 0 THEN round(CAST(spend_usd AS REAL) / clicks, 6) ELSE 0 END) VIRTUAL, roas NUMERIC(14, 6) GENERATED ALWAYS AS (CASE WHEN spend_usd > 0 THEN round(CAST(revenue_usd AS REAL) / CAST(spend_usd AS REAL), 6) ELSE 0 END) VIRTUAL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(campaign_id) REFERENCES ad_campaigns (id)\n)",
    'agent_runs': "CREATE TABLE _t17_agent_runs (\n\tid INTEGER NOT NULL, \n\ttask_id INTEGER, \n\tagent_type VARCHAR(100) NOT NULL, \n\trun_type VARCHAR(100) DEFAULT 'task', \n\tstatus VARCHAR(50) DEFAULT 'running', \n\tinput_context JSON, \n\toutput JSON, \n\traw_log TEXT, \n\ttokens_used INTEGER, \n\tcost_usd FLOAT, \n\tduration_secs FLOAT, \n\tstarted_at DATETIME DEFAULT (CURRENT_TIMESTAMP), \n\tended_at DATETIME, cli_session_id VARCHAR(120), tokens_input INTEGER, tokens_output INTEGER, usage_json JSON, attempt INTEGER, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(task_id) REFERENCES tasks (id)\n)",
}

_DOWNGRADE_FINAL_DDL: dict[str, str] = {
    'ad_campaigns': "CREATE TABLE ad_campaigns (\n\tid INTEGER NOT NULL, \n\tplatform VARCHAR(50) NOT NULL, \n\texternal_id VARCHAR(255), \n\tname VARCHAR(512) NOT NULL, \n\tgoal VARCHAR(255), \n\tstatus VARCHAR(50) DEFAULT 'active', \n\tdaily_budget_usd FLOAT DEFAULT '0', \n\ttotal_spent_usd FLOAT DEFAULT '0', \n\tcreated_at DATETIME DEFAULT (CURRENT_TIMESTAMP), \n\tPRIMARY KEY (id)\n)",
    'ad_metrics': "CREATE TABLE ad_metrics (\n\tid INTEGER NOT NULL, \n\tcampaign_id INTEGER NOT NULL, \n\tdate DATE NOT NULL, \n\timpressions INTEGER DEFAULT '0', \n\tclicks INTEGER DEFAULT '0', \n\tconversions INTEGER DEFAULT '0', \n\tspend_usd FLOAT DEFAULT '0', \n\trevenue_usd NUMERIC(14, 4) DEFAULT '0' NOT NULL, ctr NUMERIC(14, 6) GENERATED ALWAYS AS (CASE WHEN impressions > 0 THEN round(CAST(clicks AS REAL) / impressions, 6) ELSE 0 END) VIRTUAL, cpc NUMERIC(14, 6) GENERATED ALWAYS AS (CASE WHEN clicks > 0 THEN round(CAST(spend_usd AS REAL) / clicks, 6) ELSE 0 END) VIRTUAL, roas NUMERIC(14, 6) GENERATED ALWAYS AS (CASE WHEN spend_usd > 0 THEN round(CAST(revenue_usd AS REAL) / CAST(spend_usd AS REAL), 6) ELSE 0 END) VIRTUAL, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(campaign_id) REFERENCES ad_campaigns (id)\n)",
    'agent_runs': "CREATE TABLE agent_runs (\n\tid INTEGER NOT NULL, \n\ttask_id INTEGER, \n\tagent_type VARCHAR(100) NOT NULL, \n\trun_type VARCHAR(100) DEFAULT 'task', \n\tstatus VARCHAR(50) DEFAULT 'running', \n\tinput_context JSON, \n\toutput JSON, \n\traw_log TEXT, \n\ttokens_used INTEGER, \n\tcost_usd FLOAT, \n\tduration_secs FLOAT, \n\tstarted_at DATETIME DEFAULT (CURRENT_TIMESTAMP), \n\tended_at DATETIME, cli_session_id VARCHAR(120), tokens_input INTEGER, tokens_output INTEGER, usage_json JSON, attempt INTEGER, \n\tPRIMARY KEY (id), \n\tFOREIGN KEY(task_id) REFERENCES tasks (id)\n)",
}

#: copied columns per table -- generated columns (ad_metrics.ctr/cpc/roas) are excluded and
#: recompute on read; every other column is copied verbatim.
_COPY_COLUMNS: dict[str, tuple[str, ...]] = {
    'ad_campaigns': ('id', 'platform', 'external_id', 'name', 'goal', 'status', 'daily_budget_usd', 'total_spent_usd', 'created_at'),
    'ad_metrics': ('id', 'campaign_id', 'date', 'impressions', 'clicks', 'conversions', 'spend_usd', 'revenue_usd'),
    'agent_runs': ('id', 'task_id', 'agent_type', 'run_type', 'status', 'input_context', 'output', 'raw_log', 'tokens_used', 'cost_usd', 'duration_secs', 'started_at', 'ended_at', 'cli_session_id', 'tokens_input', 'tokens_output', 'usage_json', 'attempt'),
}

#: indexes recreated after the (re-)creation, exactly as the chain stores them
_INDEXES: dict[str, tuple[str, ...]] = {
    'ad_campaigns': (
    ),
    'ad_metrics': (
        'CREATE INDEX ix_ad_metrics_date_campaign ON ad_metrics (date, campaign_id)',
        'CREATE UNIQUE INDEX uq_ad_metrics_campaign_date ON ad_metrics (campaign_id, date)',
    ),
    'agent_runs': (
        'CREATE INDEX ix_agent_runs_agent_type ON agent_runs (agent_type)',
        'CREATE INDEX ix_agent_runs_agent_type_started_at ON agent_runs (agent_type, started_at, id)',
        'CREATE INDEX ix_agent_runs_started_at ON agent_runs (started_at)',
        'CREATE INDEX ix_agent_runs_task_id ON agent_runs (task_id)',
        'CREATE UNIQUE INDEX ux_agent_runs_task_attempt ON agent_runs (task_id, attempt) WHERE task_id IS NOT NULL',
    ),
}

_TABLES: tuple[str, ...] = ("ad_campaigns", "ad_metrics", "agent_runs")


def _dialect_name() -> str:
    """Active dialect name -- works online and in `--sql` (offline) mode."""
    try:
        return op.get_bind().dialect.name
    except Exception:  # noqa: BLE001 -- offline mode has no bind
        return op.get_context().dialect.name


def _is_pg() -> bool:
    return _dialect_name() == "postgresql"


def _skip(reason: str) -> None:
    print(f"[alembic 0003] SKIP ({_dialect_name()}): {reason}")


def _rebuild(temp_ddl: dict[str, str], final_ddl: dict[str, str], verb: str, target: str) -> None:
    """The explicit no-rename dance: temp create, copy, drop, re-create, copy back, reindex."""
    bind = op.get_bind()
    for table in _TABLES:
        columns = ", ".join(_COPY_COLUMNS[table])
        temp = f"_t17_{table}"
        before = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar()
        op.execute(temp_ddl[table])
        op.execute(f"INSERT INTO {temp} ({columns}) SELECT {columns} FROM {table}")
        op.execute(f"DROP TABLE {table}")
        op.execute(final_ddl[table])
        op.execute(f"INSERT INTO {table} ({columns}) SELECT {columns} FROM {temp}")
        op.execute(f"DROP TABLE {temp}")
        for index_sql in _INDEXES[table]:
            op.execute(index_sql)
        after = bind.exec_driver_sql(f"SELECT COUNT(*) FROM {table}").scalar()
        if after != before:
            raise RuntimeError(
                f"0003 rebuild lost rows in {table}: {before} before, {after} after -- refusing"
            )
        print(f"[alembic 0003] {verb} {table}: {after} row(s) preserved ({target})")


def upgrade() -> None:
    if _is_pg():
        _skip(
            "money NUMERIC(14,4): PostgreSQL was already converted by 0002 "
            "(`USING round(x::numeric, 4)`); re-running it here would duplicate that intent. "
            "Audit: SELECT table_name, column_name, data_type, numeric_precision, numeric_scale "
            "FROM information_schema.columns WHERE column_name IN ('cost_usd', 'daily_budget_usd', "
            "'total_spent_usd', 'spend_usd');"
        )
        return
    _rebuild(_UPGRADE_TEMP_DDL, _UPGRADE_FINAL_DDL, "rebuilt", "-> NUMERIC(14, 4)")


def downgrade() -> None:
    if _is_pg():
        _skip("money downgrade: PostgreSQL's money type is 0002's decision, not reverted by 0003.")
        return
    _rebuild(_DOWNGRADE_TEMP_DDL, _DOWNGRADE_FINAL_DDL, "reverted", "-> FLOAT")
