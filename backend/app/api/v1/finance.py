"""GET /api/v1/finance/* + the unauthenticated Stripe webhook.

Webhook contract (pinned by `tests/unit/api/test_finance.py`): **500** when
`stripe_webhook_secret` is empty, **400** for a missing/incorrect `stripe-signature`
header, **200** `{"received": true}` otherwise — with no `X-API-Key`, because Stripe
cannot send one, so auth is per route and never on this router.

Signature verification never imports the SDK at import time: `stripe` is imported
lazily and, when it is absent, the stdlib HMAC-SHA256 check over `"{t}.{raw_body}"`
with `hmac.compare_digest` is used, with a timestamp tolerance so a captured request
cannot be replayed forever.  (Corrected R28: this venv HAS the SDK — `stripe` 11.3.0,
measured 2026-10-13, `raw/probe_stripe_presence.txt` — the docstring used to claim it
was absent.  The stdlib ladder is a real production path on a host without the SDK and
is tested both directly and by forcing the absent-import state.)

Persistence is idempotent at the statement level (`finance_service.record_stripe_event`):
one `INSERT ... ON CONFLICT (stripe_event_id) DO NOTHING RETURNING id`, so two
concurrent duplicate deliveries produce ONE row and no `IntegrityError` (which would
be a 5xx and trigger Stripe's retry storm).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.config import settings
from app.core.errors import E_VALIDATION, ERROR_HINTS
from app.schemas.finance import (
    ExpenseRecordResponse,
    FinanceSummaryResponse,
    RevenueSnapshotResponse,
    StripeEventResponse,
)
from app.services.finance_service import (
    get_ai_spend_summary,
    get_finance_summary,
    list_expenses,
    list_revenue_snapshots,
    list_stripe_events,
    record_stripe_event,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/finance", tags=["finance"])

#: Stripe's own default tolerance, in seconds.
DEFAULT_SIGNATURE_TOLERANCE = 300


@router.get("/summary", response_model=FinanceSummaryResponse, dependencies=[Depends(verify_api_key)])
async def finance_summary(db: AsyncSession = Depends(get_db)) -> FinanceSummaryResponse:
    # `ai_spend` (R24) is the additive day view: `get_finance_summary` keeps its seven-key, ONE-
    # statement contract (and its docstring), and the spend block is composed here from the real
    # `agent_runs` day ledger — see `finance_service.get_ai_spend_summary`.
    with refusal_guard():
        summary = await get_finance_summary(db)
        ai_spend = await get_ai_spend_summary(db)
    return FinanceSummaryResponse(**summary, ai_spend=ai_spend)


@router.get(
    "/revenue", response_model=list[RevenueSnapshotResponse], dependencies=[Depends(verify_api_key)]
)
async def finance_revenue(
    limit: int = Query(default=30, ge=1, le=365), db: AsyncSession = Depends(get_db)
) -> list[RevenueSnapshotResponse]:
    with refusal_guard():
        rows = await list_revenue_snapshots(db, limit=limit)
    return [RevenueSnapshotResponse.model_validate(row) for row in rows]


@router.get(
    "/expenses", response_model=list[ExpenseRecordResponse], dependencies=[Depends(verify_api_key)]
)
async def finance_expenses(
    category: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[ExpenseRecordResponse]:
    with refusal_guard():
        rows = await list_expenses(db, category=category, limit=limit)
    return [ExpenseRecordResponse.model_validate(row) for row in rows]


@router.get(
    "/events", response_model=list[StripeEventResponse], dependencies=[Depends(verify_api_key)]
)
async def finance_events(
    limit: int = Query(default=100, ge=1, le=500), db: AsyncSession = Depends(get_db)
) -> list[StripeEventResponse]:
    with refusal_guard():
        rows = await list_stripe_events(db, limit=limit)
    return [StripeEventResponse.model_validate(row) for row in rows]


def _parse_signature_header(header: str) -> tuple[str | None, str | None]:
    timestamp: str | None = None
    signature: str | None = None
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signature = value
    return timestamp, signature


def _stdlib_verify(
    raw_body: bytes,
    signature_header: str,
    secret: str,
    tolerance: int,
) -> dict[str, Any]:
    """Stripe's documented scheme without the SDK: HMAC-SHA256 over `t.payload`."""
    timestamp, signature = _parse_signature_header(signature_header)
    if not timestamp or not signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe signature header",
        )
    try:
        signed_at = int(timestamp)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe signature timestamp",
        ) from exc
    if tolerance and abs(int(time.time()) - signed_at) > tolerance:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Stripe signature timestamp outside the {tolerance}s tolerance",
        )
    # The exact bytes Stripe signed: "<timestamp>.<raw body>".  Bytes, not a decoded
    # string — a non-UTF8 byte would otherwise be rewritten by errors="replace".
    signed_payload = timestamp.encode("utf-8") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid Stripe signature",
        )
    try:
        payload = json.loads(raw_body or b"{}")
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON body"
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid event payload"
        )
    return payload


