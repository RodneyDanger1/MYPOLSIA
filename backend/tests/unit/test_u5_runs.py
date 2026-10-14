"""Wave U5 lane P4 (B8) — GET /api/v1/runs + GET /api/v1/runs/export: the usage-ledger read surface.

The contract this file measures (stated identically in `app/api/v1/runs.py` + `run_service.py`):

    GET /api/v1/runs?task_id=&agent_type=&status=&from=&to=&limit=&offset=
      200 {"items": [<row>, ...], "total": N}  — total = the FULL filtered count, NOT the page size;
      <row> keys EXACTLY: id, task_id, task_title, agent_type, run_type, status, attempt, started_at,
      ended_at, duration_secs, tokens_input, tokens_output, tokens_used, cost_usd, cost_source,
      pricing_version.  Newest first (started_at DESC, id DESC tiebreak); NULL-started_at rows LAST.
      cost_source / pricing_version are read from the row's stored usage_json; unmeasured -> null.
    GET /api/v1/runs/export?format=csv|json  — the same filters, the FULL filtered set (no
      limit/offset): CSV = header row + one row per record; row count == the same filter's total
      (B8's declared-total rule) == the `X-Polsia-Row-Count` header; unknown format refuses by name
      (E_VALIDATION).  JSON = the same wrapper object.

What each block exists to catch:
  * auth — both routes carry the shipped `verify_api_key` dependency (401, E_AUTH envelope);
  * shape — the wrapper key set and the row key set are the FROZEN contract, pinned literally;
  * total — the wrapper's `total` is the full filtered count even when the page is smaller; offset
    beyond the end serves an empty page with the count intact; a task_id with no runs is an honest
    (0, []);
  * provenance — `task_title` comes from the JOIN (null for cycle runs), attribution strings come
    from the row's usage_json, and a malformed at-rest usage_json row STILL SERVES (junk reads as
    null — measured: the ORM JSON attribute raises while fetching);
  * the null-never-zero core — an unmeasured row serves nulls for tokens/cost/attribution, while a
    MEASURED zero is served as 0 in both directions;
  * the bound-instant rule — an AWARE `from=` window selects rows whose SQLite storage is NAIVE
    (`as_bound_instant`; without it the window silently selects zero rows);
  * export completeness — the CSV row count equals `/runs`'s total for the same filter and the
    declared `X-Polsia-Row-Count`; an empty set is a header-only CSV; an unknown format is 422
    E_VALIDATION; the undeclared `limit` param cannot truncate the export;
  * honest failure — a broken source answers the named E_DB refusal (503 + envelope +
    `X-Polsia-Error-Code`) on BOTH routes, never a 200 that reads like "no runs"; a declared-vs-read
    count mismatch refuses loudly with both numbers (B8's rule).

RED-leg evidence (each planted, observed, restored byte-exact; raw output in the lane receipt):
  * router auth dependency removed => test_both_routes_require_the_api_key fails (200);
  * `total` served as len(page) => test_list_total_is_the_full_filtered_count fails;
  * unmeasured attribution defaulted to ""/0 => test_unmeasured_stays_null_never_zero fails;
  * export capped at DEFAULT_LIMIT => test_export_row_count_equals_the_same_filters_total fails.
"""
from __future__ import annotations

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.models.task import AgentRun, Task
from app.services import run_service
from app.services.run_service import RunFilters

RUNS_PATH = "/api/v1/runs"
EXPORT_PATH = "/api/v1/runs/export"

#: The frozen row key set, pinned literally (independent of the model's own ordering).
ROW_KEYS = [
    "id", "task_id", "task_title", "agent_type", "run_type", "status", "attempt",
    "started_at", "ended_at", "duration_secs", "tokens_input", "tokens_output",
    "tokens_used", "cost_usd", "cost_source", "pricing_version",
]

#: The fixed anchor day; seeded instants are NAIVE on purpose (SQLite stores naive UTC text).
DAY = datetime(2026, 10, 14)


def _at(hour: int, minute: int = 0, second: int = 0) -> datetime:
    return DAY.replace(hour=hour, minute=minute, second=second)


