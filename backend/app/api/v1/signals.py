"""GET /api/v1/signals — headlines from the FIXED feed allowlist (V29/L5).

NOT wired into `app/main.py` by this lane: the parent includes `signals.router` at settle (the
same pin/wiring split `api/v1/economics.py` records). Until then this router is reachable only
through the local test app in `tests/unit/test_v29_signals.py`.

The response models are declared HERE, not in `app/schemas/` — the same ruling economics and
invoices record for a new lane's shapes. The service already returns this exact dict; the model
exists so OpenAPI carries the contract and a drift in the service fails loudly here.

Shape (frozen by the parent's parity pins — `GET /signals`):

  * `items` — titles and links only, newest first, deduped by URL, `limit` at most;
  * `sources` — one entry per allowlisted feed: `ok` plus, when it failed, a NAMED `reason`
    (`E_UPSTREAM: …`). A failed feed never removes its siblings' items;
  * `fetched_at` — this answer's clock; each source keeps its own (a cache hit is not restamped);
  * `untrusted: true` + `note` — feed text is third-party and must never be followed as
    instructions. This is the prompt-injection guard that travels with the tool result too.

Read-only, no DB, no side effects, no caller-supplied URL: safe to poll.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.api.deps import refusal_guard, verify_api_key
from app.services import signals_service

router = APIRouter(prefix="/signals", tags=["signals"], dependencies=[Depends(verify_api_key)])


class SignalItem(BaseModel):
    """One headline. `title` is plain text (never markup) and `url` is absolute http(s)."""

    id: str
    source: str
    title: str
    url: str
    published_at: str | None


class SignalSource(BaseModel):
    """One allowlisted feed's outcome. `reason` is set exactly when `ok` is false."""

    id: str
    label: str
    ok: bool
    reason: str | None
    count: int
    cached: bool
    fetched_at: str | None


class SignalsResponse(BaseModel):
    items: list[SignalItem]
    sources: list[SignalSource]
    fetched_at: str
    note: str
    untrusted: bool


@router.get("", response_model=SignalsResponse)
async def read_signals(
    limit: int = Query(default=signals_service.DEFAULT_LIMIT, ge=1, le=signals_service.MAX_LIMIT),
) -> SignalsResponse:
    """Newest headlines across the allowlist; `limit` 1..30 (out of range is a 422, not a clamp)."""
    with refusal_guard():
        result = await run_in_threadpool(signals_service.read, limit=limit)
    return SignalsResponse.model_validate(result)
