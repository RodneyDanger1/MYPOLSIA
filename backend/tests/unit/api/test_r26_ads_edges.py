"""R26 — the ads **API** edge sweep: auth envelope, refusal bridge, query-param boundaries, real DDL.

Measured before this lane (`raw/baseline-coverage.txt`): `app/api/v1/ads.py` 12 stmts / 3 miss / **75.0%**
— lines 21-23 (the route body) had never executed.  This file drives the shipped app object through
httpx's ASGI transport with the repo's own overrides (`tests/conftest.py:80-95`), plus a second client
bound to a **migration-head** database (fixture re-used from `tests/unit/test_r26_ads_sweep.py` — one
definition, two files) so the route is proven against the DDL production actually ships.

Shape facts pinned here (probe2, `raw/probe2-output.txt`): a missing/wrong key is the NAMED `E_AUTH`
envelope plus `WWW-Authenticate: X-API-Key` (`app/main.py:80-87`); a DB read failure arrives as 503
`E_DB` through `refusal_guard()` (`app/api/deps.py:95-125` → `app/main.py:205-220`) carrying
`X-Polsia-Error-Code`; a malformed `limit` is FastAPI's own uncoded `detail` array (by design,
`app/main.py:216-220`).
"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest_asyncio
from fastapi import Response
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.errors import E_AUTH, E_DB, ERROR_HINTS
from app.models.ads import AdCampaign
from tests.unit.test_r26_ads_sweep import (
    head_db,  # noqa: F401  (fixture registration for this module)
)

HEADERS = {"X-API-Key": "test-key"}
URL = "/api/v1/ads/campaigns"
#: The frozen response contract (`app/schemas/social.py:43-52`) the /ads page reads — key-for-key.
RESPONSE_KEYS = {
    "id",
    "platform",
    "external_id",
    "name",
    "goal",
    "status",
    "daily_budget_usd",
    "total_spent_usd",
    "created_at",
}


async def _add(db: AsyncSession, platform: str = "google_ads", name: str = "camp", **kw) -> AdCampaign:
    row = AdCampaign(platform=platform, name=name, **kw)
    db.add(row)
    await db.commit()
    return row


@pytest_asyncio.fixture
async def head_api_client(request, mock_redis):
    """The real app object wired to a migration-head SQLite database, without raising app exceptions
    (an unhandled 500 must be observable as a response — that is the point of one test below).

    `head_db` is resolved by name: naming it as a parameter would shadow the imported fixture
    function (ruff F811), and the import above is what registers it for this module.
    """
    from app.config import settings
    from app.core.database import get_db
    from app.main import app

    _db_path, url = request.getfixturevalue("head_db")
    engine = create_async_engine(url, connect_args={"check_same_thread": False})
    Session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with Session() as session:
        app.dependency_overrides[get_db] = lambda: session
        settings.api_key = "test-key"
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield client, session
    app.dependency_overrides.clear()
    await engine.dispose()


# ── auth: the named E_AUTH envelope ──────────────────────────────────────────


async def test_a_missing_key_is_the_named_401_envelope_with_the_challenge(api_client) -> None:
    resp = await api_client.get(URL)
    assert resp.status_code == 401
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_AUTH
    assert body["error"]["how_to_fix"] == ERROR_HINTS[E_AUTH]
    assert resp.headers.get("WWW-Authenticate") == "X-API-Key"


async def test_a_wrong_key_is_the_same_named_401(api_client) -> None:
    resp = await api_client.get(URL, headers={"X-API-Key": "definitely-wrong"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == E_AUTH


# ── the happy path and its boundaries ────────────────────────────────────────


async def test_an_empty_table_is_an_honest_empty_list(api_client) -> None:
    resp = await api_client.get(URL, headers=HEADERS)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_rows_come_back_newest_first_with_exactly_the_frozen_fields(
    api_client, async_db_session
) -> None:
    await _add(async_db_session, "google_ads", "old")
    await _add(
        async_db_session, "meta", "new", status="paused",
        daily_budget_usd=Decimal("12.5000"), total_spent_usd=Decimal("3.2500"),
    )

    resp = await api_client.get(URL, headers=HEADERS)
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["name"] for r in rows] == ["new", "old"], "id DESC is the documented order"
    assert set(rows[0]) == RESPONSE_KEYS, "the response schema is a contract, not a superset"
    assert rows[0]["status"] == "paused" and rows[0]["platform"] == "meta"
    assert isinstance(rows[0]["daily_budget_usd"], float)
    assert rows[0]["daily_budget_usd"] == 12.5
    assert rows[0]["total_spent_usd"] == 3.25
    assert rows[0]["created_at"], "created_at is part of the served shape"
    assert rows[1]["daily_budget_usd"] == 0.0


async def test_the_route_function_returns_validated_models_when_called_directly(
    async_db_session,
) -> None:
    """The route function's own return contract, exercised without the HTTP stack.

    Two jobs: (1) it pins what `response_model=list[AdCampaignResponse]` relies on — the handler
    returns *validated pydantic models*, not ORM objects; (2) it is the measurement that attributes
    `ads.py:23` correctly.  Over the ASGI/httpx path the same statement executes on every
    row-serving request (interpreter-level proof: `raw/probe9_trace_events.py` shows `line 23` /
    `return 23` for the route frame) but coverage.py 7.16.2 loses the event for this stack — a
    tooling artefact, and NO minimal FastAPI repro reproduces it (`raw/probe11/12/13` — same app
    shape with and without a sync dependency).  No `# pragma: no cover` was added for it: the
    miss is stated, not hidden.
    """
    from app.api.v1.ads import ads_campaigns
    from app.schemas.social import AdCampaignResponse

    await _add(async_db_session, "google_ads", "direct")
    # V20/L1: the route now also declares the honest total on `X-Polsia-Total`, so the FastAPI
    # injection it receives in production (`response: Response`) is supplied here too; the return
    # contract this test pins — validated models, in order — is unchanged.
    out = await ads_campaigns(
        response=Response(), platform=None, status_filter=None, limit=50, db=async_db_session
    )
    assert [type(row) for row in out] == [AdCampaignResponse]
    assert out[0].name == "direct"


async def test_platform_and_status_filters_are_applied(api_client, async_db_session) -> None:
    await _add(async_db_session, "google_ads", "ga-active")
    await _add(async_db_session, "meta", "meta-active")
    await _add(async_db_session, "google_ads", "ga-paused", status="paused")

    assert [r["name"] for r in (await api_client.get(
        URL, headers=HEADERS, params={"platform": "google_ads"})).json()] == [
        "ga-paused", "ga-active"]
    assert [r["name"] for r in (await api_client.get(
        URL, headers=HEADERS, params={"status": "paused"})).json()] == ["ga-paused"]
    both = await api_client.get(
        URL, headers=HEADERS, params={"platform": "google_ads", "status": "active"})
    assert [r["name"] for r in both.json()] == ["ga-active"]
    # DELIBERATE RE-PIN (V20/L1, card L2-08): an unknown `status` is caller input, not a filter
    # that happens to match nothing.  The 200-[] contract this test used to pin made a typo and an
    # empty table indistinguishable; every status-filtered /api/v1 read now answers the same named
    # 422 (`ck_ad_campaigns_status`'s vocabulary in the message + context).  No client sends an
    # unknown value: `components/ads/query.ts` omits the param for null/"" and
    # `components/ads/CampaignFilters.tsx:28` offers exactly the four CHECK values.
    unknown = await api_client.get(URL, headers=HEADERS, params={"status": "womble"})
    assert unknown.status_code == 422, unknown.text[:300]
    assert unknown.headers.get("X-Polsia-Error-Code") == "E_VALIDATION"
    assert unknown.json()["error"]["context"]["status"] == "womble"


async def test_blank_filter_values_are_refused_by_name_not_treated_as_no_filter(
    api_client, async_db_session
) -> None:
    """DELIBERATE RE-PIN (V20/L1, card L2-08) — this test used to pin `?status=` -> `200 []`.

    `?status=` arrives as `""`, not `None`, and `""` is not a status: the house-wide decision the
    old docstring called a REQUIRED-FOLLOW-UP is now made, so a blank value is the SAME named 422
    as any other unknown one instead of a quiet empty page.  It is still NOT normalised to "no
    filter" (the half the old pin was protecting): the answer is a refusal, never a full list.
    No shipped client sends it — the ads query builder omits a null/blank filter entirely, and the
    invoices/support pages build their path from `""` -> param omitted.
    """
    await _add(async_db_session, "google_ads", "ga")
    for blank in ("", "  "):
        resp = await api_client.get(URL, headers=HEADERS, params={"status": blank, "platform": ""})
        assert resp.status_code == 422, f"{blank!r} -> {resp.status_code}"
        assert resp.json()["error"]["code"] == "E_VALIDATION"
    # `platform` is a free-form value (no vocabulary), so a blank platform is still a literal
    # equality filter — unchanged by this lane.
    platform_only = await api_client.get(URL, headers=HEADERS, params={"platform": ""})
    assert platform_only.status_code == 200 and platform_only.json() == []


async def test_limit_boundaries_and_their_422s(api_client, async_db_session) -> None:
    for i in range(3):
        await _add(async_db_session, "google_ads", f"c{i}")

    one = await api_client.get(URL, headers=HEADERS, params={"limit": "1"})
    assert one.status_code == 200 and [r["name"] for r in one.json()] == ["c2"]
    top = await api_client.get(URL, headers=HEADERS, params={"limit": "200"})
    assert top.status_code == 200 and len(top.json()) == 3

    for bad, expected in (("0", "greater_than_equal"), ("201", "less_than_equal")):
        resp = await api_client.get(URL, headers=HEADERS, params={"limit": bad})
        assert resp.status_code == 422
        detail = resp.json()["detail"][0]
        assert detail["loc"] == ["query", "limit"] and detail["type"] == expected
        assert "error" not in resp.json(), (
            "framework validation is uncoded BY DESIGN (main.py:216-220); pinning it here so a "
            "future 'unify the 422 shape' change is a decision, not an accident"
        )
    non_int = await api_client.get(URL, headers=HEADERS, params={"limit": "abc"})
    assert non_int.status_code == 422
    assert non_int.json()["detail"][0]["type"] == "int_parsing"


async def test_unknown_query_parameters_are_ignored(api_client, async_db_session) -> None:
    await _add(async_db_session, "google_ads", "ga")
    resp = await api_client.get(URL, headers=HEADERS, params={"bogus": "1", "limit": "1"})
    assert resp.status_code == 200 and len(resp.json()) == 1


async def test_a_response_carries_the_correlation_headers(api_client) -> None:
    resp = await api_client.get(URL, headers=HEADERS)
    assert resp.headers.get("X-Request-ID")
    assert resp.headers.get("X-Response-Time-ms")


# ── the refusal bridge ───────────────────────────────────────────────────────


async def test_an_unreadable_table_is_a_named_503_through_the_refusal_guard(
    api_client, async_db_session
) -> None:
    await async_db_session.execute(text("DROP TABLE ad_campaigns"))
    resp = await api_client.get(URL, headers=HEADERS)
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == E_DB
    assert body["error"]["how_to_fix"] == ERROR_HINTS[E_DB]
    assert body["error"]["context"] == {"query": "ad campaigns"}
    assert resp.headers.get("X-Polsia-Error-Code") == E_DB


# ── the migration head: the route against the DDL production ships ───────────


async def test_head_schema_serves_every_documented_status_through_the_route(
    head_api_client,
) -> None:
    client, session = head_api_client
    for status in ("active", "paused", "ended", "draft"):
        await _add(session, "google_ads", f"head-{status}", status=status)
    session.add(AdCampaign(platform="meta", name="head-orm-default"))
    await session.commit()

    resp = await client.get(URL, headers=HEADERS)
    assert resp.status_code == 200
    rows = resp.json()
    assert [r["name"] for r in rows] == [
        "head-orm-default", "head-draft", "head-ended", "head-paused", "head-active"]
    assert {r["status"] for r in rows[1:]} == {"active", "paused", "ended", "draft"}
    assert rows[0]["status"] == "active", "the ORM default applies on the migrated DDL too"

    paused = await client.get(URL, headers=HEADERS, params={"status": "paused"})
    assert [r["name"] for r in paused.json()] == ["head-paused"]


async def test_head_schema_empty_table_is_an_empty_list(head_api_client) -> None:
    client, _session = head_api_client
    resp = await client.get(URL, headers=HEADERS)
    assert resp.status_code == 200 and resp.json() == []


# ── cross-file defect: PROVEN at the HTTP boundary in R26, FIXED in R31 ──────


async def test_a_schema_legal_null_row_does_not_500_the_whole_endpoint(head_api_client) -> None:
    """R26 F1 — was `xfail(strict=False)`; fixed in R31 (`app/schemas/social.py`).

    R26's proof: `status`/`daily_budget_usd`/`total_spent_usd` are nullable on both engines
    (0001:167-179; CHECK(NULL) passes) but the response model required them, so ONE schema-legal
    NULL row turned the whole list into an unnamed 500 for every row (probe2 Q6).  R31 serves the
    row: 200, with the three fields carried as `null` (never a fabricated value).
    """
    client, session = head_api_client
    await _add(session, "google_ads", "fine")
    await session.execute(text(
        "INSERT INTO ad_campaigns (platform, name, status, daily_budget_usd, total_spent_usd,"
        " created_at) VALUES ('meta','legacy-null',NULL,NULL,NULL,"
        " strftime('%Y-%m-%d %H:%M:%S','now'))"))
    await session.commit()

    resp = await client.get(URL, headers=HEADERS)
    assert resp.status_code != 500, (
        "a schema-legal row must not produce an unnamed 500; it must be served or refused by name"
    )
    assert resp.status_code == 200, f"expected the row to be served: {resp.text[:200]}"
    rows = {r["name"]: r for r in resp.json()}
    assert set(rows) == {"fine", "legacy-null"}
    null_row = rows["legacy-null"]
    assert (null_row["status"], null_row["daily_budget_usd"], null_row["total_spent_usd"]) == (
        None, None, None,
    )
    assert rows["fine"]["status"] == "active", "the fix must not disturb recorded values"


def test_the_response_model_serves_the_schema_legal_null_row() -> None:
    """Was `test_the_null_row_defect_is_reproducible_from_the_response_model_alone` (R26) — renamed
    and INVERTED in R31 because the defect it pinned is fixed.

    R26 asserted the model RAISED `ValidationError` for a schema-legal NULL row; the fix makes the
    same input validate, with `None` preserved through serialisation and no fabricated `0`.
    """
    from app.schemas.social import AdCampaignResponse

    served = AdCampaignResponse.model_validate({
        "id": 1, "platform": "meta", "name": "legacy-null",
        "status": None, "daily_budget_usd": None, "total_spent_usd": None,
    })
    assert (served.status, served.daily_budget_usd, served.total_spent_usd) == (None, None, None)
    body = json.loads(served.model_dump_json())
    assert {k: body[k] for k in ("status", "daily_budget_usd", "total_spent_usd")} == {
        "status": None, "daily_budget_usd": None, "total_spent_usd": None,
    }, "the fields are served as null, never omitted and never coerced to 0"
