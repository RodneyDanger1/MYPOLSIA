"""Support inbox — the owner's submit/read surface (R17) + the inbound-email webhook (W5).

WHY the owner route exists (RD12 receipt §7 REQUESTS 1+2): the customer_support consumer shipped in
RD12 (a task's `metadata.inbox_item` reaches the agent's prompt, quarantined), but NOTHING produced
an inbox item — the 3h `email-inbox-sweep` ran title-only.  The owner route + the sweep close that
chain end to end.

WHY the webhook exists (W5, 2026-10-28; honest gap audit `recon-2026-10-28/our-abilities/
OUR-ABILITIES.md` §3 item 11: "no inbound mail poll — the support inbox is filled only by the
owner's POST"): a real inbound-email provider (Postmark / SendGrid-style JSON) POSTs the message to
`/api/v1/support/inbox/inbound` and the item lands EXACTLY as the owner's POST writes it
(`support_inbox_service.ingest_inbound_item` -> `submit_item`), so the same attach sweep creates the
`customer_support` task.  No IMAP connector, no polling, no new dependency.

AUTH — TWO trust models, two credentials (deliberately not one):
  * the OWNER routes (`POST`/`GET /api/v1/support/inbox`) carry `Depends(verify_api_key)` on the
    ROUTE — the same auth they always had; the dependency moved from the router to the routes when
    the webhook landed (`finance.py`'s `/stripe/webhook` and `outreach.py`'s `/unsubscribe` set the
    precedent).  These are CUSTOMER EMAILS: the API-key holder and nobody else.
  * the PROVIDER route (`POST /api/v1/support/inbox/inbound`) is authenticated by a DEDICATED
    shared secret, `SUPPORT_INBOUND_SECRET`, in `X-Polsia-Inbound-Secret`, compared in constant
    time.  It is NOT the owner API key on purpose: a third party's webhook configuration must never
    have to hold the key that opens the whole dashboard API.  Unset secret => the NAMED `E_CONFIG`
    503 (never an open inbox); missing/wrong secret => `E_AUTH` 401.  Neither the secret nor the
    customer body is ever echoed in a refusal (pydantic's own `str(ValidationError)` IS never used:
    it embeds the offending input).

VALIDATION — one authority, one envelope.  The body is received as `Any` so that this module's
parser + `submit_item` (the shipped single validation authority) can name the offending field in
`context.field`; a typed FastAPI body would answer the framework's UNCODED `{"detail": [...]}` 422
for a missing/typed field and lose the project envelope.  `SupportInboundPayload` is still published
in OpenAPI as the documented request shape (it is also the ALIAS layer: `from`/`from_address`,
`MessageID`, `TextBody`, …).

Idempotency: `provider_message_id` is the key.  A re-delivery answers 200 `created:false` with the
STORED row unchanged and writes no second item/task; a new id answers 201 `created:true`.  The one
audit row per delivery uses the stable action `support_inbox_inbound`.  The row-level guarantee
under two racing FIRST deliveries is the unique constraint (`ON CONFLICT DO NOTHING`): exactly one
row exists and both callers answer its id — see `support_inbox_service.InboundIngestResult`.
"""
from __future__ import annotations

import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, Query, Response, status
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.api.v1.totals import TOTAL_HEADER, declared_total
from app.config import settings
from app.core.errors import E_AUTH, E_CONFIG, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.support import SupportInboxItem
from app.schemas.support import SupportInboxItemCreate, SupportInboxItemResponse
from app.services.support_inbox_service import ingest_inbound_item, list_items, submit_item

router = APIRouter(prefix="/support", tags=["support"])

#: The header an inbound-email provider presents.  DEDICATED: never the owner `X-API-Key`.
INBOUND_SECRET_HEADER = "X-Polsia-Inbound-Secret"

#: The documented payload fields and the aliases a real provider already sends.  The FIRST name in
#: each tuple is the documented one; `AliasChoices` picks the first alias present in the body, and
#: `_ALIAS_TO_FIELD` maps any of them back to the canonical name a refusal reports.
_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "provider_message_id": (
        "provider_message_id",
        "providerMessageId",
        "message_id",
        "messageId",
        "MessageID",  # Postmark
    ),
    # the wire spells it `from` (a Python keyword), the row spells it `from_address`
    "from_address": ("from", "from_address", "fromAddress", "From", "sender"),
    "subject": ("subject", "Subject"),
    "body": ("body", "text", "text_body", "textBody", "TextBody"),
    "received_at": ("received_at", "receivedAt"),
    "provider": ("provider", "provider_name", "providerName"),
}

