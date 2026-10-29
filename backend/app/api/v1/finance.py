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

Both branches accept a match with ANY `v1=` signature a header carries (R32): a secret
rotation can deliver several v1 values in one `Stripe-Signature` header, the SDK tries
all of them (`stripe/_webhook.py:81`) and the stdlib fallback now mirrors that
accept-any behavior instead of keeping only the last one.

Persistence is idempotent at the statement level (`finance_service.record_stripe_event`):
one `INSERT ... ON CONFLICT (stripe_event_id) DO NOTHING RETURNING id`, so two
concurrent duplicate deliveries produce ONE row and no `IntegrityError` (which would
be a 5xx and trigger Stripe's retry storm).

The 200 body is declared, not implied (T12/D20): the route carries
`response_model=StripeWebhookResponse`, whose shape was left TRUE to the handler
(`{"received": true}`) — FastAPI now validates that body and publishes it.

Refund producer (T14/D21): the delivery also extracts the PaymentIntent id a refund needs
and persists it ON the event row.  A `charge`/`invoice` object carries it as
`payment_intent`; a `payment_intent` object IS it (`id`).  Stripe's own field names it
"nullable ... if one exists", so a shape with none persists `null` — an honest "not
measured", never a substitute id (`ch_…`/`evt_…` can never refund anything: the adapter's
guard refuses them by name).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import time
from datetime import timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import and_, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.config import settings
from app.core.errors import E_CONFIG, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.core.timeutil import day_window, today_in
from app.models.finance import ExpenseRecord
from app.models.task import AgentRun
from app.schemas.finance import (
    ExpenseRecordResponse,
    FinanceSummaryResponse,
    RevenueSnapshotResponse,
    StripeEventResponse,
    StripeWebhookResponse,
)
from app.schemas.planning import FinancePlanningResponse
from app.services import pricing_service
from app.services.company_service import get_company, measured_human_hourly_rate
from app.services.finance_service import (
    REPORTING_CURRENCY,
    SpendTruth,
    extract_payment_intent_id,
    get_ai_spend_summary,
    get_finance_summary,
    list_expenses,
    list_revenue_snapshots,
    list_stripe_events,
    money,
    record_stripe_event,
    reporting_currency_filter,
    snapshot_provenance,
)
from app.services.query_helpers import fetch_rows

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
    """The snapshot rows, each carrying its TS-1 measured/unmeasured provenance marker.

    The marker is computed per row from the row's own values (`snapshot_provenance`) and attached
    with `model_copy`: it is not a column and cannot be supplied by a caller, so the wire can never
    disagree with the values it describes.  The seven pre-TS-1 keys are unchanged and in order.
    """
    with refusal_guard():
        rows = await list_revenue_snapshots(db, limit=limit)
    return [
        RevenueSnapshotResponse.model_validate(row).model_copy(update=snapshot_provenance(row))
        for row in rows
    ]


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


def _parse_signature_header(header: str) -> tuple[str | None, list[str]]:
    """`(timestamp, v1 candidates)` from a `Stripe-Signature` header.

    Every `v1=` value is kept, in header order (R32, secret rotation): Stripe documents that a
    rotation can deliver several v1 signatures in one header, and the verifier must accept a
    match with ANY of them — the SDK does exactly that (`stripe/_webhook.py:81`,
    `any(secure_compare(expected, s) for s in signatures)`); this fallback used to keep only the
    last one.  A duplicate `t=` still resolves last-wins (unchanged since R28).
    """
    timestamp: str | None = None
    signatures: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            timestamp = value
        elif key == "v1":
            signatures.append(value)
    return timestamp, signatures


