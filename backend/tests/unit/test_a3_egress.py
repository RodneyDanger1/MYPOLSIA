"""Wave A3 / seat S1 — the egress resolver (CONTRACTS §4.15) and the two sockets that must use it.

F-27: `check_domain`'s TLS leg and `check_link`'s HTTP leg both dialled the NAME, so the address a
guard had vetted was never the address connected to.  A2A1-01 / A2A1-02: the shared floor must ask
the positive question (`is_global`, not a blacklist of private-ish flags) and must restrict the port.

Every test suspends at a real seam: the stdlib socket (`socket.create_connection`,
`ssl.create_default_context`) or the module's own `_doh` / `_CLIENT` / `_wayback`.  No test opens a
socket, resolves a name for real, or touches the network.
"""
from __future__ import annotations

import socket
import ssl
from typing import Any, Literal

import httpx
import pytest

from app.core.errors import E_UPSTREAM, E_VALIDATION, PolsiaError
from app.services import trust_service

PUBLIC_V4 = "93.184.216.34"
PUBLIC_V4_ALT = "1.1.1.1"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"
#: Alibaba Cloud's instance-metadata service — RFC 6598 shared space, never a public host.
SHARED = "100.100.100.200"


# ── seams ───────────────────────────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_cache():
    """`_CACHE` is process-global module state — clear it around every test."""
    trust_service._CACHE.clear()
    yield
    trust_service._CACHE.clear()


def _doh_stub(
    monkeypatch: pytest.MonkeyPatch,
    *,
    a: tuple[str, ...] = (PUBLIC_V4,),
    aaaa: tuple[str, ...] = (),
    status: int = 0,
    calls: list[tuple[str, str]] | None = None,
) -> None:
    """A Cloudflare DoH answer for both families; records every query it is asked."""

    def fake(host: str, rrtype: str) -> dict[str, Any]:
        if calls is not None:
            calls.append((host, rrtype))
        return {"status": status, "answers": list(a if rrtype == "A" else aaaa)}

    monkeypatch.setattr(trust_service, "_doh", fake)


def _client_stub(monkeypatch: pytest.MonkeyPatch, handler) -> list[httpx.Request]:
    """Replace only the socket: an `httpx.MockTransport` behind the `_CLIENT` factory."""
    seen: list[httpx.Request] = []

    def _handle(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(_handle)
    monkeypatch.setattr(
        trust_service, "_CLIENT", lambda **kwargs: httpx.Client(transport=transport, **kwargs)
    )
    return seen


def _wayback_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        trust_service,
        "_wayback",
        lambda target: {"available": False, "url": None, "timestamp": None},
    )


def _recording_connection(dialled: list[tuple[str, int]]):
    """`socket.create_connection` that records its target and refuses to open a real socket."""

    def create_connection(address, **kwargs):
        dialled.append(address)
        raise OSError("test: the dial is recorded and refused; no real socket is opened")

    return create_connection


def _hostile_getaddrinfo(calls: list[str]):
    """An OS resolver that would send every name to a private address (the rebinding case)."""

    def getaddrinfo(host, *args, **kwargs):
        calls.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.5", 443))]

    return getaddrinfo


class _FakeRawSocket:
    def __enter__(self) -> _FakeRawSocket:
        return self

    def __exit__(self, *exc: object) -> Literal[False]:
        return False


class _FakeTLSSocket(_FakeRawSocket):
    def getpeercert(self) -> dict[str, Any]:
        return {"notAfter": "Dec 25 23:59:59 2099 GMT"}


class _FakeContext:
    """`ssl.create_default_context` stand-in: records the name TLS verifies against."""

    def __init__(self, names: list[str]) -> None:
        self._names = names

    def wrap_socket(self, raw: Any, *, server_hostname: str) -> _FakeTLSSocket:
        self._names.append(server_hostname)
        return _FakeTLSSocket()


# ── F-27: the TLS leg dials the vetted address, and keeps the name for SNI ────────────────────

