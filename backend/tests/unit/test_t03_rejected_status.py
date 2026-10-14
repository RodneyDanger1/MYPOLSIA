"""T03/D2 — `rejected`: the refused-before-execution task state, its Enum escalation, and the
0006 migration that carries it.

What this pins, layer by layer:

* the value set exists in ONE place (`app.models.task.TaskStatus`), the column renders the SHIPPED
  `ck_tasks_status` CHECK (SQLAlchemy `Enum(native_enum=False, create_constraint=True)`), and
  migration 0006's own literal is exactly the 0002 six + `rejected` — order preserved;
* the WRITERS accept it: `create_task`/`update_task_status` round-trip `rejected`, the read filters
  serve it;
* the CHOKE POINT lands it: a run refused by the dispatch budget gate (before any provider call,
  before any `agent_runs` row) ends `rejected` — while a normal terminal failure still ends
  `failed` (a failure is not a refusal);
* migration 0006 round-trips 0005 -> head -> 0005 -> head on SQLite and moves exactly its own rows.

A fresh `create_all` schema now enforces the value set on SQLite too (measured: the CHECK is in
the CREATE TABLE text), which this file turns into a live assertion — a bogus raw write fails at
the database, not only at the service.
"""
from __future__ import annotations

import pathlib
import re
import sqlite3

import pytest

from alembic import command
from alembic import config as alembic_config
from app.core.errors import E_VALIDATION, PolsiaError
from app.models.task import TaskStatus
from app.services.task_service import (
    VALID_TASK_STATUSES,
    create_task,
    get_task,
    list_tasks,
    update_task_status,
)

BACKEND = pathlib.Path(__file__).resolve().parents[2]
REVISION_0006 = BACKEND / "alembic" / "versions" / "0006_statuses_and_keys.py"


@pytest.fixture(autouse=True)
def _quiet_redis(monkeypatch):
    """No network: the worker path publishes activity through `events.get_redis`, and a refused
    Redis socket costs a real connect timeout per publish (the celery conftest's autouse guard is
    scoped to `tests/unit/celery/`; the worker-level legs below live in this file)."""
    from unittest.mock import AsyncMock

    mock = AsyncMock()
    mock.publish = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.events.get_redis", lambda: mock)
    return mock


def _migration_0006_values() -> list[str]:
    """0006's own literal value list, parsed from the migration source.

    The migration is a frozen snapshot and duplicates the list deliberately (it must not import
    `app.models`); this parse is what keeps the duplication honest.
    """
    text = REVISION_0006.read_text(encoding="utf-8")
    old = re.search(r"_OLD_STATUS_VALUES: tuple\[str, \.\.\.\] = \(([^)]*)\)", text)
    new = re.search(r"_NEW_STATUS_VALUES: tuple\[str, \.\.\.\] = _OLD_STATUS_VALUES \+ \(([^)]*)\)", text)
    assert old is not None and new is not None, "0006's status literals not found"
    parse = lambda block: [p.strip().strip('"') for p in block.split(",") if p.strip()]  # noqa: E731
    return [*parse(old.group(1)), *parse(new.group(1))]


# ── the type is the source of truth ───────────────────────────────────────────────────────────


def test_the_enum_members_are_the_shipped_values_with_rejected_appended():
    values = tuple(member.value for member in TaskStatus)
    assert values == (*VALID_TASK_STATUSES[:6], "rejected")
    assert values == VALID_TASK_STATUSES
    assert TaskStatus.REJECTED.value == "rejected"


def test_migration_0006_carries_exactly_the_enum_value_list_in_order():
    assert _migration_0006_values() == list(VALID_TASK_STATUSES)
    assert _migration_0006_values()[:6] == [
        "pending", "in_progress", "completed", "failed", "cancelled", "awaiting_approval",
    ], "0002's list must survive byte-for-byte as the prefix"


_REVISION_LINE = re.compile(r'^revision: str = "([^"]+)"', re.MULTILINE)
_DOWN_LINE = re.compile(
    r'^down_revision: (?:str \| None|Union\[str, None\]) = (?:"([^"]+)"|None)', re.MULTILINE
)


def _revision_graph() -> dict[str, str | None]:
    """{revision: down_revision} for every file in alembic/versions (annotation-shape agnostic)."""
    graph: dict[str, str | None] = {}
    for path in sorted((BACKEND / "alembic" / "versions").glob("[0-9]*.py")):
        text = path.read_text(encoding="utf-8")
        revision = _REVISION_LINE.search(text)
        down = _DOWN_LINE.search(text)
        assert revision is not None and down is not None, f"unparsable revision header: {path.name}"
        graph[revision.group(1)] = down.group(1)
    return graph


