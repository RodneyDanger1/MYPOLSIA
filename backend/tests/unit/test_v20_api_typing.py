"""V20/L1 — API typing + one contract per word (closes V19/L2 cards L2-06..L2-11).

WHY THIS FILE EXISTS
    V19/L2 measured four defects on the `/api/v1` surface and left them as cards:
      * L2-08 — `?status=<not-a-status>` on `/runs` and `/outreach/prospects` answered `200 []`
        while `/tasks`/`/invoices`/`/support/inbox` answered the named `E_VALIDATION` 422: the
        same word, two contracts, and an empty list that reads exactly like "no rows".
      * L2-09 — `dashboard.py:94` and `chat_service.py:456` raised a bare `RuntimeError` when a
        `SELECT count/aggregate` returned no row: an UNCODED 500 in a repo whose doctrine is
        that every refusal is named (the sibling `run_service.count_runs` refuses `E_DB`).
      * L2-06 — nine JSON routes declared `-> dict[str, Any]`, so OpenAPI published
        `additionalProperties: true` and the shape lived only in docstrings.
      * L2-07 / L2-11 — list routes that serve `limit` and no total (a full page is
        indistinguishable from a truncated one) and `pricing.count`, which is the PAGE length.
    Each pin below is written against the CONTRACT, not the implementation: it seeds real rows
    through the ORM/API, drives the real app over ASGI, and asserts on the wire bytes.

WIRE-COMPATIBILITY DOCTRINE (load-bearing)
    A typed response model must not change a single byte a current client reads.  Every
    `test_typed_*_wire_is_byte_equivalent...` pin therefore rebuilds the payload from the SAME
    service read the route uses and compares it to `response.text` after serializing it the way
    FastAPI's `dict[str, Any]` path did (`jsonable_encoder` + Starlette's JSONResponse render
    arguments).  `pricing` is the one route whose body GROWS (`total`), so it is pinned as
    "the old keys and their bytes are unchanged, the only delta is the added key".

TOTAL HEADER
    The list routes answer bare JSON arrays (the frontend parses them as arrays — `useRuns`,
    `ProspectRow`, the support page), so a body wrapper would be a breaking change.  The honest
    total therefore rides `X-Polsia-Total` — the full filtered match count, not the page length,
    the same "declared count" idea `X-Polsia-Row-Count` carries on `/runs/export`.
"""
from __future__ import annotations

import json
import types
import typing
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel

TOTAL_HEADER = "X-Polsia-Total"

#: A fixed instant: every seeded row is stamped with it so payloads are reproducible.
FIXED_NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
TODAY = date(2026, 10, 15)

#: Every `?status=`-filtered GET in `/api/v1`, with one value the route documents as legal.
#: `/runs/export` shares `runs.py::_filters_from` with the list, so it is the same contract.
STATUS_FILTERS = (
    ("/api/v1/tasks", "pending"),
    ("/api/v1/invoices", "draft"),
    ("/api/v1/support/inbox", "pending"),
    ("/api/v1/outreach/prospects", "new"),
    ("/api/v1/runs", "completed"),
    ("/api/v1/runs/export", "completed"),
    ("/api/v1/ads/campaigns", "active"),
    ("/api/v1/social/posts", "draft"),
)

#: The nine routes that declared `-> dict[str, Any]` at 8fa2b38 (V19 L2-06 table), keyed by the
#: response component FastAPI must publish for each.  `/` (main.py:480-489) is the card's tenth
#: entry and is NOT an `/api/v1` route (`include_in_schema=False`), so it is outside the lint.
TYPED_SURFACES = {
    "/api/v1/dashboard/reports": ({"items", "total"}, {"items", "total"}),
    "/api/v1/finance/planning": (
        {"burn_line_items", "capacity", "margin_template", "reasons", "pricing_guardrails",
         "sales_tax_note", "tax_disclaimer", "catalog_as_of", "staleness_days", "stale"},
        set(),
    ),
    "/api/v1/ops/events/stats": (
        {"failures", "skipped_while_open", "breaker_open", "cooldown_seconds", "last_error"},
        set(),
    ),
    "/api/v1/ops/events/reset": (
        {"failures", "skipped_while_open", "breaker_open", "cooldown_seconds", "last_error"},
        set(),
    ),
    "/api/v1/ops/sandbox": ({"sandbox_mode"}, set()),
    "/api/v1/ops/agent-quality": ({"agents"}, set()),
    "/api/v1/ops/capabilities": ({"agents", "registry_tools"}, set()),
    "/api/v1/pricing": (
        {"query", "count", "total", "items", "catalog_as_of", "staleness_days", "stale"},
        set(),
    ),
    "/api/v1/site/package": ({"package_dir", "files", "slug"}, set()),
}

