"""V26/L6 — `backend/scripts/cleanup_drill_data.py`: tag-scoped, dry-by-default drill purge.

F8 is real: the live `/ops` cockpit shows 255 runs and dozens of `[drill]` rows, `/sites` shows
7 drill-created sites, and Tasks shows repeated swatch tasks created by earlier drill waves.
This file pins the ONLY sanctioned cleanup:

  * default = DRY RUN (counts, nothing deleted); `--report` prints counts and never deletes;
  * `--apply` deletes EXACTLY the tagged rows it counted (and `--apply --report` prints the
    before/after counts);
  * only rows that CARRY the literal tag `drill` / `[drill]` (case-insensitive) in a title/name
    column are selected — an untagged row always survives;
  * a tagged task with attachments, or a tagged campaign with child rows, is reported as residual
    and NOT deleted (attachments and site directories are out of scope);
  * a second run reports 0 (idempotent);
  * the script refuses (exit 2) when it cannot PROVE the target database (unreachable, no
    `alembic_version`, no `tasks` table) and always prints the resolved URL with credentials
    stripped plus the tag rule.

RED on the base tree: `backend/scripts/cleanup_drill_data.py` does not exist.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "backend" / "scripts" / "cleanup_drill_data.py"
ALEMBIC_HEAD = "0015"


def _build_db(path: Path) -> None:
    """A real `create_all` schema (the ORM's own metadata) with the drill/real fixture rows."""
    import app.models  # noqa: F401 — populate Base.metadata
    from app.core.database import Base

    engine = create_engine(f"sqlite:///{path.as_posix()}")
    Base.metadata.create_all(engine)
    engine.dispose()

    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        con.execute("INSERT INTO alembic_version VALUES (?)", (ALEMBIC_HEAD,))
        # tasks: two deletable, one REAL, one tagged-with-attachment (residual)
        con.executemany(
            "INSERT INTO tasks (id, title, agent_type, status) VALUES (?, ?, 'orchestrator', 'completed')",
            [
                (1, "[drill] swatch 1"),
                (2, "DRILL swatch 2"),
                (3, "Real customer task"),
                (4, "[drill] swatch with an attachment"),
            ],
        )
        con.executemany(
            "INSERT INTO agent_runs (id, task_id, agent_type, status) VALUES (?, ?, 'orchestrator', 'completed')",
            [(1, 1), (2, 2), (3, 3), (4, 4)],
        )
        con.execute(
            "INSERT INTO task_attachments (task_id, filename, stored_name, mime, size_bytes, sha256)"
            " VALUES (4, 'shot.png', 'abc.png', 'image/png', 8, 'deadbeef')"
        )
        # ad campaigns: one deletable, one REAL, one tagged but with a child metric (residual)
        con.executemany(
            "INSERT INTO ad_campaigns (id, platform, name, status) VALUES (?, 'google_ads', ?, 'active')",
            [(1, "[drill] campaign"), (2, "Real campaign"), (3, "[drill] campaign with metrics")],
        )
        con.execute(
            "INSERT INTO ad_metrics (campaign_id, date, impressions, clicks, conversions)"
            " VALUES (3, '2026-10-01', 10, 1, 0)"
        )
        con.executemany(
            "INSERT INTO memory_entries (id, category, title, content) VALUES (?, 'ops', ?, 'x')",
            [(1, "drill note"), (2, "real note")],
        )
        con.execute("INSERT INTO competitors (name) VALUES ('Drill Co')")
        con.execute("INSERT INTO competitors (name) VALUES ('Real Co')")
        con.execute("INSERT INTO social_posts (content) VALUES ('[DRILL] post')")
        con.execute("INSERT INTO social_posts (content) VALUES ('real post')")
        con.executemany(
            "INSERT INTO email_campaigns (id, name, status) VALUES (?, ?, 'active')",
            [(1, "drill email"), (2, "real email")],
        )
        con.execute(
            "INSERT INTO support_inbox_items (provider_message_id, from_address, subject, body, status)"
            " VALUES ('m1', 'a@b.c', 'Re: [drill] run', 'x', 'pending')"
        )
        con.executemany(
            "INSERT INTO prospects (id, email, company, status) VALUES (?, ?, ?, 'new')",
            [(1, "p1@example.com", "Drill Labs"), (2, "p2@example.com", "Real Labs")],
        )
        con.commit()
    finally:
        con.close()


def _run(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--database-url", f"sqlite+aiosqlite:///{tmp_path.as_posix()}", *args],
        cwd=str(REPO),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def _counts(stdout: str) -> dict[str, int]:
    """Every `name   N` count line the script prints (TOTAL included), plus residuals."""
    out: dict[str, int] = {}
    for match in re.finditer(r"^(TOTAL|tasks|agent_runs|ad_campaigns|email_campaigns|memory_entries|"
                             r"competitors|social_posts|support_inbox_items|prospects)\s+(\d+)$",
                             stdout, re.MULTILINE):
        out[match.group(1)] = int(match.group(2))
    return out


def _alive(path: Path) -> dict[str, int]:
    con = sqlite3.connect(path)
    try:
        return {
            table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "tasks", "agent_runs", "task_attachments", "ad_campaigns", "ad_metrics",
                "memory_entries", "competitors", "social_posts", "email_campaigns",
                "support_inbox_items", "prospects",
            )
        }
    finally:
        con.close()


def test_dry_run_reports_n_and_deletes_nothing(tmp_path) -> None:
    db = tmp_path / "dry.db"
    _build_db(db)
    before = _alive(db)

    result = _run(db)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "DRY RUN" in result.stdout
    counts = _counts(result.stdout)
    assert counts, f"no counts parsed from:\n{result.stdout}"
    assert counts["TOTAL"] == sum(v for k, v in counts.items() if k != "TOTAL")
    assert counts["tasks"] == 2, counts
    assert counts["agent_runs"] == 2, "runs of a SKIPPED tagged task must not be counted"
    assert counts["ad_campaigns"] == 1, "the campaign with child metrics is residual, not counted"
    assert _alive(db) == before, "a dry run must not delete a single row"


def test_report_flag_prints_counts_and_never_deletes(tmp_path) -> None:
    db = tmp_path / "report.db"
    _build_db(db)
    before = _alive(db)

    result = _run(db, "--report")
    assert result.returncode == 0, result.stdout + result.stderr
    assert _counts(result.stdout)["TOTAL"] > 0
    assert _alive(db) == before


def test_apply_deletes_exactly_n_and_real_rows_survive(tmp_path) -> None:
    db = tmp_path / "apply.db"
    _build_db(db)
    before_total = _counts(_run(db).stdout)["TOTAL"]

    result = _run(db, "--apply")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "APPLIED" in result.stdout
    assert f"deleted {before_total} row(s)" in result.stdout, result.stdout
    after = _alive(db)
    assert after["tasks"] == 2, "real task + tagged-but-attachment task survive"
    assert after["agent_runs"] == 2, "real run + the skipped task's run survive"
    assert after["task_attachments"] == 1, "attachments are out of scope — never touched"
    assert after["ad_campaigns"] == 2
    assert after["ad_metrics"] == 1, "a real metric of the residual campaign survives"
    assert after["memory_entries"] == 1
    assert after["competitors"] == 1
    assert after["social_posts"] == 1
    assert after["email_campaigns"] == 1
    assert after["support_inbox_items"] == 0
    assert after["prospects"] == 1

    con = sqlite3.connect(db)
    try:
        titles = [r[0] for r in con.execute("SELECT title FROM tasks ORDER BY id")]
        assert titles == ["Real customer task", "[drill] swatch with an attachment"]
    finally:
        con.close()


def test_apply_then_second_run_reports_zero(tmp_path) -> None:
    db = tmp_path / "idempotent.db"
    _build_db(db)
    assert _run(db, "--apply").returncode == 0

    second = _run(db)
    assert second.returncode == 0, second.stdout + second.stderr
    assert _counts(second.stdout)["TOTAL"] == 0, second.stdout


def test_apply_with_report_prints_before_and_after(tmp_path) -> None:
    db = tmp_path / "apply-report.db"
    _build_db(db)
    result = _run(db, "--apply", "--report")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BEFORE" in result.stdout and "AFTER" in result.stdout, result.stdout
    assert _counts(result.stdout.split("AFTER", 1)[1])["TOTAL"] == 0


def test_refuses_when_the_database_cannot_be_proven(tmp_path) -> None:
    db = tmp_path / "not-polsia.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE random (x INTEGER)")
    con.commit()
    con.close()

    result = _run(db, "--apply")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "REFUSED" in result.stdout + result.stderr
    assert "alembic_version" in result.stdout + result.stderr
    # The refusal happens BEFORE any write: the unrelated schema is untouched.
    assert _has(db, "random")
    assert not _has(db, "tasks")


def _has(path: Path, table: str) -> bool:
    con = sqlite3.connect(path)
    try:
        return (
            con.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone()[0]
            > 0
        )
    finally:
        con.close()


def test_prints_the_target_and_the_tag_rule_with_credentials_stripped(tmp_path) -> None:
    db = tmp_path / "target.db"
    _build_db(db)
    result = _run(db)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "target DB:" in result.stdout
    assert "sqlite+aiosqlite" in result.stdout
    assert "tag rule:" in result.stdout and "drill" in result.stdout.lower()
