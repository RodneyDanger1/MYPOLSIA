"""Support inbox — the owner's submit/read surface (R17) + the inbound-email webhook (W5).

WHY the owner route exists (RD12 receipt §7 REQUESTS 1+2): the customer_support consumer shipped in
RD12 (a task's `metadata.inbox_item` reaches the agent's prompt, quarantined), but NOTHING produced
an inbox item — the 3h `email-inbox-sweep` ran title-only.  The owner route + the sweep close that
chain end to end.

WHY the webhook exists (W5, 2026-10-28; honest gap audit `recon-2026-10-28/our-abilities/
OUR-ABILITIES.md` §3 item 11: "no inbound mail poll — the support inbox is filled only by the
owner's POST"): a real inbound-email provider POSTs the message to
`/api/v1/support/inbox/inbound` and the item lands EXACTLY as the owner's POST writes it
(`support_inbox_service.ingest_inbound_item` -> `submit_item`), so the same attach sweep creates the
`customer_support` task.  No IMAP connector, no polling, no new dependency.

TWO PROVIDER ENCODINGS (WAVE 9, SV-W7-01 / SV-W8-04).  W5 shipped the JSON one only, which no
SendGrid Inbound Parse can speak; the route now dispatches on Content-Type BEFORE it authenticates
or parses, because the two encodings carry different credentials:

  * `multipart/form-data` — Twilio SendGrid Inbound Parse (the vendor's documented delivery:
    multipart fields, no `message_id`, an ECDSA signature over the RAW bytes or an OAuth bearer
    token).  Implemented in `support_inbox_service` (parse + verify + derive the provider id);
    the shared secret is ALSO accepted here so a non-SendGrid multipart provider can use it.
  * JSON — the shipped Postmark-style webhook, credential and behaviour unchanged.

AUTH — TWO trust models, two credentials (deliberately not one):
  * the OWNER routes (`POST`/`GET /api/v1/support/inbox`) carry `Depends(verify_api_key)` on the
    ROUTE — the same auth they always had; the dependency moved from the router to the routes when
    the webhook landed (`finance.py`'s `/stripe/webhook` and `outreach.py`'s `/unsubscribe` set the
    precedent).  These are CUSTOMER EMAILS: the API-key holder and nobody else.
  * the PROVIDER route (`POST /api/v1/support/inbox/inbound`) is authenticated by ONE of the
    provider's own mechanisms: for JSON, the DEDICATED shared secret `SUPPORT_INBOUND_SECRET` in
    `X-Meridian-Inbound-Secret` (constant time); for multipart, SendGrid's ECDSA signature
    (`X-Twilio-Email-Event-Webhook-Signature` / `-Timestamp` over the raw bytes) or its OAuth
    `Authorization: Bearer` token.  It is NOT the owner API key on purpose: a third party's webhook
    configuration must never have to hold the key that opens the whole dashboard API.  Unset JSON
    secret => the NAMED `E_CONFIG` 503 (never an open inbox); missing/wrong credentials => `E_AUTH`
    401 for every encoding (a 4xx, so the provider does not retry a delivery that cannot succeed).
    Neither a secret, a token, a signature nor the customer body is ever echoed in a refusal
    (pydantic's own `str(ValidationError)` is never used: it embeds the offending input).

VALIDATION — one authority, one envelope.  The JSON body is received as `Any` so that this
module's parser + `submit_item` (the shipped single validation authority) can name the offending
field in `context.field`; a typed FastAPI body would answer the framework's UNCODED
`{"detail": [...]}` 422 for a missing/typed field and lose the project envelope.  A body that is
not JSON at all is the same named refusal (it used to be that uncoded array).  `SupportInboundPayload`
is still published in OpenAPI as the documented JSON shape (it is also the ALIAS layer:
`from`/`from_address`, `MessageID`, `TextBody`, …), beside the documented multipart field table.

Idempotency: `provider_message_id` is the key.  A re-delivery answers 200 `created:false` with the
STORED row unchanged and writes no second item/task; a new id answers 201 `created:true`.  The one
audit row per delivery uses the stable action `support_inbox_inbound`.  The row-level guarantee
under two racing FIRST deliveries is the unique constraint (`ON CONFLICT DO NOTHING`): exactly one
row exists and both callers answer its id — see `support_inbox_service.InboundIngestResult`.
"""
from __future__ import annotations

import json
import secrets
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request, Response, status
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
from app.core.errors import E_AUTH, E_CONFIG, E_VALIDATION, ERROR_HINTS, MeridianError
from app.models.support import SupportInboxItem
from app.schemas.support import SupportInboxItemCreate, SupportInboxItemResponse
from app.services.support_inbox_service import (
    SENDGRID_SIGNATURE_HEADER,
    SENDGRID_TIMESTAMP_HEADER,
    ingest_inbound_item,
    ingest_sendgrid_inbound,
    is_multipart_form_data,
    list_items,
    media_type_of,
    submit_item,
    verify_sendgrid_oauth_token,
    verify_sendgrid_signature,
)

router = APIRouter(prefix="/support", tags=["support"])

