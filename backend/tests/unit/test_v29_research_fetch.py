"""V29 / L3 — the sync DeepSeek Fetch adapter, pinned RED-first (T3.1).

Read-verified against `https://api-docs.deepseek.com/guides/search` (verified 2026-10-20):
`POST https://api.deepseek.com/v1/tools/fetch` with `{"url": …}` answers `{"title", "content"}`;
Fetch costs $0.75 per 1,000 successful requests; 401/402/403/404/408/429/5xx each mean something
specific (403 = rejected by the security policy, 404 = no usable page content).

What these tests pin beyond the happy path:

* the cache holds the **full** body and caps per **call**, so a later larger `max_chars` is never
  served a previously truncated copy (the v1 plan bug);
* `billed` is true only for a request that actually reached the provider and produced content; a
  cache hit is `billed:false`, `estimated_usd:0.0`, and makes no HTTP request at all;
* failures never populate the cache, and every provider outcome is a NAMED `PolsiaError` — never a
  raw `httpx`/`json` exception;
* `max_chars` that is not an integer is `E_VALIDATION`, not a bare `TypeError`.

No network: `_CLIENT` is the seam and every request runs through `httpx.MockTransport`.
"""
from __future__ import annotations

import json

import httpx
import pytest

from app.core.errors import (
    E_AUTH,
    E_CONFIG,
    E_NOT_FOUND,
    E_RATE_LIMITED,
    E_UPSTREAM,
    E_VALIDATION,
    PolsiaError,
)
from app.services import research_fetch_service as service
from app.services.research_fetch_service import E_BALANCE

URL = "https://example.com/article"
#: Clearly fake — it only ever travels through a MockTransport.
FAKE_KEY = "sk-v29-l3-test-key-not-real"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Process-global cache + key interface: reset both so each test stands alone."""
    service._CACHE.clear()
    monkeypatch.setattr(service.secret_store, "get_deepseek_api_key", lambda: FAKE_KEY)
    yield
    service._CACHE.clear()


def _serve(monkeypatch, responder):
    """Install the `_CLIENT` seam over a MockTransport; return the requests it saw."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return responder(request)

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(service, "_CLIENT", lambda **kw: httpx.Client(transport=transport, **kw))
    return seen


def _page(title: str = "Example Domain", content: str = "# Example\n\nBody.") -> dict:
    return {"title": title, "content": content}


def test_success_returns_the_documented_shape(monkeypatch):
    body = "# Example Domain\n\n" + "x" * 1200
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json=_page(content=body)))

    out = service.fetch(URL)

    assert out["url"] == URL
    assert out["title"] == "Example Domain"
    assert out["content"] == body
    assert out["content_chars"] == len(body)
    assert out["truncated"] is False
    assert out["cached"] is False
    assert out["billed"] is True
    assert out["untrusted"] is True
    assert out["estimated_usd"] == pytest.approx(0.00075)
    assert out["fetched_at"].endswith("+00:00")
    assert len(seen) == 1
    # The key rides the Authorization header only — never a result field.
    assert FAKE_KEY not in json.dumps(out)


def test_request_is_a_bearer_post_of_the_single_url(monkeypatch):
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json=_page()))

    service.fetch("  https://example.com/a?b=1  ")

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == service.FETCH_URL
    assert request.headers["authorization"] == f"Bearer {FAKE_KEY}"
    assert request.headers["content-type"] == "application/json"
    assert json.loads(request.content) == {"url": "https://example.com/a?b=1"}


def test_second_call_is_a_cache_hit_not_billed(monkeypatch):
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json=_page(content="body " * 300)))

    first = service.fetch(URL)
    second = service.fetch(URL)

    assert len(seen) == 1
    assert second["cached"] is True
    assert second["billed"] is False
    assert second["estimated_usd"] == 0.0
    assert second["content"] == first["content"]
    assert second["fetched_at"] == first["fetched_at"]


