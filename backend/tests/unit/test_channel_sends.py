"""Channel-send counters — N / N+1 / unknown, pinned.

`channel_sends` had five consumers (the channel agents) and zero producers (A2 finding F-04):
every request carried `sends_today=0`, so `caps.per_channel_day/hour` could never fire. These
tests pin the honest counting the caps now depend on:

  * published-only social counting and sent-only email counting (a DRAFT was not a send);
  * the COMPANY-LOCAL day window — a row inside the local day but outside the UTC day must
    count, and one inside the UTC day but outside the local day must not (the campaign was
    already bitten once by a UTC-midnight window on a non-UTC company);
  * the trailing-60-minute hour window `(now-1h, now]`, boundaries measured in
    raw/probe-boundaries.py rather than assumed;
  * `None` for channels with no local send record (never a fabricated `0`);
  * the end-to-end path: real rows -> real count -> `decide()` DENY at the cap, ALLOW one below.

Rows are written at the instants `timeutil` itself computes for the window, so the test stores
what the counting SQL compares against (on SQLite that is the naive UTC wall clock —
raw/probe-sqlite-datetime.py).
"""
from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import app.config as app_config
from app.agents.policy_firewall import (
    ActionClass,
    ActionRequest,
    Decision,
    RateCaps,
    decide,
)
from app.core.timeutil import day_window
from app.models.outreach import EmailLog, Prospect
from app.models.social import SocialPost
from app.services.channel_service import (
    channel_sends_snapshot,
    channel_sends_this_hour,
    channel_sends_today,
)

# A fixed anchor so "today" and "the last hour" are deterministic: 2026-10-12 12:00 UTC, i.e.
# 2026-10-12 21:00 in Asia/Tokyo (UTC+9) — inside the local day `date(2026, 10, 12)` used below.
NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)

_CAPS = RateCaps(per_channel_day={"twitter": 12}, per_channel_hour={})


def _req(**overrides) -> ActionRequest:
    """A permitted social_media tweet request; this file is about the DAY cap (hour pinned 0)."""
    base = dict(
        agent_type="social_media",
        action_class=ActionClass.EXTERNAL_REVERSIBLE,
        channel="twitter",
        payload={"action": "tweet", "text": "hello"},
        sends_this_hour=0,
    )
    base.update(overrides)
    return ActionRequest(**base)


async def _social(db, platform: str, published_at, *, status: str = "published",
                  content: str = "post") -> None:
    db.add(SocialPost(platform=platform, content=content, status=status,
                      published_at=published_at))
    await db.flush()


async def _prospect(db) -> int:
    prospect = Prospect(email="prospect@example.com")
    db.add(prospect)
    await db.flush()
    return prospect.id


async def _email(db, prospect_id: int, sent_at, *, status: str = "sent") -> None:
    db.add(EmailLog(prospect_id=prospect_id, subject="hi", status=status, sent_at=sent_at))
    await db.flush()


# ── (a) social: published-only ────────────────────────────────────────────────────────────────
async def test_only_published_social_rows_count(async_db_session, monkeypatch):
    """Draft/scheduled rows in the window must NOT count — the A-T5 "counting drafts" trap."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    db = async_db_session
    for i in range(3):
        await _social(db, "twitter", NOW - timedelta(hours=2), content=f"tweet {i}")
    await _social(db, "twitter", NOW - timedelta(hours=2), status="draft")
    await _social(db, "twitter", NOW - timedelta(hours=2), status="scheduled")

    assert await channel_sends_today(db, "twitter", now=NOW) == 3

    # linkedin lives in the same table and is counted for its own channel only
    await _social(db, "linkedin", NOW - timedelta(hours=2))
    assert await channel_sends_today(db, "linkedin", now=NOW) == 1
    assert await channel_sends_today(db, "twitter", now=NOW) == 3


# ── (b) email: sent-only ──────────────────────────────────────────────────────────────────────
async def test_only_sent_email_rows_count(async_db_session, monkeypatch):
    """Queued/bounced rows whose sent_at falls in the window must NOT count."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    db = async_db_session
    prospect_id = await _prospect(db)
    await _email(db, prospect_id, NOW - timedelta(hours=1))                     # sent
    await _email(db, prospect_id, NOW - timedelta(hours=1), status="queued")    # not a send
    await _email(db, prospect_id, NOW - timedelta(hours=1), status="bounced")   # not a send

    assert await channel_sends_today(db, "email", now=NOW) == 1