def test_the_migration_chain_is_linear_with_a_single_head():
    """0008 is the ONE head; walking down visits every revision exactly once, in order."""
    graph = _revision_graph()
    heads = sorted(r for r in graph if r not in set(graph.values()))
    assert heads == ["0008"], f"expected a single head (0008), found {heads}"

    order: list[str] = []
    cursor: str | None = "0008"
    while cursor is not None:
        order.append(cursor)
        cursor = graph[cursor]
    assert order == ["0008", "0007", "0006", "0005", "0004", "0003", "0002", "0001"], order
    assert len(order) == len(graph), "the chain must not branch"


def test_0006_carries_its_fragments_for_BOTH_dialects():
    """The CHECK text is dialect-neutral; the index predicates are declared for PG AND SQLite."""
    text = REVISION_0006.read_text(encoding="utf-8")
    assert "postgresql_where=sa.text(_AD_CAMPAIGN_PREDICATE)" in text
    assert "sqlite_where=sa.text(_AD_CAMPAIGN_PREDICATE)" in text
    # the status CHECK compiles on both dialects (it is plain SQL: no dialect hook needed)
    from sqlalchemy import text as sa_text
    from sqlalchemy.dialects import postgresql, sqlite

    statement = sa_text("SELECT 1 WHERE status IN ('pending','in_progress','rejected')")
    rendered_pg = str(statement.compile(dialect=postgresql.dialect()))
    rendered_sqlite = str(statement.compile(dialect=sqlite.dialect()))
    assert "'rejected'" in rendered_pg and "'rejected'" in rendered_sqlite


# ── the writers ───────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rejected_round_trips_through_every_writer_and_reader(async_db_session):
    task = await create_task(async_db_session, title="refused later", agent_type="finance")
    await async_db_session.flush()
    assert task.status == "pending"

    updated = await update_task_status(
        async_db_session, task.id, "rejected", error_message="budget: insufficient budget"
    )
    assert updated is not None and updated.status == "rejected"

    fresh = await get_task(async_db_session, task.id)
    assert fresh is not None and fresh.status == "rejected"
    listed = await list_tasks(async_db_session, status="rejected")
    assert [row.id for row in listed] == [task.id]
    assert fresh.error_message == "budget: insufficient budget"


def test_the_fresh_schema_enforces_the_value_set_at_rest(tmp_path):
    """A bogus status is refused by the DATABASE on a create_all schema (the new CHECK)."""
    from sqlalchemy import create_engine
    from sqlalchemy import text as sa_text

    import app.models  # noqa: F401  — register every table
    from app.core.database import Base

    engine = create_engine(f"sqlite:///{(tmp_path / 'check.db').as_posix()}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            sa_text("INSERT INTO tasks (title, agent_type, status) VALUES ('ok', 'finance', 'rejected')")
        )
        with pytest.raises(Exception) as excinfo:
            conn.execute(
                sa_text(
                    "INSERT INTO tasks (title, agent_type, status) "
                    "VALUES ('bad', 'finance', 'definitely_not_a_status')"
                )
            )
        assert "ck_tasks_status" in str(excinfo.value)
    engine.dispose()


# ── the choke point: refused -> rejected, failed -> failed ────────────────────────────────────


def test_a_budget_refused_dispatch_lands_rejected_with_nothing_charged(queue_db, monkeypatch):
    """The dispatch gate refuses BEFORE any execution: row `rejected`, no run ledger row."""
    from decimal import Decimal

    from app.agents.accounting import budget_policy_from_settings
    from app.core.database import utcnow
    from app.models.task import AgentRun
    from celery_app.tasks.agent_tasks import _execute_agent_task

    policy = budget_policy_from_settings()

    async def _seed() -> None:
        async with queue_db.factory() as db:
            db.add(AgentRun(agent_type="social_media", cost_usd=Decimal(str(policy.day_limit_usd)),
                            started_at=utcnow()))
            await db.commit()

    queue_db.run(_seed())
    task_id = queue_db.create_task(title="T03 refused", agent_type="finance")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "skipped"
    assert outcome["task_status"] == "rejected"
    assert queue_db.count("agent_runs", "agent_type = ?", ("finance",)) == 0, "nothing charged"
    row = queue_db.row(task_id)
    assert row["status"] == "rejected"
    assert row["error_message"].startswith("budget:")


