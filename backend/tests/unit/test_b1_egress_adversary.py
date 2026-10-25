"""Wave B1 / seat D2 — the egress floor attacked, not described.

A3/S1 fixed `trust_service` (positive `is_global` question + port allowlist) and A3/S2 wired
`web_fetch` to `pinned_http_get`.  Two halves of A2A1-01/A2A1-02 were still open in
`tool_registry.py`, and this file is the adversary that keeps them shut:

  * `_is_public_ip` was a BLACKLIST (`is_private or is_loopback or is_link_local or
    is_multicast or is_reserved or is_unspecified`).  CPython 3.12 sets NONE of those flags for
    RFC 6598 `100.64.0.0/10`, where Alibaba Cloud's instance metadata lives
    (`100.100.100.200`) — so the module's own floor approved a metadata address.  The predicate
    now asks the POSITIVE question: `bool(ip.is_global) and not ip.is_multicast` (the second
    conjunct is load-bearing because `is_global` is True for `224.0.0.1` / `ff02::1`).
  * the redirect rule compared `hostname` only, so a same-host 302 could change the port
    (443 -> 6379) or downgrade https -> http and still be followed.  It now compares the full
    origin triple (scheme, hostname, effective port).

The eleven refusal cases the brief names are exercised END TO END through
`execute_tool("web_fetch", ...)` — the real entry point a model reaches — against the REAL
predicate and the REAL `resolve_egress`.  Only the boundaries are suspended:

  * `trust_service._doh`   — the DNS answer (never a real lookup);
  * `trust_service._CLIENT`— the socket (an `httpx.MockTransport`, never a real connect).

Nothing here stubs `_is_public_ip`, `resolve_egress`, `pinned_http_get` or `_run_web_fetch`.
"""
from __future__ import annotations

import ipaddress
import json
import socket
from typing import Any
from urllib.parse import urlsplit

import httpx
import pytest

from app.agents import tool_registry
from app.core.errors import E_POLICY_DENIED, PolsiaError
from app.services import trust_service

PUBLIC_V4 = "93.184.216.34"          # example.com's well-known address
PUBLIC_V4_ALT = "1.1.1.1"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
PRIVATE_V4 = "10.0.0.5"
SHARED_METADATA = "100.100.100.200"  # Alibaba Cloud instance metadata, RFC 6598 shared space


# ── seams (the DNS answer and the socket; never the predicate or the resolver) ───────────────

def _stub_doh(
    monkeypatch: pytest.MonkeyPatch,
    script: dict[str, list[tuple[str, ...]]],
) -> list[tuple[str, str]]:
    """Script every name's A answers; each A query advances one step, the last step repeats.

    AAAA always answers empty — the adversarial answers under test are IPv4, and a second family
    would only make the failure message noisier.  A name with no script is NXDOMAIN.
    """
    cues = {host: list(steps) for host, steps in script.items()}
    calls: list[tuple[str, str]] = []

    def fake(host: str, rrtype: str) -> dict[str, Any]:
        calls.append((host, rrtype))
        if rrtype != "A":
            return {"status": 0, "answers": []}
        steps = cues.get(host)
        if not steps:
            return {"status": 3, "answers": []}
        answer = steps.pop(0) if len(steps) > 1 else steps[0]
        return {"status": 0, "answers": list(answer)}

    monkeypatch.setattr(trust_service, "_doh", fake)
    return calls


def _stub_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Any
) -> list[httpx.Request]:
    """Replace only the socket: `trust_service._CLIENT` becomes a `MockTransport` client.

    Every request is recorded, so "was it dialled?" is answered from the wire itself.
    """
    seen: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_handle)
    monkeypatch.setattr(
        trust_service, "_CLIENT", lambda **kwargs: httpx.Client(transport=transport, **kwargs)
    )
    return seen


def _fetch(url: str, **arguments: Any) -> dict:
    """The real tool entry point, exactly as a model would call it."""
    return json.loads(tool_registry.execute_tool(
        "web_fetch", {"url": url, **arguments}, agent_type="competitor_research"))