#: Routes whose success body is NOT a JSON object/array model, each with its reason.  The lint
#: asserts this set is EXACT, so a new untyped route fails and a stale exemption fails too.
RESPONSE_MODEL_EXEMPTIONS = {
    # streams / archives / bytes / HTML: no JSON envelope exists to type.
    "/api/v1/attachments/{attachment_id}": "raw image bytes streamed with the sniffed content-type (V25/L1)",
    "/api/v1/prospects/{prospect_id}": "204 No Content on DELETE — no body to type (V27/L5)",
    "/api/v1/outreach/unsubscribe": "HTML confirmation page (a mail-client link)",
    "/api/v1/runs/export": "CSV/JSON stream; its count rides X-Polsia-Row-Count, verified pre-send",
    "/api/v1/data/export": "application/zip data-portability archive",
    "/api/v1/site/package/download": "application/zip site package",
    "/api/v1/phone/media/{part_id}": "raw attachment bytes (frozen V18 interface)",
    "/api/v1/phone/export": "application/zip phone export (frozen V16 interface)",
    # the phone JSON family is the FROZEN V16 `{ok, data}` envelope and `phone.py` belongs to
    # another V20 seat — named here instead of silently skipped.
    "/api/v1/phone/summary": "frozen V16 {ok,data} envelope (phone.py:17)",
    "/api/v1/phone/messages": "frozen V16 {ok,data} envelope (phone.py:17)",
    "/api/v1/phone/calls": "frozen V16 {ok,data} envelope (phone.py:17)",
    "/api/v1/phone/codes": "frozen V16 {ok,data} envelope (phone.py:17)",
    "/api/v1/phone/media": "frozen V16 {ok,data} envelope (phone.py:17)",
    "/api/v1/phone/send": "frozen V16 {ok,data} envelope (phone.py:17)",
}


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


def _api_routes():
    """Every published `/api/v1` APIRoute (the surface a client can reach)."""
    from fastapi.routing import APIRoute

    from app.main import app

    return [
        route
        for route in app.routes
        if isinstance(route, APIRoute) and route.path.startswith("/api/v1")
    ]


def _is_typed_response_model(model: object) -> bool:
    """True when `model` publishes a real component shape (a BaseModel, or a list/union of them).

    `dict[str, Any]` — the defect this lane closes — is a mapping generic, so it is NOT typed
    here even though FastAPI publishes a (useless) `additionalProperties: true` schema for it.
    """
    if model is None:
        return False
    origin = typing.get_origin(model)
    if origin is None:
        return isinstance(model, type) and issubclass(model, BaseModel)
    if origin in (list, set, tuple):
        args = [arg for arg in typing.get_args(model) if arg is not type(None)]
        return bool(args) and all(_is_typed_response_model(arg) for arg in args)
    if origin is typing.Union or origin is types.UnionType:
        args = [arg for arg in typing.get_args(model) if arg is not type(None)]
        return bool(args) and all(_is_typed_response_model(arg) for arg in args)
    return False


def _dict_path_body(payload: object) -> str:
    """The EXACT body FastAPI produced for a `dict[str, Any]` response model at 8fa2b38.

    `dict[str, Any]` went through `jsonable_encoder`, then Starlette's `JSONResponse.render`
    (`json.dumps(..., ensure_ascii=False, allow_nan=False, indent=None, separators=(",", ":"))`).
    """
    return json.dumps(jsonable_encoder(payload), ensure_ascii=False, allow_nan=False,
                      indent=None, separators=(",", ":"))


def _refusal(resp) -> dict:
    """The named-refusal envelope of a response, with its machine-readable code header."""
    body = resp.json()
    assert body.get("ok") is False, f"not a refusal envelope: {resp.text[:300]}"
    assert resp.headers.get("X-Polsia-Error-Code") == body["error"]["code"], (
        "the code header must agree with the envelope"
    )
    return body


async def _seed_company(db) -> None:
    """A company rich enough that the planning view is NOT the empty-database shape."""
    from app.models.company import CompanyConfig
    from app.models.finance import ExpenseRecord
    from app.models.task import AgentRun

    db.add(
        CompanyConfig(
            name="North Star SEO",
            description="Local SEO for small Minnesota businesses.",
            notification_email="owner@northstar.example",
            website_url="https://northstar.example",
            owner_hours_per_week=40,
            pricing_model="Fixed-fee productized tiers with a usage cap",
            confirmed_idea={
                "name": "SEO — small business",
                "price_band": [1500, 3000],
                "one_liner": "Full SEO program",
                "pricing_model": "fixed",
            },
        )
    )
    db.add(ExpenseRecord(category="software", vendor="Acme", amount_cents=12345, date=TODAY))
    db.add(ExpenseRecord(category="hosting", vendor="Acme", amount_cents=4000, date=TODAY))
    db.add(AgentRun(agent_type="social_media", status="completed", cost_usd=Decimal("1.5"),
                    tokens_input=100, tokens_output=50, attempt=1, run_type="task",
                    started_at=FIXED_NOW, ended_at=FIXED_NOW))
    db.add(AgentRun(agent_type="social_media", status="failed", cost_usd=None, attempt=1,
                    run_type="task", started_at=FIXED_NOW, ended_at=FIXED_NOW))
    await db.commit()


