"""V29/L6 — trust service (DoH + TLS + Wayback) and its two routes: the RED-FIRST test leg.

Written BEFORE `app/services/trust_service.py` and `app/api/v1/trust.py` exist, so pytest
collection fails with `ModuleNotFoundError: No module named 'app.services.trust_service'` until
they land (raw output preserved in
`polsia-work/build/ui-reverse/wave-v29/l6-trust/red/T6.1-service.txt` and `red/T6.2-routes.txt`).

What is pinned, and why each one exists:

1. `check_domain` reports BOTH DoH address families plus the certificate's remaining days — the
   badge's `TLS <n>d` label is only honest if the number came from the peer certificate.
2. `Status == 3` is NXDOMAIN: a named reason on a served result, never an exception, and no
   pointless TLS dial to a name that does not resolve.
3. A broken DNS leg is a FIELD on the result, not a crash — one dead source must not take the
   route (or the badge) with it.  The TLS leg is skipped when there is no address to dial.
4. A certificate failure is `tls.ok=false` with its reason.  The plan is explicit: never an
   exception out of the route.
5. The certificate parser is pinned with a FABRICATED cert dict (no socket): `notAfter` ->
   `not_after` ISO + `floor((expiry - now) / 86400)` days.
6. `check_link` follows at most THREE manual redirects, revalidating every hop, and reports the
   final URL; a 405 answer to HEAD falls back to GET on the same URL.
7. Private / loopback / link-local / reserved / multicast / unspecified addresses and any port
   outside 80/443 are refused BEFORE a request is sent (`E_VALIDATION`) — including on a redirect
   hop, which is the SSRF path a naive follow would open.
8. Wayback: an empty snapshot is `available:false` (not an error); an ARCHIVE outage degrades
   `archived` to null with a named `archive_reason` instead of falsifying the live status.
9. The 1 h cache: the second call touches no transport and keeps the ORIGINAL `checked_at`.
10. Routes: `/api/v1/trust/domain` and `/link` require the API key, refuse a malformed host with
    422, and run the service OFF the event loop (`run_in_threadpool`).

Every socket is stubbed.  The seams are `_doh` / `_tls` / `_wayback` (service legs) and `_CLIENT`
(the HTTP client factory, the same house idiom V29/L3 uses).  ONE live smoke (`example.com`) is the
parent drill, never a unit test.
"""
from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio

from app.core.errors import E_UPSTREAM, E_VALIDATION, PolsiaError
from app.services import trust_service

AUTH = {"X-API-Key": "test-key"}
DOMAIN_PATH = "/api/v1/trust/domain"
LINK_PATH = "/api/v1/trust/link"

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_cache():
    """`_CACHE` is PROCESS-GLOBAL module state — clear it around every test."""
    trust_service._CACHE.clear()
    yield
    trust_service._CACHE.clear()


def _doh_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    a: tuple[str, ...] = (PUBLIC_V4,),
    aaaa: tuple[str, ...] = (PUBLIC_V6,),
    status: int = 0,
    calls: list[str] | None = None,
):
    """Install a DoH answer for both families; records the queried record types."""

    def fake(host: str, rrtype: str) -> dict[str, Any]:
        if calls is not None:
            calls.append(rrtype)
        if rrtype == "A":
            return {"status": status, "answers": list(a)}
        return {"status": status, "answers": list(aaaa)}

    monkeypatch.setattr(trust_service, "_doh", fake)
    return fake


def _tls_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ok: bool = True,
    not_after: str | None = "2026-12-25T23:59:59+00:00",
    days_left: int | None = 66,
    reason: str | None = None,
    calls: list[str] | None = None,
):
    def fake(host: str) -> dict[str, Any]:
        if calls is not None:
            calls.append(host)
        return {"ok": ok, "not_after": not_after, "days_left": days_left, "reason": reason}

    monkeypatch.setattr(trust_service, "_tls", fake)
    return fake


def _wayback_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    available: bool = False,
    url: str | None = None,
    timestamp: str | None = None,
    error: PolsiaError | None = None,
    calls: list[str] | None = None,
):
    def fake(target: str) -> dict[str, Any]:
        if calls is not None:
            calls.append(target)
        if error is not None:
            raise error
        return {"available": available, "url": url, "timestamp": timestamp}

    monkeypatch.setattr(trust_service, "_wayback", fake)
    return fake