# ── (c) the day window is COMPANY-LOCAL ───────────────────────────────────────────────────────
async def test_the_day_window_is_company_local_not_utc(async_db_session, monkeypatch):
    """Asia/Tokyo (UTC+9) makes the local day 2026-10-12 == UTC [10-11 15:00, 10-12 15:00).

    UTC-day counting must fail this test: the first row is inside the LOCAL day but outside the
    UTC day of 10-12 (a UTC-midnight window would miss it), the second is inside the UTC day but
    outside the local day (a UTC window would over-count it). The half-open edges are asserted
    separately: `start` counts, `end` does not.
    """
    monkeypatch.setattr(app_config.settings, "company_timezone", "Asia/Tokyo")
    db = async_db_session
    start, end = day_window(db, date(2026, 10, 12), "Asia/Tokyo")

    await _social(db, "twitter", start + timedelta(hours=5))     # 05:00 local — LOCAL only
    assert await channel_sends_today(db, "twitter", now=NOW) == 1

    await _social(db, "twitter", end + timedelta(hours=1))       # 01:00 next local day — UTC only
    assert await channel_sends_today(db, "twitter", now=NOW) == 1, \
        "a row outside the company-local day must not count, even though it is inside the UTC day"

    await _social(db, "twitter", start)                          # half-open start: counts
    assert await channel_sends_today(db, "twitter", now=NOW) == 2

    await _social(db, "twitter", end)                            # exclusive end: does not count
    assert await channel_sends_today(db, "twitter", now=NOW) == 2

    await _social(db, "twitter", start - timedelta(seconds=1))   # just before the local day
    assert await channel_sends_today(db, "twitter", now=NOW) == 2


# ── (d) the hour window is the trailing 60 minutes ────────────────────────────────────────────
async def test_the_hour_window_is_the_trailing_60_minutes(async_db_session, monkeypatch):
    """`(now-1h, now]`: an exclusive lower bound, an inclusive upper bound (measured)."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    db = async_db_session
    await _social(db, "twitter", NOW - timedelta(minutes=1))     # inside
    await _social(db, "twitter", NOW - timedelta(minutes=59))    # inside
    assert await channel_sends_this_hour(db, "twitter", now=NOW) == 2

    await _social(db, "twitter", NOW - timedelta(minutes=61))    # before the window
    await _social(db, "twitter", NOW - timedelta(hours=1))       # exactly the exclusive lower bound
    assert await channel_sends_this_hour(db, "twitter", now=NOW) == 2

    await _social(db, "twitter", NOW)                            # exactly `now` — the inclusive end
    assert await channel_sends_this_hour(db, "twitter", now=NOW) == 3


# ── (e) no-source channels are unknown; snapshot shape ────────────────────────────────────────
async def test_channels_with_no_local_record_are_unknown_not_zero(async_db_session, monkeypatch):
    """`None` = "no local table records a send for this channel" — never `0`, never a guess."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    db = async_db_session
    for channel in ("github", "google_ads", "meta_ads", "stripe", "render", "mastodon"):
        assert await channel_sends_today(db, channel, now=NOW) is None
        assert await channel_sends_this_hour(db, channel, now=NOW) is None

    snapshot = await channel_sends_snapshot(db, ["twitter", "email", "github"], now=NOW)
    assert set(snapshot) == {"twitter", "email", "github"}
    assert snapshot["twitter"] == {"today": 0, "hour": 0}          # measured zero: table read
    assert snapshot["email"] == {"today": 0, "hour": 0}            # measured zero: table read
    assert snapshot["github"] == {"today": None, "hour": None}     # no source -> unknown


# ── (f) integration: the real count drives the firewall ───────────────────────────────────────
async def test_n_published_rows_drive_the_day_cap(async_db_session, monkeypatch):
    """End-to-end honest path (A2 F-04): rows -> count -> `decide()` verdict.

    RED leg A plants `channel_sends_today` returning a literal `0`: this test then fails at the
    count assertion (`assert 0 == 11` before the DENY leg is reachable), and
    `raw/redleg-A-probe.py` shows the verdict flip that defect produces downstream — ALLOW where
    the honest count of 12 DENIES.
    """
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    db = async_db_session
    published_at = NOW - timedelta(minutes=5)
    for i in range(11):
        await _social(db, "twitter", published_at, content=f"tweet {i}")

    sends = await channel_sends_today(db, "twitter", now=NOW)
    assert sends == 11
    under = decide(_req(sends_today=sends), caps=_CAPS, sandbox_mode=False)
    assert under.decision is Decision.ALLOW, "one below the cap must still go through"

    await _social(db, "twitter", published_at, content="tweet 11")
    snapshot = await channel_sends_snapshot(db, ["twitter"], now=NOW)
    assert snapshot["twitter"]["today"] == 12, "the snapshot shape the worker context carries"
    at_cap = decide(_req(sends_today=snapshot["twitter"]["today"]), caps=_CAPS, sandbox_mode=False)
    assert at_cap.decision is Decision.DENY, "N sends at an N cap must deny"
    assert at_cap.code == "POLSIA-E117"