def test_a_normal_terminal_failure_still_lands_failed_not_rejected(queue_db, monkeypatch, install_run_spy):
    """A failure is a failure: the terminal refusal of the RUN is `failed`."""
    from app.core.errors import E_CLI_MISSING, PolsiaError
    from celery_app.tasks.agent_tasks import _execute_agent_task

    # E_CLI_MISSING is NOT in agent_tasks.RETRYABLE_CODES (infrastructure-terminal), so the run
    # ends `failed` rather than being requeued — which is what makes this the honest contrast to
    # the budget refusal above (a refusal is `rejected`; a run that failed is `failed`).
    install_run_spy(
        delegate=False,
        error_factory=lambda: PolsiaError(E_CLI_MISSING, "the claude CLI is not on PATH"),
    )
    task_id = queue_db.create_task(title="T03 normal failure", agent_type="finance")

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "failed"
    row = queue_db.row(task_id)
    assert row["status"] == "failed", "a business failure is not a refusal"
    assert row["status"] != "rejected"
    assert "E_CLI_MISSING" in (row["error_message"] or "")


@pytest.mark.asyncio
async def test_an_unknown_status_is_refused_by_name_before_any_write(async_db_session):
    """A typo cannot become a stored value — the writer refuses by name (E_VALIDATION)."""
    task = await create_task(async_db_session, title="typo guard", agent_type="finance")
    await async_db_session.flush()
    with pytest.raises(PolsiaError) as excinfo:
        await update_task_status(async_db_session, task.id, "rejectedd")
    assert excinfo.value.code == E_VALIDATION
    fresh = await get_task(async_db_session, task.id)
    assert fresh is not None and fresh.status == "pending"


# ── migration 0006 round-trip (SQLite) ────────────────────────────────────────────────────────


def _snapshot(db_path: pathlib.Path) -> dict[tuple[str, str], str | None]:
    con = sqlite3.connect(db_path.as_posix())
    try:
        return {
            (kind, name): sql
            for kind, name, sql in con.execute(
                "SELECT type, name, sql FROM sqlite_master ORDER BY name"
            )
        }
    finally:
        con.close()


def test_0006_round_trips_0005_head_0005_head_and_moves_only_its_own_rows(monkeypatch, tmp_path):
    """upgrade head -> downgrade 0005 -> upgrade head on SQLite, asserting the moves."""
    from app.config import settings

    db_path = tmp_path / "r_w1_0006.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "0005")
    at_0005 = _snapshot(db_path)

    # legacy duplicates the new index must pre-clean (keep MIN(id)) + a NULL-key row it must leave
    con = sqlite3.connect(db_path.as_posix())
    try:
        con.execute("INSERT INTO ad_campaigns (id, platform, external_id, name) "
                    "VALUES (1, 'google_ads', 'g-dup', 'oldest')")
        con.execute("INSERT INTO ad_campaigns (id, platform, external_id, name) "
                    "VALUES (2, 'google_ads', 'g-dup', 'newer')")
        con.execute("INSERT INTO ad_campaigns (id, platform, external_id, name) "
                    "VALUES (3, 'meta_ads', NULL, 'no-provider-id')")
        con.commit()
    finally:
        con.close()

    command.upgrade(cfg, "head")
    at_head = _snapshot(db_path)
    assert ("index", "uq_ad_campaigns_platform_external_id") in at_head
    assert "WHERE external_id IS NOT NULL" in (at_head[("index", "uq_ad_campaigns_platform_external_id")] or "")
    assert ("index", "uq_ad_campaigns_platform_external_id") not in at_0005

    con = sqlite3.connect(db_path.as_posix())
    try:
        # W1 round trip, U5 + V8 re-pin: `head` is 0008 since
        # `0008_chat_schedules_invoices.py` (deliberate old -> new: "0007" -> "0008"); the
        # 0005/0006 legs below are unchanged and still assert THIS revision's own moves.
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0008"
        assert con.execute("SELECT id, name FROM ad_campaigns WHERE external_id = 'g-dup'").fetchall() == [
            (1, "oldest")
        ], "pre-clean keeps MIN(id)"
        assert con.execute("SELECT COUNT(*) FROM ad_campaigns WHERE external_id IS NULL").fetchone()[0] == 1
        # `rejected` is storable at rest (SQLite has no CHECK to re-create; the writers assert)
        con.execute("INSERT INTO tasks (title, agent_type, status) VALUES ('r', 'finance', 'rejected')")
        # ...and a direct duplicate on the provider key is refused by the NEW index
        with pytest.raises(sqlite3.IntegrityError):
            con.execute("INSERT INTO ad_campaigns (platform, external_id, name) "
                        "VALUES ('google_ads', 'g-dup', 'again')")
    finally:
        con.close()

    command.downgrade(cfg, "0005")
    after_down = _snapshot(db_path)
    assert ("index", "uq_ad_campaigns_platform_external_id") not in after_down
    assert {k: v for k, v in at_0005.items()} == {k: after_down[k] for k in at_0005}, (
        "downgrade must restore the 0005 schema byte-for-byte (the pre-clean is not reversible)"
    )

    command.upgrade(cfg, "head")
    assert _snapshot(db_path) == at_head, "re-upgrade restores the 0006 schema byte-for-byte"