def _transport(monkeypatch: pytest.MonkeyPatch, handler) -> list[dict[str, str]]:
    """Replace only the socket: an `httpx.MockTransport` behind the `_CLIENT` factory."""
    calls: list[dict[str, str]] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        calls.append({"method": request.method, "url": str(request.url)})
        return handler(request)

    transport = httpx.MockTransport(_handle)
    monkeypatch.setattr(
        trust_service, "_CLIENT", lambda **kwargs: httpx.Client(transport=transport, **kwargs)
    )
    return calls


@pytest_asyncio.fixture
async def trust_client(monkeypatch: pytest.MonkeyPatch):
    """A test app that includes ONLY the trust router — `main.py` wiring is the parent's (V29 §8)."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from app.api.v1 import trust
    from app.config import settings
    from app.core.errors import http_status_for, to_http_detail

    monkeypatch.setattr(settings, "api_key", "test-key")
    app = FastAPI()
    app.include_router(trust.router, prefix="/api/v1")

    async def _polsia_handler(request, exc: PolsiaError):
        return JSONResponse(
            status_code=http_status_for(exc.code),
            content=to_http_detail(exc),
            headers={"X-Polsia-Error-Code": exc.code},
        )

    app.add_exception_handler(PolsiaError, _polsia_handler)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


# ── 1. check_domain: the happy path ─────────────────────────────────────────────────────────────


def test_domain_reports_both_families_and_the_certificate_days(monkeypatch: pytest.MonkeyPatch):
    doh_calls: list[str] = []
    tls_calls: list[str] = []
    _doh_stub(monkeypatch, calls=doh_calls)
    _tls_stub(monkeypatch, calls=tls_calls)

    result = trust_service.check_domain("Example.COM")

    assert result["host"] == "example.com"  # normalised, and echoed back
    assert result["dns"]["a"] == [PUBLIC_V4]
    assert result["dns"]["aaaa"] == [PUBLIC_V6]
    assert result["dns"]["status"] == 0
    assert result["dns"]["reason"] is None
    assert result["tls"] == {
        "ok": True,
        "not_after": "2026-12-25T23:59:59+00:00",
        "days_left": 66,
        "reason": None,
    }
    assert result["reason"] is None
    assert result["cached"] is False
    assert isinstance(result["checked_at"], str) and result["checked_at"]
    assert doh_calls == ["A", "AAAA"]
    assert tls_calls == ["example.com"]


def test_malformed_host_is_refused_before_any_lookup(monkeypatch: pytest.MonkeyPatch):
    doh_calls: list[str] = []
    _doh_stub(monkeypatch, calls=doh_calls)

    for bad in ("https://example.com", "example.com/path", "example.com:443", "user@example.com",
                "example", "", "   ", "127.0.0.1"):
        with pytest.raises(PolsiaError) as refused:
            trust_service.check_domain(bad)
        assert refused.value.code == E_VALIDATION
        assert refused.value.context.get("field") == "host"
    assert doh_calls == []  # nothing was resolved for a malformed value


# ── 2. NXDOMAIN is a named result, not an exception ────────────────────────────────────────────


def test_nxdomain_is_named_and_does_not_dial_tls(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch, a=(), aaaa=(), status=3)
    tls_calls: list[str] = []
    _tls_stub(monkeypatch, calls=tls_calls)

    result = trust_service.check_domain("gone.example.com")

    assert result["dns"]["status"] == 3
    assert result["dns"]["a"] == []
    assert result["dns"]["aaaa"] == []
    assert result["dns"]["reason"] == "NXDOMAIN"
    assert result["tls"]["ok"] is False
    assert result["tls"]["days_left"] is None
    assert "NXDOMAIN" in result["reason"]
    assert tls_calls == []  # no address -> no handshake


def test_other_dns_rcodes_are_named_too(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch, a=(), aaaa=(), status=2)  # SERVFAIL
    _tls_stub(monkeypatch)

    result = trust_service.check_domain("example.com")

    assert result["dns"]["status"] == 2
    assert result["dns"]["reason"] == "DNS RCODE 2"
    assert "RCODE 2" in result["reason"]
    assert result["tls"]["ok"] is False


# ── 3. a dead DNS leg is a field, not a crash ──────────────────────────────────────────────────


def test_a_broken_dns_leg_is_reported_on_the_result(monkeypatch: pytest.MonkeyPatch):
    def boom(host: str, rrtype: str) -> dict[str, Any]:
        raise PolsiaError(
            E_UPSTREAM,
            "domain_health: the DNS resolver answered HTTP 503",
            how_to_fix="retry; Cloudflare DoH is temporarily unavailable",
        )

    monkeypatch.setattr(trust_service, "_doh", boom)
    tls_calls: list[str] = []
    _tls_stub(monkeypatch, calls=tls_calls)

    result = trust_service.check_domain("example.com")  # must NOT raise

    assert result["dns"]["status"] is None
    assert result["dns"]["a"] == [] and result["dns"]["aaaa"] == []
    assert "HTTP 503" in result["dns"]["reason"]
    assert "HTTP 503" in result["reason"]
    assert result["tls"]["ok"] is False
    assert tls_calls == []


# ── 4/5. the certificate: failures are fields, and the parser is exact ────────────────────────


def test_a_certificate_failure_is_tls_ok_false_never_an_exception(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _tls_stub(
        monkeypatch,
        ok=False,
        not_after=None,
        days_left=None,
        reason="certificate verification failed: certificate has expired",
    )

    result = trust_service.check_domain("expired.example.com")

    assert result["tls"]["ok"] is False
    assert result["tls"]["not_after"] is None
    assert "certificate has expired" in result["tls"]["reason"]
    assert "certificate has expired" in result["reason"]
    assert result["dns"]["status"] == 0  # the DNS leg still answered


def test_the_certificate_parser_computes_days_left_from_a_fabricated_cert():
    now = datetime(2026, 10, 20, 12, 0, 0, tzinfo=UTC)

    fields = trust_service._cert_fields({"notAfter": "Dec 25 23:59:59 2026 GMT"}, now=now)

    assert fields["ok"] is True
    assert fields["not_after"] == "2026-12-25T23:59:59+00:00"
    assert fields["days_left"] == 66  # floor((2026-12-25T23:59:59Z - 2026-10-20T12:00:00Z)/86400)
    assert fields["reason"] is None

    expired = trust_service._cert_fields(
        {"notAfter": "Oct 19 12:00:00 2026 GMT"}, now=now
    )
    assert expired["ok"] is True and expired["days_left"] == -1

    unreadable = trust_service._cert_fields({"notAfter": "not-a-date"}, now=now)
    assert unreadable["ok"] is False and unreadable["days_left"] is None
    assert "notAfter" in unreadable["reason"]

    absent = trust_service._cert_fields({}, now=now)
    assert absent["ok"] is False and absent["not_after"] is None


# ── 6. check_link: manual redirects + the GET fallback ───────────────────────────────────────


def test_a_301_is_followed_to_the_final_url(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _wayback_stub(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == "https://example.com/":
            return httpx.Response(301, headers={"Location": "https://www.example.com/final"},
                                  request=request)
        return httpx.Response(200, headers={"Content-Type": "text/html"}, request=request)

    calls = _transport(monkeypatch, handler)

    result = trust_service.check_link("https://example.com/")

    assert result["status"] == 200
    assert result["final_url"] == "https://www.example.com/final"
    assert result["redirects"] == ["https://example.com/"]
    assert result["ok"] is True
    assert result["reason"] is None
    assert result["cached"] is False
    assert [call["method"] for call in calls] == ["HEAD", "HEAD"]


def test_more_than_three_redirects_stops_with_the_last_answer(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _wayback_stub(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        hop = int(str(request.url).rsplit("/", 1)[-1] or 0)
        return httpx.Response(302, headers={"Location": f"https://example.com/{hop + 1}"},
                              request=request)

    _transport(monkeypatch, handler)

    result = trust_service.check_link("https://example.com/0")

    assert result["status"] == 302  # the 4th redirect is reported, not followed
    assert len(result["redirects"]) == 4
    assert result["ok"] is False
    assert "3 redirects" in result["reason"]


def test_head_405_falls_back_to_get_on_the_same_url(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _wayback_stub(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "HEAD":
            return httpx.Response(405, headers={"Allow": "GET"}, request=request)
        return httpx.Response(200, request=request)

    calls = _transport(monkeypatch, handler)

    result = trust_service.check_link("https://example.com/page")

    assert result["status"] == 200
    assert [call["method"] for call in calls] == ["HEAD", "GET"]
    assert calls[0]["url"] == calls[1]["url"] == "https://example.com/page"


def test_a_404_is_a_real_answer_with_ok_false(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _wayback_stub(monkeypatch)
    _transport(monkeypatch, lambda request: httpx.Response(404, request=request))

    result = trust_service.check_link("https://example.com/missing")

    assert result["status"] == 404
    assert result["ok"] is False
    assert result["reason"] is None  # the HTTP status speaks for itself


# ── 7. the SSRF guards ────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("url", "label"),
    [
        ("http://127.0.0.1/", "loopback"),
        ("http://10.1.2.3/admin", "private"),
        ("http://192.168.0.5/", "private"),
        ("http://169.254.169.254/latest/meta-data/", "link-local"),
        ("http://0.0.0.0/", "unspecified"),
        ("http://224.0.0.1/", "multicast"),
        ("http://240.0.0.1/", "reserved"),
        ("http://198.51.100.7/", "private"),  # TEST-NET-2: `is_private`, never a public host
    ],
)
def test_non_public_literal_addresses_are_refused_before_any_request(
    monkeypatch: pytest.MonkeyPatch, url: str, label: str
):
    _doh_stub(monkeypatch)
    _wayback_stub(monkeypatch)
    calls = _transport(monkeypatch, lambda request: httpx.Response(200, request=request))

    with pytest.raises(PolsiaError) as refused:
        trust_service.check_link(url)

    assert refused.value.code == E_VALIDATION
    assert label in refused.value.message
    assert refused.value.context.get("field") == "url"
    assert calls == []  # nothing was ever sent


def test_a_localhost_name_resolving_to_loopback_is_refused(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch, a=("127.0.0.1",), aaaa=())
    _wayback_stub(monkeypatch)
    calls = _transport(monkeypatch, lambda request: httpx.Response(200, request=request))

    with pytest.raises(PolsiaError) as refused:
        trust_service.check_link("http://localhost:80/")

    assert refused.value.code == E_VALIDATION
    assert "loopback" in refused.value.message
    assert calls == []


@pytest.mark.parametrize("url", ["https://example.com:8080/", "http://example.com:22/",
                                 "https://example.com:8443/x"])
def test_ports_outside_80_and_443_are_refused(monkeypatch: pytest.MonkeyPatch, url: str):
    _doh_stub(monkeypatch)
    calls = _transport(monkeypatch, lambda request: httpx.Response(200, request=request))

    with pytest.raises(PolsiaError) as refused:
        trust_service.check_link(url)

    assert refused.value.code == E_VALIDATION
    assert "port" in refused.value.message
    assert calls == []


def test_a_redirect_to_a_private_address_is_refused(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _wayback_stub(monkeypatch)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "http://127.0.0.1/admin"},
                              request=request)

    calls = _transport(monkeypatch, handler)

    with pytest.raises(PolsiaError) as refused:
        trust_service.check_link("https://example.com/")

    assert refused.value.code == E_VALIDATION
    assert "loopback" in refused.value.message
    assert len(calls) == 1  # the first hop was fetched, the private target never was


def test_a_redirect_to_a_non_http_scheme_is_refused(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _wayback_stub(monkeypatch)
    _transport(
        monkeypatch,
        lambda request: httpx.Response(302, headers={"Location": "file:///etc/passwd"},
                                       request=request),
    )

    with pytest.raises(PolsiaError) as refused:
        trust_service.check_link("https://example.com/")

    assert refused.value.code == E_VALIDATION


def test_a_malformed_link_is_refused(monkeypatch: pytest.MonkeyPatch):
    for bad in ("example.com", "ftp://example.com/x", "https://user:pw@example.com/", "",
                "https://example.com/" + "a" * 2048):
        with pytest.raises(PolsiaError) as refused:
            trust_service.check_link(bad)
        assert refused.value.code == E_VALIDATION
        assert refused.value.context.get("field") == "url"


def test_an_unresolvable_link_host_is_an_upstream_failure(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch, a=(), aaaa=(), status=3)

    with pytest.raises(PolsiaError) as refused:
        trust_service.check_link("https://gone.example.com/")

    assert refused.value.code == E_UPSTREAM
    assert "NXDOMAIN" in refused.value.message


# ── 8. Wayback ────────────────────────────────────────────────────────────────────────────────


def test_an_empty_wayback_snapshot_is_available_false(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _wayback_stub(monkeypatch, available=False, url=None, timestamp=None)
    _transport(monkeypatch, lambda request: httpx.Response(200, request=request))

    result = trust_service.check_link("https://example.com/")

    assert result["archived"] == {"available": False, "url": None, "timestamp": None}
    assert result["archive_reason"] is None


def test_a_snapshot_is_reported_with_its_timestamp(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _wayback_stub(
        monkeypatch,
        available=True,
        url="http://web.archive.org/web/20260101000000/https://example.com/",
        timestamp="20260101000000",
    )
    _transport(monkeypatch, lambda request: httpx.Response(200, request=request))

    result = trust_service.check_link("https://example.com/")

    assert result["archived"]["available"] is True
    assert result["archived"]["timestamp"] == "20260101000000"
    assert "web.archive.org" in result["archived"]["url"]


def test_an_archive_outage_degrades_to_null_and_keeps_the_live_status(
    monkeypatch: pytest.MonkeyPatch,
):
    _doh_stub(monkeypatch)
    _wayback_stub(
        monkeypatch,
        error=PolsiaError(E_UPSTREAM, "link_status: the Wayback API answered HTTP 500"),
    )
    _transport(monkeypatch, lambda request: httpx.Response(200, request=request))

    result = trust_service.check_link("https://example.com/")

    assert result["status"] == 200  # the link itself answered
    assert result["ok"] is True
    assert result["archived"] is None  # never a fabricated "not archived"
    assert "HTTP 500" in result["archive_reason"]


# ── 9. the 1 h cache ─────────────────────────────────────────────────────────────────────────


def test_the_link_cache_serves_a_hit_without_a_transport_call(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _wayback_stub(monkeypatch)
    calls = _transport(monkeypatch, lambda request: httpx.Response(200, request=request))

    first = trust_service.check_link("https://example.com/cached")
    second = trust_service.check_link("https://example.com/cached")

    assert len(calls) == 1
    assert second["cached"] is True
    assert second["checked_at"] == first["checked_at"]  # provenance is never restamped
    assert second["status"] == first["status"] == 200
    assert trust_service.CACHE_TTL_S == 3600.0


def test_the_domain_cache_serves_a_hit_without_a_lookup(monkeypatch: pytest.MonkeyPatch):
    doh_calls: list[str] = []
    tls_calls: list[str] = []
    _doh_stub(monkeypatch, calls=doh_calls)
    _tls_stub(monkeypatch, calls=tls_calls)

    first = trust_service.check_domain("example.com")
    second = trust_service.check_domain("example.com")

    assert doh_calls == ["A", "AAAA"]
    assert len(tls_calls) == 1
    assert second["cached"] is True
    assert second["checked_at"] == first["checked_at"]


# ── 10. the routes ───────────────────────────────────────────────────────────────────────────


async def test_domain_route_serves_the_health_result(trust_client, monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _tls_stub(monkeypatch)

    resp = await trust_client.get(DOMAIN_PATH, params={"host": "example.com"}, headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"host", "dns", "tls", "reason", "checked_at", "cached"}
    assert body["host"] == "example.com"
    assert body["dns"]["a"] == [PUBLIC_V4]
    assert body["tls"]["days_left"] == 66
    assert body["cached"] is False


async def test_link_route_serves_the_status_and_snapshot(trust_client, monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _wayback_stub(monkeypatch, available=True, url="http://web.archive.org/web/1/x",
                  timestamp="20260101000000")
    _transport(monkeypatch, lambda request: httpx.Response(200, request=request))

    resp = await trust_client.get(LINK_PATH, params={"url": "https://example.com/"}, headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == 200
    assert body["ok"] is True
    assert body["final_url"] == "https://example.com/"
    assert body["archived"]["available"] is True


async def test_both_routes_require_the_api_key(trust_client):
    assert (await trust_client.get(DOMAIN_PATH, params={"host": "example.com"})).status_code == 401
    assert (
        await trust_client.get(LINK_PATH, params={"url": "https://example.com/"})
    ).status_code == 401


async def test_a_private_host_is_a_422_on_the_route(trust_client, monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch)
    _wayback_stub(monkeypatch)
    _transport(monkeypatch, lambda request: httpx.Response(200, request=request))

    host = await trust_client.get(DOMAIN_PATH, params={"host": "example.com/x"}, headers=AUTH)
    link = await trust_client.get(LINK_PATH, params={"url": "http://127.0.0.1/"}, headers=AUTH)

    assert host.status_code == 422
    assert host.json()["detail"]["code"] == E_VALIDATION
    assert link.status_code == 422
    assert link.json()["detail"]["code"] == E_VALIDATION


async def test_the_domain_route_runs_the_service_off_the_event_loop(
    trust_client, monkeypatch: pytest.MonkeyPatch
):
    seen: list[threading.Thread] = []

    def service(host: str) -> dict[str, Any]:
        seen.append(threading.current_thread())
        return {
            "host": host,
            "dns": {"a": [PUBLIC_V4], "aaaa": [], "status": 0, "reason": None},
            "tls": {"ok": True, "not_after": "2026-12-25T23:59:59+00:00", "days_left": 66,
                    "reason": None},
            "reason": None,
            "checked_at": "2026-10-20T12:00:00+00:00",
            "cached": False,
        }

    monkeypatch.setattr(trust_service, "check_domain", service)

    resp = await trust_client.get(DOMAIN_PATH, params={"host": "example.com"}, headers=AUTH)

    assert resp.status_code == 200
    assert len(seen) == 1
    assert seen[0] is not threading.main_thread()
