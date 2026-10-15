"""GET /api/v1/outreach/prospects (owner) + the PUBLIC one-click opt-out (V11/W1).

AUTH (V11/W1): the router-level `dependencies=[Depends(verify_api_key)]` moved DOWN to the one
owner-facing route, for the same reason `finance.py` declares its `/stripe/webhook` without it — the
opt-out link travels in an email to a recipient who has no owner key, and a clear-and-conspicuous
opt-out must not require a login.  `/prospects` keeps EXACTLY the auth it had (pinned by
tests/unit/api/test_r27_social_outreach_edges.py and the parity surface): moving a dependency must
not open the owner surface, and the leg that pins it asserts both halves.

The opt-out route records the opt-out DURABLY through the service layer
(`outreach_service.unsubscribe_prospect` -> `prospects.status = "unsubscribed"`): that is the very
state `email_compliance.send_compliant_email` refuses to send to, so ONE click stops the chain for
good — including for an address whose prospect row has not been created yet by
`outreach_service.record_sent_emails`.  The token is an HMAC over the address (see
`email_compliance.unsubscribe_token`), so a guessed address cannot unsubscribe anyone.

The page echoes NOTHING from the request: a link in a mail client must not become a reflected-input
surface, and the recipient already knows their own address.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, refusal_guard, verify_api_key
from app.schemas.social import ProspectResponse
from app.services.email_compliance import verify_unsubscribe_token
from app.services.outreach_service import list_prospects, unsubscribe_prospect

router = APIRouter(prefix="/outreach", tags=["outreach"])

OPT_OUT_PAGE = (
    "<!doctype html><html><head><meta charset='utf-8'><title>Unsubscribed</title></head><body>"
    "<h1>You are unsubscribed</h1>"
    "<p>This address is on our suppression list: you will receive no further outreach email "
    "from us.</p></body></html>"
)


@router.get(
    "/prospects",
    response_model=list[ProspectResponse],
    dependencies=[Depends(verify_api_key)],
)
async def outreach_prospects(
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[ProspectResponse]:
    with refusal_guard():
        prospects = await list_prospects(db, status=status_filter, limit=limit)
    return [ProspectResponse.model_validate(prospect) for prospect in prospects]


@router.get("/unsubscribe", response_class=HTMLResponse)
async def outreach_unsubscribe(
    token: str = Query(default=""),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """One click, no login, idempotent: verify the signed token, record the opt-out, confirm.

    A malformed/forged/expired token is a named `E_VALIDATION` refusal (never a 500, never a silent
    success) and changes no state; the confirmation page carries no request-derived text.
    """
    with refusal_guard():
        address = verify_unsubscribe_token(token)
        await unsubscribe_prospect(db, address)
        await db.commit()
    return HTMLResponse(OPT_OUT_PAGE, status_code=200)
