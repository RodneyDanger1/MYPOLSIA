"""V29/L4 — Frankfurter v2 FX conversion + `GET /economics/convert` (RED-FIRST leg).

Written BEFORE `app/services/fx_service.py` exists: pytest collection fails with
`ModuleNotFoundError: app.services.fx_service` until the module and the route land (the raw
output is preserved in `wave-v29/l4-fx-invoices/red/T4.1-service.txt`).

What is pinned (the brief's contract):

1. the module constants ARE the brief: v2 `/rate/{base}/{quote}`, 8 s timeout, 6 h TTL,
   64-entry cap;
2. `[A-Z]{3}` validation — `""`, `"EURO"`, `"eu"`, `" eur "` (accepted, normalised), a
   non-string and `None` are refused by NAME (`E_VALIDATION`) before any socket opens;
3. the identity pair (`EUR` -> `EUR`) answers `rate: 1.0` / `source: "identity"` with NO
   network call and NO market date (there is no market for a same-currency conversion);
4. a real pair multiplies by the published rate and quantises to cents with ROUND_HALF_UP —
   `100 USD -> JPY 158.18` is `15818.0`, `1234.56 EUR -> USD @1.1236` is `1387.15` (the exact
   half-up cent, not a float-truncated one);
5. the 6 h cache: a hit makes NO HTTP call, returns `cached: true`, and KEEPS the original
   `fetched_at` (provenance is never restamped); an entry older than the TTL is refetched;
6. the cache is bounded: 65 distinct pairs leave 64 entries;
7. refusals are named: an unknown currency pair is `E_VALIDATION` (never `E_UPSTREAM` — the
   caller typed the code); a 5xx / transport failure / malformed body is `E_UPSTREAM`; nothing
   is ever converted from a zero, negative or invented rate;
8. the route `GET /api/v1/economics/convert?amount=&from=&to=` serves the alias keys `from`/`to`
   on the wire, rejects a negative or absurd amount with 422, demands the API key (401), and
   translates the service's `PolsiaError` to its documented status.

LIVE-PROBE CORRECTION (2026-10-20, curl from the build box): Frankfurter v2 answers an UNKNOWN
currency with **HTTP 422** `{"status":422,"message":"invalid currency: ZZZ"}` — not the 404 the
plan sketched. Both 404 and 422 are therefore treated as the SAME named caller error; a handler
that only mapped 404 would blame the provider (`E_UPSTREAM`) for a typo in `from`.

The socket is stubbed at the module's `_CLIENT` seam (the V29/L3 idiom): everything above the
socket is the real service code path, and no test touches the network.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.core.errors import E_UPSTREAM, E_VALIDATION, PolsiaError
from app.services import fx_service

AUTH = {"X-API-Key": "test-key"}
PATH = "/api/v1/economics/convert"

EUR_USD_URL = "https://api.frankfurter.dev/v2/rate/EUR/USD"
USD_JPY_URL = "https://api.frankfurter.dev/v2/rate/USD/JPY"


def rate_payload(base: str = "EUR", quote: str = "USD", rate: float = 1.1236) -> dict[str, Any]:
    """The exact v2 body (live shape, curl-probed 2026-10-20): date/base/quote/rate only."""
    return {"date": "2026-10-20", "base": base, "quote": quote, "rate": rate}


class _StubResponse:
    def __init__(self, status_code: int = 200, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


def _install_stub(
    handler: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replace ONLY the socket: record each client's kwargs and each GET (URL + headers)."""
    clients: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            clients.append(dict(kwargs))

        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc_info: object) -> bool:
            return False

        def get(self, url: str, headers: dict[str, str] | None = None):
            calls.append({"url": url, "headers": dict(headers or {})})
            return handler(url)

    monkeypatch.setattr(fx_service, "_CLIENT", _Client)
    return clients, calls


def happy(url: str) -> _StubResponse:
    if url == EUR_USD_URL:
        return _StubResponse(payload=rate_payload("EUR", "USD", 1.1236))
    if url == USD_JPY_URL:
        return _StubResponse(payload=rate_payload("USD", "JPY", 158.18))
    raise AssertionError(f"unexpected URL fetched: {url}")


