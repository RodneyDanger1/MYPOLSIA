"""V19 / L2 — API solidity: refusal uniformity, totals, serialization, uncoded-500 edges, ads read.

Pins the six lenses of the V19 L2 brief against the SHIPPED app object over httpx's ASGI
transport (the repo's own `api_client`/`async_db_session` fixtures, `tests/conftest.py`).

RED-first history (base 257e20c): the timezone, invoice-JSON and UTC-offset pins below FAILED
before the fixes in this lane; the refusal/total pins passed at base and are the regression
fence for the envelope, header and declared-total contracts.
"""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from app.core.errors import E_AUTH, E_CONFIG, E_DB, E_NOT_FOUND, PolsiaError
from app.models.ads import AdCampaign, AdMetric
from app.models.chat import ChatMessage
from app.models.company import CompanyConfig
from app.models.invoice import Invoice, InvoiceStatus
from app.models.report import ActivityLog, DailyReport
from app.models.schedule import TaskSchedule
from app.models.task import AgentRun, Task

HEADERS = {"X-API-Key": "test-key"}
#: One instant, written aware, that SQLite hands back naive — the exact cross-dialect hazard.
STAMP = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
_ISO_DATETIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")


def _walk_instants(value, path="$"):
    """Every ISO-8601 DATE-TIME string in a JSON body, with its path (date-only strings skipped)."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _walk_instants(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from _walk_instants(item, f"{path}[{index}]")
    elif isinstance(value, str) and _ISO_DATETIME.match(value):
        yield path, value


def _assert_offset(value: str, path: str) -> None:
    assert value.endswith("Z") or re.search(r"[+-]\d{2}:\d{2}$", value), (
        f"{path} = {value!r} carries NO UTC offset: the same at-rest UTC instant is served "
        "naively on SQLite and offset-bearing on PostgreSQL — a client cannot know the zone"
    )


async def _seed_company(session, **values) -> CompanyConfig:
    row = CompanyConfig(name="Solidity Co", timezone="UTC", **values)
    session.add(row)
    await session.commit()
    return row


# ── lens 1: refusal uniformity (regression fence) ─────────────────────────────


async def test_every_protected_read_refuses_a_missing_key_with_envelope_and_header(api_client):
    """Every GET/DELETE route that is not public answers ONE shape: 401 E_AUTH + header."""
    from fastapi.routing import APIRoute

    from app.main import app as application

    public = {
        "/api/v1/health",
        "/api/v1/outreach/unsubscribe",
        "/api/v1/finance/stripe/webhook",
        "/",
    }
    probed = 0
    for route in application.routes:
        if not isinstance(route, APIRoute) or route.path in public:
            continue
        if not ({"GET", "DELETE"} & route.methods):
            continue
        if "{" in route.path:
            continue  # path-id refusals are exercised by the absent-row pin below
        response = await api_client.request(sorted({"GET", "DELETE"} & route.methods)[0], route.path)
        assert response.status_code == 401, f"{route.path} answered {response.status_code} without a key"
        body = response.json()
        assert body["ok"] is False and body["error"]["code"] == E_AUTH, route.path
        assert set(body["error"]) == {"code", "message", "how_to_fix", "context"}, route.path
        assert response.headers["x-polsia-error-code"] == E_AUTH, route.path
        assert response.headers["www-authenticate"] == "X-API-Key", route.path
        probed += 1
    assert probed >= 30, f"only {probed} protected routes probed — the inventory shrank?"


@pytest.mark.parametrize(
    ("method", "path", "payload", "status", "code"),
    [
        ("PATCH", "/api/v1/tasks/999999/status", {"status": "completed"}, 404, E_NOT_FOUND),
        ("POST", "/api/v1/auto-mode", {"duration_hours": 0}, 422, "E_VALIDATION"),
        ("DELETE", "/api/v1/auto-mode", None, 422, "E_VALIDATION"),
        ("POST", "/api/v1/tasks", {"title": "t", "agent_type": "not-an-agent"}, 422, "E_AGENT_UNKNOWN"),
    ],
)
async def test_write_refusals_use_the_same_envelope_and_code_header(
    api_client, method, path, payload, status, code
):
    """The refusal contract is route-shape independent: reads AND writes carry body + header."""
    kwargs = {} if payload is None else {"json": payload}
    response = await api_client.request(method, path, headers=HEADERS, **kwargs)
    assert response.status_code == status, (response.status_code, response.text[:250])
    body = response.json()
    assert body["ok"] is False and body["error"]["code"] == code
    assert set(body["error"]) == {"code", "message", "how_to_fix", "context"}
    assert response.headers["x-polsia-error-code"] == code


@pytest.mark.parametrize(
    ("method", "path", "code"),
    [
        ("GET", "/api/v1/tasks/999999", E_NOT_FOUND),
        ("GET", "/api/v1/invoices/999999", E_NOT_FOUND),
        ("GET", "/api/v1/ads/campaigns/999999/metrics", E_NOT_FOUND),
    ],
)
async def test_an_absent_row_is_the_named_envelope_with_its_code_header(api_client, method, path, code):
    """Absent rows name themselves — never a bare 404, never an empty list pretending to be one."""
    response = await api_client.request(method, path, headers=HEADERS)
    assert response.status_code == 404, (path, response.status_code, response.text[:200])
    body = response.json()
    assert body["ok"] is False and body["error"]["code"] == code
    assert body["error"]["context"], "a refusal must say WHAT was missing"
    assert response.headers["x-polsia-error-code"] == code


# ── lens 3: declared totals are the rows they count ───────────────────────────


async def test_declared_totals_are_the_filtered_count_not_the_page_size(api_client, async_db_session):
    """`total` must be the FULL filtered count; a page slice must not shrink it."""
    for index in range(7):
        async_db_session.add(
            Task(
                title=f"t{index}",
                agent_type="social" if index < 4 else "finance",
                status="completed",
                created_at=STAMP - timedelta(hours=index),
            )
        )
        async_db_session.add(
            AgentRun(
                task_id=None,
                agent_type="social" if index < 4 else "finance",
                run_type="task",
                status="completed",
                started_at=STAMP - timedelta(hours=index),
            )
        )
    for index in range(3):
        async_db_session.add(ChatMessage(role="owner", content=f"m{index}", created_at=STAMP))
    for index in range(2):
        async_db_session.add(DailyReport(report_date=date(2026, 5, 1) - timedelta(days=index)))
    await async_db_session.commit()

    runs = await api_client.get("/api/v1/runs", headers=HEADERS, params={"agent_type": "social", "limit": 2})
    assert runs.status_code == 200
    assert runs.json()["total"] == 4 and len(runs.json()["items"]) == 2  # 4 social runs, page of 2

    chat = await api_client.get("/api/v1/chat", headers=HEADERS, params={"limit": 2})
    assert chat.json()["total"] == 3 and len(chat.json()["items"]) == 2

    reports = await api_client.get("/api/v1/dashboard/reports", headers=HEADERS, params={"limit": 1})
    assert reports.json()["total"] == 2 and len(reports.json()["items"]) == 1


# ── lens 4: serialization stability ───────────────────────────────────────────


async def test_every_datetime_on_the_wire_is_utc_with_an_offset(api_client, async_db_session):
    """One instant, one spelling: every DATE-TIME on every read carries `Z` (or ±hh:mm)."""
    async_db_session.add(CompanyConfig(name="Solidity Co", timezone="UTC"))
    async_db_session.add(Task(title="stamped", agent_type="social", status="completed",
                              created_at=STAMP, updated_at=STAMP))
    async_db_session.add(AgentRun(task_id=None, agent_type="social", run_type="task", status="completed",
                                 started_at=STAMP, ended_at=STAMP, cost_usd=Decimal("0.1000")))
    async_db_session.add(ChatMessage(role="owner", content="hi", created_at=STAMP))
    async_db_session.add(DailyReport(report_date=date(2026, 5, 1), created_at=STAMP, updated_at=STAMP))
    async_db_session.add(TaskSchedule(name="s", prompt="p", cadence="daily", hour=6, enabled=True,
                                      next_run_at=STAMP, created_at=STAMP))
    async_db_session.add(ActivityLog(agent_type="company_planner", action="idea_proposed",
                                     summary="p", created_at=STAMP))
    await async_db_session.commit()
    async_db_session.expire_all()  # force the SQLite round-trip: naive is what the DB hands back

    for path in (
        "/api/v1/tasks",
        "/api/v1/runs",
        "/api/v1/chat",
        "/api/v1/dashboard/reports",
        "/api/v1/schedules",
        "/api/v1/planner/status",
    ):
        response = await api_client.get(path, headers=HEADERS)
        assert response.status_code == 200, (path, response.text[:300])
        instants = list(_walk_instants(response.json()))
        assert instants, f"{path} returned no datetime at all — the pin would be vacuous"
        for where, value in instants:
            _assert_offset(value, f"{path}:{where}")

    tasks = (await api_client.get("/api/v1/tasks", headers=HEADERS)).json()
    assert tasks[0]["created_at"] == "2026-05-01T12:00:00Z"


async def test_runs_csv_datetimes_match_the_list_wire(api_client, async_db_session):
    """The export promises `exactly the values served by the list` — including the offset."""
    async_db_session.add(AgentRun(task_id=None, agent_type="social", run_type="task", status="completed",
                                 started_at=STAMP, ended_at=STAMP + timedelta(minutes=2)))
    await async_db_session.commit()
    async_db_session.expire_all()

    listing = await api_client.get("/api/v1/runs", headers=HEADERS)
    served = listing.json()["items"][0]
    _assert_offset(served["started_at"], "list.started_at")

    export = await api_client.get("/api/v1/runs/export", headers=HEADERS, params={"format": "csv"})
    rows = list(csv.DictReader(io.StringIO(export.text)))
    assert rows and rows[0]["started_at"] == served["started_at"]
    assert rows[0]["ended_at"] == served["ended_at"]


async def test_ops_agent_quality_last_failure_is_utc_with_an_offset(api_client, async_db_session):
    """The one hand-rolled `.isoformat()` in the ops read must carry the offset too."""
    async_db_session.add(AgentRun(task_id=None, agent_type="social", run_type="task", status="failed",
                                 started_at=STAMP, ended_at=STAMP))
    await async_db_session.commit()
    async_db_session.expire_all()
    body = (await api_client.get("/api/v1/ops/agent-quality", headers=HEADERS)).json()
    entry = next(row for row in body["agents"] if row["agent_type"] == "social")
    _assert_offset(entry["last_failure_at"], "agents[social].last_failure_at")


# ── lens 5: uncoded 500s ──────────────────────────────────────────────────────


async def test_put_config_timezone_far_too_long_is_refused_not_an_uncoded_500(api_client, async_db_session):
    """`ZoneInfo` raises OSError (ENAMETOOLONG) for a long key — that is caller input, not a fault."""
    await _seed_company(async_db_session)
    response = await api_client.put("/api/v1/config", headers=HEADERS, json={"timezone": "x" * 300})
    assert response.status_code == 422, (response.status_code, response.headers.get("content-type"), response.text[:300])
    assert response.headers["content-type"].startswith("application/json")
    errors = response.json()["detail"]
    assert any(error["loc"] == ["body", "timezone"] for error in errors), errors


def test_company_tz_refuses_a_long_zone_name_by_name():
    """The runtime belt for a value that reached the row by another path: E_CONFIG, never OSError."""
    from app.core.timeutil import company_tz

    with pytest.raises(PolsiaError) as caught:
        company_tz("x" * 300)
    assert caught.value.code == E_CONFIG
    assert caught.value.context["value"] == "x" * 300


# ── lens 4b: stored non-finite JSON is corruption, never "unmeasured" ─────────


@pytest.mark.parametrize("field", ["bill_to", "line_items"])
async def test_invoice_create_refuses_non_finite_numbers_in_free_form_json(api_client, field):
    """`bill_to`/`line_items` are free-form JSON: a NaN there used to be stored and served as null."""
    payload = {field: {"x": float("nan")} if field == "bill_to" else [{"price": float("inf")}]}
    response = await api_client.post(
        "/api/v1/invoices", headers={**HEADERS, "content-type": "application/json"},
        content=json.dumps(payload),
    )
    assert response.status_code == 422, (response.status_code, response.text[:300])
    assert response.headers["content-type"].startswith("application/json")
    assert field in response.text


async def test_invoice_read_refuses_a_stored_non_finite_json_row(api_client, async_db_session):
    """A pre-fix / import row with NaN inside `bill_to` must refuse BY NAME (E_DB), never serve null."""
    async_db_session.add(
        Invoice(
            number="INV-POISON-1",
            bill_to={"x": float("nan")},
            issue_date=date(2026, 5, 1),
            due_date=date(2026, 5, 31),
            net_days=30,
            currency="USD",
            status=InvoiceStatus.DRAFT,
        )
    )
    await async_db_session.commit()
    response = await api_client.get("/api/v1/invoices", headers=HEADERS)
    assert response.status_code == 503, (response.status_code, response.text[:300])
    body = response.json()
    assert body["error"]["code"] == E_DB
    assert "bill_to" in body["error"]["message"]
    assert response.headers["x-polsia-error-code"] == E_DB


# ── item 6: the ads metrics read (DEBT decided by wiring) ─────────────────────


async def test_ads_campaign_metrics_serves_its_stored_rows_newest_first(api_client, async_db_session):
    """`list_metrics` now has a production consumer: the ads read surface exposes stored metrics."""
    campaign = AdCampaign(platform="meta", name="c1", status="active",
                          daily_budget_usd=Decimal("10.0000"), total_spent_usd=Decimal("12.3400"))
    async_db_session.add(campaign)
    await async_db_session.flush()
    for offset in range(3):
        async_db_session.add(
            AdMetric(campaign_id=campaign.id, date=date(2026, 5, 1) - timedelta(days=offset),
                     impressions=100 + offset, clicks=10, conversions=1,
                     spend_usd=Decimal("1.0000"), revenue_usd=Decimal("2.0000"))
        )
    await async_db_session.commit()

    response = await api_client.get(f"/api/v1/ads/campaigns/{campaign.id}/metrics", headers=HEADERS)
    assert response.status_code == 200, response.text[:300]
    rows = response.json()
    assert [row["date"] for row in rows] == ["2026-05-01", "2026-04-30", "2026-04-29"]
    assert rows[0]["spend_usd"] == 1.0 and rows[0]["revenue_usd"] == 2.0
    assert {"id", "campaign_id", "date", "impressions", "clicks", "conversions",
            "spend_usd", "revenue_usd", "ctr", "cpc", "roas"} <= set(rows[0])
