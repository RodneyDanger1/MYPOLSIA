"""PHASE-3 fixer-2 / L2-08 — the run-artifact dedupes' natural keys are REAL.

RED before the fix (lane-2 `probe_lane2.py` part E + research-B `probe_dedupe_race.py`):
two sessions, one `tweet_id` -> TWO rows, `index_list(social_posts)` empty,
`channel_sends_today('twitter') == 2` for one logical send.

This file pins, per writer and per layer:
* the two-session race now ends in ONE row and the raced write is `duplicates_skipped`
  (the R33 savepoint-rescue shape);
* migration 0005 pre-cleans legacy duplicates (MIN(id) kept), creates the partial uniques,
  and its DDL is byte-equal to the ORM's `create_all` output;
* `downgrade()` drops exactly the two indexes.
"""
from __future__ import annotations

import pathlib
import sqlite3

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.outreach_service as outreach
import app.services.social_service as social
from alembic import command
from alembic import config as alembic_config
from app.core.database import Base
from app.services.channel_service import channel_sends_today

BACKEND = pathlib.Path(__file__).resolve().parents[2]


async def _file_factory(tmp_path, name: str):
    url = f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}"
    engine = create_async_engine(url, connect_args={"timeout": 30.0})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


# ── the races ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_social_race_now_ends_in_one_row(tmp_path):
    engine, factory = await _file_factory(tmp_path, "social_race.sqlite")
    try:
        result = {"posted": [{"platform": "twitter", "tweet_id": "T-RACE-1", "content": "hi"}]}
        s1, s2 = factory(), factory()

        r1 = await social.record_published_posts(s1, result)  # s1: check (miss) + insert
        assert (r1["posts_written"], r1["duplicates_skipped"]) == (1, 0)

        real_fetch = social.fetch_scalar
        fired = {"done": False}

        async def hooked_fetch(db, stmt, *, what):  # noqa: ANN001
            row = await real_fetch(db, stmt, what=what)
            if db is s2 and row is None and not fired["done"]:
                fired["done"] = True
                await s1.commit()  # s1's row lands AFTER s2's check saw nothing
            return row

        social.fetch_scalar = hooked_fetch
        try:
            r2 = await social.record_published_posts(s2, result)
        finally:
            social.fetch_scalar = real_fetch
        await s2.commit()

        assert fired["done"], "the hook never fired — the race was not exercised"
        assert r2["ok"] is True
        assert (r2["posts_written"], r2["duplicates_skipped"]) == (0, 1), (
            "the raced duplicate must be rescued by the savepoint and counted, not written"
        )
        assert await channel_sends_today(s2, "twitter") == 1
        rows = (await s2.execute(
            __import__("sqlalchemy").text("SELECT COUNT(*) FROM social_posts")
        )).scalar()
        assert rows == 1
        await s2.close()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_email_race_now_ends_in_one_row(tmp_path):
    engine, factory = await _file_factory(tmp_path, "email_race.sqlite")
    try:
        item = {
            "prospect_email": "raced@example.com",
            "subject": "s",
            "body": "b",
            "status": "sent",
            "sendgrid_id": "sg-race-1",
        }
        s1, s2 = factory(), factory()

        r1 = await outreach.record_sent_emails(s1, {"emails": [item]})
        assert (r1["emails_written"], r1["duplicates_skipped"]) == (1, 0)

        real_fetch = outreach.fetch_scalar
        fired = {"done": False}

        async def hooked_fetch(db, stmt, *, what):  # noqa: ANN001
            row = await real_fetch(db, stmt, what=what)
            if db is s2 and row is None and not fired["done"]:
                fired["done"] = True
                await s1.commit()  # s1's log lands AFTER s2's dedupe check saw nothing
            return row

        outreach.fetch_scalar = hooked_fetch
        try:
            r2 = await outreach.record_sent_emails(s2, {"emails": [item]})
        finally:
            outreach.fetch_scalar = real_fetch
        await s2.commit()

        assert fired["done"], "the hook never fired — the race was not exercised"
        assert r2["ok"] is True
        assert (r2["emails_written"], r2["duplicates_skipped"]) == (0, 1)
        assert await channel_sends_today(s2, "email") == 1
        logs = (await s2.execute(
            __import__("sqlalchemy").text("SELECT COUNT(*) FROM email_logs")
        )).scalar()
        assert logs == 1
        await s2.close()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_sequential_reruns_are_still_duplicates_skipped(tmp_path):
    """The normal (non-raced) retry path is unchanged: the SELECT fast path skips."""
    engine, factory = await _file_factory(tmp_path, "rerun.sqlite")
    try:
        result = {"posted": [{"platform": "linkedin", "tweet_id": "L-1", "content": "x"}]}
        async with factory() as db:
            first = await social.record_published_posts(db, result)
            second = await social.record_published_posts(db, result)
            await db.commit()
        assert (first["posts_written"], first["duplicates_skipped"]) == (1, 0)
        assert (second["posts_written"], second["duplicates_skipped"]) == (0, 1)
    finally:
        await engine.dispose()


# ── migration 0005 ───────────────────────────────────────────────────────────


def _snapshot(db_path: pathlib.Path):
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