def _refused(payload: dict) -> dict:
    """The refusal block, asserting it is the NAMED policy refusal — never a generic error."""
    assert payload["ok"] is False, payload
    error = payload["error"]
    assert error["code"] == E_POLICY_DENIED, error
    assert error["how_to_fix"], "a refusal must say how to fix it"
    return error


def _egress_refused(payload: dict) -> dict:
    """A refusal that came from the egress floor, with the resolver's own code kept."""
    error = _refused(payload)
    assert error["context"]["egress_code"] == trust_service.E_EGRESS_BLOCKED, error
    return error


# ── 1-8: address literals, through the real predicate and the real resolver ──────────────────

@pytest.mark.parametrize(("url", "needle", "why"), [
    (f"http://{SHARED_METADATA}/latest/meta-data/", SHARED_METADATA, "RFC 6598 metadata"),
    ("http://100.64.0.1/", "100.64.0.1", "RFC 6598 shared space"),
    ("http://169.254.169.254/latest/meta-data/", "169.254.169.254", "link-local metadata"),
    ("http://127.0.0.1/", "127.0.0.1", "loopback"),
    ("http://[::1]/", "loopback", "IPv6 loopback"),
    ("http://[::ffff:127.0.0.1]/", "::ffff:127.0.0.1", "IPv4-mapped loopback"),
    ("http://224.0.0.1/", "multicast", "IPv4 multicast (is_global is TRUE)"),
    ("http://[ff02::1]/", "multicast", "IPv6 multicast (is_global is TRUE)"),
])
def test_a_non_public_address_literal_is_refused_by_name_before_any_dial(
    monkeypatch: pytest.MonkeyPatch, url: str, needle: str, why: str
) -> None:
    seen = _stub_transport(monkeypatch, lambda request: httpx.Response(200, request=request))

    payload = _fetch(url)

    error = _egress_refused(payload)
    assert needle in error["message"], f"{why}: the refusal must name what it refused: {error}"
    assert seen == [], f"{why}: a refused address must never be dialled"


def test_the_predicate_itself_asks_the_positive_question() -> None:
    """The predicate under test, directly — `is_global`, not a blacklist of private-ish flags."""
    denied = [SHARED_METADATA, "100.64.0.1", "100.127.255.255", "169.254.169.254",
              "127.0.0.1", "0.0.0.0", "10.0.0.1", "192.168.1.1", "198.18.0.1", "240.0.0.2",
              "224.0.0.1", "239.255.255.250", "::1", "fd00::1", "::ffff:127.0.0.1",
              "::ffff:100.100.100.200", "::ffff:224.0.0.1", "ff02::1"]
    for address in denied:
        assert tool_registry._is_public_ip(ipaddress.ip_address(address)) is False, address

    allowed = [PUBLIC_V4, PUBLIC_V4_ALT, "8.8.8.8", PUBLIC_V6, "::ffff:93.184.216.34"]
    for address in allowed:
        assert tool_registry._is_public_ip(ipaddress.ip_address(address)) is True, address


# ── 9: a name whose A record is private ─────────────────────────────────────────────────────

def test_a_name_whose_a_record_is_private_is_refused_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _stub_doh(monkeypatch, {"private-a.example.com": [(PRIVATE_V4,)]})
    seen = _stub_transport(monkeypatch, lambda request: httpx.Response(200, request=request))

    payload = _fetch("https://private-a.example.com/page")

    error = _egress_refused(payload)
    assert PRIVATE_V4 in error["message"], error
    assert "private" in error["message"], error
    assert calls == [("private-a.example.com", "A")], (
        "the private A answer refuses the whole answer set before the AAAA leg is asked"
    )
    assert seen == [], "the private answer is the destination — nothing may be dialled"


# ── 10: rebinding — public on the first resolution, private on the second ───────────────────

