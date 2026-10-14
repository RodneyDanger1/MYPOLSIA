"""GET /api/v1/channels/caps — per-channel send counters + the firewall's day caps (wave U3, H-01).

WHY this file exists: the counters and the caps both existed, and only a worker could see them.
`channel_service.channel_sends_snapshot` was consumed solely at run time
(`celery_app/tasks/agent_tasks.py::_with_channel_sends` -> `context["channel_sends"]`) and
`RateCaps.per_channel_day` lived inside `app.agents.policy_firewall` with no read surface, so the
UI could not honestly render "SENT n of cap today" (BEAT B9, auditor H-01) — a widget that shows
the numerator without the denominator is guessing at the denominator. This route is the read.

THE SHAPE (frozen for wave U3; tests/unit/test_u3_channel_caps.py pins it key by key)

    200 {"channels": [{"channel": str, "sent_today": int|null, "cap": int|null,
                       "remaining": int|null}, ...  sorted by channel],
         "generated_at": ISO-8601 UTC}

PROVENANCE — every number comes from a real source; none is invented here:

  * `sent_today`  <- `channel_service.channel_sends_snapshot` (social_posts `published` for
    twitter/linkedin; email_logs `sent` for email; the COMPANY-local day window). `null` means the
    channel has NO local send table to count — UNKNOWN, and it is served as `null`, never as a
    fabricated `0` (the repo's unmeasured-not-zero discipline; a real measured zero IS served as 0).
  * `cap`         <- `RateCaps().per_channel_day` — the exact numbers the firewall enforces
    (`policy_firewall.decide()`'s own default instance; no other construction exists in the repo).
    `null` when the policy configures no day cap for the channel — never an invented cap.
  * `remaining`   <- `cap - sent_today`, computed ONLY when both sides are known, else `null`.
    Deliberately NOT clamped: a negative value honestly means the day already exceeds the cap
    (possible when rows were persisted after a run's in-run check, e.g. M-05 replies).
  * channel set   <- sorted union of `per_channel_day` and `per_channel_hour` keys — exactly the
    channels the policy layer names. Channels it does not name cannot be enumerated and are absent.
  * `generated_at`<- `app.core.database.utcnow()` — the aware UTC instant the snapshot was built.

HONEST FAILURE: the count read runs inside `refusal_guard()`; a broken source surfaces as the named
E_DB refusal (503 envelope + `X-Polsia-Error-Code`), never as a 200 whose empty list reads like
"no channels". No new error codes.

AUTH: the same `dependencies=[Depends(verify_api_key)]` every sibling v1 router carries
(`ops.py:27`, `notifications.py:49-51`).
"""
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.policy_firewall import RateCaps
from app.api.deps import get_db, refusal_guard, verify_api_key
from app.core.database import utcnow
from app.services.channel_service import channel_sends_snapshot

router = APIRouter(prefix="/channels", tags=["channels"], dependencies=[Depends(verify_api_key)])


class ChannelCapEntry(BaseModel):
    """One channel's day view. `null` = UNKNOWN / not configured — never a stand-in for zero."""

    channel: str
    sent_today: int | None
    cap: int | None
    remaining: int | None


class ChannelCapsResponse(BaseModel):
    channels: list[ChannelCapEntry]
    generated_at: datetime


def policy_channels(caps: RateCaps) -> list[str]:
    """Every channel the policy layer names (day caps ∪ hour caps), sorted for a stable payload."""
    return sorted(set(caps.per_channel_day) | set(caps.per_channel_hour))


def remaining_under_cap(sent_today: int | None, cap: int | None) -> int | None:
    """`cap - sent_today` when BOTH sides are known, else `None` (see the module docstring).

    Signed on purpose: a negative value is the honest "already over the cap today".
    """
    if sent_today is None or cap is None:
        return None
    return cap - sent_today


@router.get("/caps", response_model=ChannelCapsResponse)
async def channel_caps(db: AsyncSession = Depends(get_db)) -> ChannelCapsResponse:
    """Real per-channel day counters + policy caps; unknown counters stay `null` (H-01)."""
    caps = RateCaps()
    channels = policy_channels(caps)
    with refusal_guard():
        # The same snapshot the worker hands the channel agents — one counting source, one truth.
        snapshot = await channel_sends_snapshot(db, channels)
    entries = [
        ChannelCapEntry(
            channel=channel,
            sent_today=snapshot[channel]["today"],
            cap=caps.per_channel_day.get(channel),
            remaining=remaining_under_cap(
                snapshot[channel]["today"], caps.per_channel_day.get(channel)
            ),
        )
        for channel in channels
    ]
    return ChannelCapsResponse(channels=entries, generated_at=utcnow())
