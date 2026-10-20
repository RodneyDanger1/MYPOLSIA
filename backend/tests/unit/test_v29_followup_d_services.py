"""V29 follow-up D — the service/script layer: refusal and cache honesty.

Each test below was run RED against `9d427c7` before its fix, for the reason named in its
docstring.  The theme is the same in every case: a documented promise in the module's own
docstring is not kept by the code.

* `signals_service` promises that a link which is not an absolute http(s) URL is DROPPED and
  that a broken feed body is a NAMED per-feed reason which never touches its sibling.  A
  malformed IPv6 link raised `ValueError` out of `read()` (taking both feeds down), and an XML
  declaration naming an unknown/multi-byte encoding raised `LookupError`/`ValueError` past the
  `ParseError` handler.
* `trust_service` promises "Failures are never cached".  `check_domain` cached a DoH transport
  failure and a TLS handshake failure for the full hour, so a recovered source stayed invisible.
* `fx_service` promises that a bad amount is the named `E_VALIDATION` refusal and that every
  conversion failure is a `PolsiaError`.  An integer too large for a float raised
  `OverflowError` in `math.isfinite`, and an amount whose cent-precise product exceeded
  `Decimal`'s precision raised `decimal.InvalidOperation` out of the money path.

The network is always a labelled double at the module's documented seam (`_CLIENT`, `_doh`,
`_tls`).  No test touches the network and no product module is stubbed out.
"""
from __future__ import annotations

from typing import Any

import httpx
import pytest

from app.core.errors import E_UPSTREAM, E_VALIDATION, PolsiaError
from app.services import fx_service, signals_service, trust_service

HN = "news.ycombinator.com"
TC = "techcrunch.com"
PUBLIC_V4 = "93.184.216.34"


# ── signals ──────────────────────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _fresh_caches():
    signals_service._CACHE.clear()
    trust_service._CACHE.clear()
    fx_service._CACHE.clear()
    yield
    signals_service._CACHE.clear()
    trust_service._CACHE.clear()
    fx_service._CACHE.clear()


def _signals_transport(monkeypatch: pytest.MonkeyPatch, routes: dict[str, bytes]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = routes.get(request.url.host or "")
        if body is None:
            return httpx.Response(404, content=b"no route")
        return httpx.Response(200, content=body)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        signals_service, "_CLIENT", lambda **kwargs: httpx.Client(transport=transport, **kwargs)
    )


ATOM_OK = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>Atom one</title><link href="https://techcrunch.com/one/"/></entry>
</feed>"""


def test_a_malformed_link_is_dropped_and_its_feed_still_serves(monkeypatch):
    """A malformed IPv6 link must drop THAT item, not raise out of `read()`.

    `_safe_url`'s own docstring says an unusable link returns None "the item is then dropped".
    `urlparse("http://[::1")` raises `ValueError` before the scheme check, so the exception
    escaped `_parse`/`_fetch`/`read` and the sibling feed's items were lost with it.
    """
    feed = b"""<?xml version="1.0"?><rss version="2.0"><channel>
  <item><title>good one</title><link>https://example.com/1</link></item>
  <item><title>bad link</title><link>http://[::1</link></item>
  <item><title>good two</title><link>https://example.com/2</link></item>
