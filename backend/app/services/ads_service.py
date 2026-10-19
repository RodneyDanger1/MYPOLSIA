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

`record_ads_run` (R33) is the queue worker's writer for a completed `ads_management` run — the
seam that turns the run's `campaigns[]`/`metrics[]` payload into rows at all.  It is deliberately
NOT added to `__all__`: that list is the pinned four-name reader/writer surface
(`tests/unit/test_r26_ads_sweep.py::test_the_public_surface_is_exactly_the_four_functions`), and
the worker dispatch imports the writer by name — the same arrangement as
`task_service._as_column_decimal`, which is imported by name from `celery_app.tasks.agent_tasks`.

Consumer audit (V18/L1, closing V17/L3-08; DECIDED in V19/L2): `create_campaign` is NOT a dead
helper — it is the writer `record_ads_run` calls, and `record_ads_run` is imported by the queue
worker (`celery_app/tasks/agent_tasks.py`).  `get_campaign` serves `list_metrics`' existence
check.  `list_metrics` had no MODULE-EXTERNAL production importer (`api/v1/ads.py` read only
`list_campaigns`) while `record_ads_run` wrote real `ad_metrics` rows from every completed ads
run — the stored measurement had no product read.  V19/L2 DECIDED BY EVIDENCE: it is WIRED, not
deleted — `GET /api/v1/ads/campaigns/{campaign_id}/metrics` is that consumer
(`api/v1/ads.py::ads_campaign_metrics`).  The R33 persistence e2e reads the same helper.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_DB, E_NOT_FOUND, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.ads import AdCampaign, AdMetric
from app.services.query_helpers import db_refusal, fetch_scalar, fetch_scalars

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


# ── R33: the run-artifact writer (the worker's seam) ─────────────────────────


async def _find_existing_campaign(
    db: AsyncSession, platform: str, name: str, external_id: str | None
) -> AdCampaign | None:
    """The row this payload's campaign ALREADY is, or `None` — the idempotency read.

    The natural key is the provider's own id — rd14's discipline ("dedupe by provider ids where
    they exist"): `(platform, external_id)` when the payload reported one.  A campaign the
    provider gave no id for is keyed on `(platform, name)` — the pair `create_campaign` stores
    trimmed, so the read matches exactly what the writer wrote.  Since migration 0006 the
    provider-id half is ALSO a real constraint (`uq_ad_campaigns_platform_external_id`, partial
    unique), so this SELECT-first read is the fast path and the constraint is the backstop the
    race rescue in `record_ads_run` re-resolves against; the `(platform, name)` fallback remains
    a code-level dedupe (a unique index on a human-edited display name would refuse a rename).
    Several rows for one key (possible for the name-keyed fallback) resolve to the oldest — a
    re-run reuses one row rather than failing on a database that never promised uniqueness.
    """
    stmt = select(AdCampaign)
    if external_id is not None:
        stmt = stmt.where(
            AdCampaign.platform == platform, AdCampaign.external_id == external_id
        )
    else:
        stmt = stmt.where(AdCampaign.platform == platform, AdCampaign.name == name)
    rows = await fetch_scalars(
        db, stmt.order_by(AdCampaign.id).limit(1), what=f"ad campaign ({platform})"
    )
    return rows[0] if rows else None


async def _find_metric_id(db: AsyncSession, campaign_id: int, day: date) -> int | None:
    """The id of the `ad_metrics` row for this natural key, or `None` — the idempotency read.

    `(campaign_id, date)` is the key the SHIPPED schema already guards:
    `uq_ad_metrics_campaign_date` UNIQUE on exactly those columns (alembic `0002:157-158`,
    "the two idempotency uniques that were missing"; confirmed on the migration head by
    `PRAGMA index_list(ad_metrics)`).  The beat re-runs every 6h, so this read is what turns a
    repeated payload into a `duplicates_skipped` count instead of a second row.
    """
    return await fetch_scalar(
        db,
        select(AdMetric.id).where(AdMetric.campaign_id == campaign_id, AdMetric.date == day),
        what=f"ad metric ({campaign_id}, {day.isoformat()})",
    )


def _register_campaign(
    registry: dict[str, AdCampaign], ambiguous: set[str], external_id: str, campaign: AdCampaign
) -> None:
    """Remember which row this run's campaigns[] report for one provider id.

    `metrics[]` reference a campaign only by `campaign_external_id` (no platform), so a provider
    id claimed by TWO different rows is AMBIGUOUS — the metric cannot pick one honestly and is
    skipped by name later.  Same row twice (a duplicate campaigns[] entry) is not ambiguous.
    """
    known = registry.get(external_id)
    if known is None:
        registry[external_id] = campaign
    elif known.id != campaign.id:
        ambiguous.add(external_id)


