"""V8/C5 — GET /api/v1/dashboard/reports (documents v1 list: {items, total}).

Rows are the truth: no new table, no cached shape — `daily_reports` served newest-first with
the table's full count (the frontend documents overlay lands in V8b and consumes this).
"""
from __future__ import annotations

from datetime import date

from app.models.report import DailyReport


async def test_missing_api_key_is_refused(async_db_session, api_client):
    response = await api_client.get("/api/v1/dashboard/reports")
    assert response.status_code == 401


async def test_empty_table_returns_items_and_total(async_db_session, api_client, auth_headers):
    response = await api_client.get("/api/v1/dashboard/reports", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_real_rows_return_newest_first_with_the_full_total(
    async_db_session, api_client, auth_headers
):
    for day in ("2026-10-10", "2026-10-12", "2026-10-11"):
        async_db_session.add(
            DailyReport(report_date=date.fromisoformat(day), morning_plan=f"plan {day}")
        )
    await async_db_session.commit()

    response = await api_client.get("/api/v1/dashboard/reports", headers=auth_headers)
    body = response.json()

    assert body["total"] == 3
    assert [item["report_date"] for item in body["items"]] == [
        "2026-10-12",
        "2026-10-11",
        "2026-10-10",
    ]
    newest = body["items"][0]
    assert isinstance(newest["id"], int)
    assert newest["morning_plan"] == "plan 2026-10-12"


async def test_limit_and_offset_page_while_total_stays_the_table(
    async_db_session, api_client, auth_headers
):
    for day in ("2026-10-10", "2026-10-11", "2026-10-12"):
        async_db_session.add(DailyReport(report_date=date.fromisoformat(day)))
    await async_db_session.commit()

    response = await api_client.get(
        "/api/v1/dashboard/reports?limit=2&offset=1", headers=auth_headers
    )
    body = response.json()

    assert body["total"] == 3, "total is the table's truth, not the page's size"
    assert [item["report_date"] for item in body["items"]] == ["2026-10-11", "2026-10-10"]