def construct_stripe_event(
    raw_body: bytes,
    signature_header: str,
    secret: str,
    tolerance: int | None = None,
) -> dict[str, Any]:
    """Verify + parse a Stripe webhook.  Raises HTTPException(400) when invalid.

    Uses the stripe SDK when it is installed, otherwise the stdlib check, so the
    endpoint works (and is testable) without the SDK.  `import stripe` stays lazily
    inside the function: a module-level import would break the app on a host where
    the SDK is absent.  (R28: the SDK IS installed in this venv — 11.3.0, measured —
    so the fallback is reached in tests by forcing the absent-import state; the older
    "measured absent" note was stale.)
    """
    window = DEFAULT_SIGNATURE_TOLERANCE if tolerance is None else int(tolerance)
    try:
        import stripe  # type: ignore
    except ImportError:
        return _stdlib_verify(raw_body, signature_header, secret, window)
    try:
        return dict(stripe.Webhook.construct_event(raw_body, signature_header, secret, tolerance=window))
    except Exception as exc:  # noqa: BLE001 - any SDK verification failure is a 400
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid Stripe signature: {exc}",
        ) from exc


def _signature_tolerance() -> int:
    return int(getattr(settings, "stripe_signature_tolerance", DEFAULT_SIGNATURE_TOLERANCE))


@router.post("/stripe/webhook")
async def stripe_webhook(request: Request, db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    """Raw-body webhook.  No X-API-Key (Stripe cannot send one)."""
    secret = settings.stripe_webhook_secret or ""
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Stripe webhook secret not configured",
        )
    raw_body = await request.body()
    signature = request.headers.get("stripe-signature")
    if not signature:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing stripe-signature header",
        )
    event = construct_stripe_event(raw_body, signature, secret, _signature_tolerance())

    event_id = str(event.get("id") or "").strip()
    if not event_id:
        # Without an id the event cannot be de-duplicated: replaying it would double
        # count.  Refuse it by name instead of storing an empty-id row.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "code": E_VALIDATION,
                "message": "Stripe event has no id; it cannot be made idempotent",
                "how_to_fix": ERROR_HINTS[E_VALIDATION],
                "context": {"event_type": event.get("type")},
            },
        )

    data_object = event.get("data", {}).get("object", {}) if isinstance(event.get("data"), dict) else {}
    # A payload whose `data.object` is not an object has no readable fields at all; reading it
    # as `{}` stores the event with NULL money ("not measured") instead of raising AttributeError
    # into a 500 — a 500 makes Stripe retry the same poison payload forever (measured: R28
    # raw/probe_webhook_shapes.txt).
    if not isinstance(data_object, dict):
        data_object = {}
    # `amount or amount_paid` collapsed a measured 0 (a trial / zero-amount charge) into
    # "absent" and stored NULL for it; only fall through to `amount_paid` when `amount`
    # is really missing (F-11: measured zero vs not measured).
    amount = data_object.get("amount")
    if amount is None:
        amount = data_object.get("amount_paid")
    # A flag is not a count and a non-finite number is not a measurement (accounting.py:117,
    # :126): `int(NaN)`/`int(inf)` used to 500 the delivery, and `True` used to be stored as a
    # one-cent reading.  Neither can become cents, so the column stays NULL.
    if isinstance(amount, bool) or not isinstance(amount, int | float) or not math.isfinite(amount):
        amount_cents: int | None = None
    else:
        amount_cents = int(amount)
    # `customer` is typed `string | object (expandable)` by Stripe; a dict bound to the
    # String(255) column raised InterfaceError -> 500.  The id is recovered when the object
    # carries one; anything else is honestly NULL (the raw payload keeps the full object).
    customer = data_object.get("customer")
    if isinstance(customer, dict):
        customer = customer.get("id")
    customer_id = customer if isinstance(customer, str) else None
    # Same class of crash: `record_stripe_event` lower-cases the currency, so a non-text value
    # (`.lower()` on an int) used to 500 the delivery.  Pass text or nothing.
    currency = data_object.get("currency")
    currency = currency if isinstance(currency, str) else None
    with refusal_guard():
        await record_stripe_event(
            db,
            stripe_event_id=event_id,
            event_type=str(event.get("type") or "unknown"),
            raw_payload=event,
            customer_id=customer_id,
            amount_cents=amount_cents,
            currency=currency,
        )
    return {"received": True}