@pytest.fixture(autouse=True)
def isolated_cache():
    """`_CACHE` is PROCESS-GLOBAL module state — clear it around every test."""
    fx_service._CACHE.clear()
    yield
    fx_service._CACHE.clear()


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Fake clock for tests ONLY: the service reads its provenance stamp through `now_utc`."""
    fake = _FakeClock(datetime(2026, 10, 20, 15, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(fx_service, "now_utc", fake)
    return fake


# ── 1. the brief's constants ─────────────────────────────────────────────────────────────────


def test_constants_pin_the_brief() -> None:
    assert fx_service.RATE_URL == "https://api.frankfurter.dev/v2/rate/{base}/{quote}"
    assert fx_service.TIMEOUT_S == 8.0
    assert fx_service.CACHE_TTL_S == 6 * 3600.0
    assert fx_service.CACHE_MAX_ENTRIES == 64


# ── 2 + 3. identity pair, then a real pair ───────────────────────────────────────────────────


def test_identity_pair_returns_the_amount_unchanged_without_a_network_call(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients, calls = _install_stub(happy, monkeypatch)

    result = fx_service.convert(1234.56, "EUR", "eur")

    assert result["from"] == "EUR" and result["to"] == "EUR"  # normalised, never raw input
    assert result["rate"] == 1.0
    assert result["amount"] == 1234.56
    assert result["converted"] == 1234.56
    assert result["source"] == "identity"
    assert result["date"] is None  # no market date exists for a same-currency conversion
    assert result["fetched_at"] == clock.now.isoformat()
    assert result["cached"] is False
    assert calls == [] and clients == []  # NOTHING was fetched


def test_eur_to_usd_uses_the_v2_rate_endpoint_and_rounds_half_up(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients, calls = _install_stub(happy, monkeypatch)

    result = fx_service.convert(1234.56, "EUR", "USD")

    assert calls == [{"url": EUR_USD_URL, "headers": {}}]
    assert all(c["timeout"] == 8.0 for c in clients)
    assert result["amount"] == 1234.56
    assert result["from"] == "EUR" and result["to"] == "USD"
    assert result["rate"] == 1.1236
    # 1234.56 x 1.1236 = 1387.1516... -> the half-up cent is 1387.15, never 1387.16/1387.14.
    assert result["converted"] == 1387.15
    assert result["date"] == "2026-10-20"
    assert result["source"] == EUR_USD_URL
    assert result["fetched_at"] == clock.now.isoformat()
    assert result["cached"] is False


def test_usd_to_jpy_scales_the_amount(clock: _FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stub(happy, monkeypatch)

    result = fx_service.convert(100, "USD", "JPY")

    assert result["rate"] == 158.18
    assert result["converted"] == 15818.0
    assert result["date"] == "2026-10-20"


def test_a_measured_zero_converts_to_zero(clock: _FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_stub(happy, monkeypatch)

    result = fx_service.convert(0, "EUR", "USD")

    assert result["converted"] == 0.0  # a measured 0 IS a reading
    assert result["rate"] == 1.1236


# ── 4. the cache ─────────────────────────────────────────────────────────────────────────────


def test_cache_hit_reuses_the_rate_keeps_fetched_at_and_touches_no_network(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, calls = _install_stub(happy, monkeypatch)

    first = fx_service.convert(10, "EUR", "USD")
    clock.now = clock.now + timedelta(hours=5, minutes=59)  # still inside the 6 h TTL
    second = fx_service.convert(20, "EUR", "USD")

    assert len(calls) == 1  # NO second HTTP call
    assert second["cached"] is True
    assert second["rate"] == first["rate"]
    assert second["date"] == first["date"]  # the market date travels with the cached rate
    assert second["fetched_at"] == first["fetched_at"]  # provenance is NEVER restamped
    assert second["converted"] == 22.47  # 20 x 1.1236 = 22.472 -> half-up 22.47
    assert second["amount"] == 20  # the NEW amount is still converted at the cached rate


def test_an_entry_past_the_six_hour_ttl_is_refetched(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, calls = _install_stub(happy, monkeypatch)

    first = fx_service.convert(10, "EUR", "USD")
    assert len(calls) == 1

    # Age the stored entry past the TTL (the TTL is measured on the monotonic clock).
    stored_at, payload = fx_service._CACHE[("EUR", "USD")]
    fx_service._CACHE[("EUR", "USD")] = (stored_at - fx_service.CACHE_TTL_S - 1.0, payload)

    clock.now = clock.now + timedelta(minutes=1)
    second = fx_service.convert(10, "EUR", "USD")

    assert len(calls) == 2  # a real refetch
    assert second["cached"] is False
    assert second["fetched_at"] == clock.now.isoformat()
    assert second["fetched_at"] != first["fetched_at"]


def test_the_cache_is_bounded_at_64_entries(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def any_pair(url: str) -> _StubResponse:
        base, quote = url.rsplit("/", 2)[-2:]
        return _StubResponse(payload=rate_payload(base, quote, 2.0))

    _install_stub(any_pair, monkeypatch)
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    quotes = [letters[i] + letters[j] + "X" for i in range(5) for j in range(13)]
    assert len(quotes) == 65

    for quote in quotes:
        fx_service.convert(1, "USD", quote)

    assert len(fx_service._CACHE) == fx_service.CACHE_MAX_ENTRIES == 64


# ── 5. refusals by name ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status_code", [404, 422], ids=["404", "422-live"])
def test_unknown_currency_is_a_named_validation_refusal(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch, status_code: int
) -> None:
    def handler(url: str) -> _StubResponse:
        return _StubResponse(status_code=status_code, payload={"status": status_code})

    _install_stub(handler, monkeypatch)

    with pytest.raises(PolsiaError) as caught:
        fx_service.convert(10, "EUR", "ZZZ")

    assert caught.value.code == E_VALIDATION  # the caller typed it, so the caller is told
    assert "EUR/ZZZ" in caught.value.message
    assert caught.value.how_to_fix
    assert fx_service._CACHE == {}  # failures are never cached


def test_a_5xx_is_a_named_upstream_refusal(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(lambda url: _StubResponse(status_code=503, payload={}), monkeypatch)

    with pytest.raises(PolsiaError) as caught:
        fx_service.convert(10, "EUR", "USD")

    assert caught.value.code == E_UPSTREAM
    assert "503" in caught.value.message
    assert fx_service._CACHE == {}


def test_transport_failure_is_upstream(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(url: str) -> _StubResponse:
        raise httpx.ConnectError("connection refused")

    _install_stub(handler, monkeypatch)

    with pytest.raises(PolsiaError) as caught:
        fx_service.convert(10, "EUR", "USD")

    assert caught.value.code == E_UPSTREAM
    assert "ConnectError" in caught.value.message
    assert fx_service._CACHE == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"date": "2026-10-20", "base": "EUR", "quote": "USD"},  # no rate at all
        {"date": "2026-10-20", "base": "EUR", "quote": "USD", "rate": 0},  # a zero rate
        {"date": "2026-10-20", "base": "EUR", "quote": "USD", "rate": -1.2},  # a negative rate
        {"date": "2026-10-20", "base": "EUR", "quote": "USD", "rate": "1.12"},  # not a number
        {"date": "2026-10-20", "base": "GBP", "quote": "USD", "rate": 1.12},  # the WRONG base
        {"date": "2026-10-20", "base": "EUR", "quote": "GBP", "rate": 1.12},  # the WRONG quote
        ["not", "a", "document"],
    ],
    ids=["no-rate", "zero-rate", "negative-rate", "string-rate", "wrong-base", "wrong-quote",
         "not-an-object"],
)
def test_a_malformed_rate_body_is_an_upstream_refusal(
    clock: _FakeClock, monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    _install_stub(lambda url: _StubResponse(payload=payload), monkeypatch)

    with pytest.raises(PolsiaError) as caught:
        fx_service.convert(10, "EUR", "USD")

    assert caught.value.code == E_UPSTREAM
    assert fx_service._CACHE == {}  # no invented rate is ever stored


@pytest.mark.parametrize(
    ("amount", "base", "quote"),
    [
        (10, "", "USD"),
        (10, "EURO", "USD"),
        (10, "EU", "USD"),
        (10, "E1R", "USD"),
        (10, None, "USD"),  # type: ignore[arg-type]
        (10, 12, "USD"),  # type: ignore[arg-type]
        (10, "EUR", None),  # type: ignore[arg-type]
        (-0.01, "EUR", "USD"),
        (True, "EUR", "USD"),
        (math.nan, "EUR", "USD"),
        (math.inf, "EUR", "USD"),
        ("10", "EUR", "USD"),  # type: ignore[arg-type]
    ],
    ids=["blank-base", "too-long", "too-short", "digit-code", "null-base", "int-base",
         "null-quote", "negative", "bool", "nan", "inf", "string-amount"],
)
def test_bad_codes_and_amounts_are_refused_before_any_request(
    clock: _FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    amount: object,
    base: object,
    quote: object,
) -> None:
    _, calls = _install_stub(happy, monkeypatch)

    with pytest.raises(PolsiaError) as caught:
        fx_service.convert(amount, base, quote)  # type: ignore[arg-type]

    assert caught.value.code == E_VALIDATION
    assert calls == []  # refused BEFORE the socket


# ── 6. the route ─────────────────────────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def convert_client(monkeypatch: pytest.MonkeyPatch):
    """A test app that includes ONLY the economics router (main.py wiring predates V29)."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from app.api.v1 import economics
    from app.config import settings
    from app.core.errors import http_status_for, to_http_detail

    monkeypatch.setattr(settings, "api_key", "test-key")
    app = FastAPI()
    app.include_router(economics.router, prefix="/api/v1")

    async def _polsia_handler(request, exc: PolsiaError):
        return JSONResponse(
            status_code=http_status_for(exc.code),
            content=to_http_detail(exc),
            headers={"X-Polsia-Error-Code": exc.code},
        )

    app.add_exception_handler(PolsiaError, _polsia_handler)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def test_route_serves_the_from_and_to_aliases(
    convert_client, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(happy, monkeypatch)

    resp = await convert_client.get(
        PATH, params={"amount": 100, "from": "EUR", "to": "USD"}, headers=AUTH
    )

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "amount", "from", "to", "rate", "converted", "date", "source", "fetched_at", "cached",
    }
    assert body["from"] == "EUR" and body["to"] == "USD"
    assert body["converted"] == 112.36  # 100 x 1.1236
    assert body["source"] == EUR_USD_URL
    assert body["cached"] is False


