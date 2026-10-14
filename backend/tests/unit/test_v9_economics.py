"""V9/B7 — economics context route + service: honest macro context (RED-FIRST test leg).

Written before `app/services/economics_service.py` and `app/api/v1/economics.py` exist: pytest
collection fails with ModuleNotFoundError until they land (the OATH's RED leg; raw pytest output
preserved in polsia-work/build/ui-reverse/wave-v9/b7-economics/RECEIPT.md).

What is pinned (the brief's five behaviours + auth):

1. happy path — all three keyless sources answer -> every section populated, each carrying its
   EXACT source URL + fetched_at, fetched through a client with the frozen 10s timeout;
2. the World Bank's newest year answering null is served as `value: null` + reason — never 0,
   never a dropped section, never an older year substituted (the live shape this seat
   curl-probed on 2026-10-14: 2025 is null);
3. one source failing (ReadTimeout / HTTP 500) nulls ONLY its section, with a reason string
   that is "<ExceptionClass>: <message>"; sibling sections still serve;
4. the 6h in-process TTL cache: a second call inside the TTL touches NO HTTP and returns the
   ORIGINAL fetched_at; past the TTL a real refetch happens (fake clock = `now_utc` patched);
5. a malformed FRED CSV (garbage latest row / non-CSV body) is an honest `cpi: null` + reason,
   no crash, siblings unaffected;
6. the route demands the API key (401 without it), exactly like its siblings.

The HTTP layer is stubbed in the house idiom (`tests/unit/api/test_deepseek_key.py`): a fake
`httpx.AsyncClient`, monkeypatched over `economics_service.httpx.AsyncClient`, records every
client's constructor kwargs and every GET (URL + headers) — everything above the socket is the
real service code path.  The router is NOT wired into main.py (V9-D3 — the parent wires it at
settle), so this file builds a local app including the router DIRECTLY, mirroring
`tests/unit/api/test_v9_planner.py`, with main.py's PolsiaError handler mirrored so the
dependency-raised E_AUTH answers its documented 401 shape.
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.services import economics_service

AUTH = {"X-API-Key": "test-key"}
PATH = "/api/v1/economics/context"

WB_URL = economics_service.WORLD_BANK_INFLATION_URL
FRED_URL = economics_service.FRED_CPI_CSV_URL
FX_URL = economics_service.FRANKFURTER_LATEST_URL


def wb_payload(rows: list[dict[str, Any]]) -> list[Any]:
    """The World Bank [meta, rows] two-element array, meta first (live shape, 2026-10-14)."""
    meta = {
        "page": 1, "pages": 14, "per_page": 5, "total": 66, "sourceid": "2",
        "lastupdated": "2026-10-08",
    }
    return [meta, rows]


WB_ROWS_OK = [
    {"date": "2024", "value": 2.94952520485207},
    {"date": "2023", "value": 4.11633838374487},
]
WB_ROWS_NEWEST_NULL = [
    {"date": "2025", "value": None},
    {"date": "2024", "value": 2.94952520485207},
]

FRED_CSV = (
    "observation_date,CPIAUCSL\n"
    "1947-01-01,21.480\n"
    "2026-08-01,334.131\n"
    "2026-09-01,336.298\n"
)

FX_PAYLOAD = {
    "amount": 1.0,
    "base": "USD",
    "date": "2026-10-14",
    "rates": {
        "AUD": 1.4266, "BRL": 5.0242, "CAD": 1.4219, "CHF": 0.83341,
        "EUR": 0.89326, "GBP": 0.75377, "JPY": 158.45,
    },
}
FX_CONTRACTED = {"AUD": 1.4266, "CAD": 1.4219, "EUR": 0.89326, "GBP": 0.75377}


class _StubResponse:
    def __init__(self, status_code: int = 200, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


def _install_stub(
    handler: Callable[[str], Any], monkeypatch: pytest.MonkeyPatch
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Replace only the socket: record every client's kwargs and every GET (URL + headers)."""
    clients: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []

    class _Client:
        def __init__(self, *args: object, **kwargs: object) -> None:
            clients.append(dict(kwargs))

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *exc_info: object) -> bool:
            return False

        async def get(self, url: str, headers: dict[str, str] | None = None):
            calls.append({"url": url, "headers": dict(headers or {})})
            return handler(url)

    monkeypatch.setattr(economics_service.httpx, "AsyncClient", _Client)
    return clients, calls