async def _task(db, *, title: str = "Ledger task", agent_type: str = "content", status: str = "completed"):
    task = Task(title=title, agent_type=agent_type, status=status)
    db.add(task)
    await db.flush()
    return task


async def _run(
    db,
    *,
    agent_type: str = "content",
    status: str = "completed",
    run_type: str = "task",
    task_id: int | None = None,
    started_at: datetime | None = None,
    attempt: int | None = None,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    tokens_used: int | None = None,
    cost_usd: Decimal | None = None,
    usage_json: dict | None = None,
    ended_at: datetime | None = None,
    duration_secs: float | None = None,
) -> AgentRun:
    run = AgentRun(
        agent_type=agent_type, run_type=run_type, status=status, task_id=task_id,
        started_at=started_at, attempt=attempt, tokens_input=tokens_input,
        tokens_output=tokens_output, tokens_used=tokens_used, cost_usd=cost_usd,
        usage_json=usage_json, ended_at=ended_at, duration_secs=duration_secs,
    )
    db.add(run)
    await db.flush()
    return run


MEASURED_USAGE = {
    "usage": {"input_tokens": 120, "output_tokens": 30},
    "total_cost_usd": None,
    "token_cost_estimate_usd": 0.012345,
    "cost_source": "token_estimate",
    "pricing_version": "2026-10-01",
    "duration_ms": 5100,
}


# ── auth ──────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("path", [RUNS_PATH, EXPORT_PATH])
async def test_both_routes_require_the_api_key(api_client, path):
    """No key -> 401 E_AUTH envelope; a wrong key -> 401 (same as every sibling route)."""
    resp = await api_client.get(path)
    assert resp.status_code == 401, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_AUTH"

    wrong = await api_client.get(path, headers={"X-API-Key": "definitely-wrong"})
    assert wrong.status_code == 401


# ── service layer: filters / ordering / bounds ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_service_filters_select_the_same_rows_the_count_declares(async_db_session):
    db = async_db_session
    task_a = await _task(db, title="A")
    task_b = await _task(db, title="B", agent_type="finance")
    await _run(db, task_id=task_a.id, agent_type="content", status="completed", started_at=_at(9))
    await _run(db, task_id=task_a.id, agent_type="content", status="failed", started_at=_at(10))
    await _run(db, task_id=task_b.id, agent_type="finance", status="completed", started_at=_at(11))

    # each filter narrows; the count and the page ALWAYS come from the same builder
    rows, total = await run_service.list_runs(db, filters=RunFilters(), limit=50, offset=0)
    assert total == 3 and len(rows) == 3
    rows, total = await run_service.list_runs(db, filters=RunFilters(task_id=task_a.id), limit=50, offset=0)
    assert total == 2 and {r.task_id for r in rows} == {task_a.id}
    rows, total = await run_service.list_runs(db, filters=RunFilters(agent_type="finance"), limit=50, offset=0)
    assert total == 1 and rows[0].agent_type == "finance"
    rows, total = await run_service.list_runs(db, filters=RunFilters(status="failed"), limit=50, offset=0)
    assert total == 1 and rows[0].status == "failed"

    # page beats: total stays the FULL count while items is the page
    rows, total = await run_service.list_runs(db, filters=RunFilters(), limit=2, offset=0)
    assert total == 3 and len(rows) == 2
    rows, total = await run_service.list_runs(db, filters=RunFilters(), limit=2, offset=2)
    assert total == 3 and len(rows) == 1


@pytest.mark.asyncio
async def test_service_ordering_is_newest_first_then_id_desc(async_db_session):
    db = async_db_session
    oldest = await _run(db, started_at=_at(8))
    same_instant_first = await _run(db, started_at=_at(10))
    same_instant_second = await _run(db, started_at=_at(10))
    newest = await _run(db, started_at=_at(12))

    rows, _ = await run_service.list_runs(db, filters=RunFilters(), limit=50, offset=0)
    assert [r.id for r in rows] == [newest.id, same_instant_second.id, same_instant_first.id, oldest.id]


