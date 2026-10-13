"""Ad campaigns/metrics.

`list_metrics` for a campaign that does not exist used to return `[]` — the caller
cannot tell "this campaign has no metrics yet" from "there is no such campaign".
It now asks the cheap existence question first and raises `E_NOT_FOUND`.

Read ordering is deterministic (`date DESC, id DESC` for metrics; `id DESC` for
campaigns, which have no per-day series) and every read failure is `E_DB`.
`AdMetric.spend_usd` is `Numeric(14, 4)` (alembic `0003_money_exact`); money that must
be compared rather than merely displayed is handled as `Decimal`
(`finance_service.money`) — the column itself is exact at rest now.

Writes refuse by name too.  `ck_ad_campaigns_status` (alembic `0002_queue_and_perf`) is a
PostgreSQL-only constraint — SQLite cannot add a CHECK to an existing table, and the migration
states the consequence (0002:297-299): the value sets are "asserted by the code paths that write
them".  `create_campaign` is that assertion, so both engines refuse the same input; and it stores
`platform`/`name` trimmed, because the read path filters on exactly what the writer stored.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_NOT_FOUND, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.ads import AdCampaign, AdMetric
from app.services.query_helpers import db_refusal, fetch_scalars

#: The value set `ck_ad_campaigns_status` enforces on PostgreSQL (alembic `0002:103-104`) and that
#: SQLite cannot carry; `create_campaign` asserts it so the two engines behave identically.
AD_CAMPAIGN_STATUSES: tuple[str, ...] = ("active", "paused", "ended", "draft")


async def list_campaigns(
    db: AsyncSession,
    platform: str | None = None,
    status: str | None = None,
    limit: int = 50,
) -> list[AdCampaign]:
    stmt = select(AdCampaign)
    if platform is not None:
        stmt = stmt.where(AdCampaign.platform == platform)
    if status is not None:
        stmt = stmt.where(AdCampaign.status == status)
    stmt = stmt.order_by(AdCampaign.id.desc()).limit(limit)
    return await fetch_scalars(db, stmt, what="ad campaigns")


async def create_campaign(
    db: AsyncSession, platform: str, name: str, **values: Any
) -> AdCampaign:
    clean_platform = (platform or "").strip()
    clean_name = (name or "").strip()
    if not clean_platform or not clean_name:
        raise PolsiaError(
            E_VALIDATION,
            "ad campaign requires a non-empty platform and name",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"platform": platform, "name": name},
        )
    status = values.get("status")
    if status is not None and status not in AD_CAMPAIGN_STATUSES:
        raise PolsiaError(
            E_VALIDATION,
            f"ad campaign status {status!r} is not one of "
            f"{', '.join(AD_CAMPAIGN_STATUSES)} (ck_ad_campaigns_status)",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"platform": platform, "name": name, "status": status},
        )
    campaign = AdCampaign(platform=clean_platform, name=clean_name, **values)
    db.add(campaign)
    try:
        await db.flush()
    except IntegrityError as exc:
        # A constraint said no (the PostgreSQL CHECK above is the known one; any future
        # unique/FK lands here too).  The caller sent values the schema cannot hold — that is
        # a validation answer, not an outage (`outreach_service.create_prospect` sets the same
        # precedent for its own unique key).
        raise PolsiaError(
            E_VALIDATION,
            f"ad campaign {clean_name!r} was rejected by a database constraint",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"platform": clean_platform, "name": clean_name, "status": status},
        ) from exc
    except SQLAlchemyError as exc:
        raise db_refusal("ad campaign insert", exc) from exc
    return campaign


async def get_campaign(db: AsyncSession, campaign_id: int) -> AdCampaign | None:
    """One campaign by id, or `None`.  An unreadable read is `E_DB`, like every read here.

    `one_or_none()` (not `fetch_one`, whose single-row select yields a `Row`, not the entity)
    keeps the return shape the callers see; `MultipleResultsFound` is a `SQLAlchemyError` and
    therefore refuses as `E_DB` rather than silently taking the first row.
    """
    try:
        result = await db.execute(select(AdCampaign).where(AdCampaign.id == campaign_id))
        return result.scalars().one_or_none()
    except SQLAlchemyError as exc:
        raise db_refusal("ad campaign", exc) from exc


async def list_metrics(db: AsyncSession, campaign_id: int, limit: int = 90) -> list[AdMetric]:
    """Per-day metrics, newest first.  Unknown campaign ⇒ `E_NOT_FOUND`."""
    if await get_campaign(db, campaign_id) is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"ad campaign {campaign_id} does not exist",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"campaign_id": campaign_id},
        )
    return await fetch_scalars(
        db,
        select(AdMetric)
        .where(AdMetric.campaign_id == campaign_id)
        .order_by(AdMetric.date.desc(), AdMetric.id.desc())
        .limit(limit),
        what="ad metrics",
    )


__all__ = ["create_campaign", "get_campaign", "list_campaigns", "list_metrics"]