def default_handler(url: str) -> _StubResponse:
    if url == WB_URL:
        return _StubResponse(payload=wb_payload(WB_ROWS_OK))
    if url == FRED_URL:
        return _StubResponse(text=FRED_CSV)
    if url == FX_URL:
        return _StubResponse(payload=FX_PAYLOAD)
    raise AssertionError(f"unexpected URL fetched: {url}")


@pytest_asyncio.fixture
async def econ_client(monkeypatch: pytest.MonkeyPatch):
    """A test app that includes ONLY the economics router (main.py wiring is the parent's, V9-D3)."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from app.api.v1 import economics
    from app.config import settings
    from app.core.errors import PolsiaError, http_status_for, to_http_detail

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


@pytest.fixture(autouse=True)
def isolated_cache():
    """The service's TTL cache is PROCESS-GLOBAL module state — clear it around every test."""
    economics_service._CACHE.clear()
    yield
    economics_service._CACHE.clear()


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _FakeClock:
    """Fake clock for tests ONLY: the service reads time through its own `now_utc` name."""
    fake = _FakeClock(datetime(2026, 10, 14, 12, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(economics_service, "now_utc", fake)
    return fake


# ── 1. happy path ─────────────────────────────────────────────────────────────────────────────


async def test_happy_path_serves_all_three_sources_with_provenance(
    econ_client, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    clients, calls = _install_stub(default_handler, monkeypatch)

    resp = await econ_client.get(PATH, headers=AUTH)

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {
        "generated_at", "inflation", "inflation_reason", "fx", "fx_reason", "cpi", "cpi_reason",
    }
    assert body["generated_at"] == clock.now.isoformat()

    inflation = body["inflation"]
    assert inflation["value"] == 2.94952520485207
    assert inflation["period"] == "2024"
    assert inflation["source"] == WB_URL
    assert inflation["fetched_at"] == clock.now.isoformat()
    assert inflation["reason"] is None
    assert body["inflation_reason"] is None

    fx = body["fx"]
    assert fx["usd_base"] is True
    assert fx["date"] == "2026-10-14"
    assert fx["rates"] == FX_CONTRACTED  # the contracted four, filtered from the ~30 sent
    assert fx["source"] == FX_URL
    assert fx["fetched_at"] == clock.now.isoformat()
    assert fx["reason"] is None
    assert body["fx_reason"] is None

    cpi = body["cpi"]
    assert cpi["index_value"] == 336.298
    assert cpi["observation_date"] == "2026-09-01"
    assert cpi["source"] == FRED_URL
    assert cpi["fetched_at"] == clock.now.isoformat()
    assert cpi["reason"] is None
    assert body["cpi_reason"] is None

    # Exact URLs on the wire (the .dev host — the .app host 301s), exact Accept headers.
    assert sorted(c["url"] for c in calls) == sorted([WB_URL, FRED_URL, FX_URL])
    assert {c["url"]: c["headers"].get("Accept") for c in calls} == {
        WB_URL: "application/json", FRED_URL: "text/csv", FX_URL: "application/json",
    }
    assert "api.frankfurter.dev" in FX_URL  # never the redirecting .app host
    assert all(c["timeout"] == 10.0 for c in clients)
    assert economics_service.SOURCE_TIMEOUT_SECONDS == 10.0
    assert economics_service.CACHE_TTL_SECONDS == 6 * 60 * 60


# ── 2. honest null from the World Bank ────────────────────────────────────────────────────────


async def test_world_bank_newest_year_null_is_served_as_null_with_reason_never_zero(
    econ_client, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(url: str) -> _StubResponse:
        if url == WB_URL:
            return _StubResponse(payload=wb_payload(WB_ROWS_NEWEST_NULL))
        return default_handler(url)

    _install_stub(handler, monkeypatch)
    body = (await econ_client.get(PATH, headers=AUTH)).json()

    inflation = body["inflation"]
    assert inflation is not None  # NOT dropped
    assert inflation["value"] is None  # NOT 0, and not the older year's number
    assert inflation["period"] == "2025"
    assert inflation["source"] == WB_URL
    assert isinstance(inflation["reason"], str) and "2025" in inflation["reason"]
    assert body["inflation_reason"] is None  # the SECTION answered; its value is null

    assert body["cpi"]["index_value"] == 336.298  # siblings unaffected
    assert body["fx"]["rates"] == FX_CONTRACTED


# ── 3. per-source isolation ───────────────────────────────────────────────────────────────────


async def test_a_timing_out_source_nulls_only_its_section_with_a_reason(
    econ_client, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(url: str) -> _StubResponse:
        if url == FRED_URL:
            raise httpx.ReadTimeout("read timed out")
        return default_handler(url)

    _install_stub(handler, monkeypatch)
    body = (await econ_client.get(PATH, headers=AUTH)).json()

    assert body["cpi"] is None
    assert body["cpi_reason"] == f"SourceFetchError: timed out after 10s calling GET {FRED_URL}"
    assert body["inflation"]["value"] == 2.94952520485207  # siblings still served
    assert body["fx"]["rates"] == FX_CONTRACTED


async def test_an_http_500_nulls_only_its_section_with_a_reason(
    econ_client, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    def handler(url: str) -> _StubResponse:
        if url == WB_URL:
            return _StubResponse(status_code=500, text="upstream boom")
        return default_handler(url)

    _install_stub(handler, monkeypatch)
    body = (await econ_client.get(PATH, headers=AUTH)).json()

    assert body["inflation"] is None
    assert body["inflation_reason"] == f"SourceFetchError: HTTP 500 from GET {WB_URL}"
    assert body["cpi"]["index_value"] == 336.298
    assert body["fx"]["rates"] == FX_CONTRACTED


# ── 4. the 6h TTL cache ───────────────────────────────────────────────────────────────────────


async def test_cache_within_ttl_skips_http_and_keeps_the_original_fetched_at(
    econ_client, clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, calls = _install_stub(default_handler, monkeypatch)

    first = (await econ_client.get(PATH, headers=AUTH)).json()
    assert len(calls) == 3
    assert first["generated_at"] == first["fx"]["fetched_at"] == first["inflation"]["fetched_at"]

    clock.now = clock.now + timedelta(hours=5, minutes=59)  # still inside the 6h TTL
    second = (await econ_client.get(PATH, headers=AUTH)).json()
    assert len(calls) == 3  # NO HTTP touched
    for section, reason_key in (
        ("inflation", "inflation_reason"), ("fx", "fx_reason"), ("cpi", "cpi_reason")
    ):
        assert second[section]["fetched_at"] == first[section]["fetched_at"]  # original stamp
        assert second[reason_key] is None
    assert second["generated_at"] == clock.now.isoformat()  # the RESPONSE is new...
    assert second["generated_at"] != second["fx"]["fetched_at"]  # ...sections keep provenance

    clock.now = clock.now + timedelta(minutes=2)  # 6h01m after the first fetch
    third = (await econ_client.get(PATH, headers=AUTH)).json()
    assert len(calls) == 6  # TTL expired -> a real refetch
    for section in ("inflation", "fx", "cpi"):
        assert third[section]["fetched_at"] == clock.now.isoformat()
    assert third["inflation"]["fetched_at"] != first["inflation"]["fetched_at"]


# ── 5. malformed FRED payloads ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "csv_text",
    [
        "observation_date,CPIAUCSL\n1947-01-01,21.480\n2026-09-01,not-a-number\n",
        "<html><body>rate limited, try later</body></html>",
    ],
    ids=["garbage-latest-row", "non-csv-body"],
)
async def test_malformed_fred_csv_is_an_honest_cpi_failure(
    econ_client, clock, monkeypatch: pytest.MonkeyPatch, csv_text: str
) -> None:
    def handler(url: str) -> _StubResponse:
        if url == FRED_URL:
            return _StubResponse(text=csv_text)
        return default_handler(url)

    _install_stub(handler, monkeypatch)
    resp = await econ_client.get(PATH, headers=AUTH)
    assert resp.status_code == 200  # no crash
    body = resp.json()

    assert body["cpi"] is None
    assert body["cpi_reason"].startswith("MalformedSourcePayload: FRED CSV")
    assert body["inflation"]["value"] == 2.94952520485207
    assert body["fx"]["rates"] == FX_CONTRACTED


# ── 6. auth ───────────────────────────────────────────────────────────────────────────────────


async def test_context_requires_the_api_key(econ_client) -> None:
    resp = await econ_client.get(PATH)
    assert resp.status_code == 401