# ── item 1 · L2-08: an unknown `status=` is the SAME named 422 everywhere ────────────────────


@pytest.mark.parametrize(("path", "valid_value"), STATUS_FILTERS)
async def test_an_unknown_status_is_the_same_named_422_on_every_status_filter(
    api_client, auth_headers, path, valid_value
):
    """`?status=bogus` is caller input, not a filter that happens to match nothing.

    The reference contract (tasks/invoices/support, pinned since L1-09): 422 + the
    `E_VALIDATION` envelope + `X-Polsia-Error-Code`, the message naming the offending value and
    the legal set.  `runs`/`outreach` answered `200 []` at 8fa2b38 (V19 L2-08) — the same word
    with two contracts; the ads/social filters had the identical hole.
    """
    response = await api_client.get(path, params={"status": "bogus"}, headers=auth_headers)

    assert response.status_code == 422, (
        f"{path} answered {response.status_code} for an unknown status — a 200 [] is "
        f"indistinguishable from 'no rows' (V19 L2-08): {response.text[:300]}"
    )
    body = _refusal(response)
    assert body["error"]["code"] == "E_VALIDATION"
    message = body["error"]["message"]
    assert "bogus" in message, f"the refusal must name the offending value: {message}"
    assert valid_value in message, f"the refusal must name the legal set: {message}"
    context = body["error"].get("context") or {}
    # tasks/support/runs/outreach spell it status+valid; invoices' older validator spells the
    # same two facts value+legal.  Both name the cause and the legal set; neither is silent.
    assert context, f"{path} refused without a context: {body}"
    echoed = context.get("status", context.get("value"))
    assert echoed is not None and "bogus" in str(echoed), f"context must echo the value: {context}"


@pytest.mark.parametrize(("path", "valid_value"), STATUS_FILTERS)
async def test_a_known_status_still_filters_and_never_refuses(
    api_client, auth_headers, path, valid_value
):
    """No over-refusal: every legal value answers 2xx and still filters the rows."""
    response = await api_client.get(path, params={"status": valid_value}, headers=auth_headers)
    assert response.status_code in (200, 422), response.text[:300]
    if path.endswith("/export"):
        assert response.status_code == 200, response.text[:300]
        return
    assert response.status_code == 200, response.text[:300]


async def test_runs_status_filter_is_still_honoured_after_the_validation(
    api_client, auth_headers, async_db_session
):
    """The 422 work must not turn a VALID filter into an empty answer or a refusal."""
    from app.models.task import AgentRun

    async_db_session.add(AgentRun(agent_type="content", status="failed", run_type="task",
                                 started_at=FIXED_NOW))
    async_db_session.add(AgentRun(agent_type="content", status="completed", run_type="task",
                                 started_at=FIXED_NOW))
    await async_db_session.commit()

    body = (await api_client.get("/api/v1/runs", params={"status": "failed"},
                                 headers=auth_headers)).json()
    assert body["total"] == 1 and body["items"][0]["status"] == "failed"


async def test_outreach_status_filter_is_still_honoured_after_the_validation(
    api_client, auth_headers, async_db_session
):
    from app.models.outreach import Prospect

    async_db_session.add(Prospect(email="a@example.com", status="contacted", created_at=FIXED_NOW))
    async_db_session.add(Prospect(email="b@example.com", status="new", created_at=FIXED_NOW))
    await async_db_session.commit()

    rows = (await api_client.get("/api/v1/outreach/prospects", params={"status": "contacted"},
                                 headers=auth_headers)).json()
    assert [row["email"] for row in rows] == ["a@example.com"]


async def test_the_status_refusal_is_never_an_uncoded_framework_422(
    api_client, auth_headers
):
    """The framework's own `{"detail": [...]}` 422 is a DIFFERENT contract — this one is named."""
    response = await api_client.get("/api/v1/runs", params={"status": "下書き"},
                                    headers=auth_headers)
    assert response.status_code == 422
    assert response.json().get("ok") is False, response.text[:300]
    assert response.json()["error"]["code"] == "E_VALIDATION"
    assert "下書き" in response.json()["error"]["message"]


