"""V28/L5 — `scripts/cleanup_drill_data.py` on a SEEDED, migrated database.

The script's contract (its own docstring) is narrow and absolute: a row is deleted only when its
allowlisted title/name column carries `[drill]` or the whole word `drill`; child rows are never
touched (the tagged parent is reported as residual instead); `--apply` deletes exactly the planned
ids in ONE transaction and refuses on a mismatch; an unprovable target is exit 2.

The tests below run the REAL CLI (subprocess + exit code + stdout) against a database built by the
real chain from zero.  Two defects are proven and carried as xfail(strict=True) so the suite stays
green while CI would fail the moment the fix lands (XPASS strict):

* D-1 `cleanup_drill_data.py:337` deletes agent_runs by `task_id` while `:254` put RUN ids in the
  plan.  Ordinary case → refusal (`planned 1 / deleted 0`); coincidence case → an UNTAGGED task's
  run is deleted and the tagged task's run is stranded.
* D-2 `cleanup_drill_data.py:270-275` has no `email_logs` guard for a tagged prospect, so `--apply`
  deletes the parent and strands the send-history child (the very thing the docstring promises
  never to do).
"""
from __future__ import annotations

import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys

import pytest

from alembic import command
from alembic import config as alembic_config

BACKEND = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = BACKEND / "scripts" / "cleanup_drill_data.py"

SEEDED_TABLES = (
    "tasks",
    "agent_runs",
    "ad_campaigns",
    "ad_metrics",
    "email_campaigns",
    "email_logs",
    "memory_entries",
    "competitors",
    "social_posts",
    "support_inbox_items",
    "prospects",
    "task_attachments",
    "chat_messages",
)


@pytest.fixture(scope="module")
def template_db(tmp_path_factory):
    """One migrated-from-zero SQLite file, copied per test (the chain is the expensive part)."""
    from app.config import settings

    db_path = tmp_path_factory.mktemp("v28_cleanup") / "template.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    with pytest.MonkeyPatch.context() as patcher:
        patcher.setattr(settings, "database_url", url)
        cfg = alembic_config.Config()
        cfg.set_main_option("script_location", str(BACKEND / "alembic"))
        cfg.set_main_option("sqlalchemy.url", url)
        command.upgrade(cfg, "head")
    return db_path


@pytest.fixture
def db_path(template_db, tmp_path):
    target = tmp_path / "seeded.sqlite"
    shutil.copyfile(template_db, target)
    return target


def _run(db: pathlib.Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite+aiosqlite:///{db.as_posix()}",
        "CLAUDE_CLI_MOCK": "true",
        # V29 parent fix: never let this test touch the repo's real data/sites or data/trash.
        "POLSIA_SITES_DIR": str(db.parent / "sites"),
        "POLSIA_TRASH_DIR": str(db.parent / "trash"),
    }
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=str(BACKEND),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _snapshot(db: pathlib.Path) -> dict[str, int]:
    con = sqlite3.connect(db.as_posix())
    try:
        return {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in SEEDED_TABLES
        }
    finally:
        con.close()


def _count(stdout: str, table: str) -> int:
    """The script's per-table count line (`<table><spaces><n>`), never a substring match."""
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0] == table and parts[1].isdigit():
            return int(parts[1])
    raise AssertionError(f"no count line for {table!r} in:\n{stdout}")