def test_check_domain_refuses_a_private_dns_answer_without_opening_a_socket(
    monkeypatch: pytest.MonkeyPatch,
):
    """A name whose DoH answer is loopback must not be connected to at all.

    Before the fix `check_domain` dialled `("10-0-0-1.sslip.io", 443)` and let the OS resolver
    pick an address no guard had seen (F-27).
    """
    _doh_stub(monkeypatch, a=("127.0.0.1",), aaaa=())
    dialled: list[tuple[str, int]] = []
    monkeypatch.setattr(socket, "create_connection", _recording_connection(dialled))

    result = trust_service.check_domain("10-0-0-1.sslip.io")

    assert dialled == []
    assert result["tls"]["ok"] is False
    assert "refused" in (result["tls"]["reason"] or "")
    assert "refused" in (result["reason"] or "")
    assert result["dns"]["a"] == ["127.0.0.1"]  # the answer is still REPORTED, just not dialled
    assert result["cached"] is False


def test_the_tls_leg_dials_the_vetted_address_not_the_name(monkeypatch: pytest.MonkeyPatch):
    """The socket must target the DoH answer even when the OS resolver disagrees (F-27)."""
    _doh_stub(monkeypatch, a=(PUBLIC_V4,), aaaa=())
    dialled: list[tuple[str, int]] = []
    os_resolver: list[str] = []
    monkeypatch.setattr(socket, "create_connection", _recording_connection(dialled))
    monkeypatch.setattr(socket, "getaddrinfo", _hostile_getaddrinfo(os_resolver))

    result = trust_service.check_domain("example.com")

    assert dialled == [(PUBLIC_V4, 443)]
    assert os_resolver == []  # nothing in this leg asked the OS resolver at all
    assert result["tls"]["ok"] is False  # the recording seam refused the handshake
    assert "10.0.0.5" not in str(dialled)


def test_check_domain_skips_a_cname_answer_when_it_dials(monkeypatch: pytest.MonkeyPatch):
    """A DoH answer with a CNAME target beside the A record still dials the ADDRESS.

    `_vetted_addresses` is the one filter both the resolver and this leg use; without it the CNAME
    text would be treated as an un-vetted answer and the leg would report `refused: … not an IP
    address` for an ordinary CDN name.
    """
    _doh_stub(monkeypatch, a=("example.com.edgekey.net.", PUBLIC_V4), aaaa=())
    dialled: list[tuple[str, int]] = []
    monkeypatch.setattr(socket, "create_connection", _recording_connection(dialled))

    result = trust_service.check_domain("example.com")

    assert dialled == [(PUBLIC_V4, 443)]
    assert "not an IP address" not in (result["tls"]["reason"] or "")


def test_the_tls_handshake_verifies_the_name_on_the_vetted_address(
    monkeypatch: pytest.MonkeyPatch
):
    """Dial the address, verify the certificate against the NAME (`server_hostname`)."""
    _doh_stub(monkeypatch, a=(PUBLIC_V4,), aaaa=())
    dialled: list[tuple[str, int]] = []
    names: list[str] = []

    def handshake(address, **kwargs):
        dialled.append(address)
        return _FakeRawSocket()

    monkeypatch.setattr(socket, "create_connection", handshake)
    monkeypatch.setattr(ssl, "create_default_context", lambda: _FakeContext(names))

    result = trust_service.check_domain("example.com")

    assert dialled == [(PUBLIC_V4, 443)]
    assert names == ["example.com"]  # SNI + certificate verification still name the host
    assert result["tls"]["ok"] is True
    assert (result["tls"]["not_after"] or "").startswith("2099-12-25")
    assert result["cached"] is False


# ── F-27: the HTTP leg of check_link dials the address the guard vetted ───────────────────────

def test_check_link_dials_the_vetted_address_and_keeps_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
):
    """`_walk` already computes `_resolve_public`; the request must go to THAT address."""
    _doh_stub(monkeypatch, a=(PUBLIC_V4,), aaaa=())
    _wayback_stub(monkeypatch)
    seen = _client_stub(monkeypatch, lambda request: httpx.Response(200, request=request))
    os_resolver: list[str] = []
    monkeypatch.setattr(socket, "getaddrinfo", _hostile_getaddrinfo(os_resolver))

    result = trust_service.check_link("https://example.com/page")

    assert result["status"] == 200
    assert result["final_url"] == "https://example.com/page"
    assert os_resolver == []
    request = seen[0]
    assert request.url.host == PUBLIC_V4
    assert request.url.raw_path == b"/page"
    assert request.headers["host"] == "example.com"
    assert request.extensions["sni_hostname"] == "example.com"