def _seed_legacy_duplicates(db_path: pathlib.Path) -> None:
    con = sqlite3.connect(db_path.as_posix())
    try:
        con.execute("INSERT INTO prospects (id, email) VALUES (1, 'a@example.com')")
        con.execute(
            "INSERT INTO social_posts (id, platform, content, status, tweet_id) "
            "VALUES (1, 'twitter', 'dup-oldest', 'published', 'T-DUP')"
        )
        con.execute(
            "INSERT INTO social_posts (id, platform, content, status, tweet_id) "
            "VALUES (2, 'twitter', 'dup-newer', 'published', 'T-DUP')"
        )
        con.execute(
            "INSERT INTO social_posts (id, platform, content, status, tweet_id) "
            "VALUES (3, 'twitter', 'legacy-null-id', 'draft', NULL)"
        )
        for row_id in (1, 2):
            con.execute(
                "INSERT INTO email_logs (id, prospect_id, sendgrid_id, status) "
                f"VALUES ({row_id}, 1, 'sg-DUP', 'sent')"
            )
        con.execute(
            "INSERT INTO email_logs (id, prospect_id, sendgrid_id, status) "
            "VALUES (3, 1, NULL, 'draft')"
        )
        con.commit()
    finally:
        con.close()


def test_0005_precleans_duplicates_creates_keys_and_downgrades_cleanly(monkeypatch, tmp_path):
    from app.config import settings

    db_path = tmp_path / "r2_0005.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)

    command.upgrade(cfg, "0004")
    _seed_legacy_duplicates(db_path)

    # W1/T03+T19: scoped to THIS revision explicitly (0006 exists now, and a round trip about
    # 0005's own rows must not silently include 0006's moves — the same reason the 0004 round-trip
    # in tests/unit/test_support_inbox_migration.py names its revision).  Old -> new:
    # `command.upgrade(cfg, "head")` -> `command.upgrade(cfg, "0005")`; the version pin below stays
    # "0005" and the downgrade-to-0004 leg stays exactly as shipped.  0006's own round trip lives
    # in tests/unit/test_t03_rejected_status.py.
    command.upgrade(cfg, "0005")
    head = _snapshot(db_path)
    assert ("index", "uq_social_posts_platform_tweet_id") in head
    assert ("index", "uq_email_logs_sendgrid_id") in head
    assert "WHERE tweet_id IS NOT NULL" in head[("index", "uq_social_posts_platform_tweet_id")]
    assert "WHERE sendgrid_id IS NOT NULL" in head[("index", "uq_email_logs_sendgrid_id")]

    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT version_num FROM alembic_version").fetchone()[0] == "0005"
        # pre-clean: MIN(id) survives per natural key; the NULL-key legacy row is untouched
        assert con.execute(
            "SELECT id, content FROM social_posts WHERE tweet_id = 'T-DUP'"
        ).fetchall() == [(1, "dup-oldest")]
        assert con.execute(
            "SELECT COUNT(*) FROM social_posts WHERE tweet_id IS NULL"
        ).fetchone()[0] == 1
        assert con.execute(
            "SELECT id FROM email_logs WHERE sendgrid_id = 'sg-DUP'"
        ).fetchall() == [(1,)]
        assert con.execute(
            "SELECT COUNT(*) FROM email_logs WHERE sendgrid_id IS NULL"
        ).fetchone()[0] == 1
        # the keys are real: a duplicate write is refused at the DB level now
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO social_posts (platform, content, status, tweet_id) "
                "VALUES ('twitter', 'again', 'published', 'T-DUP')"
            )
    finally:
        con.close()

    command.downgrade(cfg, "0004")
    after_down = _snapshot(db_path)
    assert ("index", "uq_social_posts_platform_tweet_id") not in after_down
    assert ("index", "uq_email_logs_sendgrid_id") not in after_down
    # rows survive the downgrade (only the pre-clean removed anything, and it is not reversible)
    con = sqlite3.connect(db_path.as_posix())
    try:
        assert con.execute("SELECT COUNT(*) FROM social_posts").fetchone()[0] == 2
        assert con.execute("SELECT COUNT(*) FROM email_logs").fetchone()[0] == 2
    finally:
        con.close()


def test_migrated_and_create_all_schemas_agree_on_the_natural_key_ddl(monkeypatch, tmp_path):
    """The stored index DDL is byte-equal between migration 0005 and the ORM create_all."""
    from sqlalchemy import create_engine

    from app.config import settings

    orm_path = tmp_path / "orm_ddl.sqlite"
    engine = create_engine(f"sqlite:///{orm_path.as_posix()}")
    Base.metadata.create_all(engine)
    engine.dispose()

    db_path = tmp_path / "mig_ddl.sqlite"
    url = f"sqlite+aiosqlite:///{db_path.as_posix()}"
    monkeypatch.setattr(settings, "database_url", url)
    cfg = alembic_config.Config()
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    orm = _snapshot(orm_path)
    migrated = _snapshot(db_path)
    for index in (
        "uq_social_posts_platform_tweet_id",
        "uq_email_logs_sendgrid_id",
        "ux_agent_runs_task_attempt",
    ):
        assert ("index", index) in orm, f"{index} missing from create_all"
        assert ("index", index) in migrated, f"{index} missing from the migrated schema"
        assert orm[("index", index)] == migrated[("index", index)], (
            f"{index}: ORM and migration DDL drifted"
        )