def _seed_mixed(con: sqlite3.Connection) -> None:
    """Every allowlisted table, tagged and untagged, plus every residual shape.

    Tagged/blocked arithmetic (the dry-run pin below): tasks 1,6,8 deletable; 2,3,4 residual
    because they own an attachment / chat row / support row; `drilling a well` (7) is NOT a match.
    """
    con.executemany(
        "INSERT INTO tasks (id, title, agent_type, status) VALUES (?, ?, 'finance', 'pending')",
        [
            (1, "[drill] bare tagged"),
            (2, "[drill] tagged with attachment"),
            (3, "[drill] tagged with chat"),
            (4, "[drill] tagged with support row"),
            (5, "real work"),
            (6, "[DRILL] tagged with run only"),
            (7, "drilling a well"),
            (8, "a drill bit"),
        ],
    )
    con.executemany(
        "INSERT INTO agent_runs (id, task_id, agent_type, status) VALUES (?, ?, 'finance', 'completed')",
        [(1, 1), (2, 6), (3, None)],
    )
    con.execute(
        "INSERT INTO task_attachments (id, task_id, filename, stored_name, mime, size_bytes, sha256) "
        "VALUES (1, 2, 'a.png', ?, 'image/png', 3, ?)",
        ("a" * 32 + ".png", "a" * 64),
    )
    con.execute(
        "INSERT INTO chat_messages (id, task_id, role, content) VALUES (1, 3, 'owner', 'about it')"
    )
    con.executemany(
        "INSERT INTO support_inbox_items (id, provider_message_id, from_address, subject, body, task_id) "
        "VALUES (?, ?, 'a@b.c', ?, 'body', ?)",
        [
            (1, "pm-1", "[drill] attached to a task", 4),
            (2, "pm-2", "[drill] free standing", None),
        ],
    )
    con.executemany(
        "INSERT INTO ad_campaigns (id, platform, name, status) VALUES (?, 'twitter', ?, 'active')",
        [(1, "[drill] campaign"), (2, "[drill] campaign with metrics"), (3, "real campaign")],
    )
    con.executemany(
        "INSERT INTO ad_metrics (id, campaign_id, date, impressions, clicks, conversions) "
        "VALUES (?, ?, '2026-10-18', 10, 1, 0)",
        [(1, 2), (2, 3)],
    )
    con.executemany(
        "INSERT INTO email_campaigns (id, name, status) VALUES (?, ?, 'sent')",
        [(1, "[drill] email"), (2, "[drill] email with logs")],
    )
    con.executemany(
        "INSERT INTO prospects (id, email, company) VALUES (?, ?, ?)",
        [(1, "drill@example.com", "Acme"), (2, "real@example.com", "Acme"), (3, "x@example.com", "Drill Co")],
    )
    con.execute("INSERT INTO email_logs (id, prospect_id, campaign_id, status) VALUES (1, 2, 2, 'sent')")
    con.executemany(
        "INSERT INTO memory_entries (id, category, title, content) VALUES (?, 'x', ?, 'c')",
        [(1, "[drill] memory"), (2, "real memory")],
    )
    con.executemany("INSERT INTO competitors (id, name) VALUES (?, ?)", [(1, "[drill] rival"), (2, "real rival")])
    con.executemany("INSERT INTO social_posts (id, content) VALUES (?, ?)", [(1, "[drill] post"), (2, "real post")])
    con.commit()


def test_the_tag_respects_a_word_boundary() -> None:
    """`[drill]`, `drill`, `DRILL` match; `drilling` and other stems do not."""
    sys.path.insert(0, str(BACKEND))
    from scripts.cleanup_drill_data import TAG_RE

    assert TAG_RE.search("[drill] anything")
    assert TAG_RE.search("a drill bit")
    assert TAG_RE.search("DRILL")
    assert TAG_RE.search("a [Drill] tag")
    assert TAG_RE.search("drilling a well") is None
    assert TAG_RE.search("mandrill") is None
    assert TAG_RE.search("drillbit") is None


def test_dry_run_and_report_count_only_tagged_rows_and_delete_nothing(db_path) -> None:
    con = sqlite3.connect(db_path.as_posix())
    try:
        _seed_mixed(con)
    finally:
        con.close()
    before = _snapshot(db_path)

    dry = _run(db_path)
    assert dry.returncode == 0, dry.stderr or dry.stdout
    assert "DRY RUN" in dry.stdout and "nothing was deleted" in dry.stdout

    report = _run(db_path, "--report")
    assert report.returncode == 0, report.stderr or report.stdout
    assert "REPORT" in report.stdout

    expected = {
        "tasks": 3,             # 1, 6, 8 — 2/3/4 are residual, "drilling" (7) never matches
        "agent_runs": 2,        # runs of tasks 1 and 6 (the task-less run 3 is untouched)
        "ad_campaigns": 1,      # campaign 2 has ad_metrics children → residual
        "email_campaigns": 1,   # campaign 2 has email_logs children → residual
        "memory_entries": 1,
        "competitors": 1,
        "social_posts": 1,
        "support_inbox_items": 2,
        "prospects": 2,         # email match + company-word match
    }
    for table, count in expected.items():
        assert _count(dry.stdout, table) == count, (table, dry.stdout)
        assert _count(report.stdout, table) == count, (table, report.stdout)
    assert _count(dry.stdout, "TOTAL") == sum(expected.values()) == 14
    assert _snapshot(db_path) == before, "a dry run (or --report) must not write a single row"
    assert "residual (NOT deleted" in dry.stdout
    assert "tagged tasks with attachments/chat/support rows" in dry.stdout