#: The header an inbound-email provider presents.  DEDICATED: never the owner `X-API-Key`.
INBOUND_SECRET_HEADER = "X-Meridian-Inbound-Secret"

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
        raise MeridianError(
            E_VALIDATION,
            "the inbound support payload is required: POST the documented JSON object "
            "({provider_message_id, from, subject, body, received_at?, provider?})",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"field": "payload"},
        )
    if not isinstance(raw, dict):
        raise MeridianError(
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
        raise MeridianError(
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
        raise MeridianError(
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


def require_inbound_secret(provided: str | None) -> None:
    """The shared-secret credential (the shipped W5 rule), or a NAMED refusal.

    Called from the route body rather than as a FastAPI dependency since WAVE 9: the route now
    dispatches on Content-Type BEFORE it knows which credential applies, and a route-level
    dependency would have refused every real SendGrid multipart delivery (which carries no
    `X-Meridian-Inbound-Secret` at all).  The RULES are unchanged, in the shipped order: a missing
    or wrong secret is the named 401 `E_AUTH`; an UNSET `SUPPORT_INBOUND_SECRET` is the named 503
    `E_CONFIG` (never an open inbox) — pinned by `tests/unit/test_w5_inbound.py`.
    """
    if not _inbound_secret_matches(provided):
        raise MeridianError(
            E_AUTH,
            f"the {INBOUND_SECRET_HEADER} header is missing or does not match the configured "
            "inbound secret",
            how_to_fix=f"send SUPPORT_INBOUND_SECRET's value in the {INBOUND_SECRET_HEADER} header",
        )


def _parse_json_body(raw: bytes) -> object:
    """The JSON body (as the shipped route received it), or the NAMED `E_VALIDATION` refusal.

    `None` for an absent body (the shipped `Body(default=None)` semantics), so the
    `_parse_inbound_payload` refusal keeps naming `payload`.  A body that is not JSON used to
    reach the client as FastAPI's uncoded `{"detail": [...]}` array; this route's own doctrine is
    ONE envelope, so it is the named refusal here.
    """
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise MeridianError(
            E_VALIDATION,
            "the inbound support payload is not valid JSON: the body of a non-multipart delivery "
            "must be the documented JSON object",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"field": "payload", "reason": "invalid_json"},
        ) from exc


def _authenticate_sendgrid_delivery(request: Request, *, raw_body: bytes) -> str:
    """Authenticate a multipart Inbound Parse delivery; returns the mechanism that did it.

    SendGrid's own credential set, in the order the vendor documents them: the ECDSA signature
    headers, then an OAuth `Authorization: Bearer` token.  Both are checked against the RAW bytes
    (the signature covers them, so it must be verified before any parsing) and both are OPTIONAL
    at configuration time — but with neither configured no delivery can be authenticated, which
    is a named 401 `E_AUTH` and never a 5xx (a 5xx sends the provider into a retry loop over a
    delivery that can never succeed).

    The shipped shared secret is handled by the route before this function is called (an
    operator may still point a multipart provider at it), so it is not repeated here.
    """
    signature = request.headers.get(SENDGRID_SIGNATURE_HEADER)
    timestamp = request.headers.get(SENDGRID_TIMESTAMP_HEADER)
    if signature is not None or timestamp is not None:
        verify_sendgrid_signature(
            raw_body,
            signature=signature,
            timestamp=timestamp,
            public_key=settings.sendgrid_inbound_public_key,
            tolerance_seconds=settings.sendgrid_inbound_signature_tolerance_seconds,
        )
        return "signature"
    authorization = request.headers.get("authorization")
    if authorization is not None:
        verify_sendgrid_oauth_token(
            authorization=authorization,
            oauth_token=settings.sendgrid_inbound_oauth_token,
        )
        return "oauth"
    raise MeridianError(
        E_AUTH,
        "the multipart/form-data delivery carried no credential: SendGrid's Inbound Parse sends "
        f"either the ECDSA signature headers ({SENDGRID_SIGNATURE_HEADER} + "
        f"{SENDGRID_TIMESTAMP_HEADER}) or an OAuth `Authorization: Bearer` token",
        how_to_fix=(
            "attach a security policy to the Parse webhook and copy its public key into "
            "SENDGRID_INBOUND_PUBLIC_KEY (signature verification) or set "
            "SENDGRID_INBOUND_OAUTH_TOKEN to the token your authorization server issues (OAuth); "
            f"the {INBOUND_SECRET_HEADER} header is also accepted for a non-SendGrid provider"
        ),
        context={
            "mechanisms": ["signature", "oauth", "shared_secret"],
            "setting": "SENDGRID_INBOUND_PUBLIC_KEY",
        },
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


_MULTIPART_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["from"],
    "properties": {
        "headers": {
            "type": "string",
            "description": "the raw message headers (a `Message-ID:` header here is the item's "
            "provider id; there is no `message_id` form field in Inbound Parse)",
        },
        "from": {"type": "string", "description": "the From header; reduced to its addr-spec"},
        "to": {"type": "string"},
        "subject": {"type": "string"},
        "text": {"type": "string", "description": "the text body (preferred body source)"},
        "html": {"type": "string", "description": "the HTML body (used when `text` is blank)"},
        "envelope": {"type": "string", "description": "JSON: {\"to\": [...], \"from\": ...}"},
        "dkim": {"type": "string"},
        "charsets": {"type": "string", "description": "JSON: {field: charset}"},
        "SPF": {"type": "string"},
        "spam_score": {"type": "string"},
        "spam_report": {"type": "string"},
        "sender_ip": {"type": "string"},
        "content-ids": {"type": "string"},
        "attachment-info": {"type": "string"},
        "attachments": {"type": "string", "description": "the attachment COUNT; files are not stored"},
        "email": {
            "type": "string",
            "description": "Raw format (`send_raw`): the full MIME message; its first text/plain "
            "(else text/html) part supplies the body",
        },
    },
    "additionalProperties": True,
}


@router.post(
    "/inbox/inbound",
    response_model=SupportInboundIngestResponse,
    status_code=status.HTTP_201_CREATED,
    responses={
        200: {
            "model": SupportInboundIngestResponse,
            "description": (
                "Re-delivery of a provider_message_id that is already stored: `created: false`, "
                "the stored row unchanged, no new item/task."
            ),
        }
    },
    # The body is read from the Request so BOTH documented provider encodings can be handled (and
    # so the credential is decided before anything is parsed); the DOCUMENTED request shapes are
    # still published for clients and for OpenAPI.
    openapi_extra={
        "requestBody": {
            "required": True,
            "description": (
                "Two provider encodings, two credentials. (1) `application/json` — a JSON email "
                "provider (Postmark-style aliases accepted), authenticated by "
                f"`{INBOUND_SECRET_HEADER}` = SUPPORT_INBOUND_SECRET. (2) `multipart/form-data` — "
                "Twilio SendGrid Inbound Parse: no `message_id` field, the provider id derived "
                "from the message's own `Message-ID` (else a content digest), authenticated by "
                "the ECDSA signature headers "
                f"`{SENDGRID_SIGNATURE_HEADER}` + `{SENDGRID_TIMESTAMP_HEADER}` "
                "(SENDGRID_INBOUND_PUBLIC_KEY) or an OAuth `Authorization: Bearer` token "
                "(SENDGRID_INBOUND_OAUTH_TOKEN). Malformed or unverifiable deliveries are named "
                "4xx refusals, never a 5xx (which would make the provider retry)."
            ),
            "content": {
                "application/json": {"schema": SupportInboundPayload.model_json_schema()},
                "multipart/form-data": {"schema": _MULTIPART_REQUEST_SCHEMA},
            },
        }
    },
)
async def ingest_inbound_message(
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> SupportInboundIngestResponse:
    """Ingest one inbound-email-provider delivery (201 new / 200 re-delivery).

    TWO trust models behind one path, dispatched on Content-Type BEFORE any parsing:

    * `multipart/form-data` — Twilio SendGrid Inbound Parse.  The credential is SendGrid's own
      (ECDSA signature over the raw bytes, or OAuth bearer); the shared secret is accepted too so
      a non-SendGrid multipart provider can use the shipped credential.  The item's provider id is
      DERIVED (the vendor sends none) and recorded with the fields that produced it.
    * anything else — the shipped JSON webhook (Postmark-style aliases), authenticated by
      `X-Meridian-Inbound-Secret` with exactly the rules W5 shipped (missing/wrong → 401, unset
      secret → 503 `E_CONFIG`), and an unsupported Content-Type is a named 422 rather than a
      framework 415/422 array.

    The item lands `pending` with the same fields the owner's POST writes, so the 3h attach sweep
    creates the `customer_support` task; a re-delivery answers `created:false` with the stored row
    and writes no second item or task.  Every failure is a named refusal — the provider's retry
    loop treats a 5xx as "try again for days", so an unverifiable or malformed delivery is a 4xx.
    """
    raw_body = await request.body()
    content_type = request.headers.get("content-type")
    media_type = media_type_of(content_type)
    provided_secret = request.headers.get(INBOUND_SECRET_HEADER)

    with refusal_guard():
        if is_multipart_form_data(content_type):
            if provided_secret is None:
                delivery_auth = _authenticate_sendgrid_delivery(request, raw_body=raw_body)
            else:
                require_inbound_secret(provided_secret)
                delivery_auth = "shared_secret"
            result = await ingest_sendgrid_inbound(
                db,
                raw_body=raw_body,
                content_type=content_type or "",
                delivery_auth=delivery_auth,
            )
        else:
            require_inbound_secret(provided_secret)
            if media_type not in ("", "application/json"):
                raise MeridianError(
                    E_VALIDATION,
                    f"unsupported Content-Type {media_type!r} for the inbound support webhook: "
                    "send application/json (the documented JSON providers) or multipart/form-data "
                    "(Twilio SendGrid Inbound Parse)",
                    how_to_fix=ERROR_HINTS[E_VALIDATION],
                    context={"field": "payload", "content_type": media_type},
                )
            parsed = _parse_inbound_payload(_parse_json_body(raw_body))
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

    V20/L1 (card L2-07): `X-Meridian-Total` declares the FULL filtered count (the same `WHERE`,
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
