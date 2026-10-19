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
    cannot verify (`POLSIA-E114`, `cap_unknown`), which is the safe answer;
  * `PolsiaError(E_DB)` — the read itself failed. "No source" is `None`; "read broke" is a named
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

from sqlalchemy import Select, and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_DB, ERROR_HINTS, PolsiaError
from app.core.timeutil import as_bound_instant, company_tz, day_window, now_utc
from app.models.outreach import EmailLog
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
        raise PolsiaError(
            E_DB, f"count for {what} returned no value",
            how_to_fix=ERROR_HINTS[E_DB], context={"query": what})
    return int(value)


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
    if not social and not email_wanted:
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
        today, hour = rows[0] if rows else (None, None)
        # The aggregate always yields one row; a NULL sum there is zero matching rows, and email
        # HAS a local send table — so this is a measured 0, never the unknown None.
        snapshot[_EMAIL_CHANNEL] = {"today": int(today or 0), "hour": int(hour or 0)}

    return snapshot


__all__ = [
    "channel_sends_snapshot",
    "channel_sends_this_hour",
    "channel_sends_today",
]