# ── item 2 · L2-09: an absent aggregate row is the named E_DB, never an uncoded 500 ──────────


async def test_dashboard_summary_without_an_aggregate_row_is_the_named_db_refusal(
    api_client, auth_headers, monkeypatch
):
    """`SELECT count/aggregate` always yields one row; no row is a DRIVER fault, not a 500.

    8fa2b38 raised `RuntimeError("dashboard summary statement returned no row")` — an uncoded
    500.  The sibling pattern (`run_service.count_runs`) refuses `E_DB` BY NAME.
    """
    import app.api.v1.dashboard as dashboard_module

    async def _no_row(*args, **kwargs):  # noqa: ANN002, ANN003 - stands in for the read
        return None

    monkeypatch.setattr(dashboard_module, "fetch_one", _no_row)

    response = await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)
    assert response.status_code == 503, response.text[:300]
    body = _refusal(response)
    assert body["error"]["code"] == "E_DB"
    assert "dashboard" in body["error"]["message"]


async def test_chat_dashboard_answer_without_an_aggregate_row_is_the_named_db_refusal(
    api_client, auth_headers, monkeypatch
):
    """The chat answer reads the SAME statement; its no-row branch was a bare RuntimeError."""
    import app.services.chat_service as chat_module

    async def _no_row(*args, **kwargs):  # noqa: ANN002, ANN003
        return None

    monkeypatch.setattr(chat_module, "fetch_one", _no_row)

    response = await api_client.post("/api/v1/chat", json={"message": "give me an overview"},
                                     headers=auth_headers)
    assert response.status_code == 503, response.text[:300]
    body = _refusal(response)
    assert body["error"]["code"] == "E_DB"


async def test_dashboard_reports_absent_count_row_is_named_db_not_a_fabricated_zero(
    api_client, auth_headers, monkeypatch
):
    """`total: 0` for a count that never returned is a fabricated measurement.

    The sibling defect of L2-09 in the same file: `int(total_row[0]) if total_row is not None
    else 0` turned a broken count into "no reports exist".  The seam is now the shared
    `totals.fetch_scalar` (the one place a `COUNT(*)` no-row can be observed).
    """
    import app.api.v1.totals as totals_module

    async def _no_row(*args, **kwargs):  # noqa: ANN002, ANN003
        return None

    monkeypatch.setattr(totals_module, "fetch_scalar", _no_row)

    response = await api_client.get("/api/v1/dashboard/reports", headers=auth_headers)
    assert response.status_code == 503, response.text[:300]
    assert _refusal(response)["error"]["code"] == "E_DB"


# ── item 3 · L2-06: every /api/v1 route declares a real response model ──────────────────────


def test_every_api_v1_route_declares_a_typed_response_model():
    """A `dict[str, Any]` return annotation publishes `additionalProperties: true` — no shape.

    The exemption set is asserted EXACTLY: a new untyped route fails the lint, and an exemption
    that no longer applies fails it too (no fossilised escape hatches).
    """
    untyped = sorted({route.path for route in _api_routes()
                      if not _is_typed_response_model(route.response_model)})
    assert untyped == sorted(RESPONSE_MODEL_EXEMPTIONS), (
        "untyped /api/v1 routes changed.\n"
        f"  unexpectedly untyped: {sorted(set(untyped) - set(RESPONSE_MODEL_EXEMPTIONS))}\n"
        f"  stale exemptions:     {sorted(set(RESPONSE_MODEL_EXEMPTIONS) - set(untyped))}\n"
        "Type the route with a real pydantic model, or add a NAMED reason to "
        "RESPONSE_MODEL_EXEMPTIONS in this file."
    )


def test_openapi_publishes_the_shape_of_every_typed_surface():
    """The point of the typing: the CONTRACT is visible to a client/OpenAPI consumer."""
    from app.main import app

    #: The two surfaces whose success response is a POST, not a GET.
    post_surfaces = {"/api/v1/site/package", "/api/v1/ops/events/reset"}
    schema = app.openapi()
    for path, (expected_properties, exact_properties) in TYPED_SURFACES.items():
        operation = schema["paths"][path]["post" if path in post_surfaces else "get"]
        published = operation["responses"]["200"]["content"]["application/json"]["schema"]
        ref = published.get("$ref")
        assert ref, f"{path} still publishes no component shape: {published}"
        name = ref.rsplit("/", 1)[-1]
        component = schema["components"]["schemas"][name]
        properties = set(component.get("properties", {}))
        assert expected_properties <= properties, (
            f"{path} -> {name} is missing {sorted(expected_properties - properties)}"
        )
        if exact_properties:
            assert properties == exact_properties, (
                f"{path} -> {name} carries undocumented extras: "
                f"{sorted(properties - exact_properties)}"
            )
        assert component.get("additionalProperties") is not True, (
            f"{path} -> {name} still allows arbitrary keys"
        )