def test_apply_deletes_exactly_the_tagged_rows_when_no_task_owns_a_run(db_path) -> None:
    """The script's happy path: a plan with no agent_runs deletes only its own tagged rows."""
    con = sqlite3.connect(db_path.as_posix())
    try:
        con.executemany(
            "INSERT INTO tasks (id, title, agent_type, status) VALUES (?, ?, 'finance', 'pending')",
            [(10, "[drill] task with no children"), (11, "real task")],
        )
        con.executemany(
            "INSERT INTO ad_campaigns (id, platform, name) VALUES (?, 'twitter', ?)",
            [(10, "[drill] campaign"), (11, "real campaign")],
        )
        con.executemany(
            "INSERT INTO email_campaigns (id, name) VALUES (?, ?)",
            [(10, "[drill] email"), (11, "real email")],
        )
        con.executemany(
            "INSERT INTO prospects (id, email) VALUES (?, ?)",
            [(10, "drill@example.com"), (11, "real@example.com")],
        )
        con.executemany(
            "INSERT INTO memory_entries (id, category, title, content) VALUES (?, 'x', ?, 'c')",
            [(10, "[drill] memory"), (11, "real memory")],
        )
        con.executemany(
            "INSERT INTO competitors (id, name) VALUES (?, ?)",
            [(10, "[drill] rival"), (11, "real rival")],
        )
        con.executemany(
            "INSERT INTO social_posts (id, content) VALUES (?, ?)",
            [(10, "[drill] post"), (11, "real post")],
        )
        con.executemany(
            "INSERT INTO support_inbox_items (id, provider_message_id, from_address, subject, body) "
            "VALUES (?, ?, 'a@b.c', ?, 'body')",
            [(10, "pm-10", "[drill] support"), (11, "pm-11", "real support")],
        )
        con.commit()
    finally:
        con.close()

    applied = _run(db_path, "--apply", "--report")
    assert applied.returncode == 0, applied.stderr or applied.stdout
    assert "BEFORE" in applied.stdout and "AFTER" in applied.stdout
    assert "APPLIED — deleted 8 row(s)" in applied.stdout

    con = sqlite3.connect(db_path.as_posix())
    try:
        for table in ("tasks", "ad_campaigns", "email_campaigns", "prospects",
                      "memory_entries", "competitors", "social_posts", "support_inbox_items"):
            assert [row[0] for row in con.execute(f"SELECT id FROM {table} ORDER BY id")] == [11], (
                f"{table}: only the untagged row 11 may survive"
            )
        assert con.execute("PRAGMA foreign_key_check").fetchall() == []
        assert con.execute(
            "SELECT COUNT(*) FROM tasks WHERE title LIKE '%drill%'"
        ).fetchone()[0] == 0
    finally:
        con.close()


