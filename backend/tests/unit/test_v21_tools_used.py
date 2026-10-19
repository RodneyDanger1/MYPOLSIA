"""V21 / L2 — run transparency: `GET /api/v1/runs/{id}` and the `tools_used` finding.

THE FINDING THIS FILE PINS (proven before any product code was written):

  `tools_used` is PRODUCED by the DeepSeek runtime — `deepseek_runtime.py:1225` writes
  `envelope["tools_used"]` for a tool-enabled run — but it is NEVER PERSISTED:

    * `agent_runs` (app/models/task.py:AgentRun) has no `tools_used` column (no migration adds one);
    * `accounting.RunUsage` (app/agents/accounting.py:37-66) has no such field and
      `usage_from_envelope` (158-205) never reads `env["tools_used"]`;
    * `RunUsage.as_dict` (79-118) writes usage/modelUsage/costs/num_turns/session/subtype only;
    * `base_agent.RUN_META_KEYS` (69-94) — the documented result->column keys — omits it;
    * the ONE writer, `agent_tasks._apply_run_usage` (136-195), records ONLY `result["usage_json"]`
      plus RUN_META_KEYS, so the envelope's tool list is dropped at that boundary.

  Therefore the detail route serves ONLY what the row truly stores: the frozen ledger fields plus
  `num_turns` and `is_error` from `usage_json`.  `tools_used` is deliberately ABSENT rather than
  served as `[]`: `[]` would read as the MEASURED ZERO "tools were available and none ran", which
  is a different fact from "never recorded".  These tests fail if the route invents a tool array.

Shape / behaviour pinned here:
  * auth — the shipped `verify_api_key` dependency (401, E_AUTH envelope), like every sibling;
  * stored counters — `num_turns` int and `is_error` bool from `usage_json`, exact values;
  * legacy / absent usage_json -> BOTH counters null (never 0, never false);
  * malformed at-rest usage_json -> nulls, the row STILL serves (the U5 per-row junk rule);
  * junk counter types ("4", "yes", True-as-bool) -> null, never coerced;
  * no run -> the named 404 `E_NOT_FOUND` envelope;
  * the list route's frozen row key set is UNCHANGED (the detail fields stay detail-only);
  * `/runs/export` still routes to the export (the detail route must not swallow it).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.models.task import AgentRun, Task

RUNS_PATH = "/api/v1/runs"
EXPORT_PATH = "/api/v1/runs/export"
DETAIL_PATH = "/api/v1/runs/{run_id}"

#: The frozen U5 list row key set — the detail route must NOT widen it.
ROW_KEYS = [
    "id", "task_id", "task_title", "agent_type", "run_type", "status", "attempt",
    "started_at", "ended_at", "duration_secs", "tokens_input", "tokens_output",
    "tokens_used", "cost_usd", "cost_source", "pricing_version",
]

#: The detail adds exactly the two counters the record truly stores.
DETAIL_EXTRA_KEYS = {"num_turns", "is_error"}

DAY = datetime(2026, 10, 14)


async def _task(db, *, title: str = "Detail task") -> Task:
    task = Task(title=title, agent_type="content", status="completed")
    db.add(task)
    await db.flush()
    return task


async def _run(
    db,
    *,
    task_id: int | None = None,
    usage_json: dict | None = None,
    status: str = "completed",
) -> AgentRun:
    run = AgentRun(
        agent_type="content",
        run_type="task",
        status=status,
        task_id=task_id,
        started_at=DAY.replace(hour=9),
        ended_at=DAY.replace(hour=9, minute=5),
        duration_secs=300.0,
        tokens_input=120,
        tokens_output=30,
        tokens_used=150,
        cost_usd=Decimal("0.0123"),
        usage_json=usage_json,
        attempt=1,
    )
    db.add(run)
    await db.flush()
    return run


# ── auth ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_detail_requires_the_api_key(api_client):
    """No key -> 401 E_AUTH envelope; a wrong key -> 401 (the sibling-route contract)."""
    resp = await api_client.get(DETAIL_PATH.format(run_id=1))
    assert resp.status_code == 401, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_AUTH"

    wrong = await api_client.get(
        DETAIL_PATH.format(run_id=1), headers={"X-API-Key": "definitely-wrong"}
    )
    assert wrong.status_code == 401


# ── stored counters ──────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_stored_run_serves_the_row_plus_its_stored_counters(api_client, auth_headers, async_db_session):
    """A row whose usage_json carries num_turns/is_error serves them EXACTLY as stored."""
    db = async_db_session
    task = await _task(db, title="Backfilled ledger task")
    run = await _run(
        db,
        task_id=task.id,
        usage_json={
            "usage": {"input_tokens": 120, "output_tokens": 30},
            "num_turns": 4,
            "is_error": True,
            "cost_source": "token_estimate",
            "pricing_version": "2026-10-01",
        },
    )

    resp = await api_client.get(DETAIL_PATH.format(run_id=run.id), headers=auth_headers)
    assert resp.status_code == 200, resp.text[:500]
    body = resp.json()

    assert body["id"] == run.id
    assert body["task_id"] == task.id
    assert body["task_title"] == "Backfilled ledger task"
    assert body["agent_type"] == "content"
    assert body["num_turns"] == 4
    assert body["is_error"] is True
    assert body["cost_source"] == "token_estimate"
    assert body["pricing_version"] == "2026-10-01"
    # The key set is the frozen row + the two stored counters — NOTHING invented.
    assert set(body) == set(ROW_KEYS) | DETAIL_EXTRA_KEYS


@pytest.mark.asyncio
async def test_tools_used_is_absent_because_the_record_never_stores_it(api_client, auth_headers, async_db_session):
    """The core honesty pin: no tool array is fabricated for a stored run.

    `[]` would be a MEASURED ZERO ("tools were on, none ran"); the record stores no tool evidence
    at all, so the field must not exist.  If a future writer persists it, this test is the flag to
    update the contract ON PURPOSE (not by accident).
    """
    db = async_db_session
    run = await _run(db, usage_json={"num_turns": 2, "is_error": False})

    resp = await api_client.get(DETAIL_PATH.format(run_id=run.id), headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "tools_used" not in body
    assert body["num_turns"] == 2 and body["is_error"] is False


# ── legacy / hostile rows ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_legacy_row_without_usage_json_serves_nulls_not_zeroes(api_client, auth_headers, async_db_session):
    """A pre-V12 row has no accounting envelope: both counters are null, never 0 / False."""
    db = async_db_session
    run = await _run(db, usage_json=None)

    resp = await api_client.get(DETAIL_PATH.format(run_id=run.id), headers=auth_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["num_turns"] is None
    assert body["is_error"] is None
    # The unmeasured ledger columns already follow the same null-not-zero rule.
    assert body["tokens_used"] == 150 and body["cost_usd"] == 0.0123


@pytest.mark.asyncio
async def test_malformed_usage_json_reads_as_null_and_still_serves(api_client, auth_headers, async_db_session):
    """One bad at-rest row must not refuse the detail (the U5 per-row junk rule)."""
    db = async_db_session
    run = await _run(db, usage_json={"num_turns": 3, "is_error": False})
    await db.execute(
        text("UPDATE agent_runs SET usage_json = '{not json' WHERE id = :i"), {"i": run.id}
    )
    await db.flush()

    resp = await api_client.get(DETAIL_PATH.format(run_id=run.id), headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()
    assert body["num_turns"] is None and body["is_error"] is None


@pytest.mark.asyncio
async def test_junk_counter_types_are_unmeasured_not_coerced(api_client, auth_headers, async_db_session):
    """A string / bool-as-count / string-flag is NOT a measurement — null, never coerced."""
    db = async_db_session
    stringly = await _run(db, usage_json={"num_turns": "4", "is_error": "yes"})
    bool_count = await _run(db, usage_json={"num_turns": True, "is_error": None})

    first = (await api_client.get(DETAIL_PATH.format(run_id=stringly.id), headers=auth_headers)).json()
    assert first["num_turns"] is None and first["is_error"] is None

    second = (await api_client.get(DETAIL_PATH.format(run_id=bool_count.id), headers=auth_headers)).json()
    assert second["num_turns"] is None, "bool is a flag, not a count (the _int_or_none rule)"
    assert second["is_error"] is None


# ── missing row ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_unknown_run_is_the_named_404_envelope(api_client, auth_headers):
    """A missing run refuses BY NAME, never an empty 200 body that reads like a run."""
    resp = await api_client.get(DETAIL_PATH.format(run_id=999999), headers=auth_headers)
    assert resp.status_code == 404, resp.text[:300]
    # The app-level coded-HTTPException handler emits the one refusal envelope (main.py).
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_NOT_FOUND"
    assert "999999" in body["error"]["message"]
    assert resp.headers["X-Polsia-Error-Code"] == "E_NOT_FOUND"


# ── the frozen list contract is untouched ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_row_keys_are_unchanged_by_the_detail_route(api_client, auth_headers, async_db_session):
    """The detail counters stay detail-only; the list row set remains the U5 frozen contract."""
    db = async_db_session
    await _run(db, usage_json={"num_turns": 4, "is_error": False})

    resp = await api_client.get(RUNS_PATH, headers=auth_headers)
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 1
    assert list(items[0]) == ROW_KEYS


@pytest.mark.asyncio
async def test_export_route_is_not_swallowed_by_the_detail_route(api_client, auth_headers, async_db_session):
    """`/runs/export` must still reach the export (the detail path param is numeric)."""
    db = async_db_session
    await _run(db, usage_json={"num_turns": 1, "is_error": False})

    resp = await api_client.get(EXPORT_PATH, params={"format": "json"}, headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["total"] == 1