_ALIAS_TO_FIELD: dict[str, str] = {
    alias: field for field, aliases in _FIELD_ALIASES.items() for alias in aliases
}


class SupportInboundPayload(BaseModel):
    """The documented inbound payload (also published as the route's OpenAPI requestBody).

    Every field is OPTIONAL here on purpose: this model is the NORMALISER (aliases + string -> the
    canonical names `submit_item` takes).  REQUIREDNESS is enforced by the one validation authority
    (`support_inbox_service.submit_item`), so a missing field answers the project's named
    `E_VALIDATION` with `context.field` instead of FastAPI's uncoded 422 array.  Unknown keys are
    allowed (`extra="allow"`): providers send more than the minimal shape, and an extra key is not
    a reason to drop a customer message.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    provider_message_id: str | None = Field(
        default=None, validation_alias=AliasChoices(*_FIELD_ALIASES["provider_message_id"])
    )
    from_address: str | None = Field(
        default=None, validation_alias=AliasChoices(*_FIELD_ALIASES["from_address"])
    )
    subject: str | None = Field(
        default=None, validation_alias=AliasChoices(*_FIELD_ALIASES["subject"])
    )
    body: str | None = Field(
        default=None, validation_alias=AliasChoices(*_FIELD_ALIASES["body"])
    )
    received_at: datetime | None = Field(
        default=None, validation_alias=AliasChoices(*_FIELD_ALIASES["received_at"])
    )
    provider: str | None = Field(
        default=None, validation_alias=AliasChoices(*_FIELD_ALIASES["provider"])
    )

    @field_validator("received_at")
    @classmethod
    def _received_at_must_be_aware(cls, value: datetime | None) -> datetime | None:
        """A timezone-less arrival instant is refused by NAME, never read as UTC.

        `submit_item` enforces the same rule (it is the writer's authority); enforcing it here as
        well is what makes the refusal name the WIRE field (`received_at`) instead of a value the
        caller cannot see, and it keeps a naive stamp out of the DB path entirely.
        """
        if value is not None and value.tzinfo is None:
            raise ValueError(
                "received_at must carry a timezone offset (a timezone-less value would be read as "
                "UTC and silently reorder the attach)"
            )
        return value


class SupportInboundIngestResponse(BaseModel):
    """The webhook's answer: `created` says NEW, `item_id` is the row, `item` is the stored row."""

    created: bool
    item_id: int
    provider: str | None = None
    item: SupportInboxItemResponse


def _parse_inbound_payload(raw: object) -> SupportInboundPayload:
    """Normalise the raw body; every problem is a NAMED `E_VALIDATION` carrying `context.field`.

    The pydantic error STRING is never used in a message: pydantic v2 embeds the offending INPUT in
    it, and that input is the customer's own message — a refusal must never reflect it back.
    """
    if raw is None:
        raise PolsiaError(
            E_VALIDATION,
            "the inbound support payload is required: POST the documented JSON object "
            "({provider_message_id, from, subject, body, received_at?, provider?})",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"field": "payload"},
        )
    if not isinstance(raw, dict):
        raise PolsiaError(
            E_VALIDATION,
            f"the inbound support payload must be a JSON object; received {type(raw).__name__}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"field": "payload"},
        )
    try:
        return SupportInboundPayload.model_validate(raw)
    except ValidationError as exc:
        errors = exc.errors()
        if errors:
            first = errors[0]
            loc = first.get("loc") or ()
            field = _ALIAS_TO_FIELD.get(str(loc[0]), "payload") if loc else "payload"
            # `msg` is pydantic's STATIC template ("Input should be a valid string") — never the
            # offending value, which is what keeps the customer body out of the refusal.
            reason = first.get("msg", "invalid value")
        else:  # pragma: no cover - pydantic always reports at least one error on failure
            field, reason = "payload", "invalid value"
        raise PolsiaError(
            E_VALIDATION,
            f"inbound support payload field {field!r} is invalid: {reason}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"field": field},
        ) from exc