# ── A2A1-01: the floor asks the POSITIVE question (`is_global`) ───────────────────────────────

def test_the_contract_code_is_frozen():
    assert trust_service.E_EGRESS_BLOCKED == "E_EGRESS_BLOCKED"


@pytest.mark.parametrize("address", [SHARED, "100.64.0.1", "100.127.255.255"])
def test_resolve_egress_refuses_rfc6598_shared_space(
    monkeypatch: pytest.MonkeyPatch, address: str
):
    """RFC 6598 is neither private nor reserved, yet it is not public (A2A1-01).

    Alibaba Cloud's metadata service lives at 100.100.100.200; a blacklist of `is_private`-style
    flags approved it.
    """
    with pytest.raises(PolsiaError) as refused:
        trust_service.resolve_egress(address, 443)

    assert refused.value.code == trust_service.E_EGRESS_BLOCKED
    assert address in refused.value.message
    assert "6598" in refused.value.message
    assert refused.value.how_to_fix


def test_resolve_egress_refuses_the_whole_answer_when_one_address_is_shared(
    monkeypatch: pytest.MonkeyPatch,
):
    """Fail closed: one bad address in a mixed answer refuses the host (no partial list)."""
    _doh_stub(monkeypatch, a=(PUBLIC_V4, SHARED), aaaa=())

    with pytest.raises(PolsiaError) as refused:
        trust_service.resolve_egress("mixed.example.com", 443)

    assert refused.value.code == trust_service.E_EGRESS_BLOCKED
    assert SHARED in refused.value.message


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",          # loopback
        "10.0.0.1",           # RFC 1918
        "169.254.169.254",    # link-local, the cloud metadata address
        "0.0.0.0",            # unspecified
        "224.0.0.1",          # multicast — `is_global` is TRUE here, so the second filter matters
        "239.255.255.250",
        "ff02::1",
        "240.0.0.2",          # reserved
        "198.18.0.1",         # benchmarking, `is_private`
        "::ffff:127.0.0.1",   # loopback in disguise
    ],
)
def test_resolve_egress_refuses_every_non_public_family(
    monkeypatch: pytest.MonkeyPatch, address: str
):
    with pytest.raises(PolsiaError) as refused:
        trust_service.resolve_egress(address, 443)

    assert refused.value.code == trust_service.E_EGRESS_BLOCKED
    assert refused.value.how_to_fix


@pytest.mark.parametrize("address", ["8.8.8.8", PUBLIC_V4, PUBLIC_V4_ALT, PUBLIC_V6])
def test_resolve_egress_approves_globally_routable_addresses(
    monkeypatch: pytest.MonkeyPatch, address: str
):
    assert trust_service.resolve_egress(address, 443) == [address]


def test_resolve_egress_returns_the_doh_answers_in_order_without_duplicates(
    monkeypatch: pytest.MonkeyPatch,
):
    _doh_stub(monkeypatch, a=(PUBLIC_V4, PUBLIC_V4_ALT, PUBLIC_V4), aaaa=(PUBLIC_V6,))

    assert trust_service.resolve_egress("example.com", 443) == [
        PUBLIC_V4, PUBLIC_V4_ALT, PUBLIC_V6,
    ]


def test_resolve_egress_skips_a_cname_answer_and_keeps_the_addresses(
    monkeypatch: pytest.MonkeyPatch,
):
    """DoH answers carry CNAME targets beside the A records; a name is not a destination."""
    _doh_stub(monkeypatch, a=("example.com.edgekey.net.", PUBLIC_V4), aaaa=())

    assert trust_service.resolve_egress("example.com", 443) == [PUBLIC_V4]