def test_delete_refuses_a_plan_with_a_stale_id_and_rolls_back(db_path) -> None:
    """The fail-safe: `delete != planned` raises BEFORE commit, so every table keeps its rows.

    Driven directly at `_delete()` with a plan holding a row that does not exist. The old CLI
    version of this test only produced a mismatch because `agent_runs` was deleted by task_id
    (V28/L5 D-1); with that fixed, the invariant needs an explicit stale id to be exercised.
    """
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    sys.path.insert(0, str(BACKEND))
    from scripts.cleanup_drill_data import Refusal, _delete

    con = sqlite3.connect(db_path.as_posix())
    try:
        _seed_mixed(con)
    finally:
        con.close()
    before = _snapshot(db_path)

    plan = {
        "ids": {
            table: set()
            for table in (
                "tasks",
                "agent_runs",
                "ad_campaigns",
                "email_campaigns",
                "memory_entries",
                "competitors",
                "social_posts",
                "support_inbox_items",
                "prospects",
            )
        }
    }
    plan["ids"]["tasks"] = {999_999}

    async def go() -> None:
        engine = create_async_engine(f"sqlite+aiosqlite:///{db_path.as_posix()}")
        try:
            async with engine.begin() as conn:
                await _delete(conn, plan)
        finally:
            await engine.dispose()

    with pytest.raises(Refusal, match="delete plan mismatch"):
        asyncio.run(go())

    assert _snapshot(db_path) == before, "a refused apply must roll back every delete it planned"
    assert _dangling(db_path) == []


def _dangling(db: pathlib.Path) -> list[tuple]:
    con = sqlite3.connect(db.as_posix())
    try:
        return con.execute("PRAGMA foreign_key_check").fetchall()
    finally:
        con.close()


def test_apply_deletes_the_tagged_task_and_its_run(db_path) -> None:
    con = sqlite3.connect(db_path.as_posix())
    try:
        con.execute(
            "INSERT INTO tasks (id, title, agent_type, status) VALUES (6, '[drill] with a run', "
            "'finance', 'pending')"
        )
        con.execute(
            "INSERT INTO agent_runs (id, task_id, agent_type, status) VALUES (1, 6, 'finance', 'completed')"
        )
        con.commit()
    finally:
        con.close()

    result = _run(db_path, "--apply")
    assert result.returncode == 0, result.stderr or result.stdout
    assert "APPLIED — deleted 2 row(s)" in result.stdout
    assert _snapshot(db_path)["tasks"] == 0
    assert _snapshot(db_path)["agent_runs"] == 0
    assert _dangling(db_path) == []


def test_apply_never_deletes_a_run_that_belongs_to_an_untagged_task(db_path) -> None:
    con = sqlite3.connect(db_path.as_posix())
    try:
        con.executemany(
            "INSERT INTO tasks (id, title, agent_type, status) VALUES (?, ?, 'finance', 'pending')",
            [(20, "[drill] tagged task"), (21, "real task")],
        )
        con.executemany(
            "INSERT INTO agent_runs (id, task_id, agent_type, status) VALUES (?, ?, 'finance', 'completed')",
            [(21, 20), (100, 21)],
        )
        con.commit()
    finally:
        con.close()

    result = _run(db_path, "--apply")
    assert result.returncode == 0, result.stderr or result.stdout
    rows = _agent_runs(db_path)
    assert rows == [(100, 21)], f"only the tagged task's run may go; found {rows}"
    assert [row[0] for row in _tasks(db_path)] == [21], "the untagged task must survive"
    assert _dangling(db_path) == []


def test_apply_leaves_no_dangling_email_logs_behind_a_deleted_prospect(db_path) -> None:
    con = sqlite3.connect(db_path.as_posix())
    try:
        con.execute("INSERT INTO prospects (id, email) VALUES (1, 'drill@example.com')")
        con.execute("INSERT INTO email_campaigns (id, name, status) VALUES (1, 'real', 'sent')")
        con.execute(
            "INSERT INTO email_logs (id, prospect_id, campaign_id, status) VALUES (1, 1, 1, 'sent')"
        )
        con.commit()
    finally:
        con.close()

    result = _run(db_path, "--apply")
    assert result.returncode == 0, result.stderr or result.stdout
    assert _email_logs(db_path) == [(1, 1)], "a child row carrying no tag must never be deleted"
    assert _dangling(db_path) == [], "deleting a prospect must not orphan its send history"


def _agent_runs(db: pathlib.Path) -> list[tuple]:
    con = sqlite3.connect(db.as_posix())
    try:
        return con.execute("SELECT id, task_id FROM agent_runs ORDER BY id").fetchall()
    finally:
        con.close()


def _tasks(db: pathlib.Path) -> list[tuple]:
    con = sqlite3.connect(db.as_posix())
    try:
        return con.execute("SELECT id FROM tasks ORDER BY id").fetchall()
    finally:
        con.close()