async def test_openapi_documents_the_nested_shapes_a_client_reads():
    """Spot-check the nested pieces the owner-facing pages consume (agents/tools, planning)."""
    from app.main import app

    schema = app.openapi()
    components = schema["components"]["schemas"]

    capability = components["CapabilityManifestResponse"]
    agent_entry = components["AgentCapabilityEntry"]
    tool_entry = components["CapabilityTool"]
    assert set(capability["properties"]) == {"agents", "registry_tools"}
    assert set(agent_entry["properties"]) == {"agent_type", "tools", "action_classes", "channels"}
    assert set(tool_entry["properties"]) == {"name", "description"}

    planning = components["FinancePlanningResponse"]
    assert set(components["PlanningCapacityResponse"]["properties"]) == {
        "hours_per_client_lo", "hours_per_client_hi", "max_clients", "mrr_at_capacity",
        "break_even_clients", "hours_per_client_source",
    }
    assert set(components["PlanningMarginTemplateResponse"]["properties"]) == {
        "price", "ai_cost", "human_hours", "human_rate", "margin",
    }
    assert "reasons" in planning["properties"]

    assert "PricingCatalogResponse" in components
    assert set(components["PricingRow"]["properties"]) >= {
        "service", "price_low", "price_high", "model", "hours", "source_url", "fetch_type",
        "date", "source_label", "taxable_hint",
    }


# ── item 3 · wire-compatibility: the typed path is byte-identical to the dict path ──────────


async def test_typed_dashboard_reports_wire_is_byte_equivalent(api_client, auth_headers,
                                                               async_db_session):
    from sqlalchemy import func, select

    from app.models.report import DailyReport
    from app.schemas.report import DailyReportResponse
    from app.services.query_helpers import fetch_one, fetch_scalars

    async_db_session.add(DailyReport(report_date=date(2026, 10, 14), tasks_planned=2,
                                     tasks_completed=1, tasks_failed=0))
    await async_db_session.commit()

    response = await api_client.get("/api/v1/dashboard/reports?limit=5", headers=auth_headers)
    assert response.status_code == 200, response.text

    # EXACTLY the pre-change route body (8fa2b38 dashboard.py:164-181), same two reads.
    rows = await fetch_scalars(
        async_db_session,
        select(DailyReport).order_by(DailyReport.report_date.desc(), DailyReport.id.desc())
        .limit(5).offset(0),
        what="daily reports list",
    )
    total_row = await fetch_one(async_db_session, select(func.count()).select_from(DailyReport),
                               what="daily reports total")
    payload = {
        "items": [DailyReportResponse.model_validate(row).model_dump(mode="json") for row in rows],
        "total": int(total_row[0]),
    }
    assert response.text == _dict_path_body(payload)