@pytest.mark.parametrize("status", [3, 2])
def test_resolve_egress_refuses_a_name_the_resolver_cannot_answer(
    monkeypatch: pytest.MonkeyPatch, status: int
):
    _doh_stub(monkeypatch, a=(), aaaa=(), status=status)

    with pytest.raises(PolsiaError) as refused:
        trust_service.resolve_egress("gone.example.com", 443)

    assert refused.value.code == trust_service.E_EGRESS_BLOCKED
    assert "NXDOMAIN" in refused.value.message or "RCODE 2" in refused.value.message
    assert refused.value.context.get("refused_by") == E_UPSTREAM


# ── A2A1-02: the port allowlist ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("port", [22, 6379, 9200, 11211, 2375, 8080, 8443, 0, -1])
def test_resolve_egress_refuses_a_port_outside_80_and_443(
    monkeypatch: pytest.MonkeyPatch, port: int
):
    """Only the web ports are egress (A2A1-02): Redis/SSH/Docker are not fetchable."""
    with pytest.raises(PolsiaError) as refused:
        trust_service.resolve_egress("example.com", port)

    assert refused.value.code == trust_service.E_EGRESS_BLOCKED
    assert "port" in refused.value.message
    assert refused.value.context.get("port") == port


@pytest.mark.parametrize("port", [80, 443])
def test_resolve_egress_approves_the_web_ports(monkeypatch: pytest.MonkeyPatch, port: int):
    _doh_stub(monkeypatch)

    assert trust_service.resolve_egress("example.com", port) == [PUBLIC_V4]


def test_a_redirect_may_not_change_the_port(monkeypatch: pytest.MonkeyPatch):
    """`check_link` revalidates every hop: a same-host 302 to :8443 is refused, not followed."""
    _doh_stub(monkeypatch)
    _wayback_stub(monkeypatch)
    seen = _client_stub(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"Location": "https://example.com:8443/admin"}, request=request
        ),
    )

    with pytest.raises(PolsiaError) as refused:
        trust_service.check_link("https://example.com/")

    assert refused.value.code == E_VALIDATION
    assert "port" in refused.value.message
    assert len(seen) == 1  # the first hop was fetched, the :8443 hop never was


# ── §4.15 `pinned_http_get` ───────────────────────────────────────────────────────────────────

def test_pinned_http_get_dials_one_vetted_address_and_keeps_host_and_sni(
    monkeypatch: pytest.MonkeyPatch,
):
    _doh_stub(monkeypatch, a=(PUBLIC_V4,), aaaa=())
    seen = _client_stub(
        monkeypatch,
        lambda request: httpx.Response(
            200, headers={"Content-Type": "text/plain"}, content=b"hello", request=request
        ),
    )

    status, headers, body = trust_service.pinned_http_get(
        "https://example.com/a/b?q=1", timeout=5.0, max_bytes=1024,
        headers={"Accept": "text/plain"},
    )

    assert (status, body) == (200, b"hello")
    assert headers["content-type"] == "text/plain"
    request = seen[0]
    assert request.method == "GET"
    assert request.url.host == PUBLIC_V4
    assert request.url.raw_path == b"/a/b?q=1"
    assert request.headers["host"] == "example.com"
    assert request.headers["accept"] == "text/plain"
    assert request.extensions["sni_hostname"] == "example.com"


def test_pinned_http_get_returns_a_redirect_instead_of_following_it(
    monkeypatch: pytest.MonkeyPatch,
):
    """Redirects are the caller's to re-check (`web_fetch` re-passes the floor per hop)."""
    _doh_stub(monkeypatch, a=(PUBLIC_V4,), aaaa=())
    _client_stub(
        monkeypatch,
        lambda request: httpx.Response(
            302, headers={"Location": "https://example.com/next"}, request=request
        ),
    )

    status, headers, body = trust_service.pinned_http_get(
        "https://example.com/", timeout=5.0, max_bytes=1024
    )

    assert status == 302
    assert headers["location"] == "https://example.com/next"
    assert body == b""


