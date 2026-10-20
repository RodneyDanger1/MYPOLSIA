"""V29 follow-up B — `research_fetch`'s `max_chars` must REFUSE a non-integer, never coerce it.

WHY THIS FILE EXISTS
--------------------
`research_fetch_service.fetch` documents `max_chars` as an INTEGER and the tool spec declares
`{"type": "integer"}`.  The shipped body ran `int(max_chars)` inside a `(TypeError, ValueError)`
guard, which is a silent coercion for every one of these:

* `True` / `False` — `int(True) == 1`, so a boolean was accepted and floored to the 1,000-char
  minimum instead of being refused;
* `"5000"` — a numeric STRING was parsed and accepted;
* `2.5` — a fractional value was truncated to 2 and then floored to 1,000;
* `float("inf")` — `int(inf)` raises `OverflowError`, which the guard did NOT catch, so the raw
  exception escaped `fetch` and the tool runner (which catches only `PolsiaError`) would turn a
  caller mistake into an internal error instead of a named `E_VALIDATION`.

The V30/A3 adversarial list expects `max_chars` string/bool to answer `E_VALIDATION`; these tests
are RED on the shipped code and GREEN after the strict-integer check.

No network: `_CLIENT` is the documented seam and every answer is a `httpx.MockTransport`.
"""
from __future__ import annotations

import httpx
import pytest

from app.core.errors import E_VALIDATION, PolsiaError
from app.services import research_fetch_service as service

URL = "https://example.com/article"
#: Clearly fake, ASCII, and not shaped like a provider credential.
FAKE_KEY = "v29-followup-b-fake-key-do-not-use"


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    """Process-global cache + the key interface: reset both so each test stands alone."""
    service._CACHE.clear()
    monkeypatch.setattr(service.secret_store, "get_deepseek_api_key", lambda: FAKE_KEY)
    yield
    service._CACHE.clear()


def _serve(monkeypatch) -> list[httpx.Request]:
    """Install the `_CLIENT` seam over a MockTransport; return the requests it saw."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"title": "T", "content": "x" * 2400})

    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(
        service, "_CLIENT", lambda **kwargs: httpx.Client(transport=transport, **kwargs)
    )
    return seen


def _assert_refused_without_a_request(seen: list[httpx.Request], max_chars: object) -> None:
    with pytest.raises(PolsiaError) as caught:
        service.fetch(URL, max_chars=max_chars)  # type: ignore[arg-type]

    assert caught.value.code == E_VALIDATION, (
        f"max_chars={max_chars!r} answered {caught.value.code!r}; a non-integer is the caller's "
        "E_VALIDATION, never a coerced cap or a raw interpreter error"
    )
    assert "max_chars" in caught.value.message
    assert seen == [], "a refused max_chars must be refused BEFORE the provider is called"


def test_a_boolean_max_chars_is_refused_not_coerced_to_one(monkeypatch):
    """`int(True) == 1` used to pass validation and silently floor the cap to 1,000 chars."""
    _assert_refused_without_a_request(_serve(monkeypatch), True)


def test_a_numeric_string_max_chars_is_refused_not_parsed(monkeypatch):
    """`int("5000")` used to pass validation — a JSON string is not the declared integer."""
    _assert_refused_without_a_request(_serve(monkeypatch), "5000")


def test_a_fractional_max_chars_is_refused_not_truncated(monkeypatch):
    """`int(2.5)` used to truncate to 2 and then floor to 1,000 — a silent, lossy coercion."""
    _assert_refused_without_a_request(_serve(monkeypatch), 2.5)


def test_an_infinite_max_chars_is_a_named_refusal_not_a_raw_overflow(monkeypatch):
    """`int(inf)` raised an uncaught `OverflowError`; the caller must see `E_VALIDATION`."""
    _assert_refused_without_a_request(_serve(monkeypatch), float("inf"))