async def test_route_accepts_a_lowercase_code(
    convert_client, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(happy, monkeypatch)

    resp = await convert_client.get(
        PATH, params={"amount": 1, "from": "eur", "to": "usd"}, headers=AUTH
    )

    assert resp.status_code == 200
    assert resp.json()["rate"] == 1.1236


async def test_route_refuses_a_negative_amount(convert_client) -> None:
    resp = await convert_client.get(
        PATH, params={"amount": -1, "from": "EUR", "to": "USD"}, headers=AUTH
    )
    assert resp.status_code == 422


async def test_route_refuses_an_unknown_code_with_its_named_status(
    convert_client, clock: _FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_stub(lambda url: _StubResponse(status_code=422, payload={"status": 422}), monkeypatch)

    resp = await convert_client.get(
        PATH, params={"amount": 10, "from": "EUR", "to": "ZZZ"}, headers=AUTH
    )

    assert resp.status_code == 422
    # `refusal_guard` raises HTTPException(detail=...) — FastAPI's handler serialises it under
    # `detail` (the same alternate wire shape `lib/api.ts::refusalFrom` parses).
    body = resp.json()
    assert body["detail"]["code"] == E_VALIDATION
    assert body["detail"]["how_to_fix"]
    assert resp.headers["X-Polsia-Error-Code"] == E_VALIDATION


async def test_route_requires_the_api_key(convert_client) -> None:
    resp = await convert_client.get(PATH, params={"amount": 1, "from": "EUR", "to": "USD"})
    assert resp.status_code == 401