def _stdlib_verify(
    raw_body: bytes,
    signature_header: str,
    secret: str,
    tolerance: int,
) -> dict[str, Any]:
    """Stripe's documented scheme without the SDK: HMAC-SHA256 over `t.payload`.

    Accept-any over every `v1=` candidate (secret rotation), each comparison constant-time
    (`hmac.compare_digest`) with `any` short-circuiting exactly like the SDK; the timestamp
    tolerance is unchanged (R32).
    """
    timestamp, candidates = _parse_signature_header(signature_header)
    if not timestamp or not any(candidates):
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
    # V12/V1 RECONCILIATION (the shipped contract wins — decision + evidence in
    # wave-v12/f2-defects/RECEIPT.md): `tolerance=0` passed to this verifier is the documented
    # escape hatch — it SKIPS the age check.  Pinned by the shipped R28 suite
    # (`test_stdlib_verify_tolerance_zero_disables_the_window`) and R32
    # (`test_tolerance_boundaries_are_not_loosened`, "the accept-any fix must not have touched
    # the escape hatch either"), and it is the installed stripe SDK's own semantics
    # (`stripe/_webhook.py:89`: `if tolerance and timestamp < time.time() - tolerance` — a
    # falsy tolerance performs no age check).  The F21 floor lives at the ENTRY POINTS instead
    # (`_signature_tolerance` + `construct_stripe_event`): a CONFIGURED non-positive value can
    # never disable the guard, while this low-level seam keeps its shipped semantics.
    if tolerance and abs(int(time.time()) - signed_at) > tolerance:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Stripe signature timestamp outside the {tolerance}s tolerance",
        )
    # The exact bytes Stripe signed: "<timestamp>.<raw body>".  Bytes, not a decoded
    # string — a non-UTF8 byte would otherwise be rewritten by errors="replace".
    signed_payload = timestamp.encode("utf-8") + b"." + raw_body
    expected = hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()
    # Accept ANY matching candidate (secret rotation).  Each comparison is constant-time;
    # `any` short-circuits like the SDK's `any(secure_compare(...))`.  R32: the old code kept
    # only the LAST v1, so a rotated header whose matching v1 was not last was refused.
    if not any(hmac.compare_digest(expected, candidate) for candidate in candidates):
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
    "measured absent" note was stale.)  Both branches verify a rotated multi-`v1=`
    header by accept-any (R32, see `_stdlib_verify`).
    """
    window = DEFAULT_SIGNATURE_TOLERANCE if tolerance is None else int(tolerance)
    if window <= 0:
        # F21 floor, kept AT THE ENTRY POINT (V12/V1 reconciliation): the caller-facing window
        # can never be "off".  The low-level `_stdlib_verify(..., 0)` seam keeps its shipped
        # R28/R32 escape-hatch semantics untouched (see that function).
        window = DEFAULT_SIGNATURE_TOLERANCE
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
    """The replay-guard tolerance in seconds — a non-positive value is the DEFAULT, never
    "off" (F21/V12-W-C: a configured 0 silently accepted any-age signatures)."""
    value = int(getattr(settings, "stripe_signature_tolerance", DEFAULT_SIGNATURE_TOLERANCE))
    return value if value > 0 else DEFAULT_SIGNATURE_TOLERANCE


@router.post("/stripe/webhook", response_model=StripeWebhookResponse)
async def stripe_webhook(
    request: Request, db: AsyncSession = Depends(get_db)
) -> StripeWebhookResponse:
    """Raw-body webhook.  No X-API-Key (Stripe cannot send one).

    The 200 body is the declared `StripeWebhookResponse` (T12/D20) — the handler returns that
    model, so the document and the response cannot drift apart.
    """
    secret = settings.stripe_webhook_secret or ""
    if not secret:
        # L3-04 (V17): this used to be an UNCODED `HTTPException(500, detail="…")` — a plain
        # string detail, so neither the named envelope nor `X-Meridian-Error-Code` reached Stripe
        # (a public route: every delivery was an unexplained 500 and the operator had no code to
        # grep).  The same missing-configuration condition elsewhere in the API is `E_CONFIG`
        # (`api/v1/data.py`: APPROVAL_SECRET unset -> 503 + how_to_fix); this route now answers
        # the same way.  Nothing was verified and nothing was stored.
        raise PolsiaError(
            E_CONFIG,
            "the Stripe webhook secret is not configured, so no delivery can be verified",
            how_to_fix=(
                "set STRIPE_WEBHOOK_SECRET in .env to the endpoint's signing secret "
                "(Stripe dashboard -> Developers -> Webhooks), then restart the service"
            ),
            context={"setting": "STRIPE_WEBHOOK_SECRET"},
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
    # T14/D21 — the refund producer.  Extract the PaymentIntent id a refund would need from the
    # delivery's own SHAPE (charge/invoice -> `payment_intent`; a payment_intent object -> `id`)
    # and persist it on the event row, so the finance agent's reader has a durable, machine-
    # readable answer instead of Stripe's per-object shapes.  A shape that carries none stays
    # `None` (the column of the record answers "not measured") — never the event id, never the
    # charge id: `integrations.refund_payment` refuses a non-`pi_` id BY NAME, so writing a
    # substitute would only move the refusal one layer later.
    payment_intent_id = extract_payment_intent_id(event)
    with refusal_guard():
        await record_stripe_event(
            db,
            stripe_event_id=event_id,
            event_type=str(event.get("type") or "unknown"),
            raw_payload=event,
            customer_id=customer_id,
            amount_cents=amount_cents,
            currency=currency,
            payment_intent_id=payment_intent_id,
        )
    return StripeWebhookResponse(received=True)


# ── GET /finance/planning (V9 B2, plan §9 lines 451-456) ──────────────────────────────────────
# The frozen object: burn_line_items / capacity / margin_template, inputs read from config and
# the pricing catalog.  Anything unmeasured is null + a reason in `reasons` — NEVER 0.  The
# planning math answers with the LOW band of the confirmed idea's price (conservative), and every
# derived figure names the row or field it came from.

#: Days of run-ledger history the AI-cost allocation reads.
AI_SPEND_WINDOW_DAYS = 30


def _exact(value: Any) -> Decimal:
    """Exact decimal view of a measured number (F10/V12-W-C): via `str`, so a float input keeps
    its shortest-repr decimal value instead of its binary noise."""
    return Decimal(str(value))


def margin_fraction(price: Any, ai_cost: Any, human_hours: Any, human_rate: Any) -> float | None:
    """`(price - (ai_cost + human_hours * human_rate)) / price` — null unless every input is measured.

    A non-positive contribution is not a margin (the offer loses money per unit), so it is refused
    as `None` rather than reported as a negative percentage that reads like a margin.  The math is
    Decimal (F10): binary-float division drifted the last digits of the returned fraction.
    """
    if any(value is None for value in (price, ai_cost, human_hours, human_rate)):
        return None
    price_d = _exact(price)
    contribution = price_d - (_exact(ai_cost) + _exact(human_hours) * _exact(human_rate))
    if price_d <= 0 or contribution <= 0:
        return None
    return float(contribution / price_d)


def break_even_clients(
    burn_usd: Any, price: Any, ai_cost: Any, human_hours: Any, human_rate: Any
) -> int | None:
    """Clients needed to cover monthly burn at the given contribution — null when unmeasurable.

    Decimal + Decimal `ceil` (F10/V12-W-C): `math.ceil(float(burn) / float(contribution))` was
    off by one at exact boundaries (21 / 0.7 answered 31).
    """
    if burn_usd is None or any(
        value is None for value in (price, ai_cost, human_hours, human_rate)
    ):
        return None
    contribution = _exact(price) - (_exact(ai_cost) + _exact(human_hours) * _exact(human_rate))
    if contribution <= 0:
        return None
    return math.ceil(_exact(burn_usd) / contribution)


def pricing_guardrails_for(idea: Any, pricing_model: Any) -> list[str]:
    """R3's pricing-model anti-patterns as deterministic warnings (empty when clean).

    Inspects STRUCTURED fields first (`pricing_model` / `model` / `billing` on the confirmed idea
    or the company's pricing model), then the idea's own text; each warning names the term it
    matched, so a warning is never an invented accusation.  Anti-patterns (RESEARCH §4d /
    Appendix A2): hourly billing (speed earns less), uncapped flat fees (AI usage eats the
    margin), performance/rev-share (attribution disputes — avoid in v1).
    """
    evidence: list[str] = []
    if isinstance(idea, dict):
        evidence.extend(
            value
            for key in ("name", "one_liner", "unit_cost_sketch", "pricing_model", "model", "billing")
            if isinstance((value := idea.get(key)), str)
        )
    elif isinstance(idea, str):
        evidence.append(idea)
    if isinstance(pricing_model, dict):
        evidence.extend(
            value
            for key in ("model", "type", "billing", "pricing_model")
            if isinstance((value := pricing_model.get(key)), str)
        )
    elif isinstance(pricing_model, str):
        evidence.append(pricing_model)
    text = " ".join(evidence).lower()
    if not text:
        return []

    warnings: list[str] = []
    if "hourly" in text or "/hr" in text or "per hour" in text:
        matched = "hourly" if "hourly" in text else ("/hr" if "/hr" in text else "per hour")
        warnings.append(
            f"Hourly pricing punishes speed (matched {matched!r}): as delivery gets faster the same "
            "work earns less. Prefer fixed-scope productized tiers with usage caps (R3; RESEARCH §4d)."
        )
    capped = "usage cap" in text or "caps" in text or ("capped" in text and "uncapped" not in text)
    if ("flat" in text or "fixed fee" in text) and not capped:
        warnings.append(
            "Flat fee with no usage cap: variable AI usage comes straight out of the margin when "
            "the fee is fixed. Add an explicit usage cap or a metered tier (R3; RESEARCH §4d)."
        )
    if "rev-share" in text or "revenue share" in text or "% of revenue" in text or "performance" in text:
        warnings.append(
            "Performance / rev-share pricing needs attribution this business cannot settle yet "
            "(attribution disputes are common) — avoid it in v1 (R3; Appendix A2)."
        )
    return warnings


async def _burn_line_items(db: AsyncSession, reasons: dict[str, str]) -> tuple[list[dict[str, Any]], int]:
    """The current month's measured expenses, one line per category — USD-only and COUNTED.

    D4-R2 / A2A7-03.  This used to sum `expense_records.amount_cents` over EVERY currency, so one
    EUR 500.00 row beside a USD 100.00 row served `monthly_usd: 600.0` — a euro priced as dollars
    on the one finance sum A3 never reached (measured: `probe_attacks.out.json` §C).  The sum is
    now scoped through `finance_service.reporting_currency_filter` — the ONE definition of the
    rule every other money sum uses — and what it left out is never silent:

      * a category whose rows are ALL in another currency (or carry no amount) is UNMEASURED:
        `monthly_usd: None` + its `reason` (never a fabricated `$0.00` and never the foreign sum);
      * a category with SOME USD rows keeps the USD subtotal and its `source` NAMES the excluded
        count — the string the panel already prints beside the number (`finance/page.tsx` reads
        `item.source` for a measured row), so the partial is labelled without a new wire key;
      * the second return value is the month's excluded-row COUNT, which `build_planning_view`
        uses to refuse a break-even computed from a partial burn.
    """
    month_start = today_in().replace(day=1)
    month_end = (month_start + timedelta(days=32)).replace(day=1)
    rows = await fetch_rows(
        db,
        select(
            ExpenseRecord.category,
            func.sum(
                case(
                    (reporting_currency_filter(ExpenseRecord.currency), ExpenseRecord.amount_cents),
                    else_=None,
                )
            ).label("usd_cents"),
            func.sum(
                case((reporting_currency_filter(ExpenseRecord.currency), 1), else_=0)
            ).label("usd_rows"),
            func.count().label("rows"),
        )
        .where(ExpenseRecord.date >= month_start, ExpenseRecord.date < month_end)
        .group_by(ExpenseRecord.category)
        .order_by(ExpenseRecord.category),
        what="planning burn line items",
    )
    if not rows:
        reasons["burn_line_items"] = (
            f"no expense records for {month_start:%Y-%m} — monthly burn is unmeasured"
        )
        return [], 0
    items: list[dict[str, Any]] = []
    excluded_total = 0
    for category, usd_cents, usd_rows, row_count in rows:
        row_count = int(row_count or 0)
        usd_row_count = int(usd_rows or 0)
        excluded = row_count - usd_row_count
        excluded_total += excluded
        label = str(category or "uncategorized")
        if usd_cents is None:
            # Nothing was measured in the reporting currency. Keep the row VISIBLE as a null row
            # carrying its cause instead of dropping it (v14 S3-05 — the panel renders
            # `monthly_usd: null` as the em dash plus this `reason`; the `reasons` map keeps the
            # per-category key as the response-level provenance of the same fact).
            if usd_row_count == 0 and excluded:
                reason = (
                    f"all {row_count} expense record(s) in this category are in another or "
                    f"unstated currency than {REPORTING_CURRENCY}; nothing was converted, so the "
                    "amount is unmeasured"
                )
            else:
                reason = "expenses in this category carry no measured amount"
                if excluded:
                    reason += (
                        f" ({excluded} record(s) in another or unstated currency than "
                        f"{REPORTING_CURRENCY} were excluded too)"
                    )
            reasons[f"burn_line_items.{category}"] = reason
            items.append(
                {
                    "label": label,
                    "monthly_usd": None,
                    "source": None,
                    "reason": reason,
                }
            )
            continue
        source = f"expense_records {month_start:%Y-%m}"
        if excluded:
            # The provenance slot the panel prints for a MEASURED row: it says the subtotal is
            # scoped, so an operator can never read 100.00 as the whole category.
            source = (
                f"{source} ({REPORTING_CURRENCY.upper()} only: {excluded} record(s) in another "
                "or unstated currency excluded)"
            )
        items.append(
            {
                "label": label,
                "monthly_usd": round(float(usd_cents) / 100, 2),
                "source": source,
            }
        )
    return items, excluded_total


async def _ai_spend_window_truth(db: AsyncSession) -> SpendTruth:
    """The last 30 company-local days of AI spend, as the SHIPPED `SpendTruth` shape (D4-R3).

    The defect this replaces: `_ai_spend_window_usd` read every `cost_usd` in the window and
    returned the SUM OF THE PRICED PART — one priced run beside one finished run whose cost was
    never recorded served `3.0` as "the 30-day spend" (measured: `probe_attacks.out.json` §E).
    That is the same lie `AgentRun`'s day reader refuses (`finance_service.day_spend_truth`,
    A1-03/F-19) and `task_service.run_totals` fixed per task, so it is fixed with the SAME
    vocabulary rather than a third shape:

      * `finished_unpriced` counts the rows that ENDED without a cost — their presence makes the
        window UNMEASURED (`total is None`), because a partial sum is not a total;
      * `in_flight` counts the runs still open: their cost is bounded by the per-run cap, not
        guessed into the total (`SpendTruth`'s own rule);
      * `per_agent` carries only agents with a measured sum (a `0` entry would be a fabricated
        zero one level down).

    Two deliberate differences from `day_spend_truth`, both the planning view's SHIPPED contract:
    the window is 30 days (not the local day) and it is bounded on BOTH edges — a future-dated
    `started_at` must not inflate a projection (the correction `_month_bounds` records) — and an
    ABSENT/entirely-unpriced ledger stays UNMEASURED (None) instead of SpendTruth's measured `0`
    for an absent DAY: the planning panel must not price a per-client AI cost from a ledger that
    never measured one.  (`finance_service.day_spend_truth` owns the day window and is not editable
    here; unifying the two needs a window parameter there — see REQUESTS.)
    """
    start, _ = day_window(db, today_in() - timedelta(days=AI_SPEND_WINDOW_DAYS - 1))
    _, end = day_window(db, today_in())
    finished_unpriced = and_(AgentRun.cost_usd.is_(None), AgentRun.ended_at.is_not(None))
    open_unpriced = and_(AgentRun.cost_usd.is_(None), AgentRun.ended_at.is_(None))
    grouped = (
        select(
            AgentRun.agent_type,
            func.sum(AgentRun.cost_usd).label("measured"),
            func.count().label("runs"),
            func.sum(case((finished_unpriced, 1), else_=0)).label("finished_unpriced"),
            func.sum(case((open_unpriced, 1), else_=0)).label("in_flight"),
        )
        .where(AgentRun.started_at >= start, AgentRun.started_at < end)
        .group_by(AgentRun.agent_type)
    )
    rows = await fetch_rows(db, grouped, what="ai spend window")
    per_agent: dict[str, Decimal] = {}
    finished_unpriced_total = in_flight_total = measured_runs = 0
    for agent, measured, runs, unpriced_runs, open_runs in rows:
        finished_unpriced_total += int(unpriced_runs or 0)
        in_flight_total += int(open_runs or 0)
        measured_runs += int(runs) - int(unpriced_runs or 0) - int(open_runs or 0)
        converted = money(measured)
        if converted is not None:
            per_agent[str(agent)] = converted
    if finished_unpriced_total:
        total: Decimal | None = None      # a partial sum is not a total
    elif not per_agent:
        total = None                       # nothing was measured: not a priced $0.00
    else:
        total = sum(per_agent.values(), Decimal("0"))
    return SpendTruth(
        total=total,
        per_agent=per_agent,
        finished_unpriced=finished_unpriced_total,
        in_flight=in_flight_total,
        measured_runs=measured_runs,
    )


async def _ai_spend_window_usd(db: AsyncSession) -> float | None:
    """The 30-day AI spend when it is a TOTAL — otherwise None (never the priced part).

    Thin reader over `_ai_spend_window_truth`: `None` means the window is unmeasured (no run
    reported a cost), so the caller must refuse to allocate rather than divide a partial.
    """
    truth = await _ai_spend_window_truth(db)
    return float(truth.total) if truth.total is not None else None



async def build_planning_view(db: AsyncSession) -> dict[str, Any]:
    """The frozen planning object (plan §9 lines 451-456) with the tax + guardrail additions.

    The capacity inputs (hours per client) come from the DATED catalog snapshot; the response
    therefore carries the computed `catalog_as_of` / `staleness_days` / `stale` provenance so the
    numbers can never read as live market truth (owner amendment, wave V9).

    THE REASON CONTRACT (wave v14 S3-05 — ONE seam, consumed by the Finance page's Planning panel,
    `frontend/src/app/finance/page.tsx`; do not rename a key here without the FE change):
      - every input this view could not measure stays `null`, and its cause is recorded in the
        `reasons` map keyed 1:1 to the field it explains: `capacity.<field>`,
        `margin_template.<field>`, `burn_line_items` (the whole month) / `burn_line_items.<category>`
        (one category);
      - a burn row whose category carried no measured amount is still ANSWERED — as
        `monthly_usd: null` with the same string inlined as the row's `reason`;
      - a reason is written for an UNMEASURED field ONLY: the panel prints every reason it finds,
        so a reason beside a measured value would read as a claim about a number that exists.
    """
    reasons: dict[str, str] = {}
    today = today_in()
    provenance = pricing_service.catalog_provenance(today)
    company = await get_company(db)
    burn_line_items, excluded_currency_rows = await _burn_line_items(db, reasons)
    # A null burn row is UNMEASURED (see `_burn_line_items`): summing it would fabricate a 0, so the
    # total only ever adds rows that carry a measured amount — and with none measured it is null.
    measured_burn = [
        float(item["monthly_usd"])
        for item in burn_line_items
        if item["monthly_usd"] is not None
    ]
    burn_total = round(sum(measured_burn), 2) if measured_burn else None

    idea = (
        company.confirmed_idea
        if company is not None and isinstance(company.confirmed_idea, dict)
        else None
    )
    price: float | None = None
    price_band = idea.get("price_band") if isinstance(idea, dict) else None
    if (
        isinstance(price_band, list | tuple)
        and price_band
        and isinstance(price_band[0], int | float)
        and not isinstance(price_band[0], bool)
    ):
        price = float(price_band[0])
    if price is None:
        reasons["margin_template.price"] = (
            "no confirmed idea with a numeric price band is configured (run the planner and "
            "confirm a candidate)"
        )

    row = pricing_service.match_idea_row(
        str(idea.get("name") or "") if isinstance(idea, dict) else None
    )
    hours = row.get("hours") if isinstance(row, dict) else None
    hours_lo: float | None = None
    hours_hi: float | None = None
    if isinstance(hours, dict) and hours.get("low") is not None and hours.get("high") is not None:
        hours_lo, hours_hi = float(hours["low"]), float(hours["high"])
    else:
        reason = (
            f"the catalog row {row['service']!r} states no workload hours"
            if isinstance(row, dict)
            else "no catalog row matches the confirmed idea, so hours per client are unmeasured"
        )
        reasons["capacity.hours_per_client_lo"] = reason
        reasons["capacity.hours_per_client_hi"] = reason
        # `margin_template.human_hours` IS hours_hi: mirror the same cause under the margin key so
        # the panel can read every null field 1:1 from this map (v14 S3-05 contract).
        reasons["margin_template.human_hours"] = reason

    owner_hours = company.owner_hours_per_week if company is not None else None
    monthly_hours: float | None = None
    if isinstance(owner_hours, int | float) and not isinstance(owner_hours, bool):
        monthly_hours = round(float(owner_hours) * 52 / 12, 2)
    if monthly_hours is None:
        reasons["capacity.max_clients"] = "owner_hours_per_week is not set in the company profile"

    max_clients: int | None = None
    if monthly_hours is not None and hours_hi:
        max_clients = math.floor(monthly_hours / hours_hi)
    elif "capacity.max_clients" not in reasons:
        reasons["capacity.max_clients"] = (
            "hours per client are unmeasured, so capacity cannot be divided honestly"
        )

    ai_truth = await _ai_spend_window_truth(db)
    ai_spend = float(ai_truth.total) if ai_truth.total is not None else None
    ai_cost: float | None = None
    if max_clients and ai_spend is not None:
        ai_cost = round(ai_spend / max_clients, 4)
    elif ai_spend is None:
        # D4-R3: a window with a finished-but-unpriced run is UNMEASURED, and the reason NAMES the
        # count — the priced part is never served as the window's spend, so it is never allocated.
        if ai_truth.finished_unpriced:
            reasons["margin_template.ai_cost"] = (
                f"{ai_truth.finished_unpriced} finished run(s) in the last {AI_SPEND_WINDOW_DAYS} "
                "days recorded no cost — the priced part is not a total, so the window's AI spend "
                "is unmeasured and cannot be allocated per client"
            )
        else:
            reasons["margin_template.ai_cost"] = (
                f"no AI cost measured in the last {AI_SPEND_WINDOW_DAYS} days (no runs reported a cost)"
            )
    else:
        reasons["margin_template.ai_cost"] = (
            "no client capacity is measured, so the measured AI spend cannot be allocated per client"
        )

    # V23/L3: the rate is READ from company_config (migration 0011), never hard-coded.  Absent
    # stays None + a reason that says where to set it; present makes the margin measurable — but
    # ONLY together with capacity (owner hours -> max_clients -> ai_cost), which `margin_fraction`
    # and the `ai_cost` branch above already require.  A reason is written for an unmeasured field
    # ONLY: the panel prints every reason it is given.
    human_rate = measured_human_hourly_rate(company)
    if human_rate is None:
        reasons["margin_template.human_rate"] = (
            "no human hourly rate is set — set it on the Finance page's Human rate card, and "
            "this margin template will use it"
        )

    margin = margin_fraction(price, ai_cost, hours_hi, human_rate)
    if margin is None:
        reasons["margin_template.margin"] = (
            "needs a measured price, ai_cost, human_hours and human_rate — see the fields above"
        )

    mrr_at_capacity: float | None = None
    if max_clients is not None and price is not None:
        mrr_at_capacity = round(max_clients * price, 2)
    else:
        reasons["capacity.mrr_at_capacity"] = (
            "needs both a measured client capacity and the confirmed idea's price band"
        )

    break_even = break_even_clients(burn_total, price, ai_cost, hours_hi, human_rate)
    if excluded_currency_rows:
        # D4-R2: the burn subtotal left foreign-currency rows out, so it is PARTIAL — a break-even
        # computed from it would be fabricated precision, even when the arithmetic yields a number.
        # The count is named instead of the figure.
        break_even = None
        reasons["capacity.break_even_clients"] = (
            f"{excluded_currency_rows} expense record(s) for {today.replace(day=1):%Y-%m} are "
            f"in another or unstated currency than {REPORTING_CURRENCY} and were EXCLUDED "
            "(nothing was converted), so the month's burn is a partial USD subtotal — "
            "break-even needs a complete month"
        )
    elif break_even is None:
        reasons["capacity.break_even_clients"] = (
            "needs measured monthly burn and a positive per-client contribution (see the fields above)"
        )

    return {
        "burn_line_items": burn_line_items,
        "capacity": {
            "hours_per_client_lo": hours_lo,
            "hours_per_client_hi": hours_hi,
            "max_clients": max_clients,
            "mrr_at_capacity": mrr_at_capacity,
            "break_even_clients": break_even,
            "hours_per_client_source": str(row["service"]) if isinstance(row, dict) else None,
        },
        "margin_template": {
            "price": price,
            "ai_cost": ai_cost,
            "human_hours": hours_hi,
            "human_rate": human_rate,
            "margin": margin,
        },
        "reasons": reasons,
        "pricing_guardrails": pricing_guardrails_for(
            idea, company.pricing_model if company is not None else None
        ),
        "sales_tax_note": pricing_service.SALES_TAX_NOTE,
        "tax_disclaimer": pricing_service.TAX_DISCLAIMER,
        **provenance,
    }


@router.get(
    "/planning",
    response_model=FinancePlanningResponse,
    # V20/L1: the payload is assembled from dicts; `exclude_unset` keeps an ABSENT optional key
    # absent (`burn_line_items[].reason`, the F24 provenance `reason`). A `null` we never wrote
    # would both change the wire and read as "unmeasured" where the fact simply does not apply.
    response_model_exclude_unset=True,
    dependencies=[Depends(verify_api_key)],
)
async def finance_planning(db: AsyncSession = Depends(get_db)) -> FinancePlanningResponse:
    """The planning view: burn line items, capacity, margin template, guardrails, tax notes.

    V20/L1 (card L2-06): `dict[str, Any]` -> the declared `FinancePlanningResponse`, so the
    v14 S3-05 reason contract is visible in OpenAPI. The service dict is VALIDATED in place — not
    rebuilt — and the field order matches the dict's build order, so the bytes do not move.
    """
    with refusal_guard():
        payload = await build_planning_view(db)
    return FinancePlanningResponse.model_validate(payload)
