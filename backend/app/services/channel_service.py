"""Channel-send counters — the REAL counts behind the firewall's per-channel caps.

`RateCaps.per_channel_day` / `per_channel_hour` (app/agents/policy_firewall.py) bound how often a
channel may send, but every caller supplied a hard-coded `0` (A2 finding F-04): the caps were
wired to a counter that had no producer, so they could never fire. This module is the one place
that answers "how many sends has this channel made?" — from local tables, honestly.

Three states stay distinguishable, which is the point of the module:
  * `int` — a measured count; `0` means "the source was read and there were no sends";
  * `None` — there is NO local record of sends for that channel, so the count is UNKNOWN. Only
    `social_posts` (twitter/linkedin, published) and `email_logs` (sent) record a send; github,
    google_ads, meta_ads, stripe, render and anything unknown have no such table. Fabricating a
    `0` for them would be a second fake gate — the firewall instead refuses a configured cap it
    cannot verify (`MERIDIAN-E114`, `cap_unknown`), which is the safe answer;
  * `MeridianError(E_DB)` — the read itself failed. "No source" is `None`; "read broke" is a named
    refusal, never a `0` that reads like a measured zero.

Windows:
  * day — the COMPANY-local day via `app.core.timeutil.day_window` (the company timezone, not
    the process's: the campaign was already bitten once by a UTC-midnight window on a non-UTC
    company);
  * hour — the trailing 60 minutes `(now-1h, now]`: sustained-rate semantics, deliberately
    stricter than a clock hour — three tweets at 09:59 plus three at 10:01 are six in any
    trailing hour, where a clock-hour counter would see three.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_DB, ERROR_HINTS, MeridianError
from app.core.timeutil import as_bound_instant, company_tz, day_window, now_utc
from app.models.finance import StripeEvent
from app.models.outreach import EmailLog
from app.models.report import ActivityLog
from app.models.social import SocialPost
from app.services.query_helpers import fetch_rows, fetch_scalar

# Which local table can count a send per channel. A channel absent from both branches has no
# local record at all -> `None` (unknown), never `0` (see module docstring).
# U5/P1: facebook/instagram joined once their adapters could write rows via
# `record_published_posts`. V8/T4: tiktok/youtube joined the same way (their Content Posting /
# videos.insert adapters landed) — their counts are now MEASURABLE (a real 0 until rows exist,
# then the caps fire on real sends; before V8 they were UNKNOWN and the firewall refused their
# configured caps by name, fail closed). A channel absent from this tuple still counts as None.
_SOCIAL_CHANNELS = ("twitter", "linkedin", "facebook", "instagram", "tiktok", "youtube")   # social_posts, 'published'
_EMAIL_CHANNEL = "email"                     # email_logs, status='sent'
#: B2-C4-R1: channels that had NO local record, so an approved PR/refund replay was refused
#: `E114 cap_unknown` — the cap could not be verified, so it refused.  They now have real sources:
#:   * `stripe`  — money-moving events land in `stripe_events` (one row per event received);
#:   * `github`  — nothing recorded a PR, so `record_channel_send` writes ONE `activity_log` row
#:     per send under `channel_send:github`, and this module counts those rows.
_STRIPE_CHANNEL = "stripe"
_ACTIVITY_RECORDED_CHANNELS = ("github",)
#: The action prefix of a send recorded in the audit ledger (`channel_send:<channel>`).
CHANNEL_SEND_ACTION_PREFIX = "channel_send:"


def _as_utc(now: datetime | None) -> datetime:
    """`now` as an aware UTC instant; a naive value is read as UTC (how every row is stored)."""
    anchor = now or now_utc()
    return anchor.replace(tzinfo=UTC) if anchor.tzinfo is None else anchor.astimezone(UTC)


async def _count(db: AsyncSession, stmt: Select, *, what: str) -> int:
    """One COUNT aggregate — a real number, or a named `E_DB` refusal.

    `fetch_scalar` turns a driver/SQLAlchemy failure into `db_refusal(...)`; the `None` guard
    covers "the statement was not the aggregate it claims to be". Returning `0` there would
    collapse "not measured" into "measured zero" — the exact defect this module prevents.
    """
    value = await fetch_scalar(db, stmt, what=what)
    if value is None:
        raise MeridianError(
            E_DB, f"count for {what} returned no value",
            how_to_fix=ERROR_HINTS[E_DB], context={"query": what})
    return int(value)


def _stripe_stmt(start: datetime, end: datetime) -> Select:
    """Stripe traffic in `[start, end)` — one row per event RECEIVED on `stripe_events`.

    A refund this product creates comes back as an event, so this is the money-movement count the
    `stripe` channel cap is about (B2-C4-R1: the channel had no source at all and every configured
    cap was refused `E114 cap_unknown`).
    """
    return (
        select(func.count())
        .select_from(StripeEvent)
        .where(StripeEvent.processed_at >= start, StripeEvent.processed_at < end)
    )


def _recorded_send_stmt(channel: str, start: datetime, end: datetime) -> Select:
    """`channel_send:<channel>` ledger rows in `[start, end)` — that channel's recorded sends.

    The audit ledger IS the send record for a channel with no table of its own (`github`); a row
    is written by `record_channel_send` at the moment the send succeeded, so the count is a
    measured zero before the first send and a real number after it.
    """
    return (
        select(func.count())
        .select_from(ActivityLog)
        .where(
            ActivityLog.action == f"{CHANNEL_SEND_ACTION_PREFIX}{channel}",
            ActivityLog.created_at >= start,
            ActivityLog.created_at < end,
        )
    )


async def record_channel_send(
    db: AsyncSession,
    channel: str,
    *,
    summary: str | None = None,
    detail: dict[str, Any] | None = None,
    agent_type: str = "orchestrator",
) -> None:
    """Record ONE send on a channel that has no table of its own (B2-C4-R1).

    The counter has to be written by whoever really sends.  `social_posts` and `email_logs` are
    written by their own services; `github` never recorded anything, so the firewall could not
    verify a configured github cap and refused the send (`E114 cap_unknown`) — including the
    replay of an approval the owner had already granted.  This writes ONE `activity_log` row
    (`channel_send:<channel>`) in the CALLER's transaction, which `channel_sends_today` /
    `channel_sends_this_hour` count; it publishes the live event through `log_activity`, so the
    owner's feed sees the send at the same moment the cap counts it.
    """
    if not channel or not str(channel).strip():
        raise ValueError("record_channel_send requires a channel name")
    from app.services.activity_service import log_activity

    await log_activity(
        db,
        agent_type=agent_type,
        action=f"{CHANNEL_SEND_ACTION_PREFIX}{channel}",
        summary=summary or f"{channel}: one send recorded",
        level="info",
        detail={"channel": channel, **(detail or {})},
    )


async def channel_sends_today(
    db: AsyncSession, channel: str, *, now: datetime | None = None
) -> int | None:
    """Sends on `channel` during the company-local day containing `now` (UTC `[start, end)`).

    `None` = no local record of this channel's sends exists (unknown, not zero).
    """
    anchor = _as_utc(now)
    start, end = day_window(db, day=anchor.astimezone(company_tz()).date())
    if channel in _SOCIAL_CHANNELS:
        stmt = (
            select(func.count())
            .select_from(SocialPost)
            .where(
                SocialPost.platform == channel,      # a per-channel cap needs per-channel rows
                SocialPost.status == "published",    # a DRAFT was not a send (the A2 F-04 trap)
                SocialPost.published_at >= start,
                SocialPost.published_at < end,
            )
        )
        return await _count(db, stmt, what=f"{channel} sends today")
    if channel == _EMAIL_CHANNEL:
        stmt = (
            select(func.count())
            .select_from(EmailLog)
            .where(
                EmailLog.status == "sent",           # queued/failed rows were not sent
                EmailLog.sent_at >= start,
                EmailLog.sent_at < end,
            )
        )
        return await _count(db, stmt, what="email sends today")
    if channel == _STRIPE_CHANNEL:
        return await _count(db, _stripe_stmt(start, end), what="stripe events today")
    if channel in _ACTIVITY_RECORDED_CHANNELS:
        return await _count(
            db, _recorded_send_stmt(channel, start, end), what=f"{channel} sends today"
        )
    return None


async def channel_sends_this_hour(
    db: AsyncSession, channel: str, *, now: datetime | None = None
) -> int | None:
    """Sends on `channel` in the trailing 60 minutes `(now-1h, now]` (see module docstring).

    `None` = no local record of this channel's sends exists (unknown, not zero).
    """
    anchor = _as_utc(now)
    start = as_bound_instant(anchor - timedelta(hours=1), db)   # exclusive lower bound
    end = as_bound_instant(anchor, db)                          # inclusive upper bound
    if channel in _SOCIAL_CHANNELS:
        stmt = (
            select(func.count())
            .select_from(SocialPost)
            .where(
                SocialPost.platform == channel,
                SocialPost.status == "published",
                SocialPost.published_at > start,
                SocialPost.published_at <= end,
            )
        )
        return await _count(db, stmt, what=f"{channel} sends in the last hour")
    if channel == _EMAIL_CHANNEL:
        stmt = (
            select(func.count())
            .select_from(EmailLog)
            .where(
                EmailLog.status == "sent",
                EmailLog.sent_at > start,
                EmailLog.sent_at <= end,
            )
        )
        return await _count(db, stmt, what="email sends in the last hour")
    if channel == _STRIPE_CHANNEL:
        return await _count(db, _stripe_stmt(start, end), what="stripe events in the last hour")
    if channel in _ACTIVITY_RECORDED_CHANNELS:
        return await _count(
            db, _recorded_send_stmt(channel, start, end), what=f"{channel} sends in the last hour"
        )
    return None


async def channel_sends_snapshot(
    db: AsyncSession, channels: Iterable[str], *, now: datetime | None = None
) -> dict[str, dict[str, int | None]]:
    """`{"today": int|None, "hour": int|None}` for exactly the requested channels.

    This is the shape the worker context carries as `context["channel_sends"]`: the channel
    agents read `[channel]["today"]` / `[channel]["hour"]` and hand the raw value to the
    firewall. `None` entries are deliberate — they tell the firewall the count is unknown, and
    it refuses a configured cap it cannot verify.

    V24/L6 — WHY THIS IS NOT A LOOP OF `channel_sends_today`/`channel_sends_this_hour` ANY MORE:
    the per-channel functions issue one COUNT each, so the snapshot cost 2 statements PER
    CHANNEL — 14 on `GET /channels/caps` (7 policy channels) and 14 more on every agent run
    that needed the context (measured: `pre/probe_bench_before.json`, `/channels/caps` sql=14).
    The counts are now computed by at most TWO grouped statements — one over `social_posts`
    (per platform, both windows as conditional SUMs) and one over `email_logs` — so the cost is
    constant in the channel count.  The values are unchanged: a channel with a local send table
    and no matching rows is a measured 0, and a channel with no local record stays `None`.
    """
    requested = list(dict.fromkeys(channels))   # dedupe, keep the caller's order
    anchor = _as_utc(now)
    snapshot: dict[str, dict[str, int | None]] = {
        channel: {"today": None, "hour": None} for channel in requested
    }

    social = [channel for channel in requested if channel in _SOCIAL_CHANNELS]
    email_wanted = _EMAIL_CHANNEL in requested
    # B2-C4-R1: the two channels that had no counter at all.  A snapshot that answered `None`
    # for them made the firewall refuse every configured cap (`E114 cap_unknown`), including the
    # replay of an approval the owner had already granted.
    stripe_wanted = _STRIPE_CHANNEL in requested
    recorded = [channel for channel in requested if channel in _ACTIVITY_RECORDED_CHANNELS]
    if not (social or email_wanted or stripe_wanted or recorded):
        return snapshot

    day_start, day_end = day_window(db, day=anchor.astimezone(company_tz()).date())
    hour_start = as_bound_instant(anchor - timedelta(hours=1), db)   # exclusive lower bound
    hour_end = as_bound_instant(anchor, db)                          # inclusive upper bound
    # One WHERE that is a superset of both windows; each window is a conditional SUM below.
    lower = min(day_start, hour_start)
    upper = max(day_end, hour_end)

    if social:
        today_branch = and_(
            SocialPost.published_at >= day_start, SocialPost.published_at < day_end
        )
        hour_branch = and_(
            SocialPost.published_at > hour_start, SocialPost.published_at <= hour_end
        )
        rows = await fetch_rows(
            db,
            select(
                SocialPost.platform,
                func.sum(case((today_branch, 1), else_=0)).label("today"),
                func.sum(case((hour_branch, 1), else_=0)).label("hour"),
            )
            .where(
                SocialPost.platform.in_(social),
                SocialPost.status == "published",
                SocialPost.published_at >= lower,
                SocialPost.published_at <= upper,
            )
            .group_by(SocialPost.platform),
            what="social channel send counts",
        )
        measured = {platform: (int(today or 0), int(hour or 0)) for platform, today, hour in rows}
        for channel in social:
            today, hour = measured.get(channel, (0, 0))   # measured zero, not unknown
            snapshot[channel] = {"today": today, "hour": hour}

    if email_wanted:
        rows = await fetch_rows(
            db,
            select(
                func.sum(
                    case(
                        (
                            and_(EmailLog.sent_at >= day_start, EmailLog.sent_at < day_end),
                            1,
                        ),
                        else_=0,
                    )
                ).label("today"),
                func.sum(
                    case(
                        (
                            and_(EmailLog.sent_at > hour_start, EmailLog.sent_at <= hour_end),
                            1,
                        ),
                        else_=0,
                    )
                ).label("hour"),
            ).where(
                EmailLog.status == "sent",
                EmailLog.sent_at >= lower,
                EmailLog.sent_at <= upper,
            ),
            what="email channel send counts",
        )
        row = rows[0] if rows else None
        today = int(row[0]) if row is not None and row[0] is not None else 0
        hour = int(row[1]) if row is not None and row[1] is not None else 0
        # The aggregate always yields one row; a NULL sum there is zero matching rows, and email
        # HAS a local send table — so this is a measured 0, never the unknown None.
        snapshot[_EMAIL_CHANNEL] = {"today": today, "hour": hour}

    if stripe_wanted:
        rows = await fetch_rows(
            db,
            select(
                func.sum(
                    case(
                        (
                            and_(
                                StripeEvent.processed_at >= day_start,
                                StripeEvent.processed_at < day_end,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("today"),
                func.sum(
                    case(
                        (
                            and_(
                                StripeEvent.processed_at > hour_start,
                                StripeEvent.processed_at <= hour_end,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("hour"),
            ).where(
                StripeEvent.processed_at >= lower,
                StripeEvent.processed_at <= upper,
            ),
            what="stripe channel event counts",
        )
        row = rows[0] if rows else None
        # No event in either window is a MEASURED zero: `stripe_events` is the source, it was read.
        snapshot[_STRIPE_CHANNEL] = {
            "today": int(row[0] or 0) if row is not None else 0,
            "hour": int(row[1] or 0) if row is not None else 0,
        }

    for channel in recorded:
        rows = await fetch_rows(
            db,
            select(
                func.sum(
                    case(
                        (
                            and_(
                                ActivityLog.created_at >= day_start,
                                ActivityLog.created_at < day_end,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("today"),
                func.sum(
                    case(
                        (
                            and_(
                                ActivityLog.created_at > hour_start,
                                ActivityLog.created_at <= hour_end,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("hour"),
            ).where(
                ActivityLog.action == f"{CHANNEL_SEND_ACTION_PREFIX}{channel}",
                ActivityLog.created_at >= lower,
                ActivityLog.created_at <= upper,
            ),
            what=f"{channel} channel send counts",
        )
        row = rows[0] if rows else None
        snapshot[channel] = {
            "today": int(row[0] or 0) if row is not None else 0,
            "hour": int(row[1] or 0) if row is not None else 0,
        }

    return snapshot


__all__ = [
    "CHANNEL_SEND_ACTION_PREFIX",
    "channel_sends_snapshot",
    "channel_sends_this_hour",
    "channel_sends_today",
    "record_channel_send",
]