async def record_ads_run(db: AsyncSession, result: dict[str, Any]) -> dict[str, Any]:
    """Persist what a finished `ads_management` run REALLY reported — the worker calls this.

    WHY this lives here: agents hold no DB session, and before R33 nothing in the tree ever called
    the `ad_campaigns`/`ad_metrics` writers — the run's `campaigns[]`/`metrics[]` payload (whose
    documented destination IS those two tables, `app/agents/ads_management/agent.py:6-7`) was
    discarded and both tables stayed empty.  The worker owns the session, so the rows are written
    on the caller's transaction (`flush` only; `_execute_agent_task` commits — one transaction
    for the run's artifacts, like every other branch).

    The payload is the schema's own dump (`app/schemas/agent_outputs.py:298-323`).  `campaigns[]`
    are written through `create_campaign` (its named refusals are caught as ITEM skips, under a
    savepoint so the batch survives); `metrics[]` become `AdMetric` rows whose raw inputs are the
    reported numbers — `ctr`/`cpc`/`roas` are GENERATED columns (`models/ads.py:85-87`) computed
    by the engine and deliberately never written.  Money crosses to its columns only through
    `task_service._as_column_decimal` (the A-T17 single boundary), imported lazily like
    `agent_tasks._apply_run_usage` does.

    IDEMPOTENCY (the beat re-runs every 6h): campaigns dedupe on the provider's own id
    (`platform`+`external_id`; `(platform, name)` fallback when no id was reported) and existing
    rows are REUSED, never updated; metrics dedupe on `(campaign_id, date)` — the key the shipped
    `uq_ad_metrics_campaign_date` unique index guards — with a savepoint + re-resolve rescue for
    the raced unique violation (two ads runs over the same day), because without the savepoint a
    failed flush would poison the session and lose the whole batch.

    Every unpersistable entry is skipped BY NAME in the report and the batch continues (the run
    stays alive); the FIRST skip is the report's `error` (ok=False), duplicates are NOT failures
    (a steady-state 6h beat produces them by design) and an empty payload is the honest zero:
    `{ok: True, 0, 0, 0}` — a report of measured zeros, never an error.  Never raises: any
    batch-fatal failure (a session that may be poisoned, a refused read) comes back in `error`.

    Returns `{"ok": bool, "campaigns_written": int, "metrics_written": int,
    "duplicates_skipped": int, "error": None | {"code", "message"}}`, plus
    `"skipped_malformed": [{kind, index, code, message, ...}]` only when something was skipped.
    """
    report: dict[str, Any] = {
        "ok": True,
        "campaigns_written": 0,
        "metrics_written": 0,
        "duplicates_skipped": 0,
        "error": None,
    }

    def skip(kind: str, index: int, code: str, message: str, **identifiers: Any) -> None:
        """Record one item-scoped skip (RD30 shape) — never silent, never batch-fatal."""
        report.setdefault("skipped_malformed", []).append(
            {"kind": kind, "index": index, "code": code, "message": message, **identifiers}
        )
        if report["ok"]:  # the FIRST skip names the report's error
            report["ok"] = False
            report["error"] = {"code": code, "message": message}

    try:
        # A-T17: money crosses to a column only through the one boundary in task_service (lazy
        # import — this module's import graph stays as it was).
        from app.services.task_service import _as_column_decimal

        by_external: dict[str, AdCampaign] = {}
        ambiguous_external: set[str] = set()

        for index, item in enumerate((result or {}).get("campaigns") or []):
            if not isinstance(item, dict):
                skip("campaign", index, E_VALIDATION, "campaign entry is not an object")
                continue
            platform = str(item.get("platform") or "").strip()
            name = str(item.get("name") or "").strip()
            if not platform or not name:
                skip(
                    "campaign", index, E_VALIDATION,
                    "campaign entry has no usable platform/name",
                    platform=item.get("platform"), name=item.get("name"),
                )
                continue
            raw_external = item.get("external_id")
            if raw_external is not None and not isinstance(raw_external, str):
                # T7: a provider ref must BE one.  Coercing an int into an id invents a value the
                # platform may not have issued ("100" vs "0100"), so it is refused by name.
                skip(
                    "campaign", index, E_VALIDATION,
                    f"campaign external_id {raw_external!r} is not a string",
                    platform=platform, name=name,
                )
                continue
            external_id = (raw_external or "").strip() or None

            existing = await _find_existing_campaign(db, platform, name, external_id)
            if existing is not None:
                report["duplicates_skipped"] += 1
                if external_id is not None:
                    _register_campaign(by_external, ambiguous_external, external_id, existing)
                continue

            values: dict[str, Any] = {}
            if external_id is not None:
                values["external_id"] = external_id
            if item.get("status") is not None:
                # create_campaign asserts ck_ad_campaigns_status (SQLite cannot carry the CHECK);
                # its refusal lands in the E_VALIDATION arm below, as an item skip.
                values["status"] = item["status"]
            bad_money = None
            for field in ("daily_budget_usd", "total_spent_usd"):
                value = item.get(field)
                if value is None:
                    continue  # not reported -> the column's own default; never fabricated
                try:
                    values[field] = _as_column_decimal(value)
                except (TypeError, ValueError, ArithmeticError):
                    bad_money = f"campaign {field}={value!r} is not numeric money"
                    break
            if bad_money is not None:
                skip("campaign", index, E_VALIDATION, bad_money, platform=platform, name=name)
                continue

            try:
                # Savepoint: create_campaign's constraint refusal is preceded by a failed flush,
                # and that flush would poison the whole run transaction without this scope.
                async with db.begin_nested():
                    created = await create_campaign(db, platform, name, **values)
            except PolsiaError as exc:
                if exc.code == E_VALIDATION:
                    # T19 race rescue — the same savepoint + re-resolve shape the metrics path
                    # below ships (441-460).  With migration 0006's partial unique
                    # `uq_ad_campaigns_platform_external_id` in place, a concurrent ads run can win
                    # this (platform, external_id) key between the find-or-create read above and
                    # this insert.  `create_campaign` converts the violated constraint into
                    # E_VALIDATION (84-94), so the rescue keys on that named refusal and re-resolves
                    # with the SAME reader the fast path used: the fast path missed a moment ago,
                    # so a row now present can only be the raced writer's — a duplicate, not a
                    # failure.  A real validation refusal (bad status, no race) re-resolves nothing
                    # and stays an item skip.
                    raced = (
                        await _find_existing_campaign(db, platform, name, external_id)
                        if external_id is not None
                        else None
                    )
                    if raced is not None and external_id is not None:
                        report["duplicates_skipped"] += 1
                        _register_campaign(by_external, ambiguous_external, external_id, raced)
                        continue
                    skip(
                        "campaign", index, exc.code, exc.message,
                        platform=platform, name=name,
                    )
                    continue
                raise  # E_DB and friends: a session that may be poisoned must not be driven on
            report["campaigns_written"] += 1
            if external_id is not None:
                _register_campaign(by_external, ambiguous_external, external_id, created)

        for index, item in enumerate((result or {}).get("metrics") or []):
            if not isinstance(item, dict):
                skip("metric", index, E_VALIDATION, "metric entry is not an object")
                continue
            external_ref = str(item.get("campaign_external_id") or "").strip()
            if not external_ref:
                skip(
                    "metric", index, E_VALIDATION,
                    "metric entry has no campaign_external_id",
                )
                continue

            campaign = by_external.get(external_ref)
            if external_ref in ambiguous_external:
                skip(
                    "metric", index, E_VALIDATION,
                    f"metric names campaign {external_ref!r}, which matches several "
                    "ad_campaigns rows; cannot pick one",
                    campaign_external_id=external_ref,
                )
                continue
            if campaign is None:
                candidates = await fetch_scalars(
                    db,
                    select(AdCampaign).where(AdCampaign.external_id == external_ref).limit(2),
                    what=f"ad campaigns (external_id={external_ref})",
                )
                if not candidates:
                    # A metric never invents its FK parent: a campaign row for this provider id
                    # was neither reported by this run nor written by an earlier one.
                    skip(
                        "metric", index, E_VALIDATION,
                        f"metric names campaign {external_ref!r}, which is not in this run's "
                        "campaigns[] and not in ad_campaigns",
                        campaign_external_id=external_ref,
                    )
                    continue
                if len(candidates) > 1:
                    skip(
                        "metric", index, E_VALIDATION,
                        f"metric names campaign {external_ref!r}, which matches several "
                        "ad_campaigns rows; cannot pick one",
                        campaign_external_id=external_ref,
                    )
                    continue
                campaign = candidates[0]
                by_external[external_ref] = campaign  # cache the resolution for this run

            raw_date = str(item.get("date") or "").strip()
            try:
                day = date.fromisoformat(raw_date)
            except ValueError:
                # Strict: a timestamp is not the DAY the natural key is built on, and truncating
                # one here would invent the as-of semantics the payload never stated.
                skip(
                    "metric", index, E_VALIDATION,
                    f"metric date {raw_date!r} is not an ISO date (YYYY-MM-DD)",
                    campaign_external_id=external_ref,
                )
                continue

            numbers: dict[str, int] = {}
            bad_number = None
            for field in ("impressions", "clicks", "conversions"):
                value = item.get(field)
                if not isinstance(value, int) or isinstance(value, bool):
                    bad_number = f"metric {field}={value!r} is not an integer"
                    break
                if value < 0:
                    bad_number = f"metric {field}={value!r} violates ck_ad_metrics_nonneg"
                    break
                numbers[field] = value
            if bad_number is None and numbers["clicks"] > numbers["impressions"]:
                bad_number = (
                    f"metric clicks={numbers['clicks']!r} > impressions="
                    f"{numbers['impressions']!r} violates ck_ad_metrics_clicks_le_impressions"
                )
            if bad_number is not None:
                # ck_ad_metrics_nonneg / ck_ad_metrics_clicks_le_impressions are PostgreSQL-only
                # CHECKs (0002:110-112; SQLite cannot carry them) — 0002:297-299 states the rule:
                # the code path that writes the rows asserts them, so both engines agree.
                skip(
                    "metric", index, E_VALIDATION, bad_number,
                    campaign_external_id=external_ref, date=raw_date,
                )
                continue

            money: dict[str, Any] = {}
            bad_money = None
            for field in ("spend_usd", "revenue_usd"):
                value = item.get(field)
                if value is None:
                    # Not reported -> the column's own default (0).  "Not reported" must never
                    # become a fabricated measurement, so nothing is written for it.
                    continue
                try:
                    converted = _as_column_decimal(value)
                except (TypeError, ValueError, ArithmeticError):
                    bad_money = f"metric {field}={value!r} is not numeric money"
                    break
                if converted is not None and converted < 0:
                    bad_money = f"metric {field}={value!r} violates ck_ad_metrics_nonneg"
                    break
                money[field] = converted
            if bad_money is not None:
                skip(
                    "metric", index, E_VALIDATION, bad_money,
                    campaign_external_id=external_ref, date=raw_date,
                )
                continue

            existing_id = await _find_metric_id(db, campaign.id, day)
            if existing_id is not None:
                report["duplicates_skipped"] += 1
                continue

            metric = AdMetric(campaign_id=campaign.id, date=day, **numbers, **money)
            try:
                # Savepoint: on the migrated schema (and PostgreSQL) the natural key is a REAL
                # unique index (uq_ad_metrics_campaign_date), so a concurrent ads run over the
                # same day can trip it between the read above and this flush.  Scoped rollback
                # keeps the batch alive; the re-resolve below decides what it actually was.
                async with db.begin_nested():
                    db.add(metric)
            except IntegrityError as exc:
                again = await _find_metric_id(db, campaign.id, day)
                if again is not None:
                    report["duplicates_skipped"] += 1  # the raced row is the natural key's row
                else:
                    skip(
                        "metric", index, E_DB,
                        f"ad_metrics insert was rejected by a database constraint: "
                        f"{getattr(exc, 'orig', exc)}",
                        campaign_external_id=external_ref, date=raw_date,
                    )
                continue
            except SQLAlchemyError as exc:
                raise db_refusal("ad metric insert", exc) from exc
            report["metrics_written"] += 1
    except PolsiaError as exc:
        report["ok"] = False
        report["error"] = {"code": exc.code, "message": exc.message}
    except Exception as exc:  # noqa: BLE001 — the worker contract is a report, never a raise
        refusal = PolsiaError(
            E_DB,
            f"could not persist ads run artifacts: {type(exc).__name__}: {exc}",
            how_to_fix=ERROR_HINTS[E_DB],
            context={
                "agent": "ads_management",
                "campaigns_written": report["campaigns_written"],
                "metrics_written": report["metrics_written"],
            },
        )
        report["ok"] = False
        report["error"] = {"code": refusal.code, "message": refusal.message}
    return report


# `record_ads_run` is deliberately NOT in this list — see the module docstring.
__all__ = ["create_campaign", "get_campaign", "list_campaigns", "list_metrics"]
