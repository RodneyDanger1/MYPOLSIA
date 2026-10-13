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

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import E_DB, ERROR_HINTS, PolsiaError
from app.core.timeutil import as_bound_instant, company_tz, day_window, now_utc
from app.models.outreach import EmailLog
from app.models.social import SocialPost
from app.services.query_helpers import fetch_scalar

# Which local table can count a send per channel. A channel absent from both branches has no
# local record at all -> `None` (unknown), never `0` (see module docstring).
_SOCIAL_CHANNELS = ("twitter", "linkedin")   # social_posts, status='published'
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
    """
    anchor = _as_utc(now)
    snapshot: dict[str, dict[str, int | None]] = {}
    for channel in dict.fromkeys(channels):   # dedupe, keep the caller's order
        snapshot[channel] = {
            "today": await channel_sends_today(db, channel, now=anchor),
            "hour": await channel_sends_this_hour(db, channel, now=anchor),
        }
    return snapshot


__all__ = [
    "channel_sends_snapshot",
    "channel_sends_this_hour",
    "channel_sends_today",
]
