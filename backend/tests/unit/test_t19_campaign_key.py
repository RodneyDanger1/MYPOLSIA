"""T19/D18 — the ad-campaign identity key: a PARTIAL UNIQUE (platform, external_id) at rest, and
the savepoint + re-resolve rescue for the raced insert.

Premise (the shipped gap, read off the code): `ads_service._find_existing_campaign` documents that
`ad_campaigns` carried NO unique index — "this SELECT-first read IS the guard — there is no
constraint to catch" — so two overlapping writers (Celery `--concurrency=4`; a beat run and a
manual trigger) both miss the SELECT and both insert, and every later run then reads an AMBIGUOUS
campaign for a metric.  Migration 0006 closes it with the natural key (`uq_ad_campaigns_platform_
external_id`, partial: `WHERE external_id IS NOT NULL`) plus the same savepoint+re-resolve shape
the metrics path already ships.

This file pins:
* the key is REAL at rest (a direct duplicate is refused by the database) and the legs with NO
  provider id stay legal (the index is partial by design — identity for those is a human-edited
  name, which must stay renameable);
* the two-session race ends in ONE row with the raced write counted as `duplicates_skipped`
  (RED before the rescue: the second run reports the campaign as a `skipped_malformed` item);
* the ordinary sequential rerun is unchanged (`duplicates_skipped`, one row).
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.ads_service as ads
from app.core.errors import E_VALIDATION, PolsiaError

INDEX_NAME = "uq_ad_campaigns_platform_external_id"


async def _file_factory(tmp_path, name: str):
    """File-backed SQLite so two sessions are two REAL connections (the L2-08 harness shape)."""
    import app.models  # noqa: F401  — register every table before create_all
    from app.core.database import Base

    url = f"sqlite+aiosqlite:///{(tmp_path / name).as_posix()}"
    engine = create_async_engine(url, connect_args={"timeout": 30.0})
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


def _campaign(external_id: str | None = "g-1", name: str = "Race Camp") -> dict:
    item: dict = {"platform": "google_ads", "name": name}
    if external_id is not None:
        item["external_id"] = external_id
    return item


@pytest.mark.asyncio
async def test_the_partial_unique_is_real_and_the_no_provider_id_legs_stay_legal(tmp_path):
    engine, factory = await _file_factory(tmp_path, "ads_key.sqlite")
    try:
        async with factory() as db:
            stored = (await db.execute(text(
                "SELECT sql FROM sqlite_master WHERE name = :n"
            ), {"n": INDEX_NAME})).scalar()
            assert stored is not None, "the identity index is missing from create_all"
            assert "UNIQUE" in stored and "WHERE external_id IS NOT NULL" in stored

            await ads.create_campaign(db, "google_ads", "First", external_id="g-9")
            await db.commit()
            with pytest.raises(PolsiaError) as excinfo:
                async with db.begin_nested():  # savepoint: keep the session usable after the refusal
                    await ads.create_campaign(db, "google_ads", "Second", external_id="g-9")
            assert excinfo.value.code == E_VALIDATION
            assert "constraint" in excinfo.value.message

            # ...while rows with NO provider id are deliberately NOT covered: their identity is the
            # (platform, name) pair a human edits, which the code-level dedupe handles and which a
            # unique index would make un-renameable.
            await ads.create_campaign(db, "google_ads", "No Id")
            await ads.create_campaign(db, "google_ads", "No Id")
            await db.commit()
            count = (await db.execute(text("SELECT COUNT(*) FROM ad_campaigns"))).scalar()
            assert count == 3
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_a_raced_campaign_insert_resolves_as_a_duplicate(tmp_path):
    """One reader misses, a competing writer's row lands, the insert collides: re-resolve counts it.

    The vehicle is the read hook — the competing writer's SHIPPED writer
    (`ads_service.create_campaign`, not raw SQL) runs at the exact race window: after this
    session's find-or-create returned "miss", before its insert executes.  Everything after
    that point is real: the collision is the migration-0006 index refusing the duplicate, and the
    rescue's re-read is the real `_find_existing_campaign` seeing the rival row.
    """
    engine, factory = await _file_factory(tmp_path, "ads_race.sqlite")
    try:
        payload = {"campaigns": [_campaign()], "metrics": []}
        reader = factory()
        rival = factory()

        real = ads._find_existing_campaign
        fired = {"done": False}

        async def hooked(db, platform, name, external_id):  # noqa: ANN001
            row = await real(db, platform, name, external_id)
            if db is reader and not fired["done"]:
                fired["done"] = True
                assert row is None, "the race window was not exercised (reader already saw a row)"
                # the competing writer finishes between the read above and the insert below
                await ads.create_campaign(rival, platform, name, external_id=external_id)
                await rival.commit()
            return row

        ads._find_existing_campaign = hooked
        try:
            report = await ads.record_ads_run(reader, payload)
        finally:
            ads._find_existing_campaign = real
        await reader.commit()

        assert fired["done"], "the hook never fired — the race was not exercised"
        assert report["ok"] is True, f"the raced insert must resolve, not skip: {report.get('error')}"
        assert (report["campaigns_written"], report["duplicates_skipped"]) == (0, 1), (
            "the raced duplicate must be rescued by the savepoint and counted"
        )
        count = (await reader.execute(text("SELECT COUNT(*) FROM ad_campaigns"))).scalar()
        assert count == 1, "one logical campaign, one row"
        await reader.close()
        await rival.close()
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_the_ordinary_sequential_rerun_is_still_duplicates_skipped(tmp_path):
    engine, factory = await _file_factory(tmp_path, "ads_rerun.sqlite")
    try:
        payload = {"campaigns": [_campaign("g-2", "Rerun")], "metrics": []}
        async with factory() as db:
            first = await ads.record_ads_run(db, payload)
            second = await ads.record_ads_run(db, payload)
            await db.commit()
        assert (first["campaigns_written"], first["duplicates_skipped"]) == (1, 0)
        assert (second["campaigns_written"], second["duplicates_skipped"]) == (0, 1)
    finally:
        await engine.dispose()