def test_resolve_egress_re_resolves_on_every_call_and_refuses_the_private_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public host whose SECOND resolution is private: the second call must refuse."""
    _stub_doh(monkeypatch, {"rebind.example.com": [(PUBLIC_V4,), (PRIVATE_V4,)]})

    assert trust_service.resolve_egress("rebind.example.com", 443) == [PUBLIC_V4]

    with pytest.raises(PolsiaError) as refused:
        trust_service.resolve_egress("rebind.example.com", 443)

    assert refused.value.code == trust_service.E_EGRESS_BLOCKED
    assert PRIVATE_V4 in refused.value.message


def test_a_redirect_that_would_rebind_to_a_private_answer_is_refused_before_hop_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End to end: hop 1 resolves public and is dialled; hop 2 resolves private and is NOT."""
    calls = _stub_doh(
        monkeypatch, {"rebind.example.com": [(PUBLIC_V4,), (PRIVATE_V4,)]}
    )
    seen = _stub_transport(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"Location": "/next"}, request=request),
    )

    payload = _fetch("https://rebind.example.com/start")

    error = _egress_refused(payload)
    assert PRIVATE_V4 in error["message"], error
    assert [str(request.url.host) for request in seen] == [PUBLIC_V4], (
        "hop 2 must never be dialled at the private answer"
    )
    assert [host for host, rrtype in calls if rrtype == "A"] == [
        "rebind.example.com", "rebind.example.com",
    ], "the name is resolved again on the second hop (never a cached, stale answer)"


# ── 11: a same-host redirect that changes the port ──────────────────────────────────────────

def test_a_same_host_redirect_that_changes_the_port_is_refused_at_the_redirect_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`https://example.com:443` -> `https://example.com:6379` is a DIFFERENT origin.

    Pre-fix the rule compared `hostname` only, so this hop passed the redirect gate and was only
    stopped later by the resolver's port allowlist — i.e. the gate itself did not hold.  The
    assertion is that the REDIRECT refusal fires (it names both origins) and hop 2 is never even
    resolved, let alone dialled.
    """
    calls = _stub_doh(monkeypatch, {"example.com": [(PUBLIC_V4,)]})
    seen = _stub_transport(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"Location": "https://example.com:6379/admin"}, request=request),
    )

    payload = _fetch("https://example.com:443/start")

    error = _refused(payload)
    assert "origin" in error["message"], error
    assert "https://example.com:443" in error["message"], error
    assert "https://example.com:6379" in error["message"], error
    assert len(seen) == 1, "only hop 1 was requested"
    assert calls == [("example.com", "A"), ("example.com", "AAAA")], (
        "the redirect target's origin is compared BEFORE any resolution of hop 2"
    )


# ── 12: a same-host https -> http downgrade ─────────────────────────────────────────────────

def test_a_same_host_https_to_http_redirect_is_refused_not_downgraded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 302 from `https://example.com` to `http://example.com` is a different origin.

    Pre-fix the hostname matched, so the downgraded hop was resolved (port 80 is allowed) and
    fetched over PLAINTEXT — this test's pre-fix run returned `ok: True` with an `http://` final
    URL.  The fix refuses at the redirect gate, before hop 2 exists.
    """
    _stub_doh(monkeypatch, {"example.com": [(PUBLIC_V4,)]})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(
                302, headers={"Location": "http://example.com/next"}, request=request)
        return httpx.Response(200, content=b"plaintext body", request=request)

    seen = _stub_transport(monkeypatch, handler)

    payload = _fetch("https://example.com/start")

    error = _refused(payload)
    assert "origin" in error["message"], error
    assert "https://example.com:443" in error["message"], error
    assert "http://example.com:80" in error["message"], error
    assert len(seen) == 1, "the downgraded hop must never be requested"
    assert [str(request.url.scheme) for request in seen] == ["https"], (
        "no plaintext request may be made for a URL that started on https"
    )


# ── the one resolver: the legacy export no longer re-resolves through the OS ────────────────