def test_cache_hit_reapplies_a_larger_cap_to_the_full_body(monkeypatch):
    body = "abcdefghij" * 600  # 6,000 chars
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json=_page(content=body)))

    small = service.fetch(URL, max_chars=1000)
    large = service.fetch(URL, max_chars=5000)
    whole = service.fetch(URL, max_chars=6000)

    assert len(seen) == 1
    assert len(small["content"]) == 1000 and small["truncated"] is True
    assert large["content"] == body[:5000] and large["truncated"] is True
    assert large["content_chars"] == 5000
    assert whole["content"] == body and whole["truncated"] is False
    assert whole["cached"] is True and whole["billed"] is False


def test_cache_hit_applies_a_smaller_cap_downward(monkeypatch):
    body = "z" * 2400
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json=_page(content=body)))

    full = service.fetch(URL)
    cut = service.fetch(URL, max_chars=1000)

    assert len(seen) == 1
    assert len(full["content"]) == 2400
    assert len(cut["content"]) == 1000
    assert cut["content_chars"] == 1000
    assert cut["truncated"] is True
    assert cut["cached"] is True and cut["billed"] is False


def test_cache_entry_expires_after_one_hour(monkeypatch):
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json=_page()))

    service.fetch(URL)
    timestamp, payload = service._CACHE[URL]

    # 3,599 s old: still inside the 1 h window (no request).
    service._CACHE[URL] = (timestamp - 3599.0, payload)
    assert service.fetch(URL)["cached"] is True
    assert len(seen) == 1

    # 3,601 s old: expired, so the provider is asked again and the call is billed.
    service._CACHE[URL] = (timestamp - service.CACHE_TTL_S - 1.0, payload)
    again = service.fetch(URL)
    assert len(seen) == 2
    assert again["cached"] is False and again["billed"] is True


def test_cache_is_bounded_to_128_full_bodies(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(200, json=_page(content=r.url.path)))

    for index in range(service.CACHE_MAX_ENTRIES + 1):
        service.fetch(f"https://example.com/page-{index}")

    assert len(service._CACHE) == service.CACHE_MAX_ENTRIES == 128
    assert "https://example.com/page-0" not in service._CACHE
    assert f"https://example.com/page-{service.CACHE_MAX_ENTRIES}" in service._CACHE


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, E_AUTH),
        (402, E_BALANCE),
        (403, E_UPSTREAM),
        (404, E_NOT_FOUND),
        (408, E_UPSTREAM),
        (413, E_UPSTREAM),
        (429, E_RATE_LIMITED),
        (500, E_UPSTREAM),
        (502, E_UPSTREAM),
        (504, E_UPSTREAM),
    ],
)
def test_provider_statuses_are_named_refusals(monkeypatch, status, code):
    _serve(monkeypatch, lambda r: httpx.Response(status, json={"error": {"message": "nope"}}))

    with pytest.raises(PolsiaError) as excinfo:
        service.fetch(URL)

    assert excinfo.value.code == code
    assert excinfo.value.how_to_fix
    assert excinfo.value.message.startswith("research_fetch:")
    assert FAKE_KEY not in str(excinfo.value)
    assert service._CACHE == {}


def test_a_provider_failure_is_never_cached(monkeypatch):
    calls = {"n": 0}

    def responder(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(500, json={"error": {"message": "boom"}})
        return httpx.Response(200, json=_page(content="recovered body"))

    seen = _serve(monkeypatch, responder)

    with pytest.raises(PolsiaError) as excinfo:
        service.fetch(URL)
    assert excinfo.value.code == E_UPSTREAM
    assert service._CACHE == {}

    out = service.fetch(URL)
    assert out["billed"] is True and out["cached"] is False
    assert out["content"] == "recovered body"
    assert len(seen) == 2


@pytest.mark.parametrize(
    "bad_url",
    ["", "   ", None, 42, ["https://example.com/"], "ftp://example.com/x", "example.com/x",
     "https:///no-host", "https://user:pw@example.com/"],
)
def test_malformed_urls_are_validation(monkeypatch, bad_url):
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json=_page()))

    with pytest.raises(PolsiaError) as excinfo:
        service.fetch(bad_url)

    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.how_to_fix
    assert seen == []


