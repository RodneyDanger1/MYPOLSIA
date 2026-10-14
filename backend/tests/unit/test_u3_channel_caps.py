"""Wave U3 (H-01) — GET /api/v1/channels/caps: real counters, real caps, NULL is never 0.

The contract this file measures (stated identically in `app/api/v1/channels.py`):

    GET /api/v1/channels/caps  (same X-API-Key auth as every sibling route)
    200 {"channels": [{"channel": str, "sent_today": int|null, "cap": int|null,
                       "remaining": int|null}, ... sorted by channel],
         "generated_at": ISO-8601 UTC (aware)}

What each block exists to catch:
  * auth — the router carries the shipped `verify_api_key` dependency (401, E_AUTH envelope);
  * provenance — counts come from REAL `social_posts` (`published` only) and `email_logs`
    (`sent` only) rows; a draft/scheduled/queued row must NOT count; the `cap` values served are
    exactly `RateCaps().per_channel_day` (asserted against the live class, never literals);
  * the NULL discipline (the point of H-01) — a channel whose sends no local table records
    (github/google_ads/meta_ads/stripe/render) serves `sent_today: null` (and `remaining: null`),
    while a readable-but-empty channel serves a measured `0` with `remaining == cap`;
  * `remaining` is computed ONLY when both sides are known — a channel with a measured count but
    NO configured day cap serves `cap: null, remaining: null` (the `RateCaps` seam is patched to
    move linkedin to an hour-only cap for exactly this case);
  * honest failure — a broken counting source answers the named E_DB refusal (503 + envelope +
    `X-Polsia-Error-Code`), never a 200 that reads like "no channels";
  * `generated_at` parses as an aware UTC instant.

Clock: the route reads the real `now`; tests pin a fixed anchor by patching
`channel_service.now_utc` (the module's own imported seam — `_as_utc` calls it when `now=None`)
plus the company timezone, so the day window is deterministic.

RED-leg evidence (each planted, observed, restored byte-exact; raw output in the lane receipt):
  * `sent_today` fake-zeroed (`or 0`) => test_unknown_channels_serve_null_never_zero fails;
  * `remaining` computed from an unknown count => test_unknown_channels_serve_null_never_zero
    fails on the remaining assertion;
  * the router's auth dependency removed => test_the_route_requires_the_api_key fails (200).
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import SQLAlchemyError

import app.config as app_config
from app.agents.policy_firewall import RateCaps
from app.api.v1 import channels as channels_api
from app.models.outreach import EmailLog, Prospect
from app.models.social import SocialPost
from app.services import channel_service

CAPS_PATH = "/api/v1/channels/caps"

ENTRY_KEYS = {"channel", "sent_today", "cap", "remaining"}

#: The fixed anchor: 2026-10-14 12:00 UTC — inside the UTC day the window is pinned to.
NOW = datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
def caps_clock(monkeypatch):
    """Pin the counter's clock + company timezone so the day window is deterministic."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(channel_service, "now_utc", lambda: NOW)
    return NOW


def _entry(body: dict, channel: str) -> dict:
    for item in body["channels"]:
        if item["channel"] == channel:
            return item
    raise AssertionError(f"channel {channel!r} missing from the caps payload: {body}")


async def _social(db, platform: str, published_at, *, status: str = "published",
                  content: str = "post") -> None:
    """Real `social_posts` row — the same seeding shape as tests/unit/test_channel_sends.py."""
    db.add(SocialPost(platform=platform, content=content, status=status,
                      published_at=published_at))
    await db.flush()


async def _prospect(db) -> int:
    prospect = Prospect(email="prospect@example.com")
    db.add(prospect)
    await db.flush()
    return prospect.id


async def _email(db, prospect_id: int, sent_at, *, status: str = "sent") -> None:
    """Real `email_logs` row — `status` decides whether it was a send."""
    db.add(EmailLog(prospect_id=prospect_id, subject="hi", status=status, sent_at=sent_at))
    await db.flush()


# ── auth ──────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_route_requires_the_api_key(api_client):
    """No key -> 401 E_AUTH envelope; a wrong key -> 401 (same as every sibling route)."""
    resp = await api_client.get(CAPS_PATH)
    assert resp.status_code == 401, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_AUTH"

    wrong = await api_client.get(CAPS_PATH, headers={"X-API-Key": "definitely-wrong"})
    assert wrong.status_code == 401


# ── shape + the channel set comes from the policy layer ───────────────────────────────────────


