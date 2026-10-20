"""V29 follow-up B — a control character in `/trust/link`'s url is a CALLER error, never a 500.

MEASURED DEFECT (live V29 stack, docker logs 2026-10-20)
------------------------------------------------------
`GET /api/v1/trust/link?url=https%3A%2F%2Fexample.com%2F%00x` answered a bare
`500 Internal Server Error`.  The traceback is
`httpx.InvalidURL: Invalid non-printable ASCII character in URL, '\\x00' at position 20.`

The chain: `trust_service._link_target` accepts the string (`urllib.parse` is permissive), the
service even performs the DoH A/AAAA lookups, and then `_fetch_headers` builds an `httpx.Request`
— which refuses the character BEFORE any request is sent.  `refusal_guard` translates only
`PolsiaError`, so the caller-supplied junk leaks a 500 (and burns two DNS lookups).

TWO LAYERS, ONE FIX
-------------------
* `app/api/v1/trust.py`: refuse the control character BEFORE the threadpool/DNS work, so the HTTP
  surface answers `422 E_VALIDATION` with a fix hint.  Pinned by the first test.
* `app/services/trust_service.py`: `_link_target` refuses the same characters, so the `link_status`
  tool runner gets the same named refusal.  Pinned by the second test (was a strict xfail while the
  service fix belonged to another lane; V29 follow-up D landed the service fix, so the marker is gone).
"""
from __future__ import annotations

import httpx
import pytest

from app.core.errors import E_VALIDATION, PolsiaError
from app.services import trust_service

LINK_PATH = "/api/v1/trust/link"
PUBLIC_V4 = "93.184.216.34"
#: A NUL in the middle of the path: urlparse accepts it, httpx refuses to build the request.
CONTROL_URL = "https://example.com/a" + chr(0) + "b"


def _stub_dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A DoH answer that would let the service proceed; records every queried record type."""
    calls: list[str] = []

    def fake(host: str, rrtype: str) -> dict[str, object]:
        calls.append(rrtype)
        return {"status": 0, "answers": [PUBLIC_V4] if rrtype == "A" else []}

    monkeypatch.setattr(trust_service, "_doh", fake)
    return calls


def _no_http(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """A transport that records instead of dialling, so 'no request' is provable."""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        trust_service, "_CLIENT", lambda **kwargs: httpx.Client(transport=transport, **kwargs)
    )
    return calls


async def test_link_route_refuses_a_control_character_before_any_lookup(
    api_client, auth_headers, monkeypatch: pytest.MonkeyPatch
):
    """RED on the shipped route: the 500 escapes as `httpx.InvalidURL` after two DNS queries."""
    doh_calls = _stub_dns(monkeypatch)
    http_calls = _no_http(monkeypatch)

    response = await api_client.get(
        LINK_PATH, params={"url": CONTROL_URL}, headers=auth_headers
    )

    assert response.status_code == 422, (
        f"a caller URL with a control character must be a named refusal, got "
        f"{response.status_code}: {response.text[:200]}"
    )
    body = response.json()
    assert body["error"]["code"] == E_VALIDATION
    assert body["error"]["context"]["field"] == "url"
    assert body["error"]["how_to_fix"]
    assert doh_calls == [], "the route refused AFTER the DNS lookups; the check must run first"
    assert http_calls == [], "no HTTP request may be attempted for an unsendable URL"


def test_check_link_refuses_a_control_character_at_the_service_boundary(monkeypatch):
    """The tool path calls the service directly; the same class must be refused there."""
    _stub_dns(monkeypatch)
    _no_http(monkeypatch)

    with pytest.raises(PolsiaError) as caught:
        trust_service.check_link(CONTROL_URL)

    assert caught.value.code == E_VALIDATION
    assert caught.value.context.get("field") == "url"
