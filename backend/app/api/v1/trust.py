"""GET /api/v1/trust/domain and /api/v1/trust/link — site and link health (V29/L6).

NOT wired into `app/main.py` by this lane: the parent includes `trust.router` at settle (V29 §8.2,
the same ownership split every V29 lane follows).  Until then the router is reachable only through
the local test app in `tests/unit/test_v29_trust.py`.

Both routes are READ-ONLY and keyless upstream (Cloudflare DoH, a direct TLS handshake, the
Internet Archive) — safe to poll, cached 1 h in the service.  They run the sync service through
`starlette.concurrency.run_in_threadpool`: a DNS+TLS leg can block for up to 8 s per hop and must
never stall the event loop for every other request.

The response models live HERE, not in `app/schemas/` — the schemas package is not this lane's to
edit (the same ruling `economics.py` records), and the shapes mirror the service contract exactly:

  * `dns.status` is the DNS RCODE (0 = NOERROR, 3 = NXDOMAIN); `dns.reason` names a failed leg.
  * `tls.ok = false` is a REAL answer (expired/unverifiable certificate), never an error — the
    route still answers 200 with the reason in `tls.reason` and the summary in `reason`.
  * `archived: null` + `archive_reason` means the archive could not be asked, which is NOT the
    same as "there is no snapshot" (`available: false`).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from app.api.deps import refusal_guard, verify_api_key
from app.services import trust_service

router = APIRouter(prefix="/trust", tags=["trust"], dependencies=[Depends(verify_api_key)])


class DnsSection(BaseModel):
    """The A/AAAA answer — an empty list is a real "no such record", `status` names the RCODE."""

    a: list[str] = Field(default_factory=list)
    aaaa: list[str] = Field(default_factory=list)
    status: int | None = None
    reason: str | None = None


class TlsSection(BaseModel):
    """The certificate leg.  `ok = false` carries the reason instead of raising."""

    ok: bool
    not_after: str | None = None
    days_left: int | None = None
    reason: str | None = None


class DomainHealthResponse(BaseModel):
    host: str
    dns: DnsSection
    tls: TlsSection
    reason: str | None = None
    checked_at: str
    cached: bool


class ArchivedSnapshot(BaseModel):
    """The Wayback answer.  `available: false` is a measured "no snapshot", not an outage."""

    available: bool
    url: str | None = None
    timestamp: str | None = None


class LinkStatusResponse(BaseModel):
    url: str
    status: int
    final_url: str
    ok: bool
    archived: ArchivedSnapshot | None = None
    archive_reason: str | None = None
    redirects: list[str] = Field(default_factory=list)
    reason: str | None = None
    checked_at: str
    cached: bool


@router.get("/domain", response_model=DomainHealthResponse)
async def domain_health(
    host: str = Query(min_length=1, max_length=253),
) -> DomainHealthResponse:
    """DNS + TLS health for one bare hostname (e.g. `example.com`)."""
    with refusal_guard():
        result = await run_in_threadpool(trust_service.check_domain, host)
    return DomainHealthResponse.model_validate(result)


@router.get("/link", response_model=LinkStatusResponse)
async def link_status(
    url: str = Query(min_length=1, max_length=2048),
) -> LinkStatusResponse:
    """HTTP status of one PUBLIC http(s) URL plus its closest Wayback snapshot."""
    with refusal_guard():
        result = await run_in_threadpool(trust_service.check_link, url)
    return LinkStatusResponse.model_validate(result)