@pytest.mark.asyncio
async def test_shape_and_channel_set_are_the_frozen_contract(api_client, auth_headers, caps_clock):
    resp = await api_client.get(CAPS_PATH, headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()

    assert set(body) == {"channels", "generated_at"}
    assert isinstance(body["channels"], list) and body["channels"]
    for item in body["channels"]:
        assert set(item) == ENTRY_KEYS, "the entry key set is the frozen contract"

    # The channel set is exactly what the policy layer names (day caps ∪ hour caps), sorted —
    # asserted against the live class, so a cap-map change moves this route automatically.
    default = RateCaps()
    expected = sorted(set(default.per_channel_day) | set(default.per_channel_hour))
    assert [item["channel"] for item in body["channels"]] == expected


# ── real rows drive the counts; drafts/queued rows do not ─────────────────────────────────────


@pytest.mark.asyncio
async def test_real_rows_drive_the_counts(api_client, auth_headers, async_db_session, caps_clock):
    db = async_db_session
    published_at = NOW - timedelta(minutes=5)
    for i in range(2):
        await _social(db, "twitter", published_at, content=f"tweet {i}")
    await _social(db, "twitter", published_at, status="draft")       # a draft was not a send
    await _social(db, "twitter", published_at, status="scheduled")   # nor a scheduled one
    await _social(db, "linkedin", published_at)
    prospect_id = await _prospect(db)
    await _email(db, prospect_id, NOW - timedelta(hours=1))                    # sent: counts
    await _email(db, prospect_id, NOW - timedelta(hours=1), status="queued")   # not a send

    body = (await api_client.get(CAPS_PATH, headers=auth_headers)).json()
    caps = RateCaps()

    twitter = _entry(body, "twitter")
    assert twitter["sent_today"] == 2, "published-only counting"
    assert twitter["cap"] == caps.per_channel_day["twitter"], "caps come from the live policy"
    assert twitter["remaining"] == caps.per_channel_day["twitter"] - 2

    linkedin = _entry(body, "linkedin")
    assert (linkedin["sent_today"], linkedin["cap"], linkedin["remaining"]) == (
        1, caps.per_channel_day["linkedin"], caps.per_channel_day["linkedin"] - 1)

    email = _entry(body, "email")
    assert email["sent_today"] == 1, "sent-only counting"
    assert email["cap"] == caps.per_channel_day["email"]
    assert email["remaining"] == caps.per_channel_day["email"] - 1


# ── the H-01 honesty core: NULL ≠ 0 ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_channels_serve_null_never_zero(api_client, auth_headers, caps_clock):
    """No local send table -> `sent_today: null` (and `remaining: null`); empty-but-readable -> 0."""
    body = (await api_client.get(CAPS_PATH, headers=auth_headers)).json()
    caps = RateCaps()

    for channel in ("github", "google_ads", "meta_ads", "stripe", "render"):
        item = _entry(body, channel)
        assert item["sent_today"] is None, (
            f"{channel} has no local send table; the count is UNKNOWN and must be null, "
            f"never a fabricated 0 (got {item['sent_today']!r})"
        )
        assert item["remaining"] is None, f"{channel}: remaining cannot be computed from an unknown count"
        assert item["cap"] == caps.per_channel_day[channel], "the cap is policy and IS known"

    # ...while a channel whose table WAS read and held no rows is a measured zero, not unknown:
    twitter = _entry(body, "twitter")
    assert twitter["sent_today"] == 0
    assert twitter["remaining"] == caps.per_channel_day["twitter"]


# ── remaining requires BOTH sides; the cap comes from the policy each request ─────────────────


@pytest.mark.asyncio
async def test_remaining_needs_a_configured_cap(api_client, auth_headers, async_db_session,
                                                caps_clock, monkeypatch):
    """A channel with a measured count but no day cap serves `cap: null, remaining: null`."""
    # Hour-only cap for linkedin (moved out of the day map): the channel stays in the set via the
    # hour map, its DAY cap is now absent -> null, and `remaining` must follow.
    custom = RateCaps(
        per_channel_day={"email": 50, "twitter": 12, "github": 6, "google_ads": 10,
                         "meta_ads": 10, "stripe": 5, "render": 3},
        per_channel_hour={"email": 15, "twitter": 3, "github": 2, "linkedin": 4},
    )
    monkeypatch.setattr(channels_api, "RateCaps", lambda: custom)

    await _social(async_db_session, "linkedin", NOW - timedelta(minutes=5))
    await _social(async_db_session, "twitter", NOW - timedelta(minutes=5))

    body = (await api_client.get(CAPS_PATH, headers=auth_headers)).json()

    linkedin = _entry(body, "linkedin")
    assert linkedin["sent_today"] == 1, "the count is still real and measured"
    assert linkedin["cap"] is None, "the policy configures no DAY cap for linkedin any more"
    assert linkedin["remaining"] is None, "remaining needs a cap; without one it is unknown"

    twitter = _entry(body, "twitter")
    assert twitter["cap"] == 12, "caps are read from the policy on every request, not constants"
    assert twitter["remaining"] == 11


# ── honest failure: a broken source is a named refusal, never a fake zero ─────────────────────


@pytest.mark.asyncio
async def test_snapshot_failure_is_a_named_error_not_a_fake_answer(api_client, auth_headers,
                                                                   async_db_session, monkeypatch):
    """Break the DRIVER under the real translation layer: 503 + E_DB, never a 200 of nulls/zeros.

    The session's `execute` is failed like an outage; `query_helpers.fetch_scalar` (the real
    translation layer, NOT mocked here) turns that into `PolsiaError(E_DB)` and the route's
    `refusal_guard` into the coded 503 envelope — the full honest chain, endpoint to driver.
    """
    async def _explode(stmt):  # noqa: ANN001 - stands in for AsyncSession.execute during an outage
        raise SQLAlchemyError("simulated snapshot outage")

    monkeypatch.setattr(async_db_session, "execute", _explode)

    resp = await api_client.get(CAPS_PATH, headers=auth_headers)
    assert resp.status_code == 503, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_DB"
    assert "channels" not in body, "a failure must never look like an empty/zeroed payload"
    assert resp.headers.get("X-Polsia-Error-Code") == "E_DB"


# ── generated_at ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generated_at_is_an_aware_utc_instant(api_client, auth_headers, caps_clock):
    from app.core.database import utcnow

    raw = (await api_client.get(CAPS_PATH, headers=auth_headers)).json()["generated_at"]
    parsed = datetime.fromisoformat(raw)
    assert parsed.tzinfo is not None, "an ambiguous naive timestamp must not be shipped"
    assert parsed.utcoffset() == timedelta(0), "generated_at is a UTC instant"
    assert abs((utcnow() - parsed).total_seconds()) < 120