def test_pinned_http_get_refuses_past_max_bytes_instead_of_truncating(
    monkeypatch: pytest.MonkeyPatch,
):
    """A typed refusal — NOT a silently truncated body (CONTRACTS §4.15)."""
    _doh_stub(monkeypatch, a=(PUBLIC_V4,), aaaa=())

    def body_chunks():
        for _ in range(10):
            yield b"x" * 100

    _client_stub(monkeypatch, lambda request: httpx.Response(200, content=body_chunks()))

    with pytest.raises(PolsiaError) as refused:
        trust_service.pinned_http_get("https://example.com/big", timeout=5.0, max_bytes=250)

    assert refused.value.code == E_UPSTREAM
    assert refused.value.context.get("reason") == "body_exceeds_max_bytes"
    assert refused.value.context.get("max_bytes") == 250
    assert refused.value.context.get("bytes_read", 0) > 250
    assert refused.value.how_to_fix


def test_pinned_http_get_accepts_a_body_exactly_at_the_bound(monkeypatch: pytest.MonkeyPatch):
    _doh_stub(monkeypatch, a=(PUBLIC_V4,), aaaa=())
    _client_stub(monkeypatch, lambda request: httpx.Response(200, content=b"y" * 250))

    status, _headers, body = trust_service.pinned_http_get(
        "https://example.com/exact", timeout=5.0, max_bytes=250
    )

    assert (status, len(body)) == (200, 250)


def test_pinned_http_get_refuses_a_shared_host_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
):
    _doh_stub(monkeypatch)
    seen = _client_stub(monkeypatch, lambda request: httpx.Response(200, request=request))

    with pytest.raises(PolsiaError) as refused:
        trust_service.pinned_http_get(
            f"http://{SHARED}/latest/meta-data/iam/security-credentials/",
            timeout=5.0, max_bytes=4096,
        )

    assert refused.value.code == trust_service.E_EGRESS_BLOCKED
    assert seen == []  # nothing was dialled


def test_pinned_http_get_tries_the_next_vetted_address(monkeypatch: pytest.MonkeyPatch):
    """A refused connection on the first address moves to the next VETTED one — no re-resolve."""
    _doh_stub(monkeypatch, a=(PUBLIC_V4, PUBLIC_V4_ALT), aaaa=())

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == PUBLIC_V4:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, content=b"second address", request=request)

    seen = _client_stub(monkeypatch, handler)

    status, _headers, body = trust_service.pinned_http_get(
        "https://example.com/", timeout=5.0, max_bytes=1024
    )

    assert (status, body) == (200, b"second address")
    assert [request.url.host for request in seen] == [PUBLIC_V4, PUBLIC_V4_ALT]


def test_pinned_http_get_never_uses_the_os_resolver(monkeypatch: pytest.MonkeyPatch):
    """One DoH resolution per family, then the socket is pinned — no `getaddrinfo` (F-27/F-28)."""
    calls: list[tuple[str, str]] = []
    _doh_stub(monkeypatch, a=(PUBLIC_V4,), aaaa=(), calls=calls)
    _client_stub(monkeypatch, lambda request: httpx.Response(200, content=b"ok", request=request))
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("pinned_http_get consulted the OS resolver")
        ),
    )

    status, _headers, body = trust_service.pinned_http_get(
        "https://example.com/", timeout=5.0, max_bytes=1024
    )

    assert (status, body) == (200, b"ok")
    assert calls == [("example.com", "A"), ("example.com", "AAAA")]


@pytest.mark.parametrize(
    ("url", "kwargs"),
    [
        ("https://example.com/", {"timeout": 5.0, "max_bytes": 0}),
        ("https://example.com/", {"timeout": 0.0, "max_bytes": 1024}),
        ("ftp://example.com/", {"timeout": 5.0, "max_bytes": 1024}),
        ("https://user:pw@example.com/", {"timeout": 5.0, "max_bytes": 1024}),
    ],
)
def test_pinned_http_get_refuses_an_unusable_request(
    monkeypatch: pytest.MonkeyPatch, url: str, kwargs: dict[str, Any]
):
    _doh_stub(monkeypatch)

    with pytest.raises(PolsiaError) as refused:
        trust_service.pinned_http_get(url, **kwargs)

    assert refused.value.code == E_VALIDATION
    assert refused.value.how_to_fix
