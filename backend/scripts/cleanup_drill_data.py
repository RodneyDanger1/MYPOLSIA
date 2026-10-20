#!/usr/bin/env python3
"""`backend/scripts/cleanup_drill_data.py` — tag-scoped cleanup of drill rows (F8).

WHY
    Drill waves leave real rows behind: the live `/ops` cockpit showed 255 runs and dozens of
    `[drill]` tasks, and every such task can own an `agent_runs` ledger row.  The UI then shows
    rehearsal data as if it were the owner's business.  This script removes EXACTLY the rows that
    carry the literal tag — nothing else.

WHAT IS DELETED (and what never is)
    Selection is by TAG, not by age, id or table contents: a row is a candidate only when one of
    its title/name-like columns contains `[drill]` or the whole word `drill` (case-insensitive).
    An allowlist names the table and the columns; there is no `DELETE ... WHERE 1=1` and no
    `TRUNCATE` anywhere.

        tasks                 title                  (+ agent_runs rows for those task ids)
        ad_campaigns          name                   (only if it has NO ad_metrics children)
        email_campaigns       name                   (only if it has NO email_logs children)
        memory_entries        title
        competitors           name
        social_posts          content
        support_inbox_items   subject
        prospects             email / company

    NEVER touched: `ad_metrics`, `email_logs` or any other CHILD row of a tagged parent (the
    parent is reported as residual instead), `task_attachments` and the `data/sites` directories
    (out of scope — reported as residual counts so an operator sees they exist), and any row whose
    title/name does not carry the tag.  A tagged task that owns attachments or chat/support rows
    is skipped and reported: deleting it would either strand a blob or delete rows that carry no
    tag themselves.

MODES
    (default)          DRY RUN — prints the target, the tag rule and the per-table counts.
    --report           same counts, explicitly a report; never deletes.
    --apply            deletes exactly the rows the plan counted, in ONE transaction.
    --apply --report   prints BEFORE and AFTER count blocks, deleting between them.

EXIT CODES
    0  success (including a dry run / a report that found 0 rows)
    2  REFUSAL — the target database could not be PROVEN (unreachable, not a Polsia DB, or a
       remote PostgreSQL host without `--allow-remote`), or a delete did not match its plan.

SAFETY
    The resolved `DATABASE_URL` is printed on every run with credentials stripped, alongside the
    exact tag rule, so an operator can see WHERE the script points before trusting a count.  A
    non-local PostgreSQL host is refused unless `--allow-remote` is passed explicitly.

USAGE
    python backend/scripts/cleanup_drill_data.py            # dry run (host, from the repo root)
    python scripts/cleanup_drill_data.py                    # dry run (inside the backend image)
    python scripts/cleanup_drill_data.py --report           # counts only
    python scripts/cleanup_drill_data.py --apply --report   # before/after, then delete
    python scripts/cleanup_drill_data.py --database-url sqlite+aiosqlite:///./polsia.db
"""
from __future__ import annotations

import argparse
import asyncio
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND = REPO_ROOT
if str(BACKEND) not in sys.path:  # `app` lives beside this script (backend/app)
    sys.path.insert(0, str(BACKEND))

#: The literal tag.  `[drill]` anywhere, or the whole word `drill` — never `drilling`, and never
#: a stem match inside another word.  Applied in Python AFTER a cheap SQL prefilter so both
#: engines agree (SQLite's LIKE cannot express a word boundary; PostgreSQL's regex could, and
#: then the two dialects would select different rows).
TAG_RE = re.compile(r"\[drill\]|\bdrill\b", re.IGNORECASE)
SQL_TAG = "%drill%"

#: PostgreSQL hosts considered local.  `postgres`/`db` are the docker-compose service names; the
#: loopback names cover a host-run Postgres.  Anything else needs `--allow-remote`.
LOCAL_PG_HOSTS = {"localhost", "127.0.0.1", "::1", "postgres", "db", ""}


@dataclass(frozen=True)
class TaggedTable:
    """One allowlisted table and the title/name-like columns the tag may appear in."""

    table: str
    columns: tuple[str, ...]