@pytest.mark.asyncio
async def test_service_null_started_at_sorts_last_and_out_of_windows(async_db_session):
    db = async_db_session
    dated = await _run(db, started_at=_at(10))
    undated = await _run(db, started_at=_at(11))
    await db.execute(text("UPDATE agent_runs SET started_at = NULL WHERE id = :i"), {"i": undated.id})

    rows, _ = await run_service.list_runs(db, filters=RunFilters(), limit=50, offset=0)
    assert [r.id for r in rows] == [dated.id, undated.id], "NULL-started_at rows sort LAST on every dialect"
    # A NULL instant cannot fall inside a from/to window (three-valued logic) — honestly excluded.
    rows, total = await run_service.list_runs(
        db, filters=RunFilters(from_instant=datetime(2026, 10, 14, 0, 0, tzinfo=UTC)), limit=50, offset=0
    )
    assert total == 1 and [r.id for r in rows] == [dated.id]


@pytest.mark.asyncio
async def test_service_bound_instant_makes_aware_bounds_select_naive_rows(async_db_session):
    """The `as_bound_instant` rule: an AWARE from/to must select rows stored as NAIVE UTC text."""
    db = async_db_session
    inside = await _run(db, started_at=_at(10))
    await _run(db, started_at=_at(9, 30))

    rows, total = await run_service.list_runs(
        db,
        filters=RunFilters(
            from_instant=datetime(2026, 10, 14, 10, 0, tzinfo=UTC),
            to_instant=datetime(2026, 10, 14, 10, 0, tzinfo=UTC),
        ),
        limit=50,
        offset=0,
    )
    # inclusive on BOTH ends: the row exactly AT the bound is selected; the 09:30 row is not.
    assert total == 1 and [r.id for r in rows] == [inside.id]


# ── service layer: null-never-zero + usage_json attribution ─────────────────────────────────

@pytest.mark.asyncio
async def test_service_unmeasured_stays_null_and_measured_zero_stays_zero(async_db_session):
    db = async_db_session
    unmeasured = await _run(db, started_at=_at(10))
    measured_zero = await _run(
        db, started_at=_at(11), tokens_input=0, tokens_output=0, tokens_used=0,
        cost_usd=Decimal("0.0000"),
        usage_json={"cost_source": None, "pricing_version": None,
                    "total_cost_usd": 0.0, "token_cost_estimate_usd": None},
    )

    rows, _ = await run_service.list_runs(db, filters=RunFilters(), limit=50, offset=0)
    by_id = {r.id: r for r in rows}

    none_row = by_id[unmeasured.id]
    assert none_row.tokens_input is None and none_row.tokens_output is None
    assert none_row.tokens_used is None and none_row.cost_usd is None
    assert none_row.cost_source is None and none_row.pricing_version is None, "unmeasured stays null, never ''"

    zero_row = by_id[measured_zero.id]
    assert zero_row.tokens_input == 0 and zero_row.tokens_output == 0 and zero_row.tokens_used == 0
    assert zero_row.cost_usd == Decimal("0.0000"), "a measured zero IS served as 0"
    assert zero_row.cost_source is None, "None inside usage_json stays null (never '' or 'None')"


@pytest.mark.asyncio
async def test_service_malformed_usage_json_row_still_serves(async_db_session):
    """One bad at-rest row must not refuse the feed (measured: the ORM attr raises while fetching)."""
    db = async_db_session
    good = await _run(db, started_at=_at(10), usage_json=MEASURED_USAGE)
    bad = await _run(db, started_at=_at(11))
    await db.execute(text("UPDATE agent_runs SET usage_json = :j WHERE id = :i"),
                     {"j": "not-json{", "i": bad.id})

    rows, total = await run_service.list_runs(db, filters=RunFilters(), limit=50, offset=0)
    assert total == 2
    by_id = {r.id: r for r in rows}
    assert by_id[good.id].cost_source == "token_estimate"
    assert by_id[good.id].pricing_version == "2026-10-01"
    assert by_id[bad.id].cost_source is None and by_id[bad.id].pricing_version is None


