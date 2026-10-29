"""SV-W4-04 — the F-26 interruption markers reach `GET /api/v1/runs/{id}`.

Live defect (systems-validator wave 4): runs 311/314 were closed by the stale-run sweep with
`usage_json = {"cost_scope": "unmeasured", "interrupted": {"reason_code": "E_RUN_STALE", …}}`, but
the run read model had no such field, so `GET /runs/311` served `cost_usd: null` and looked
exactly like "not measured yet" — the marker the closer writes was invisible to the API and to any
UI reading it.

What this file pins (the detail-only wire contract, `app/api/v1/runs.py::RunDetail`):
  * `cost_scope` — `"unmeasured"` (cost unknown, stays NULL, never 0) or `"in_flight_partial"`
    (cost measured up to the kill — a LOWER BOUND, not the run's total);
  * `interrupted_reason` — the coded reason the closer stored (`"E_RUN_STALE"`);
  * both are `null` when the row carries no marker (a settled run, or a legacy row);
  * junk at rest reads as null and the row STILL SERVES (the per-row junk rule);
  * the list/export row is B8's frozen field set and does NOT grow these fields (`tools_used`
    precedent) — a reader must open the detail to tell interrupted from unmeasured.

RED leg: remove `cost_scope=...`/`interrupted_reason=...` from `_detail_model`
-> `test_a_cancelled_unmeasured_run_exposes_the_marker` fails on `KeyError`/missing key (raw output
in `taskmaster/raw/w4/RED-B.txt`).
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

from app.models.task import AgentRun

RUNS_PATH = "/api/v1/runs"
DETAIL_PATH = "/api/v1/runs/{run_id}"

#: The frozen ledger row — B8's list contract, pinned literally (same list as `test_u5_runs.py`).
ROW_KEYS = [
    "id", "task_id", "task_title", "agent_type", "run_type", "status", "attempt",
    "started_at", "ended_at", "duration_secs", "tokens_input", "tokens_output",
    "tokens_used", "cost_usd", "cost_source", "pricing_version",
]

#: The two new detail-only keys this change adds (the pin `test_v22_tools_persist.DETAIL_EXTRA_KEYS`
#: carries too — one convention, two files).
SV04_DETAIL_KEYS = {"cost_scope", "interrupted_reason"}

#: The real shape `maintenance._close_stale_runs_in_session` writes for a row with NO measurement
#: (the live 311/314 case, read back from PostgreSQL in wave 3).
UNMEASURED_USAGE = {
    "cost_scope": "unmeasured",
    "interrupted": {
        "at": "2026-10-29T00:51:57.328609+00:00",
        "cost_scope": "unmeasured",
        "reason_code": "E_RUN_STALE",
        "usage_source": "none",
        "bound_seconds": 7200,
        "provider_calls_persisted": None,
    },
}

#: The same marker for a row whose provider loop DID persist a partial measurement (`in_flight`).
PARTIAL_USAGE = {
    "cost_scope": "in_flight_partial",
    "interrupted": {
        "at": "2026-10-29T00:51:57.328609+00:00",
        "cost_scope": "in_flight_partial",
        "reason_code": "E_RUN_STALE",
        "usage_source": "in_flight",
        "bound_seconds": 7200,
        "provider_calls_persisted": 2,
    },
    "in_flight": {
        "provider_calls": 2,
        "cost_usd": 0.0009,
        "cost_basis": "upper_bound",
        "recorded_at": "2026-10-29T00:51:40.+00:00",
    },
}

#: A settled run's stored usage — no interruption marker at all.
SETTLED_USAGE = {
    "usage": {"input_tokens": 120, "output_tokens": 30},
    "cost_source": "token_estimate",
    "pricing_version": "2026-10-01",
    "num_turns": 2,
    "is_error": False,
}


async def _run(
    db,
    *,
    status: str = "cancelled",
    cost_usd: Decimal | None = None,
    usage_json: dict | None = None,
    started_at: datetime | None = datetime(2026, 10, 29, 0, 40),
) -> AgentRun:
    run = AgentRun(
        agent_type="social_media",
        run_type="task",
        status=status,
        cost_usd=cost_usd,
        usage_json=usage_json,
        started_at=started_at,
    )
    db.add(run)
    await db.flush()
    return run


@pytest.mark.asyncio
async def test_a_cancelled_unmeasured_run_exposes_the_marker(api_client, auth_headers, async_db_session):
    """The live 311/314 shape: cancelled, cost NULL, marker `unmeasured` + `E_RUN_STALE`."""
    run = await _run(async_db_session, status="cancelled", usage_json=UNMEASURED_USAGE)

    body = (await api_client.get(DETAIL_PATH.format(run_id=run.id), headers=auth_headers)).json()

    assert body["status"] == "cancelled"
    assert body["cost_usd"] is None, "an unmeasured cost is NULL, never 0"
    assert body["cost_scope"] == "unmeasured"
    assert body["interrupted_reason"] == "E_RUN_STALE"
    assert set(body) >= SV04_DETAIL_KEYS


@pytest.mark.asyncio
async def test_a_partially_measured_run_names_its_scope_and_keeps_the_cost(
    api_client, auth_headers, async_db_session
):
    """A killed run that HAD measured calls: the partial cost is served, labelled partial."""
    run = await _run(
        async_db_session,
        status="cancelled",
        cost_usd=Decimal("0.0009"),
        usage_json=PARTIAL_USAGE,
    )

    body = (await api_client.get(DETAIL_PATH.format(run_id=run.id), headers=auth_headers)).json()

    assert body["cost_scope"] == "in_flight_partial"
    assert body["interrupted_reason"] == "E_RUN_STALE"
    assert body["cost_usd"] == pytest.approx(0.0009), (
        "the money measured up to the kill must survive the close — it is a lower bound, not zero"
    )


@pytest.mark.asyncio
async def test_a_settled_run_carries_no_markers(api_client, auth_headers, async_db_session):
    """No marker -> both fields null.  Never a default `unmeasured` (that would be an invented fact)."""
    run = await _run(
        async_db_session,
        status="completed",
        cost_usd=Decimal("0.0123"),
        usage_json=SETTLED_USAGE,
    )

    body = (await api_client.get(DETAIL_PATH.format(run_id=run.id), headers=auth_headers)).json()

    assert body["cost_scope"] is None
    assert body["interrupted_reason"] is None
    assert body["cost_usd"] == pytest.approx(0.0123)


@pytest.mark.asyncio
async def test_a_legacy_row_with_no_usage_json_still_serves_with_nulls(
    api_client, auth_headers, async_db_session
):
    """The per-row junk rule: nothing to read -> null markers, and the row still serves."""
    run = await _run(async_db_session, status="completed", usage_json=None)

    response = await api_client.get(DETAIL_PATH.format(run_id=run.id), headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert body["cost_scope"] is None
    assert body["interrupted_reason"] is None


@pytest.mark.asyncio
async def test_junk_markers_read_as_null_not_as_a_fabricated_scope(
    api_client, auth_headers, async_db_session
):
    """A wrong-typed marker is not coerced: `cost_scope: 42` / a string `interrupted` read null."""
    run = await _run(
        async_db_session,
        status="cancelled",
        usage_json={"cost_scope": 42, "interrupted": "E_RUN_STALE"},
    )

    body = (await api_client.get(DETAIL_PATH.format(run_id=run.id), headers=auth_headers)).json()

    assert body["cost_scope"] is None
    assert body["interrupted_reason"] is None


@pytest.mark.asyncio
async def test_the_list_row_never_grows_the_markers(api_client, auth_headers, async_db_session):
    """B8's frozen list/export row is unchanged; the markers are DETAIL-ONLY (`tools_used` rule)."""
    run = await _run(async_db_session, status="cancelled", usage_json=UNMEASURED_USAGE)
    assert run.id is not None

    listing = (await api_client.get(RUNS_PATH, headers=auth_headers)).json()

    assert listing["total"] == 1
    item = listing["items"][0]
    assert list(item) == ROW_KEYS, "the ledger feed's frozen key set must not drift"
    assert not (SV04_DETAIL_KEYS & set(item))