async def test_typed_finance_planning_wire_is_byte_equivalent(api_client, auth_headers,
                                                              async_db_session):
    from app.api.v1.finance import build_planning_view

    await _seed_company(async_db_session)

    response = await api_client.get("/api/v1/finance/planning", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = await build_planning_view(async_db_session)
    assert response.text == _dict_path_body(payload)


async def test_typed_finance_planning_wire_is_byte_equivalent_on_an_empty_database(
    api_client, auth_headers, async_db_session
):
    """The empty-database shape (all nulls + reasons) must be byte-identical too."""
    from app.api.v1.finance import build_planning_view

    response = await api_client.get("/api/v1/finance/planning", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = await build_planning_view(async_db_session)
    assert response.text == _dict_path_body(payload)


@pytest.mark.parametrize("path", ["/api/v1/ops/events/stats", "/api/v1/ops/events/reset"])
async def test_typed_ops_breaker_wire_is_byte_equivalent(api_client, auth_headers, path):
    from app.core import events

    response = (await (api_client.get(path, headers=auth_headers)
                       if path.endswith("stats") else api_client.post(path, headers=auth_headers)))
    assert response.status_code == 200, response.text
    assert response.text == _dict_path_body(events.publish_stats())


async def test_typed_ops_sandbox_wire_is_byte_equivalent(api_client, auth_headers):
    from app.config import settings

    response = await api_client.get("/api/v1/ops/sandbox", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.text == _dict_path_body({"sandbox_mode": bool(settings.sandbox_mode)})


async def test_typed_ops_agent_quality_wire_is_byte_equivalent(api_client, auth_headers,
                                                               async_db_session):
    from app.api.v1.ops import _agent_quality_rows

    await _seed_company(async_db_session)  # gives the ledger a completed + a failed run

    response = await api_client.get("/api/v1/ops/agent-quality", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = {"agents": await _agent_quality_rows(async_db_session)}
    assert response.text == _dict_path_body(payload)


async def test_typed_ops_capabilities_wire_is_byte_equivalent(api_client, auth_headers):
    from app.services import capability_manifest

    response = await api_client.get("/api/v1/ops/capabilities", headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.text == _dict_path_body(capability_manifest.capability_manifest())


async def test_typed_pricing_wire_is_byte_equivalent_and_only_grows_a_total(
    api_client, auth_headers
):
    """`pricing` is the ONE typed route whose body grows (`total`): pin the exact delta."""
    from app.services import pricing_service

    response = await api_client.get("/api/v1/pricing?limit=3", headers=auth_headers)
    assert response.status_code == 200, response.text
    rows = pricing_service.search(None, limit=3)
    old_payload = {"query": None, "count": len(rows), "items": rows,
                   **pricing_service.catalog_provenance()}
    parsed = json.loads(response.text)

    # the only added key is `total`, and it is the FULL match count (50-row catalog), not 3
    assert set(parsed) - set(old_payload) == {"total"}
    assert parsed["total"] == 50 and parsed["count"] == 3
    # every pre-existing key keeps its order and its exact bytes
    assert [key for key in parsed if key != "total"] == list(old_payload)
    for key, value in old_payload.items():
        assert json.dumps(parsed[key], ensure_ascii=False, separators=(",", ":")) == \
            json.dumps(jsonable_encoder(value), ensure_ascii=False, separators=(",", ":")), (
                f"pricing.{key} changed shape on the wire"
            )


async def test_typed_pricing_query_wire_is_byte_equivalent(api_client, auth_headers):
    from app.services import pricing_service

    response = await api_client.get("/api/v1/pricing?q=local+seo&limit=2", headers=auth_headers)
    assert response.status_code == 200, response.text
    rows = pricing_service.search("local seo", limit=2)
    parsed = json.loads(response.text)
    assert parsed["query"] == "local seo" and parsed["count"] == 2
    assert parsed["items"] == jsonable_encoder(rows)
    assert parsed["total"] == len(pricing_service.search("local seo", limit=50))


async def test_typed_site_package_wire_is_byte_equivalent(api_client, auth_headers,
                                                          async_db_session, tmp_path,
                                                          monkeypatch):
    import app.services.site_package_service as site_service

    monkeypatch.setattr(site_service, "EXPORTS_ROOT", tmp_path)
    await _seed_company(async_db_session)

    response = await api_client.post("/api/v1/site/package", headers=auth_headers)
    assert response.status_code == 200, response.text
    payload = await site_service.build_site_package(async_db_session)
    assert response.text == _dict_path_body(payload)


# ── item 4 · L2-07: the honest total on every `limit`-only list ─────────────────────────────


async def _seed_lists(db) -> None:
    """One row per list surface, plus the counts each `total` must declare."""
    from app.models.ads import AdCampaign
    from app.models.invoice import Invoice, InvoiceStatus
    from app.models.memory import MemoryEntry
    from app.models.outreach import Prospect
    from app.models.report import ActivityLog
    from app.models.support import SupportInboxItem
    from app.models.task import Task

    for index in range(3):
        db.add(Task(title=f"Task {index}", agent_type="social_media",
                    status="pending" if index < 2 else "completed", created_at=FIXED_NOW))
    for number, status in (("INV-1", InvoiceStatus.DRAFT), ("INV-2", InvoiceStatus.SENT)):
        db.add(Invoice(number=number, issue_date=TODAY, due_date=TODAY, status=status,
                       bill_to={"name": "Client"}, subtotal=Decimal("10.00"),
                       tax=Decimal("0.00"), total=Decimal("10.00")))
    for index, status in enumerate(("new", "contacted", "contacted")):
        db.add(Prospect(email=f"p{index}@example.com", status=status, created_at=FIXED_NOW))
    for index, status in enumerate(("pending", "dispatched")):
        db.add(SupportInboxItem(provider_message_id=f"m{index}@example.com",
                                from_address="c@example.com", subject="s", body="b",
                                status=status, received_at=FIXED_NOW))
    for index, status in enumerate(("active", "paused")):
        db.add(AdCampaign(platform="meta", external_id=f"ext-{index}", name=f"Campaign {index}",
                          status=status, daily_budget_usd=Decimal("10.0"),
                          total_spent_usd=Decimal("2.5")))
    for index, category in enumerate(("business", "business", "other")):
        db.add(MemoryEntry(category=category, title=f"Note {index}", content="Body"))
    for index in range(3):
        db.add(ActivityLog(agent_type="social_media", action="task_completed",
                           summary=f"Activity {index}", level="info", created_at=FIXED_NOW))
    await db.commit()


#: (path, query params, expected FULL match count) — the seed above fixes every expectation.
TOTAL_CASES = (
    ("/api/v1/tasks", {"limit": 1}, 3),
    ("/api/v1/tasks", {"status": "pending", "limit": 1}, 2),
    ("/api/v1/invoices", {"limit": 1}, 2),
    ("/api/v1/invoices", {"status": "draft", "limit": 1}, 1),
    ("/api/v1/outreach/prospects", {"limit": 1}, 3),
    ("/api/v1/outreach/prospects", {"status": "contacted", "limit": 1}, 2),
    ("/api/v1/support/inbox", {"limit": 1}, 2),
    ("/api/v1/support/inbox", {"status": "pending", "limit": 1}, 1),
    ("/api/v1/ads/campaigns", {"limit": 1}, 2),
    ("/api/v1/ads/campaigns", {"status": "paused", "limit": 1}, 1),
    ("/api/v1/memory", {"limit": 1}, 3),
    ("/api/v1/memory", {"category": "business", "limit": 1}, 2),
    ("/api/v1/dashboard/activity", {"limit": 1}, 3),
)


@pytest.mark.parametrize(("path", "params", "expected_total"), TOTAL_CASES)
async def test_a_limit_only_list_declares_the_honest_total(api_client, auth_headers,
                                                           async_db_session, path, params,
                                                           expected_total):
    """A full page must be distinguishable from a truncated one.

    The body stays a bare array (a wrapper would break every existing client), so the declared
    total rides `X-Polsia-Total` — the FULL filtered match count, never the page length.
    """
    await _seed_lists(async_db_session)

    response = await api_client.get(path, params=params, headers=auth_headers)
    assert response.status_code == 200, response.text[:300]
    items = response.json()
    assert isinstance(items, list), "the list bodies must stay bare arrays (wire-compat)"
    assert len(items) == min(params["limit"], expected_total)
    declared = response.headers.get(TOTAL_HEADER)
    assert declared is not None, (
        f"{path} declares no total: a caller cannot tell a full page from a truncated one "
        f"(V19 L2-07). Expected {TOTAL_HEADER}."
    )
    assert int(declared) == expected_total
    assert int(declared) != len(items) or len(items) == expected_total, (
        "the header must not be the page length in a truncation case"
    )


async def test_a_list_total_is_zero_on_an_empty_table_not_absent(api_client, auth_headers):
    """An honest zero is a measurement; an ABSENT header is the defect."""
    response = await api_client.get("/api/v1/tasks?limit=10", headers=auth_headers)
    assert response.status_code == 200
    assert response.headers.get(TOTAL_HEADER) == "0"
    assert response.json() == []


async def test_a_list_total_equals_the_page_when_the_table_is_smaller_than_the_limit(
    api_client, auth_headers, async_db_session
):
    from app.models.task import Task

    async_db_session.add(Task(title="Only", agent_type="social_media", status="pending",
                              created_at=FIXED_NOW))
    await async_db_session.commit()

    response = await api_client.get("/api/v1/tasks?limit=500", headers=auth_headers)
    assert response.headers.get(TOTAL_HEADER) == "1"
    assert len(response.json()) == 1


async def test_pricing_declares_the_full_match_count_beside_the_page_count(
    api_client, auth_headers
):
    """L2-11: `count` is `len(rows)` AFTER the limit; `total` is the full match count."""
    from app.services import pricing_service

    browse = json.loads((await api_client.get("/api/v1/pricing?limit=3",
                                              headers=auth_headers)).text)
    assert browse["count"] == 3
    assert browse["total"] == 50, "the catalog holds exactly 50 rows (load_catalog refuses otherwise)"

    query = json.loads((await api_client.get("/api/v1/pricing?q=seo&limit=1",
                                             headers=auth_headers)).text)
    assert query["count"] == 1
    assert query["total"] == len(pricing_service.search("seo", limit=50))
    assert query["total"] > query["count"]


async def test_pricing_total_is_zero_for_a_query_that_matches_nothing(api_client, auth_headers):
    body = json.loads((await api_client.get("/api/v1/pricing?q=zzz-no-such-service",
                                            headers=auth_headers)).text)
    assert body["count"] == 0 and body["total"] == 0 and body["items"] == []


async def test_pricing_serves_the_one_free_form_price_claim_and_invents_none_elsewhere(
    api_client, auth_headers
):
    """The catalog carries `price_text` on exactly ONE row (49/50 have no such key).

    The typed row model declares the field optional and the route serializes with
    `exclude_unset`, so the claim survives on its own row and the other 49 do NOT grow a
    `"price_text": null` — a fabricated "unmeasured" value and a wire change.
    """
    from app.services import pricing_service

    claimed = [row for row in pricing_service.load_catalog() if row.get("price_text")]
    assert len(claimed) == 1

    items = json.loads((await api_client.get("/api/v1/pricing?limit=50",
                                             headers=auth_headers)).text)["items"]
    assert len(items) == 50
    carrying = [item for item in items if "price_text" in item]
    assert len(carrying) == 1, f"price_text leaked onto {len(carrying)} rows"
    assert carrying[0]["service"] == claimed[0]["service"]
    assert carrying[0]["price_text"] == claimed[0]["price_text"]


async def test_pricing_hour_estimates_keep_their_stored_numeric_spelling(
    api_client, auth_headers
):
    """Whole hours are INTEGERS in the dataset; the wire must not respell `25` as `25.0`."""
    from app.services import pricing_service

    stored = {row["service"]: row["hours"] for row in pricing_service.load_catalog()
              if isinstance(row.get("hours"), dict)}
    assert stored, "the frozen catalog is expected to carry hour estimates"

    items = json.loads((await api_client.get("/api/v1/pricing?limit=50",
                                             headers=auth_headers)).text)["items"]
    seen = 0
    for item in items:
        if item["service"] not in stored:
            continue
        seen += 1
        for edge in ("low", "high"):
            wire = item["hours"][edge]
            at_rest = stored[item["service"]][edge]
            assert type(wire) is type(at_rest), (
                f"{item['service']}.hours.{edge}: at rest {at_rest!r}, on the wire {wire!r}"
            )
    assert seen == len(stored)


# ── hostile pass ────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status_value", ["", " ", "PENDING", "pending ", "../etc/passwd",
                                          "x" * 300, "\x00", "😀"])
async def test_hostile_status_values_are_refused_by_name_never_500(
    api_client, auth_headers, status_value
):
    """A typo'd/case-shifted/huge value is a NAMED 422 — never a 500, never `200 []`."""
    response = await api_client.get("/api/v1/runs", params={"status": status_value},
                                    headers=auth_headers)
    assert response.status_code == 422, f"{status_value!r} -> {response.status_code}"
    assert _refusal(response)["error"]["code"] == "E_VALIDATION"


@pytest.mark.parametrize("path", sorted(TYPED_SURFACES))
async def test_typed_routes_refuse_absent_auth(api_client, path):
    """Typing must not have moved the router-level `verify_api_key` dependency."""
    if path in ("/api/v1/site/package", "/api/v1/ops/events/reset"):
        response = await api_client.post(path)
    else:
        response = await api_client.get(path)
    assert response.status_code == 401, response.text[:200]
    assert response.headers.get("X-Polsia-Error-Code") == "E_AUTH"


@pytest.mark.parametrize("query", ["%00", "😀", "x" * 2000, "  ", "SEO OR 1=1"])
async def test_hostile_pricing_queries_never_500(api_client, auth_headers, query):
    response = await api_client.get("/api/v1/pricing", params={"q": query, "limit": 2},
                                    headers=auth_headers)
    assert response.status_code == 200, response.text[:300]
    body = response.json()
    assert body["count"] == len(body["items"]) <= 2
    assert body["total"] >= body["count"]


async def test_huge_limit_is_clamped_by_the_framework_not_by_the_total(
    api_client, auth_headers, async_db_session
):
    """A limit past the documented ceiling is the framework's named 422 — unchanged by this lane."""
    response = await api_client.get("/api/v1/tasks?limit=100000", headers=auth_headers)
    assert response.status_code == 422
    assert "detail" in response.json(), "framework refusal shape (documented exemption)"


@pytest.mark.parametrize(
    "path",
    [
        # the count predicate this lane added (`ActivityLog.id < before`) binds an int too
        "/api/v1/dashboard/activity?limit=1&before=9223372036854775808",
        "/api/v1/dashboard/activity?limit=1&before=99999999999999999999",
        "/api/v1/tasks?limit=1&offset=9223372036854775808",
    ],
)
async def test_an_integer_past_the_storage_range_is_named_not_a_500(api_client, auth_headers, path):
    """Hostile pass: an int64+1 must be a NAMED refusal, never a driver 500."""
    response = await api_client.get(path, headers=auth_headers)
    assert response.status_code == 422, response.text[:300]
    assert _refusal(response)["error"]["code"] == "E_VALIDATION"