def test_url_longer_than_the_documented_8192_is_refused(monkeypatch):
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json=_page()))

    with pytest.raises(PolsiaError) as excinfo:
        service.fetch("https://example.com/" + "a" * 8200)

    assert excinfo.value.code == E_VALIDATION
    assert "8192" in excinfo.value.message
    assert seen == []


def test_max_chars_truncates_and_flags(monkeypatch):
    body = "".join(str(i % 10) for i in range(5000))
    _serve(monkeypatch, lambda r: httpx.Response(200, json=_page(content=body)))

    out = service.fetch(URL, max_chars=1234)

    assert len(out["content"]) == 1234
    assert out["content"] == body[:1234]
    assert out["content_chars"] == 1234
    assert out["truncated"] is True


def test_max_chars_is_floored_at_1000_and_capped_at_40000(monkeypatch):
    body = "q" * 60_000
    _serve(monkeypatch, lambda r: httpx.Response(200, json=_page(content=body)))

    tiny = service.fetch(URL, max_chars=10)
    huge = service.fetch("https://example.com/other", max_chars=10**9)

    assert len(tiny["content"]) == 1000
    assert len(huge["content"]) == service.MAX_CONTENT_CHARS == 40_000
    assert huge["truncated"] is True


@pytest.mark.parametrize("bad_cap", ["lots", None, object()])
def test_non_integer_max_chars_is_validation(monkeypatch, bad_cap):
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json=_page()))

    with pytest.raises(PolsiaError) as excinfo:
        service.fetch(URL, max_chars=bad_cap)

    assert excinfo.value.code == E_VALIDATION
    assert "max_chars" in excinfo.value.message
    assert seen == []


def test_empty_content_is_not_found(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(200, json={"title": "Empty", "content": "   \n"}))

    with pytest.raises(PolsiaError) as excinfo:
        service.fetch(URL)

    assert excinfo.value.code == E_NOT_FOUND
    assert service._CACHE == {}


def test_missing_content_key_is_not_found(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(200, json={"title": "No body"}))

    with pytest.raises(PolsiaError) as excinfo:
        service.fetch(URL)

    assert excinfo.value.code == E_NOT_FOUND


def test_non_object_payload_is_a_named_upstream_error(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(200, json=["not", "an", "object"]))

    with pytest.raises(PolsiaError) as excinfo:
        service.fetch(URL)

    assert excinfo.value.code == E_UPSTREAM
    assert excinfo.value.how_to_fix


def test_non_json_body_is_a_named_upstream_error(monkeypatch):
    _serve(monkeypatch, lambda r: httpx.Response(200, text="<html>gateway</html>"))

    with pytest.raises(PolsiaError) as excinfo:
        service.fetch(URL)

    assert excinfo.value.code == E_UPSTREAM
    assert "non-JSON" in excinfo.value.message


def test_missing_key_is_config_and_makes_no_request(monkeypatch):
    monkeypatch.setattr(service.secret_store, "get_deepseek_api_key", lambda: None)
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json=_page()))

    with pytest.raises(PolsiaError) as excinfo:
        service.fetch(URL)

    assert excinfo.value.code == E_CONFIG
    assert "Settings" in excinfo.value.how_to_fix
    assert seen == []


def test_a_cache_hit_needs_no_key(monkeypatch):
    seen = _serve(monkeypatch, lambda r: httpx.Response(200, json=_page(content="cached body")))

    service.fetch(URL)
    monkeypatch.setattr(service.secret_store, "get_deepseek_api_key", lambda: None)

    out = service.fetch(URL)
    assert out["content"] == "cached body"
    assert out["cached"] is True and out["billed"] is False
    assert len(seen) == 1


def test_timeout_is_upstream(monkeypatch):
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow provider", request=request)

    _serve(monkeypatch, responder)

    with pytest.raises(PolsiaError) as excinfo:
        service.fetch(URL)

    assert excinfo.value.code == E_UPSTREAM
    assert "timed out" in excinfo.value.message
    assert service._CACHE == {}


def test_transport_failure_is_upstream(monkeypatch):
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    _serve(monkeypatch, responder)

    with pytest.raises(PolsiaError) as excinfo:
        service.fetch(URL)

    assert excinfo.value.code == E_UPSTREAM
    assert "transport" in excinfo.value.message