def _email_logs(db: pathlib.Path) -> list[tuple]:
    con = sqlite3.connect(db.as_posix())
    try:
        return con.execute("SELECT id, prospect_id FROM email_logs ORDER BY id").fetchall()
    finally:
        con.close()


def test_apply_skips_a_tagged_parent_that_owns_an_untagged_child(db_path) -> None:
    """The docstring's absolute: a tagged parent with children is reported, never deleted."""
    con = sqlite3.connect(db_path.as_posix())
    try:
        con.execute(
            "INSERT INTO tasks (id, title, agent_type, status) VALUES (30, '[drill] owner', "
            "'finance', 'pending')"
        )
        con.execute(
            "INSERT INTO task_attachments (id, task_id, filename, stored_name, mime, size_bytes, "
            "sha256) VALUES (30, 30, 'a.png', ?, 'image/png', 3, ?)",
            ("a" * 32 + ".png", "a" * 64),
        )
        con.execute("INSERT INTO ad_campaigns (id, platform, name) VALUES (30, 'twitter', '[drill] campaign')")
        con.execute(
            "INSERT INTO ad_metrics (id, campaign_id, date, impressions, clicks, conversions) "
            "VALUES (30, 30, '2026-10-18', 10, 1, 0)"
        )
        con.execute("INSERT INTO prospects (id, email) VALUES (30, 'real@example.com')")
        con.execute("INSERT INTO email_campaigns (id, name, status) VALUES (30, '[drill] email', 'sent')")
        con.execute(
            "INSERT INTO email_logs (id, prospect_id, campaign_id, status) VALUES (30, 30, 30, 'sent')"
        )
        con.commit()
    finally:
        con.close()

    result = _run(db_path, "--apply", "--report")
    assert result.returncode == 0, result.stderr or result.stdout
    assert "APPLIED — deleted 0 row(s)" in result.stdout
    assert _count(result.stdout, "TOTAL") == 0

    snapshot = _snapshot(db_path)
    for table in ("tasks", "task_attachments", "ad_campaigns", "ad_metrics",
                  "email_campaigns", "email_logs", "prospects"):
        assert snapshot[table] == 1, f"{table}: a tagged parent with a child must be residual"
    assert _dangling(db_path) == []
    # The three residual lines each count one blocked parent.
    residual_block = result.stdout.split("residual (NOT deleted", 1)[1]
    for label in ("tagged tasks with attachments/chat/support rows",
                  "tagged ad_campaigns with ad_metrics children",
                  "tagged email_campaigns with email_logs children"):
        assert any(label in line and line.strip().endswith("1") for line in residual_block.splitlines()), (
            f"{label} must report 1 residual row:\n{result.stdout}"
        )


def test_an_unproven_database_is_refused_and_nothing_is_created(tmp_path) -> None:
    """An empty file is not a Polsia database: exit 2, and the script writes nothing to it."""
    db = tmp_path / "not_polsia.sqlite"
    sqlite3.connect(db.as_posix()).close()

    result = _run(db)
    assert result.returncode == 2, result.stdout
    assert "REFUSED" in result.stdout and "cannot prove" in result.stdout
    con = sqlite3.connect(db.as_posix())
    try:
        assert con.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    finally:
        con.close()


def test_the_target_is_masked_and_a_remote_postgres_host_needs_an_explicit_flag() -> None:
    """A deleting script must show WHERE it points without printing a credential."""
    sys.path.insert(0, str(BACKEND))
    from scripts.cleanup_drill_data import Refusal, _check_remote, _masked

    masked = _masked("postgresql+asyncpg://owner:hunter2@db.example.com:5432/polsia")
    assert "hunter2" not in masked and "owner" not in masked
    assert "db.example.com" in masked

    with pytest.raises(Refusal):
        _check_remote("postgresql+asyncpg://u:p@db.example.com/polsia", allow_remote=False)
    # Local names and an explicit opt-in are allowed.
    _check_remote("postgresql+asyncpg://u:p@localhost/polsia", allow_remote=False)
    _check_remote("postgresql+asyncpg://u:p@db.example.com/polsia", allow_remote=True)