</channel></rss>"""
    _signals_transport(monkeypatch, {HN: feed, TC: ATOM_OK})

    result = signals_service.read(limit=10)  # must not raise

    sources = {source["id"]: source for source in result["sources"]}
    assert sources["hn"]["ok"] is True
    assert sources["hn"]["count"] == 2
    assert sources["tc"]["ok"] is True
    assert {item["url"] for item in result["items"] if item["id"] == "hn"} == {
        "https://example.com/1",
        "https://example.com/2",
    }


def test_a_feed_body_with_an_unsupported_encoding_is_a_named_reason(monkeypatch):
    """An unknown/multi-byte XML encoding is a NAMED per-feed reason, not a crash.

    ElementTree raises `LookupError` (unknown encoding) or `ValueError` (multi-byte encoding)
    rather than `ParseError`, so the parser's handler missed it and `read()` raised instead of
    reporting `sources[i].ok = false` and serving the sibling.
    """
    body = (
        b'<?xml version="1.0" encoding="x-bogus"?><rss version="2.0"><channel>'
        b"<item><title>unreachable</title><link>https://example.com/x</link></item>"
        b"</channel></rss>"
    )
    _signals_transport(monkeypatch, {HN: body, TC: ATOM_OK})

    result = signals_service.read(limit=10)  # must not raise

    sources = {source["id"]: source for source in result["sources"]}
    assert sources["hn"]["ok"] is False
    assert sources["hn"]["reason"].startswith(f"{E_UPSTREAM}: ")
    assert "x-bogus" in sources["hn"]["reason"]
    assert sources["tc"]["ok"] is True
    assert [item["id"] for item in result["items"]] == ["tc"]


# ── trust ────────────────────────────────────────────────────────────────────────────────────

HEALTHY_TLS = {
    "ok": True,
    "not_after": "2026-12-25T23:59:59+00:00",
    "days_left": 66,
    "reason": None,
}


def _doh_stub(monkeypatch: pytest.MonkeyPatch, state: dict[str, Any]) -> None:
    """Cloudflare DoH double: raises while `state['fail']`, answers once it is cleared."""

    def fake(host: str, rrtype: str) -> dict[str, Any]:
        if state["fail"]:
            raise PolsiaError(
                E_UPSTREAM, "domain_health: the DNS resolver answered HTTP 503"
            )
        return {"status": 0, "answers": [PUBLIC_V4] if rrtype == "A" else []}

    monkeypatch.setattr(trust_service, "_doh", fake)


def test_a_dns_outage_is_not_cached_so_recovery_is_visible(monkeypatch):
    """A resolver outage is not a measurement; caching it pinned `dns.reason` for an hour.

    The module docstring and the V29/L6 receipt both say "Failures are never cached".  A second
    `check_domain` after the resolver recovered must re-measure (cached False, clean reason).
    """
    state = {"fail": True}
    _doh_stub(monkeypatch, state)
    monkeypatch.setattr(trust_service, "_tls", lambda host: dict(HEALTHY_TLS))

    first = trust_service.check_domain("example.com")
    assert first["cached"] is False
    assert first["dns"]["status"] is None
    assert "HTTP 503" in first["dns"]["reason"]

    state["fail"] = False
    second = trust_service.check_domain("example.com")

    assert second["cached"] is False, "a failed measurement was served from the cache"
    assert second["dns"]["status"] == 0
    assert second["dns"]["reason"] is None
    assert second["tls"]["ok"] is True
    assert second["reason"] is None


def test_a_tls_failure_is_not_cached_so_recovery_is_visible(monkeypatch):
    """A TLS handshake failure/timeout is not a settled certificate reading.

    `_tls` reports transport failures (timeout, reset) and certificate failures with the same
    shape, so no `tls.ok=false` result is cached; a second check must re-handshake after the
    endpoint recovers instead of replaying the old reason for an hour.
    """
    state = {"fail": False, "tls_fail": True}
    _doh_stub(monkeypatch, state)

    def tls(host: str) -> dict[str, Any]:
        if state["tls_fail"]:
            return {
                "ok": False,
                "not_after": None,
                "days_left": None,
                "reason": "the TLS handshake timed out after 8s",
            }
        return dict(HEALTHY_TLS)

    state["tls_fail"] = True
    monkeypatch.setattr(trust_service, "_tls", tls)

    first = trust_service.check_domain("example.com")
    assert first["cached"] is False
    assert first["tls"]["ok"] is False
    assert "timed out" in first["tls"]["reason"]

    state["tls_fail"] = False
    second = trust_service.check_domain("example.com")

    assert second["cached"] is False, "a failed TLS leg was served from the cache"
    assert second["tls"]["ok"] is True
    assert second["tls"]["reason"] is None
    assert second["reason"] is None


# ── trust: an unsendable URL is a caller error, not an internal failure ─────────────────────

#: A NUL in the middle of the path.  `urllib.parse` accepts it; `httpx` refuses to build the
#: request ("Invalid non-printable ASCII character in URL"), and `httpx.InvalidURL` is NOT an
#: `httpx.HTTPError`, so it used to escape the service and the `link_status` tool runner.
CONTROL_URL = "https://example.com/a" + chr(0) + "b"


def _trust_no_network(monkeypatch: pytest.MonkeyPatch) -> tuple[list[str], list[str]]:
    """Stub DoH and HTTP so "refused BEFORE any lookup" is measurable."""
    doh_calls: list[str] = []

    def fake_doh(host: str, rrtype: str) -> dict[str, Any]:
        doh_calls.append(rrtype)
        return {"status": 0, "answers": [PUBLIC_V4] if rrtype == "A" else []}

    http_calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        http_calls.append(str(request.url))
        return httpx.Response(200, request=request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(trust_service, "_doh", fake_doh)
    monkeypatch.setattr(
        trust_service, "_CLIENT", lambda **kwargs: httpx.Client(transport=transport, **kwargs)
    )
    return doh_calls, http_calls


def test_a_control_character_url_is_refused_by_name_before_any_lookup(monkeypatch):
    """`httpx.InvalidURL` must become the service's own `E_VALIDATION` refusal.

    `_link_target` accepted the string (`urllib.parse` is permissive), the service ran BOTH DoH
    lookups, and only `_fetch_headers` raised — an `httpx.InvalidURL`, which is outside the
    `httpx.HTTPError` tree and so outside every handler here.  The refusal belongs at the input
    boundary, before a resolver or a socket is touched.
    """
    doh_calls, http_calls = _trust_no_network(monkeypatch)

    with pytest.raises(PolsiaError) as caught:
        trust_service.check_link(CONTROL_URL)

    assert caught.value.code == E_VALIDATION
    assert caught.value.context.get("field") == "url"
    assert caught.value.how_to_fix
    assert doh_calls == [], "the control character was refused only after DNS lookups"
    assert http_calls == [], "no HTTP request may be attempted for an unsendable URL"


def test_the_link_status_tool_runner_returns_the_named_refusal_not_an_internal_error(monkeypatch):
    """The tool path calls `check_link` directly; the same refusal must reach the model.

    `_run_link_status` translates `PolsiaError` only, so an escaping `InvalidURL` would surface
    as an internal failure (or a bare 500 at the route B owns).  The runner must answer
    `ok:false` with `E_VALIDATION` and make no network call.
    """
    from app.agents.tool_registry import _RUNNERS

    doh_calls, http_calls = _trust_no_network(monkeypatch)

    out = _RUNNERS["link_status"](
        agent_type="competitor_research", arguments={"url": CONTROL_URL}
    )

    assert out["ok"] is False
    assert out["error"]["code"] == E_VALIDATION
    assert out["error"]["context"]["tool"] == "link_status"
    assert out["error"]["how_to_fix"]
    assert doh_calls == []
    assert http_calls == []


# ── fx ──────────────────────────────────────────────────────────────────────────────────────


class _FxResponse:
    def __init__(self, payload: object, status_code: int = 200) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


def _fx_stub(monkeypatch: pytest.MonkeyPatch, rate: float = 158.18) -> list[str]:
    calls: list[str] = []

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def get(self, url: str):
            calls.append(url)
            base, quote = url.rsplit("/", 2)[-2:]
            return _FxResponse({"date": "2026-10-20", "base": base, "quote": quote, "rate": rate})

    monkeypatch.setattr(fx_service, "_CLIENT", _Client)
    return calls


def test_an_integer_too_large_for_a_float_is_refused_by_name_before_any_request(monkeypatch):
    """`_amount` promises E_VALIDATION, but `math.isfinite(10**400)` raises OverflowError.

    `json` parses arbitrarily long integers, and the `fx_convert` tool runner forwards the
    caller's amount unbounded.  An uncaught `OverflowError` is not the house refusal.
    """
    calls = _fx_stub(monkeypatch)

    with pytest.raises(PolsiaError) as caught:
        fx_service.convert(10**400, "EUR", "USD")

    assert caught.value.code == E_VALIDATION
    assert "too large" in caught.value.message
    assert calls == [], "a refused amount must not open a socket"


def test_an_amount_too_large_for_cent_precision_is_refused_by_name(monkeypatch):
    """A product beyond Decimal's 28-digit precision must be a named refusal, not a crash.

    `10**30 USD @ 158.18` is a finite, non-negative number, so `_amount` accepts it, but
    `quantize(Decimal("0.01"))` raises `decimal.InvalidOperation` out of `_cents` — a raw
    exception from the money path instead of the documented `E_VALIDATION`.
    """
    _fx_stub(monkeypatch)

    with pytest.raises(PolsiaError) as caught:
        fx_service.convert(10**30, "USD", "JPY")

    assert caught.value.code == E_VALIDATION
    assert "too large" in caught.value.message
    assert caught.value.how_to_fix