def test_the_legacy_floor_export_delegates_to_the_one_resolver(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`egress_policy_refusal` (the YouTube media floor) must ask the SAME resolver as web_fetch.

    Post-fix it calls `trust_service.resolve_egress`; the OS resolver is not consulted at all.
    Pre-fix it called `socket.getaddrinfo` and its own blacklist, which approved
    `100.100.100.200`.
    """
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("the legacy floor consulted the OS resolver")),
    )

    refusal = tool_registry.egress_policy_refusal(
        f"http://{SHARED_METADATA}/clip.mp4", agent_type="social_media",
        tool="youtube media fetch")

    assert refusal is not None
    assert refusal["error"]["code"] == E_POLICY_DENIED
    assert SHARED_METADATA in refusal["error"]["message"]
    assert "6598" in refusal["error"]["message"]
    assert refusal["error"]["context"]["egress_code"] == trust_service.E_EGRESS_BLOCKED


def test_the_legacy_floor_export_still_approves_a_public_literal() -> None:
    assert tool_registry.egress_policy_refusal(
        "http://93.184.216.34/", agent_type="competitor_research") is None


def test_the_legacy_floor_rechecks_the_resolvers_answer_with_the_real_predicate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Defence in depth: a resolver that answered a non-public address is still refused.

    The ONE resolver is the decision; `_is_public_ip` is the second pair of eyes on its answer.
    Stubbing `resolve_egress` here is the only way to reach that branch — the predicate itself
    is the real one.
    """
    monkeypatch.setattr(
        trust_service, "resolve_egress", lambda host, port: [SHARED_METADATA])

    refusal = tool_registry.egress_policy_refusal(
        "https://sneaky.example.com/clip.mp4", agent_type="social_media")

    assert refusal is not None
    assert refusal["error"]["code"] == E_POLICY_DENIED
    assert SHARED_METADATA in refusal["error"]["message"]
    assert "not a public address" in refusal["error"]["message"]


# ── positive controls: the floor is not "refuse everything" ─────────────────────────────────

def test_a_public_host_resolves_and_fetches(monkeypatch: pytest.MonkeyPatch) -> None:
    """`https://example.com` resolves (DoH seam) and fetches (socket seam) end to end."""
    calls = _stub_doh(monkeypatch, {"example.com": [(PUBLIC_V4,)]})
    seen = _stub_transport(
        monkeypatch,
        lambda request: httpx.Response(
            200,
            headers={"Content-Type": "text/html; charset=utf-8"},
            content=b"<html><body><h1>Example Domain</h1></body></html>",
            request=request,
        ),
    )

    payload = _fetch("https://example.com/")

    assert payload["ok"] is True, payload
    result = payload["result"]
    assert result["status"] == 200
    assert result["url"] == "https://example.com/"
    assert "Example Domain" in result["text"]
    assert result["untrusted"] is True
    assert calls == [("example.com", "A"), ("example.com", "AAAA")]

    request = seen[0]
    assert str(request.url.host) == PUBLIC_V4, "the socket dials the VETTED address"
    assert str(request.url) == f"https://{PUBLIC_V4}/", "https on its default port 443"
    assert request.headers["host"] == "example.com", "Host still names the host"
    assert request.extensions["sni_hostname"] == "example.com", "TLS still verifies the name"


def test_a_same_origin_redirect_is_still_followed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The stricter origin rule must not break an ordinary same-origin redirect."""
    _stub_doh(monkeypatch, {"example.com": [(PUBLIC_V4,)]})

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "/next"}, request=request)
        return httpx.Response(
            200, headers={"Content-Type": "text/plain"}, content=b"final body", request=request)

    seen = _stub_transport(monkeypatch, handler)

    payload = _fetch("https://example.com/start")

    assert payload["ok"] is True, payload
    assert payload["result"]["url"] == "https://example.com/next"
    assert payload["result"]["text"] == "final body"
    assert [str(request.url.host) for request in seen] == [PUBLIC_V4, PUBLIC_V4]


def test_the_origin_triple_is_scheme_host_and_effective_port() -> None:
    """The triple the redirect rule compares: scheme + hostname + EFFECTIVE port.

    `https://example.com/x` and `https://example.com:443/x` are the same origin; `http://…/x`
    and `https://…/x` are not, and neither is `https://…:6379/x`.  An unparseable port has no
    origin at all, so the rule must fail closed rather than compare a missing triple.
    """
    assert tool_registry._web_origin(urlsplit("https://Example.COM/x")) == (
        "https", "example.com", 443)
    assert tool_registry._web_origin(urlsplit("https://example.com:443/x")) == (
        "https", "example.com", 443)
    assert tool_registry._web_origin(urlsplit("http://example.com/x")) == (
        "http", "example.com", 80)
    assert tool_registry._web_origin(urlsplit("http://example.com:8080/x")) == (
        "http", "example.com", 8080)
    assert tool_registry._web_origin(urlsplit("https://example.com:not-a-port/x")) is None