# ── service layer: export + B8's declared-total rule ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_service_export_serves_the_full_set_and_verifies_the_count(async_db_session):
    db = async_db_session
    for hour in (9, 10, 11):
        await _run(db, started_at=_at(hour))

    rows, total = await run_service.export_runs(db, filters=RunFilters())
    assert total == 3 and len(rows) == 3, "the export is the FULL filtered set"
    assert [r.started_at.hour for r in rows] == [11, 10, 9]


@pytest.mark.asyncio
async def test_service_export_refuses_loudly_on_count_mismatch(async_db_session, monkeypatch):
    db = async_db_session
    await _run(db, started_at=_at(10))

    async def _bogus_count(db_, *, filters):  # noqa: ANN001 - stands in for a racing writer's count
        return 99

    monkeypatch.setattr(run_service, "count_runs", _bogus_count)
    with pytest.raises(Exception) as excinfo:
        await run_service.export_runs(db, filters=RunFilters())
    err = excinfo.value
    assert getattr(err, "code", None) == "E_DB"
    assert "declared total=99" in str(err) and "1 row(s) read" in str(err)
    assert getattr(err, "context", {}) == {"declared_total": 99, "rows_read": 1}


# ── routes: the list endpoint ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_shape_total_and_row_keys_are_the_frozen_contract(api_client, auth_headers, async_db_session):
    db = async_db_session
    task = await _task(db, title="Publish the audit")
    for hour in (9, 10, 11, 12, 13):
        # distinct attempts: the partial unique `ux_agent_runs_task_attempt` keys (task_id, attempt)
        await _run(db, task_id=task.id, started_at=_at(hour), attempt=hour - 8, duration_secs=1.5,
                   usage_json=MEASURED_USAGE, tokens_input=120, tokens_output=30, tokens_used=150,
                   cost_usd=Decimal("0.0123"), ended_at=_at(hour, 1))

    resp = await api_client.get(RUNS_PATH, params={"limit": 2}, headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    body = resp.json()

    assert set(body) == {"items", "total"}, "the frozen wrapper object has exactly items+total"
    assert body["total"] == 5 and len(body["items"]) == 2, "total is the FULL count, not the page size"
    row = body["items"][0]
    assert list(row) == ROW_KEYS, "row keys are the frozen contract, in order"
    # newest first: the 13:00 row leads; the JOIN title rides along
    assert row["started_at"].startswith("2026-10-14T13:00:00")
    assert row["task_title"] == "Publish the audit" and row["task_id"] == task.id
    assert row["cost_usd"] == 0.0123
    assert row["cost_source"] == "token_estimate" and row["pricing_version"] == "2026-10-01"


@pytest.mark.asyncio
async def test_list_empty_ledger_and_unknown_task_are_honest_zeroes(api_client, auth_headers):
    body = (await api_client.get(RUNS_PATH, headers=auth_headers)).json()
    assert body == {"items": [], "total": 0}

    body = (await api_client.get(RUNS_PATH, params={"task_id": 99999}, headers=auth_headers)).json()
    assert body == {"items": [], "total": 0}, "a task_id with no runs is (0, []) — not an error"


@pytest.mark.asyncio
async def test_list_offset_beyond_total_serves_empty_page_with_count_intact(api_client, auth_headers,
                                                                            async_db_session):
    for hour in (9, 10):
        await _run(async_db_session, started_at=_at(hour))

    body = (await api_client.get(RUNS_PATH, params={"offset": 50}, headers=auth_headers)).json()
    assert body["items"] == [] and body["total"] == 2


@pytest.mark.asyncio
async def test_list_unmeasured_is_null_over_the_wire_measured_zero_is_zero(api_client, auth_headers,
                                                                          async_db_session):
    await _run(async_db_session, started_at=_at(10))  # nothing measured at all
    await _run(async_db_session, started_at=_at(11), tokens_input=0, tokens_output=0,
               tokens_used=0, cost_usd=Decimal("0.0000"))

    items = (await api_client.get(RUNS_PATH, headers=auth_headers)).json()["items"]
    zero, unmeasured = items[0], items[1]  # newest first: 11:00 zero, 10:00 unmeasured
    assert unmeasured["tokens_used"] is None and unmeasured["cost_usd"] is None
    assert unmeasured["cost_source"] is None and unmeasured["pricing_version"] is None
    assert zero["tokens_used"] == 0 and zero["cost_usd"] == 0.0
    assert zero["cost_source"] is None


@pytest.mark.asyncio
async def test_list_cycle_run_has_no_title_but_still_serves(api_client, auth_headers, async_db_session):
    await _run(async_db_session, run_type="cycle", agent_type="orchestrator", task_id=None,
               started_at=_at(10))

    items = (await api_client.get(RUNS_PATH, headers=auth_headers)).json()["items"]
    assert len(items) == 1
    assert items[0]["task_id"] is None and items[0]["task_title"] is None
    assert items[0]["run_type"] == "cycle"


@pytest.mark.asyncio
async def test_list_filters_over_the_wire_including_aware_window(api_client, auth_headers, async_db_session):
    db = async_db_session
    task = await _task(db, title="Windowed")
    await _run(db, task_id=task.id, agent_type="content", status="completed", started_at=_at(8))
    inside = await _run(db, task_id=task.id, agent_type="content", status="failed", started_at=_at(10))
    await _run(db, task_id=task.id, agent_type="finance", status="completed", started_at=_at(12))

    body = (await api_client.get(RUNS_PATH, params={"agent_type": "content"}, headers=auth_headers)).json()
    assert body["total"] == 2

    body = (await api_client.get(RUNS_PATH, params={"status": "failed"}, headers=auth_headers)).json()
    assert body["total"] == 1 and body["items"][0]["id"] == inside.id

    # AWARE window vs NAIVE SQLite storage — the as_bound_instant leg, over the wire.
    body = (await api_client.get(
        RUNS_PATH,
        params={"from": "2026-10-14T09:00:00Z", "to": "2026-10-14T11:00:00Z"},
        headers=auth_headers,
    )).json()
    assert body["total"] == 1 and body["items"][0]["id"] == inside.id


@pytest.mark.asyncio
async def test_list_db_outage_is_a_named_refusal_never_an_empty_page(api_client, auth_headers,
                                                                    async_db_session, monkeypatch):
    async def _explode(stmt):  # noqa: ANN001 - stands in for AsyncSession.execute during an outage
        raise SQLAlchemyError("simulated ledger outage")

    monkeypatch.setattr(async_db_session, "execute", _explode)

    resp = await api_client.get(RUNS_PATH, headers=auth_headers)
    assert resp.status_code == 503, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_DB"
    assert "items" not in body, "a failure must never look like an empty ledger"
    assert resp.headers.get("X-Polsia-Error-Code") == "E_DB"


# ── routes: the export endpoint ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_export_row_count_equals_the_same_filters_total(api_client, auth_headers, async_db_session):
    db = async_db_session
    task = await _task(db, title="Exported")
    for hour in (9, 10, 11):
        await _run(db, task_id=task.id, agent_type="content", started_at=_at(hour),
                   usage_json=MEASURED_USAGE, tokens_input=120, tokens_output=30, tokens_used=150,
                   cost_usd=Decimal("0.0123"))
    await _run(db, agent_type="finance", run_type="cycle", started_at=_at(12))  # outside the filter

    listed = (await api_client.get(RUNS_PATH, params={"agent_type": "content"}, headers=auth_headers)).json()
    assert listed["total"] == 3

    resp = await api_client.get(EXPORT_PATH, params={"agent_type": "content", "format": "csv"},
                                headers=auth_headers)
    assert resp.status_code == 200, resp.text[:300]
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.headers["x-polsia-row-count"] == "3", "the declared count rides the response headers"

    parsed = list(csv.reader(io.StringIO(resp.text)))
    assert parsed[0] == ROW_KEYS, "CSV header = the frozen field names, in order"
    data_rows = parsed[1:]
    assert len(data_rows) == listed["total"] == 3, "B8: export row count == the same filter's total"
    # newest-first, and the null discipline survives the CSV (null -> empty cell, never 0)
    assert data_rows[0][ROW_KEYS.index("started_at")].startswith("2026-10-14T11:00:00")
    assert data_rows[0][ROW_KEYS.index("cost_usd")] == "0.0123"
    assert data_rows[0][ROW_KEYS.index("task_title")] == "Exported"


@pytest.mark.asyncio
async def test_export_empty_set_is_header_only(api_client, auth_headers):
    resp = await api_client.get(EXPORT_PATH, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.headers["x-polsia-row-count"] == "0"
    lines = resp.text.strip().splitlines()
    assert lines == [",".join(ROW_KEYS)], "an empty export is a header-only CSV"
    assert list(csv.reader(io.StringIO(resp.text))) == [ROW_KEYS]


@pytest.mark.asyncio
async def test_export_json_is_the_wrapper_and_default_format_is_csv(api_client, auth_headers,
                                                                    async_db_session):
    await _run(async_db_session, started_at=_at(10), usage_json=MEASURED_USAGE,
               tokens_input=120, tokens_output=30, tokens_used=150, cost_usd=Decimal("0.0123"))

    # no format param -> CSV (the contract's first listed format)
    default_resp = await api_client.get(EXPORT_PATH, headers=auth_headers)
    assert default_resp.headers["content-type"].startswith("text/csv")
    assert default_resp.headers["x-polsia-row-count"] == "1"

    resp = await api_client.get(EXPORT_PATH, params={"format": "json"}, headers=auth_headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert set(body) == {"items", "total"} and body["total"] == len(body["items"]) == 1
    row = body["items"][0]
    assert list(row) == ROW_KEYS
    assert row["cost_source"] == "token_estimate" and row["pricing_version"] == "2026-10-01"
    assert row["tokens_used"] == 150


@pytest.mark.asyncio
async def test_export_unknown_format_refuses_by_name(api_client, auth_headers):
    resp = await api_client.get(EXPORT_PATH, params={"format": "xml"}, headers=auth_headers)
    assert resp.status_code == 422, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_VALIDATION"
    assert "xml" in body["error"]["message"]
    assert body["error"]["context"]["supported"] == ["csv", "json"]
    assert resp.headers.get("X-Polsia-Error-Code") == "E_VALIDATION"

    # format matching is case-insensitive (documented normalization) — CSV is not "unknown"
    ok = await api_client.get(EXPORT_PATH, params={"format": "CSV"}, headers=auth_headers)
    assert ok.status_code == 200 and ok.headers["content-type"].startswith("text/csv")


@pytest.mark.asyncio
async def test_export_cannot_be_truncated_by_a_limit_param(api_client, auth_headers, async_db_session):
    for hour in (9, 10, 11):
        await _run(async_db_session, started_at=_at(hour))

    resp = await api_client.get(EXPORT_PATH, params={"limit": 1}, headers=auth_headers)
    assert resp.headers["x-polsia-row-count"] == "3"
    assert len(list(csv.reader(io.StringIO(resp.text)))) - 1 == 3, "the export is never a page"


@pytest.mark.asyncio
async def test_export_db_outage_is_a_named_refusal_with_no_body(api_client, auth_headers,
                                                               async_db_session, monkeypatch):
    async def _explode(stmt):  # noqa: ANN001 - stands in for AsyncSession.execute during an outage
        raise SQLAlchemyError("simulated ledger outage")

    monkeypatch.setattr(async_db_session, "execute", _explode)

    resp = await api_client.get(EXPORT_PATH, headers=auth_headers)
    assert resp.status_code == 503, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_DB"
    assert "items" not in body and "id," not in resp.text, "nothing may be streamed on a refusal"
    assert resp.headers.get("X-Polsia-Error-Code") == "E_DB"


@pytest.mark.asyncio
async def test_export_count_mismatch_refuses_loudly(api_client, auth_headers, monkeypatch):
    """B8's declared-total rule over the wire: count != rows -> named refusal, never a short CSV."""
    async def _bogus_count(db_, *, filters):  # noqa: ANN001 - a racing writer, simulated
        return 7

    monkeypatch.setattr(run_service, "count_runs", _bogus_count)

    resp = await api_client.get(EXPORT_PATH, headers=auth_headers)
    assert resp.status_code == 503, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_DB"
    assert "declared total=7" in body["error"]["message"]
    assert body["error"]["context"]["rows_read"] == 0
    assert resp.headers.get("X-Polsia-Error-Code") == "E_DB"
