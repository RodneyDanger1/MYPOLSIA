"""V29 follow-up B — `workspace_root` with a control character is the CALLER's error, not a 503.

MEASURED DEFECT
---------------
`POST /api/v1/ops/run-workspace-cleanup {"workspace_root": "C:\\\\x\\u0000y"}` passed the route's
own body validation (a non-blank STRING) and then failed inside the retention resolver:

    PolsiaError E_CONFIG: run workspace cleanup: the workspace root '...' cannot be resolved \
    (ValueError: stat: embedded null character in path)

`E_CONFIG` maps to **503** — "this service cannot take the work right now" — which blames the
server for a path no caller could ever mean.  The route's own contract says a bad body is the
named `E_VALIDATION` (422), and nothing may be recorded for a refused body.

This is the same caller-input class as the `/trust/link` control-character 500: a value that no
downstream syscall can represent must be refused at the boundary, BY NAME, before any work.
"""
from __future__ import annotations

from sqlalchemy import func, select

from app.core.errors import E_VALIDATION
from app.models.report import ActivityLog

ROUTE = "/api/v1/ops/run-workspace-cleanup"


async def test_a_control_character_in_workspace_root_is_e_validation(
    api_client, auth_headers, async_db_session
):
    """RED on the shipped route: it answers 503 `E_CONFIG`, not 422 `E_VALIDATION`."""
    from celery_app.tasks.maintenance import RUN_WORKSPACE_CLEANUP_ACTION

    bad_root = "C:" + chr(92) + "x" + chr(0) + "y"

    response = await api_client.post(
        ROUTE, json={"workspace_root": bad_root}, headers=auth_headers
    )

    assert response.status_code == 422, (
        "a workspace_root no path call can resolve is caller input; it must not be dressed up as "
        f"a config outage (got {response.status_code}: {response.text[:200]})"
    )
    body = response.json()
    assert body["error"]["code"] == E_VALIDATION
    assert body["error"]["context"]["field"] == "workspace_root"
    assert body["error"]["how_to_fix"]

    recorded = (
        await async_db_session.execute(
            select(func.count())
            .select_from(ActivityLog)
            .where(ActivityLog.action == RUN_WORKSPACE_CLEANUP_ACTION)
        )
    ).scalar_one()
    assert recorded == 0, "a refused body must not record a cleanup row"