#: The whole allowlist.  Order is the deletion order (a parent only after its dependents).
TAGGED_TABLES: tuple[TaggedTable, ...] = (
    TaggedTable("agent_runs", ()),  # handled via the selected task ids, never by tag alone
    TaggedTable("tasks", ("title",)),
    TaggedTable("ad_campaigns", ("name",)),
    TaggedTable("email_campaigns", ("name",)),
    TaggedTable("memory_entries", ("title",)),
    TaggedTable("competitors", ("name",)),
    TaggedTable("social_posts", ("content",)),
    TaggedTable("support_inbox_items", ("subject",)),
    TaggedTable("prospects", ("email", "company")),
)

REPORT_ORDER: tuple[str, ...] = (
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


class Refusal(Exception):
    """A named refusal — printed, exit 2, and never followed by a write."""


# ── target resolution / proof ────────────────────────────────────────────────────────────────


def _normalize_url(url: str) -> str:
    """A SQLAlchemy async URL: bare `postgresql://` / `sqlite:///` gain their async driver."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    scheme = {"postgres": "postgresql", "sqlite3": "sqlite"}.get(scheme, scheme)
    if "+" not in scheme:
        scheme = {
            "postgresql": "postgresql+asyncpg",
            "sqlite": "sqlite+aiosqlite",
        }.get(scheme, scheme)
    return f"{scheme}://{rest}"


def _masked(url: str) -> str:
    """The URL with any userinfo replaced by `***:***` — never print a credential."""
    parts = urlsplit(url)
    if not parts.netloc:
        # sqlite URLs are all path: keep the three-slash form `sqlite+aiosqlite:///C:/...`.
        return f"{parts.scheme}://{parts.path}"
    if "@" not in parts.netloc:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, parts.query, parts.fragment))
    host = parts.netloc.rsplit("@", 1)[1]
    return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, parts.query, parts.fragment))


def _resolve_url(explicit: str | None) -> str:
    if explicit:
        return _normalize_url(explicit.strip())
    import os

    env_url = (os.environ.get("DATABASE_URL") or "").strip()
    if env_url:
        return _normalize_url(env_url)
    from app.config import settings  # the app's own resolved configuration

    return _normalize_url(settings.database_url)


def _check_remote(url: str, allow_remote: bool) -> None:
    parts = urlsplit(url)
    if not parts.scheme.startswith("postgresql"):
        return
    host = (parts.hostname or "").lower()
    if allow_remote or host in LOCAL_PG_HOSTS:
        return
    raise Refusal(
        f"refusing a REMOTE PostgreSQL target (host {host!r}); this script deletes rows. "
        "Pass --allow-remote only after confirming the host is the database you mean."
    )


def _tag_rule_text() -> str:
    return (
        "tag rule: a row is selected only when a title/name-like column contains '[drill]' or "
        "the whole word 'drill' (case-insensitive); tables: "
        + ", ".join(f"{spec.table}({', '.join(spec.columns)})" for spec in TAGGED_TABLES if spec.columns)
    )


async def _prove(conn: Any, url: str) -> str:
    """Prove this is a Polsia database and return its alembic head.  Refuses otherwise."""
    from sqlalchemy import text

    try:
        head = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    except Exception as exc:  # noqa: BLE001 — any failure is "cannot prove"
        raise Refusal(
            "cannot prove the target is a Polsia database: the alembic_version table is absent "
            f"or unreadable ({type(exc).__name__}: {exc})"
        ) from exc
    if not head:
        raise Refusal("cannot prove the target is a Polsia database: alembic_version is empty")
    try:
        await conn.execute(text("SELECT COUNT(*) FROM tasks"))
    except Exception as exc:  # noqa: BLE001
        raise Refusal(
            f"cannot prove the target is a Polsia database: no `tasks` table ({type(exc).__name__})"
        ) from exc
    return str(head)


# ── selection ────────────────────────────────────────────────────────────────────────────────


async def _tagged_ids(conn: Any, spec: TaggedTable) -> set[int]:
    """Ids whose allowlisted columns carry the tag (SQL prefilter, Python word-boundary verify)."""
    from sqlalchemy import text

    if not spec.columns:
        return set()
    where = " OR ".join(f"LOWER({column}) LIKE :pattern" for column in spec.columns)
    columns = ", ".join(("id", *spec.columns))
    rows = (await conn.execute(
        text(f"SELECT {columns} FROM {spec.table} WHERE {where}"), {"pattern": SQL_TAG}
    )).fetchall()
    selected: set[int] = set()
    for row in rows:
        values = row[1:]
        if any(isinstance(value, str) and TAG_RE.search(value) for value in values):
            selected.add(int(row[0]))
    return selected


async def _ids(conn: Any, sql: str, params: dict[str, Any] | None = None) -> set[int]:
    from sqlalchemy import text

    rows = (await conn.execute(text(sql), params or {})).fetchall()
    return {int(row[0]) for row in rows}


async def _plan(conn: Any) -> dict[str, Any]:
    """The full plan: deletable ids per table, plus the residual rows this run will NOT touch."""
    tasks = await _tagged_ids(conn, TaggedTable("tasks", ("title",)))
    # A tagged task is deletable only when it owns nothing out of scope.
    blocked_tasks: set[int] = set()
    for sql in (
        "SELECT DISTINCT task_id FROM task_attachments WHERE task_id IS NOT NULL",
        "SELECT DISTINCT task_id FROM chat_messages WHERE task_id IS NOT NULL",
        "SELECT DISTINCT task_id FROM support_inbox_items WHERE task_id IS NOT NULL",
    ):
        blocked_tasks |= await _ids(conn, sql)
    residual_tasks = tasks & blocked_tasks
    tasks -= blocked_tasks

    agent_runs: set[int] = set()
    if tasks:
        placeholders = ", ".join(f":id{i}" for i in range(len(tasks)))
        agent_runs = await _ids(
            conn,
            f"SELECT id FROM agent_runs WHERE task_id IN ({placeholders})",
            {f"id{i}": value for i, value in enumerate(sorted(tasks))},
        )

    ad_campaigns = await _tagged_ids(conn, TaggedTable("ad_campaigns", ("name",)))
    ad_children = await _ids(conn, "SELECT DISTINCT campaign_id FROM ad_metrics")
    residual_campaigns = ad_campaigns & ad_children
    ad_campaigns -= ad_children

    email_campaigns = await _tagged_ids(conn, TaggedTable("email_campaigns", ("name",)))
    email_children = await _ids(
        conn, "SELECT DISTINCT campaign_id FROM email_logs WHERE campaign_id IS NOT NULL"
    )
    residual_email_campaigns = email_campaigns & email_children
    email_campaigns -= email_children

    others = {
        spec.table: await _tagged_ids(conn, spec)
        for spec in TAGGED_TABLES
        if spec.table not in {"tasks", "agent_runs", "ad_campaigns", "email_campaigns"}
    }

    return {
        "ids": {
            "tasks": tasks,
            "agent_runs": agent_runs,
            "ad_campaigns": ad_campaigns,
            "email_campaigns": email_campaigns,
            **others,
        },
        "residual": {
            "tasks": residual_tasks,
            "ad_campaigns": residual_campaigns,
            "email_campaigns": residual_email_campaigns,
            "site_dirs": _residual_site_dirs(),
        },
    }


def _residual_site_dirs() -> int:
    """Drill-named site directories under `backend/data/sites` — counted, NEVER deleted."""
    root = BACKEND / "data" / "sites"
    if not root.is_dir():
        return 0
    return sum(1 for child in root.iterdir() if child.is_dir() and TAG_RE.search(child.name))


def _counts(plan: dict[str, Any]) -> dict[str, int]:
    return {table: len(plan["ids"].get(table, ())) for table in REPORT_ORDER}


# ── printing ─────────────────────────────────────────────────────────────────────────────────


def _print_counts(title: str, counts: dict[str, int]) -> None:
    print(title)
    for table in REPORT_ORDER:
        print(f"{table:<28} {counts[table]:>6}")
    print(f"{'TOTAL':<28} {sum(counts.values()):>6}")


def _print_residuals(plan: dict[str, Any]) -> None:
    residual = plan["residual"]
    print("residual (NOT deleted — inspect and remove by hand if intended):")
    print(f"  {'tagged tasks with attachments/chat/support rows':<58} {len(residual['tasks']):>5}")
    print(f"  {'tagged ad_campaigns with ad_metrics children':<58} {len(residual['ad_campaigns']):>5}")
    print(f"  {'tagged email_campaigns with email_logs children':<58} {len(residual['email_campaigns']):>5}")
    print(f"  {'drill-named site directories (disk, out of scope)':<58} {residual['site_dirs']:>5}")


# ── deletion ─────────────────────────────────────────────────────────────────────────────────


async def _delete(conn: Any, plan: dict[str, Any]) -> int:
    """Delete exactly the planned ids in ONE transaction; a mismatch aborts the whole thing."""
    from sqlalchemy import text

    deleted = 0
    ids = plan["ids"]

    def statement(table: str, values: set[int]) -> tuple[str, dict[str, Any]]:
        params = {f"id{i}": value for i, value in enumerate(sorted(values))}
        placeholders = ", ".join(f":id{i}" for i in range(len(values)))
        column = "task_id" if table == "agent_runs" else "id"
        return f"DELETE FROM {table} WHERE {column} IN ({placeholders})", params

    if ids["agent_runs"]:
        sql, params = statement("agent_runs", ids["agent_runs"])
        deleted += (await conn.execute(text(sql), params)).rowcount
    for table in ("tasks", "ad_campaigns", "email_campaigns", "memory_entries", "competitors",
                  "social_posts", "support_inbox_items", "prospects"):
        values = ids.get(table, set())
        if not values:
            continue
        sql, params = statement(table, values)
        deleted += (await conn.execute(text(sql), params)).rowcount

    planned = sum(_counts(plan).values())
    if deleted != planned:
        raise Refusal(
            f"delete plan mismatch: planned {planned} row(s), deleted {deleted} — rolled back"
        )
    return deleted


# ── main ─────────────────────────────────────────────────────────────────────────────────────


async def _run(args: argparse.Namespace) -> int:
    url = _resolve_url(args.database_url)
    print(f"target DB: {_masked(url)}")
    print(_tag_rule_text())
    print(f"mode: {'APPLY' if args.apply else 'DRY RUN (default; pass --apply to delete)'}")
    _check_remote(url, args.allow_remote)

    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(url)
    try:
        async with engine.connect() as conn:
            head = await _prove(conn, url)
            print(f"proven: alembic_version head = {head}")
            plan = await _plan(conn)

        if args.report:
            _print_counts("BEFORE" if args.apply else "REPORT", _counts(plan))
            _print_residuals(plan)
        elif not args.apply:
            _print_counts("DRY RUN — counts that WOULD be deleted", _counts(plan))
            _print_residuals(plan)

        if not args.apply:
            print("DRY RUN — nothing was deleted. Pass --apply to delete exactly these rows.")
            return 0

        async with engine.begin() as conn:
            deleted = await _delete(conn, plan)
        print(f"APPLIED — deleted {deleted} row(s) in one transaction")

        if args.report:
            async with engine.connect() as conn:
                after = await _plan(conn)
            _print_counts("AFTER", _counts(after))
        return 0
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Delete ONLY drill-tagged rows (dry run by default).",
        epilog="Exit 0 = success; 2 = refusal (unproven target or a delete/plan mismatch).",
    )
    parser.add_argument("--apply", action="store_true", help="perform the deletes (default: dry run)")
    parser.add_argument("--report", action="store_true", help="print per-table counts, never delete")
    parser.add_argument("--database-url", default=None, help="override DATABASE_URL")
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="permit a non-local PostgreSQL host (refused otherwise)",
    )
    args = parser.parse_args(argv)
    try:
        return asyncio.run(_run(args))
    except Refusal as refusal:
        print(f"REFUSED: {refusal}", file=sys.stderr)
        print(f"REFUSED: {refusal}", file=sys.stdout)
        return 2
    except Exception as exc:  # noqa: BLE001 — an unprovable/unsafe target is a refusal, not a crash
        message = f"cannot use the target database ({type(exc).__name__}: {exc})"
        print(f"REFUSED: {message}", file=sys.stderr)
        print(f"REFUSED: {message}", file=sys.stdout)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