def _inbound_secret_matches(provided: str | None) -> bool:
    """Constant-time compare of the provider secret — or the NAMED `E_CONFIG` refusal when unset.

    Same byte-wise `secrets.compare_digest` idiom the owner key uses (`app/core/security.py:12-34`)
    so a header that is not UTF-8-decodable is simply a WRONG secret (401), never a 500.  Neither
    operand is logged or returned; the refusal names the SETTING, never a value.
    """
    expected = settings.support_inbound_secret or ""
    if not expected:
        raise PolsiaError(
            E_CONFIG,
            "the inbound support-inbox webhook is not configured: SUPPORT_INBOUND_SECRET is "
            "unset, so no caller can be authenticated",
            how_to_fix=(
                "set SUPPORT_INBOUND_SECRET in .env to a long random value (for example "
                "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"`), give the "
                f"provider the same value in the {INBOUND_SECRET_HEADER} header, and restart the "
                "API — while it is unset this route refuses every delivery by design"
            ),
            context={"setting": "SUPPORT_INBOUND_SECRET"},
        )
    try:
        return secrets.compare_digest((provided or "").encode("utf-8"), expected.encode("utf-8"))
    except UnicodeError:  # lone surrogates cannot round-trip UTF-8 — not a possible secret value
        return False


async def require_inbound_secret(
    x_polsia_inbound_secret: str | None = Header(default=None, alias=INBOUND_SECRET_HEADER),
) -> None:
    """Dependency for the provider route: the shared secret, or a NAMED refusal."""
    if not _inbound_secret_matches(x_polsia_inbound_secret):
        raise PolsiaError(
            E_AUTH,
            f"the {INBOUND_SECRET_HEADER} header is missing or does not match the configured "
            "inbound secret",
            how_to_fix=f"send SUPPORT_INBOUND_SECRET's value in the {INBOUND_SECRET_HEADER} header",
        )


@router.post(
    "/inbox",
    response_model=SupportInboxItemResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(verify_api_key)],
)
async def submit_inbox_item(
    payload: SupportInboxItemCreate, db: AsyncSession = Depends(get_db)
) -> SupportInboxItemResponse:
    """Store one customer message (validated by the service; idempotent on provider id)."""
    with refusal_guard():
        item = await submit_item(
            db,
            provider_message_id=payload.provider_message_id,
            from_address=payload.from_address,
            subject=payload.subject,
            body=payload.body,
            received_at=payload.received_at,
        )
    return SupportInboxItemResponse.model_validate(item)


@router.post(
    "/inbox/inbound",
    response_model=SupportInboundIngestResponse,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_inbound_secret)],
    responses={
        200: {
            "model": SupportInboundIngestResponse,
            "description": (
                "Re-delivery of a provider_message_id that is already stored: `created: false`, "
                "the stored row unchanged, no new item/task."
            ),
        }
    },
    # The body is accepted as `Any` so the named-refusal layer can own validation (see the module
    # docstring); the DOCUMENTED request shape is still published for clients and OpenAPI.
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": SupportInboundPayload.model_json_schema()}},
        }
    },
)
async def ingest_inbound_message(
    response: Response,
    payload: Any = Body(default=None),
    db: AsyncSession = Depends(get_db),
) -> SupportInboundIngestResponse:
    """Ingest one inbound-email-provider delivery (201 new / 200 re-delivery).

    The provider route: `X-Polsia-Inbound-Secret` is the only credential (the owner key is NOT
    accepted — a webhook must not carry the key that opens the dashboard).  The item lands
    `pending` with the same fields the owner's POST writes, so the 3h attach sweep creates the
    `customer_support` task; a re-delivery of the same `provider_message_id` answers `created:false`
    with the stored row and writes no second item or task.
    """
    with refusal_guard():
        parsed = _parse_inbound_payload(payload)
        result = await ingest_inbound_item(
            db,
            provider_message_id=parsed.provider_message_id or "",
            from_address=parsed.from_address or "",
            subject=parsed.subject or "",
            body=parsed.body or "",
            received_at=parsed.received_at,
            provider=parsed.provider,
        )
    response.status_code = (
        status.HTTP_201_CREATED if result.created else status.HTTP_200_OK
    )
    return SupportInboundIngestResponse(
        created=result.created,
        item_id=int(result.item.id),
        provider=result.provider,
        item=SupportInboxItemResponse.model_validate(result.item),
    )


@router.get(
    "/inbox",
    response_model=list[SupportInboxItemResponse],
    dependencies=[Depends(verify_api_key)],
)
async def read_inbox(
    response: Response,
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=20, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[SupportInboxItemResponse]:
    """The stored items, newest first. `?status=pending|dispatched` filters; unknown → 422.

    V20/L1 (card L2-07): `X-Polsia-Total` declares the FULL filtered count (the same `WHERE`,
    without LIMIT/OFFSET) so a full page is distinguishable from a truncated one.  The body stays
    a bare array.
    """
    with refusal_guard():
        items = await list_items(db, status=status_filter, limit=limit, offset=offset)
        total = await declared_total(
            db,
            SupportInboxItem,
            [] if status_filter is None else [SupportInboxItem.status == status_filter],
            what="support inbox total",
        )
    response.headers[TOTAL_HEADER] = str(total)
    return [SupportInboxItemResponse.model_validate(item) for item in items]
