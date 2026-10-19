"""V24b — two truth-shape defects L6 measured but could not fix in its territory.

1. `GET /api/v1/dashboard/summary` answered a bare **500** when `company_config.kpis` held a
   JSON **list**.  `kpis` is `jsonb` on PostgreSQL, so a list is perfectly legal at rest; the
   response model (`DashboardSummaryResponse.kpis: dict`) rejects it inside the route and
   FastAPI turns the validation error into an uncoded 500.  Measured by V24/L6 in
   `wave-v24/l6-backend-hardening/pre/junk_probe_after.txt`.
2. `GET /api/v1/tasks` answered a **500** for the WHOLE page when one `tasks.metadata` cell held
   bytes that are not JSON.  On PostgreSQL `metadata` is `jsonb` (impossible at rest), but the
   SQLite deployment/test path stores it as TEXT and a raw/ops writer can leave junk — the same
   one-bad-row-kills-the-list defect V24/L6 fixed for `daily_reports` and `activity_log`.

Both fixes follow the codebase's standing rule: unreadable/mis-shaped data is "not measured"
(`{}` / `None` + a WARNING), never a fabricated value and never a 500 for the whole surface.
"""
from __future__ import annotations

from sqlalchemy import select, text

HEADERS = {"X-API-Key": "test-key"}


async def test_summary_reads_a_list_shaped_kpis_cell_as_absent(api_client, auth_headers, async_db_session):
    from app.models.company import CompanyConfig

    cfg = (await async_db_session.execute(select(CompanyConfig))).scalars().first()
    if cfg is None:
        cfg = CompanyConfig(name="Shape probe Co")
        async_db_session.add(cfg)
    cfg.kpis = ["not", "a", "mapping"]
    await async_db_session.commit()

    response = await api_client.get("/api/v1/dashboard/summary", headers=auth_headers)

    assert response.status_code == 200, response.text
    assert response.json()["kpis"] == {}


async def test_task_list_survives_one_malformed_metadata_cell(async_db_session):
    from app.models.task import Task
    from app.services import task_service

    task = Task(title="malformed metadata probe", agent_type="finance", status="pending", priority=2)
    async_db_session.add(task)
    await async_db_session.commit()

    # A raw/ops writer (not the ORM) leaves bytes that are not JSON in the TEXT column.
    await async_db_session.execute(
        text("UPDATE tasks SET metadata = :junk WHERE id = :id"),
        {"junk": "{this is not json", "id": task.id},
    )
    await async_db_session.commit()
    async_db_session.expire_all()

    rows = await task_service.list_tasks(async_db_session)

    probe = next(row for row in rows if row.id == task.id)
    assert probe.metadata_json is None, "junk metadata must read as 'not measured', never a parsed guess"
