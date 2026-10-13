"""Test GET /api/v1/dashboard/* endpoints."""
import pytest

from app.models.company import CompanyConfig


@pytest.mark.asyncio
async def test_get_summary(api_client, auth_headers, async_db_session):
    company = CompanyConfig(name="Test Co", kpis={"mrr_usd": 5000}, timezone="UTC", daily_cycle_hour=6)
    async_db_session.add(company)
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "tasks_today_total" in data
    assert data["kpis"]["mrr_usd"] == 5000


@pytest.mark.asyncio
async def test_get_activity_empty(api_client, auth_headers):
    resp = await api_client.get("/api/v1/dashboard/activity", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


@pytest.mark.asyncio
async def test_get_activity_null_level_row_is_normalized(api_client, auth_headers, async_db_session):
    """A DDL-nullable `level` must not 500 the feed (A-T3 decision b).

    The ORM constructor fires `default="info"`, so NULL is only reachable by a Core/raw
    insert (measured by seat S1-t3) — which is exactly this insert.  The response boundary
    normalises None -> "info"; the endpoint must answer 200 with the documented value.
    """
    from sqlalchemy import insert

    from app.models.report import ActivityLog

    await async_db_session.execute(
        insert(ActivityLog).values(
            agent_type="social_media", action="probe", summary="null level", level=None
        )
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/dashboard/activity", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body) == 1
    assert body[0]["level"] == "info"


@pytest.mark.asyncio
async def test_get_daily_report_none(api_client, auth_headers):
    resp = await api_client.get("/api/v1/dashboard/reports/daily", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() is None


@pytest.mark.asyncio
async def test_health_endpoint(api_client):
    resp = await api_client.get("/api/v1/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
