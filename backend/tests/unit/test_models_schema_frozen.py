"""Frozen schema contract for `app.models` — the in-repo guard for the SQLAlchemy 2.0 migration.

Every value below is pinned to the PRE-migration ORM `create_all` output (the D-models receipt's
`probes/ddl_before.columns.tsv`: 17 tables / 182 columns / 6 unique constraints / 6 explicit
indexes; canonical sqlite_master sha256 `4bf064cd928d663566d6a05d8e681c2d57e1cdf285ae6a3b43d23d17beacc04e`).
The `Column(...)` -> `Mapped[...]` + `mapped_column(...)` migration must be zero-drift, so a
failure here means the DB contract changed — which belongs in a new `backend/alembic/versions/`
revision, not in a typing change.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import UniqueConstraint, create_engine, select
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.core.database import Base
from app.models import ActivityLog, CompanyConfig, DailyReport, Task

MODELS_DIR = Path(__file__).resolve().parents[2] / "app" / "models"

DDL_SHA256 = "4bf064cd928d663566d6a05d8e681c2d57e1cdf285ae6a3b43d23d17beacc04e"

# generated from probes/ddl_before.columns.tsv (pre-migration ORM create_all)
FROZEN_TABLE_NAMES = (
    'activity_log',
    'ad_campaigns',
    'ad_metrics',
    'agent_runs',
    'company_config',
    'competitors',
    'daily_reports',
    'email_campaigns',
    'email_logs',
    'expense_records',
    'memory_entries',
    'prospects',
    'revenue_snapshots',
    'social_engagements',
    'social_posts',
    'stripe_events',
    'tasks',
)

# table -> ((column, type, nullable, python_default, server_default, indexes, unique), ...)
FROZEN_COLUMNS = {
    'activity_log': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('agent_type', 'VARCHAR(100)', False, '', '', '', False),
        ('action', 'VARCHAR(255)', False, '', '', '', False),
        ('summary', 'TEXT', False, '', '', '', False),
        ('detail', 'JSON', True, '', '', '', False),
        ('level', 'VARCHAR(20)', True, "'info'", 'info', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'ad_campaigns': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('platform', 'VARCHAR(50)', False, '', '', '', False),
        ('external_id', 'VARCHAR(255)', True, '', '', '', False),
        ('name', 'VARCHAR(512)', False, '', '', '', False),
        ('goal', 'VARCHAR(255)', True, '', '', '', False),
        ('status', 'VARCHAR(50)', True, "'active'", 'active', '', False),
        ('daily_budget_usd', 'FLOAT', True, '0', '0', '', False),
        ('total_spent_usd', 'FLOAT', True, '0', '0', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'ad_metrics': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('campaign_id', 'INTEGER', False, '', '', '', False),
        ('date', 'DATE', False, '', '', '', False),
        ('impressions', 'INTEGER', True, '0', '0', '', False),
        ('clicks', 'INTEGER', True, '0', '0', '', False),
        ('conversions', 'INTEGER', True, '0', '0', '', False),
        ('spend_usd', 'FLOAT', True, '0', '0', '', False),
        ('ctr', 'FLOAT', True, '0', '0', '', False),
        ('cpc', 'FLOAT', True, '0', '0', '', False),
        ('roas', 'FLOAT', True, '0', '0', '', False),
    ),
    'agent_runs': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('task_id', 'INTEGER', True, '', '', 'ix_agent_runs_task_id', False),
        ('agent_type', 'VARCHAR(100)', False, '', '', 'ix_agent_runs_agent_type', False),
        ('run_type', 'VARCHAR(100)', True, "'task'", 'task', '', False),
        ('status', 'VARCHAR(50)', True, "'running'", 'running', '', False),
        ('input_context', 'JSON', True, '', '', '', False),
        ('output', 'JSON', True, '', '', '', False),
        ('raw_log', 'TEXT', True, '', '', '', False),
        ('tokens_used', 'INTEGER', True, '', '', '', False),
        ('cost_usd', 'FLOAT', True, '', '', '', False),
        ('duration_secs', 'FLOAT', True, '', '', '', False),
        ('started_at', 'DATETIME', True, '', 'now()', 'ix_agent_runs_started_at', False),
        ('ended_at', 'DATETIME', True, '', '', '', False),
        ('cli_session_id', 'VARCHAR(120)', True, '', '', '', False),
        ('tokens_input', 'INTEGER', True, '', '', '', False),
        ('tokens_output', 'INTEGER', True, '', '', '', False),
        ('usage_json', 'JSON', True, '', '', '', False),
    ),
    'company_config': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('name', 'VARCHAR(255)', False, '', '', '', False),
        ('mission', 'TEXT', True, '', '', '', False),
        ('vision', 'TEXT', True, '', '', '', False),
        ('description', 'TEXT', True, '', '', '', False),
        ('target_market', 'TEXT', True, '', '', '', False),
        ('value_prop', 'TEXT', True, '', '', '', False),
        ('pricing_model', 'JSON', True, '', '', '', False),
        ('goals', 'JSON', True, 'dict', '', '', False),
        ('kpis', 'JSON', True, 'dict', '', '', False),
        ('website_url', 'VARCHAR(512)', True, '', '', '', False),
        ('github_repo', 'VARCHAR(512)', True, '', '', '', False),
        ('product_type', 'VARCHAR(100)', True, '', '', '', False),
        ('industry', 'VARCHAR(100)', True, '', '', '', False),
        ('timezone', 'VARCHAR(50)', True, "'UTC'", 'UTC', '', False),
        ('daily_cycle_hour', 'INTEGER', True, '6', '6', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
        ('updated_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'competitors': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('name', 'VARCHAR(255)', False, '', '', '', False),
        ('website', 'VARCHAR(512)', True, '', '', '', False),
        ('pricing_info', 'JSON', True, '', '', '', False),
        ('positioning', 'TEXT', True, '', '', '', False),
        ('strengths', 'JSON', True, '', '', '', False),
        ('weaknesses', 'JSON', True, '', '', '', False),
        ('last_researched', 'DATETIME', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'daily_reports': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('report_date', 'DATE', False, '', '', '', True),
        ('morning_plan', 'TEXT', True, '', '', '', False),
        ('evening_summary', 'TEXT', True, '', '', '', False),
        ('tasks_planned', 'INTEGER', True, '0', '0', '', False),
        ('tasks_completed', 'INTEGER', True, '0', '0', '', False),
        ('tasks_failed', 'INTEGER', True, '0', '0', '', False),
        ('metrics_snapshot', 'JSON', True, '', '', '', False),
        ('insights', 'JSON', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
        ('updated_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'email_campaigns': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('name', 'VARCHAR(255)', False, '', '', '', False),
        ('goal', 'TEXT', True, '', '', '', False),
        ('target_segment', 'VARCHAR(255)', True, '', '', '', False),
        ('status', 'VARCHAR(50)', True, "'active'", 'active', '', False),
        ('total_sent', 'INTEGER', True, '0', '0', '', False),
        ('total_opened', 'INTEGER', True, '0', '0', '', False),
        ('total_replied', 'INTEGER', True, '0', '0', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'email_logs': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('prospect_id', 'INTEGER', False, '', '', '', False),
        ('campaign_id', 'INTEGER', True, '', '', '', False),
        ('sequence_step', 'INTEGER', True, '1', '1', '', False),
        ('subject', 'VARCHAR(512)', True, '', '', '', False),
        ('body', 'TEXT', True, '', '', '', False),
        ('sendgrid_id', 'VARCHAR(255)', True, '', '', '', False),
        ('status', 'VARCHAR(50)', True, "'sent'", 'sent', '', False),
        ('sent_at', 'DATETIME', True, '', '', '', False),
        ('opened_at', 'DATETIME', True, '', '', '', False),
        ('replied_at', 'DATETIME', True, '', '', '', False),
        ('follow_up_due', 'DATETIME', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'expense_records': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('category', 'VARCHAR(100)', False, '', '', '', False),
        ('vendor', 'VARCHAR(255)', False, '', '', '', False),
        ('amount_cents', 'INTEGER', False, '', '', '', False),
        ('currency', 'VARCHAR(10)', True, "'usd'", 'usd', '', False),
        ('description', 'TEXT', True, '', '', '', False),
        ('date', 'DATE', False, '', '', '', False),
        ('external_ref', 'VARCHAR(255)', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'memory_entries': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('category', 'VARCHAR(100)', False, '', '', '', False),
        ('title', 'VARCHAR(512)', False, '', '', '', False),
        ('content', 'TEXT', False, '', '', '', False),
        ('source', 'VARCHAR(100)', True, '', '', '', False),
        ('tags', 'JSON', True, '', '', '', False),
        ('chroma_id', 'VARCHAR(255)', True, '', '', '', True),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'prospects': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('email', 'VARCHAR(255)', False, '', '', '', True),
        ('first_name', 'VARCHAR(100)', True, '', '', '', False),
        ('last_name', 'VARCHAR(100)', True, '', '', '', False),
        ('company', 'VARCHAR(255)', True, '', '', '', False),
        ('title', 'VARCHAR(255)', True, '', '', '', False),
        ('source', 'VARCHAR(100)', True, '', '', '', False),
        ('status', 'VARCHAR(50)', True, "'new'", 'new', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'revenue_snapshots': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('snapshot_date', 'DATE', False, '', '', '', True),
        ('mrr_cents', 'INTEGER', True, '0', '0', '', False),
        ('arr_cents', 'INTEGER', True, '0', '0', '', False),
        ('active_subscribers', 'INTEGER', True, '0', '0', '', False),
        ('churned_today', 'INTEGER', True, '0', '0', '', False),
        ('new_today', 'INTEGER', True, '0', '0', '', False),
        ('total_revenue_month_cents', 'INTEGER', True, '0', '0', '', False),
        ('stripe_balance_cents', 'INTEGER', True, '0', '0', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'social_engagements': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('mention_id', 'VARCHAR(100)', True, '', '', '', True),
        ('author_handle', 'VARCHAR(100)', True, '', '', '', False),
        ('content', 'TEXT', True, '', '', '', False),
        ('our_reply', 'TEXT', True, '', '', '', False),
        ('reply_id', 'VARCHAR(100)', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'social_posts': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('platform', 'VARCHAR(50)', True, "'twitter'", 'twitter', '', False),
        ('content', 'TEXT', False, '', '', '', False),
        ('status', 'VARCHAR(50)', True, "'draft'", 'draft', '', False),
        ('tweet_id', 'VARCHAR(100)', True, '', '', '', False),
        ('scheduled_for', 'DATETIME', True, '', '', '', False),
        ('published_at', 'DATETIME', True, '', '', '', False),
        ('engagement', 'JSON', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'stripe_events': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('stripe_event_id', 'VARCHAR(255)', False, '', '', '', True),
        ('event_type', 'VARCHAR(255)', False, '', '', '', False),
        ('customer_id', 'VARCHAR(255)', True, '', '', '', False),
        ('amount_cents', 'INTEGER', True, '', '', '', False),
        ('currency', 'VARCHAR(10)', True, '', '', '', False),
        ('status', 'VARCHAR(50)', True, "'processed'", 'processed', '', False),
        ('raw_payload', 'JSON', True, '', '', '', False),
        ('processed_at', 'DATETIME', True, '', 'now()', '', False),
    ),
    'tasks': (
        ('id', 'INTEGER', False, '', '', '', False),
        ('title', 'VARCHAR(512)', False, '', '', '', False),
        ('description', 'TEXT', True, '', '', '', False),
        ('agent_type', 'VARCHAR(100)', False, '', '', '', False),
        ('priority', 'INTEGER', True, '3', '3', 'ix_tasks_pending_queue', False),
        ('status', 'VARCHAR(50)', True, "'pending'", 'pending', 'ix_tasks_pending_queue', False),
        ('source', 'VARCHAR(100)', True, "'orchestrator'", 'orchestrator', '', False),
        ('scheduled_date', 'DATETIME', True, '', '', 'ix_tasks_pending_queue', False),
        ('result_summary', 'TEXT', True, '', '', '', False),
        ('error_message', 'TEXT', True, '', '', '', False),
        ('metadata', 'JSON', True, '', '', '', False),
        ('created_at', 'DATETIME', True, '', 'now()', 'ix_tasks_created_at', False),
        ('updated_at', 'DATETIME', True, '', 'now()', '', False),
        ('claimed_at', 'DATETIME', True, '', '', '', False),
        ('claimed_by', 'VARCHAR(120)', True, '', '', '', False),
        ('heartbeat_at', 'DATETIME', True, '', '', '', False),
        ('attempts', 'INTEGER', True, '0', '0', '', False),
        ('dedupe_key', 'VARCHAR(255)', True, '', '', 'ix_tasks_dedupe_key', True),
    ),
}

FROZEN_UNIQUE_CONSTRAINTS = {
    'daily_reports.report_date',
    'memory_entries.chroma_id',
    'prospects.email',
    'revenue_snapshots.snapshot_date',
    'social_engagements.mention_id',
    'stripe_events.stripe_event_id',
}

# (name, columns, unique)
FROZEN_INDEXES = {
    ('ix_agent_runs_agent_type', ('agent_type',), False),
    ('ix_agent_runs_started_at', ('started_at',), False),
    ('ix_agent_runs_task_id', ('task_id',), False),
    ('ix_tasks_created_at', ('created_at',), False),
    ('ix_tasks_dedupe_key', ('dedupe_key',), True),
    ('ix_tasks_pending_queue', ('status', 'priority', 'scheduled_date'), False),
}


def _render_python_default(arg: object) -> str:
    """Deterministic rendering: callables by __qualname__ (no memory address), literals by repr."""
    if callable(arg):
        return getattr(arg, "__qualname__", type(arg).__name__)
    return repr(arg)


def _orm_rows() -> dict[str, list[tuple[str, str, bool, str, str, str, bool]]]:
    """The per-column table exactly as probes/dump_schema.py derives it."""
    out: dict[str, list[tuple[str, str, bool, str, str, str, bool]]] = {}
    for tname in sorted(Base.metadata.tables):
        table = Base.metadata.tables[tname]
        index_names_by_col: dict[str, list[str]] = {c.name: [] for c in table.columns}
        unique_idx_cols: set[str] = set()
        for ix in sorted(table.indexes, key=lambda i: i.name or ""):
            col_names = [c.name for c in ix.columns]
            for cn in col_names:
                index_names_by_col.setdefault(cn, []).append(ix.name or "")
            if ix.unique and len(col_names) == 1:
                unique_idx_cols.add(col_names[0])
        unique_constraint_cols: set[str] = set()
        for con in table.constraints:
            if isinstance(con, UniqueConstraint):
                con_cols = [c.name for c in con.columns]
                if len(con_cols) == 1:
                    unique_constraint_cols.add(con_cols[0])
        rows = []
        for col in table.columns:
            py_default = "" if col.default is None else _render_python_default(col.default.arg)
            sv_default = "" if col.server_default is None else str(col.server_default.arg)
            unique = bool(
                col.unique or col.name in unique_idx_cols or col.name in unique_constraint_cols
            )
            idx = ",".join(sorted(index_names_by_col.get(col.name, [])))
            rows.append(
                (col.name, str(col.type), bool(col.nullable), py_default, sv_default, idx, unique)
            )
        out[tname] = rows
    return out


def test_table_set_is_frozen() -> None:
    assert sorted(Base.metadata.tables) == sorted(FROZEN_TABLE_NAMES)


def test_per_column_rows_are_frozen() -> None:
    actual = _orm_rows()
    assert set(actual) == set(FROZEN_COLUMNS)
    for table in sorted(FROZEN_COLUMNS):
        expected_rows = list(FROZEN_COLUMNS[table])
        assert actual[table] == expected_rows, (
            f"{table}: column contract drifted\n"
            f"  expected: {expected_rows}\n"
            f"  actual  : {actual[table]}"
        )


def test_single_column_uniques_are_frozen() -> None:
    found: set[str] = set()
    for tname, table in Base.metadata.tables.items():
        for con in table.constraints:
            if isinstance(con, UniqueConstraint) and len(con.columns) == 1:
                found.add(f"{tname}.{list(con.columns)[0].name}")
    assert found == FROZEN_UNIQUE_CONSTRAINTS


def test_explicit_indexes_are_frozen() -> None:
    found = {
        (ix.name, tuple(c.name for c in ix.columns), bool(ix.unique))
        for table in Base.metadata.tables.values()
        for ix in table.indexes
    }
    assert found == FROZEN_INDEXES


def test_create_all_ddl_sha256_is_frozen(tmp_path: Path) -> None:
    """The full create_all DDL (tables + indexes, whitespace-exact) hashed as a constant."""
    db_path = tmp_path / "ddl_check.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    conn = sqlite3.connect(db_path)
    try:
        rows = conn.execute("SELECT type, name, sql FROM sqlite_master ORDER BY name").fetchall()
    finally:
        conn.close()
    payload = json.dumps(
        [{"type": t, "name": n, "sql": s} for t, n, s in rows],
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert digest == DDL_SHA256, f"create_all DDL drifted: {digest} != {DDL_SHA256}"


def test_metadata_column_keeps_its_db_name() -> None:
    """DB column stays `metadata`; the Python attribute stays `metadata_json` and is mapped."""
    col = Task.__table__.c["metadata"]
    assert col.name == "metadata"
    assert Task.metadata_json.property.columns[0] is col
    assert isinstance(Task.metadata_json, InstrumentedAttribute)
    assert "metadata" not in vars(Task)


def test_legacy_column_spelling_is_gone() -> None:
    """No module under app/models/ may use the legacy `Column(...)` spelling any more.

    This is what lets mypy see an instance attribute (its value) where the class attribute
    (the mapped object) used to be typed `Column[T]` for every consumer.
    """
    legacy_call = re.compile(r"\bColumn\(")
    legacy_import = re.compile(r"^\s*(?:from|import)\s+[^\n]*\bColumn\b", re.MULTILINE)
    offenders = []
    for path in sorted(MODELS_DIR.glob("*.py")):
        src = path.read_text(encoding="utf-8")
        if legacy_call.search(src) or legacy_import.search(src):
            offenders.append(path.name)
    assert offenders == [], f"legacy Column( spelling still present in: {offenders}"


def test_sqlite_roundtrip_reads_values_not_columns(tmp_path: Path) -> None:
    """Instance access returns VALUES (and python defaults apply); class access stays a mapped attr."""
    engine = create_engine(f"sqlite:///{tmp_path / 'roundtrip.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = Task(title="t1", agent_type="orchestrator", scheduled_date=datetime(2026, 10, 11, 6))
        session.add(task)
        session.add(
            ActivityLog(agent_type="orchestrator", action="x", summary="s", detail={"a": 1})
        )
        session.add(DailyReport(report_date=date(2026, 10, 11), insights=["i1"]))
        session.add(CompanyConfig(name="Co", timezone="UTC"))
        session.commit()

        got = session.execute(select(Task)).scalar_one()
        assert isinstance(got.id, int) and got.id == 1
        assert got.title == "t1"
        assert got.status == "pending" and got.attempts == 0  # python-side defaults applied
        assert got.metadata_json is None

        activity = session.execute(select(ActivityLog)).scalar_one()
        assert activity.level == "info"  # python-side default applied
        assert activity.detail == {"a": 1}  # JSON column round-trips

        report = session.execute(select(DailyReport)).scalar_one()
        assert report.report_date == date(2026, 10, 11)
        assert report.insights == ["i1"]

    assert isinstance(Task.id, InstrumentedAttribute)  # class access = the mapped attribute
    assert isinstance(Task.metadata_json, InstrumentedAttribute)
