"""WAVE B1 / phase B / seat C1 — the three API-side P1s, pinned in BOTH directions.

F-17  `PATCH /api/v1/tasks/{task_id}/status` was a BLIND setter: it wrote any value over any
      state, so a `completed` row could be set back to `pending` and the ledger kept no record
      of who moved what.  The route must now go through the service's transition table (the
      same edges `claim_task` / `release_claim` implement), refuse an illegal cell BY NAME with
      the accepted set in the context, and write ONE activity row per accepted change.
      The matrix below is stated HERE independently of the service's table, so the two are a
      contract: `LEGAL_TRANSITIONS` is what this build promises, cell by cell (14 accepted,
      35 refused out of the 49 ordered pairs).

D6-R2  `agents.py` wrote `failed` when `.delay()` raised.  A publish refusal cannot prove that
      nothing was delivered (the A2-03 class: a connection that dies mid-publish leaves a live
      message), so terminalising the row both loses a delivered task and claims a fact nobody
      measured.  The row stays `pending` and carries `metadata.rearmed_at` (the marker
      `maintenance._reconcile_stranded_pending` selects on), and a second attempt can still
      claim it.

D4-R2/R3  `api/v1/finance.py` had two currency-blind / partial readers: `_burn_line_items`
      summed a EUR amount as USD (measured: EUR 500 -> `monthly_usd: 600.0`) and
      `_ai_spend_window_usd` served the PRICED PART of a mixed window as the 30-day spend
      (measured: 3.0 with one unpriced finished run beside it).  Both now use the shipped
      `reporting_currency_filter` / `SpendTruth` vocabulary and label what they excluded.

Every test here fails on the pre-fix tree for the reason it names (RED legs recorded in
`wave-b1/raw/C1/RED.txt`), and each positive case is paired with its negative so neither an
over-refuser nor a blind writer can pass.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.database import utcnow
from app.core.errors import (
    E_NOT_FOUND,
    E_REDIS,
    E_TASK_NOT_RUNNABLE,
    E_VALIDATION,
)
from app.core.timeutil import today_in
from app.models.company import CompanyConfig
from app.models.finance import ExpenseRecord
from app.models.report import ActivityLog
from app.models.task import AgentRun, Task, TaskStatus

TASKS = "/api/v1/tasks"

STATUS_VALUES: tuple[str, ...] = tuple(member.value for member in TaskStatus)

#: The cells this build ACCEPTS — one line per edge, naming the writer that edge exists for.
#: Deliberately written out here (not derived from the service's table): a change to either side
#: must be a deliberate, reviewed change to both.
LEGAL_TRANSITIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("pending", "in_progress"),           # claim_task's own edge (the queue's one claim)
        ("pending", "completed"),             # operator completes a queued row (T12/U2 pins)
        ("pending", "failed"),                # operator records a failure (V12 redteam control)
        ("pending", "cancelled"),             # owner cancels a queued row
        ("pending", "rejected"),              # refused BEFORE execution (budget/policy)
        ("in_progress", "pending"),           # release_claim / a retryable failure's requeue
        ("in_progress", "completed"),         # the worker's outcome
        ("in_progress", "failed"),            # the worker's outcome
        ("in_progress", "cancelled"),         # owner cancels a run mid-flight
        ("in_progress", "awaiting_approval"),  # the worker parks the run for approval
        ("in_progress", "rejected"),          # the budget gate's refusal (agent_tasks:826)
        ("failed", "pending"),                # re-arm a failed row for a retry (V12 control)
        ("awaiting_approval", "pending"),     # attach_approval_token re-queues after approval
        ("awaiting_approval", "cancelled"),   # task_repeat: "approve it … or cancel"
    }
)

ALL_CELLS: list[tuple[str, str]] = [(from_, to) for from_ in STATUS_VALUES for to in STATUS_VALUES]
LEGAL_CELLS: list[tuple[str, str]] = sorted(LEGAL_TRANSITIONS)
ILLEGAL_CELLS: list[tuple[str, str]] = [cell for cell in ALL_CELLS if cell not in LEGAL_TRANSITIONS]

assert len(LEGAL_CELLS) + len(ILLEGAL_CELLS) == len(STATUS_VALUES) ** 2


# ── helpers ──────────────────────────────────────────────────────────────────────────────────


async def _seed_task(db, status: str, title: str = "b1 c1 task") -> Task:
    task = Task(title=title, agent_type="finance", source="manual", status=status)
    db.add(task)
    await db.commit()
    return task


async def _reload_task(db, task_id: int) -> Task:
    """Re-read the row through the SAME session the request used, refreshing the identity map.

    `populate_existing=True` overwrites the already-loaded copy from the query's rows; `expire_all`
    is NOT used because an expired attribute read outside a greenlet is a `MissingGreenlet` in
    aiosqlite (the ORM would try to lazily load synchronously).
    """
    return (
        await db.execute(
            select(Task).where(Task.id == task_id).execution_options(populate_existing=True)
        )
    ).scalars().one()


async def _activity_rows(db) -> list[ActivityLog]:
    return list(
        (
            await db.execute(
                select(ActivityLog)
                .order_by(ActivityLog.id)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


def _accepted_for(from_status: str) -> list[str]:
    return sorted(to for from_, to in LEGAL_TRANSITIONS if from_ == from_status)


# ── F-17: one test per LEGAL cell (accepted + audited) ──────────────────────────────────────


@pytest.mark.parametrize(("from_status", "to_status"), LEGAL_CELLS)
@pytest.mark.asyncio
async def test_each_legal_cell_is_accepted_and_leaves_one_audit_row(
    api_client, auth_headers, async_db_session, from_status, to_status
):
    """A legal cell writes the row AND exactly one activity row naming the transition."""
    task = await _seed_task(async_db_session, from_status, title=f"{from_status}->{to_status}")

    resp = await api_client.patch(
        f"{TASKS}/{task.id}/status",
        headers=auth_headers,
        json={"status": to_status, "result_summary": "moved by the owner"},
    )
    assert resp.status_code == 200, f"{from_status}->{to_status} must be accepted: {resp.text}"
    assert resp.json()["status"] == to_status

    row = await _reload_task(async_db_session, task.id)
    assert row.status == to_status, "the accepted transition must have been written"
    assert row.result_summary == "moved by the owner"

    entries = await _activity_rows(async_db_session)
    assert len(entries) == 1, f"one accepted change, one audit row; got {[e.action for e in entries]}"
    entry = entries[0]
    assert entry.action == "task_status_changed"
    assert entry.detail["task_id"] == task.id
    assert (entry.detail["from"], entry.detail["to"]) == (from_status, to_status)
    assert from_status in entry.summary and to_status in entry.summary


# ── F-17: one test per ILLEGAL cell (refused, nothing written, nothing audited) ─────────────


@pytest.mark.parametrize(("from_status", "to_status"), ILLEGAL_CELLS)
@pytest.mark.asyncio
async def test_each_illegal_cell_is_refused_with_the_accepted_set_and_writes_nothing(
    api_client, auth_headers, async_db_session, from_status, to_status
):
    """An illegal cell is a 409 naming the code, the from/to pair and the accepted targets."""
    task = await _seed_task(async_db_session, from_status, title=f"refuse {from_status}->{to_status}")
    before = await _reload_task(async_db_session, task.id)
    stamp_before, summary_before = before.updated_at, before.result_summary

    resp = await api_client.patch(
        f"{TASKS}/{task.id}/status",
        headers=auth_headers,
        json={"status": to_status, "result_summary": "must not land"},
    )
    assert resp.status_code == 409, (
        f"{from_status}->{to_status} must be refused, not written: {resp.text}"
    )
    assert resp.headers.get("X-Polsia-Error-Code") == E_TASK_NOT_RUNNABLE
    error = resp.json()["error"]
    assert error["code"] == E_TASK_NOT_RUNNABLE
    assert error["how_to_fix"]
    assert error["context"]["task_id"] == task.id
    assert error["context"]["from"] == from_status
    assert error["context"]["to"] == to_status
    assert error["context"]["accepted"] == _accepted_for(from_status)

    row = await _reload_task(async_db_session, task.id)
    assert row.status == from_status, "a refused transition must leave the row byte-identical"
    assert row.updated_at == stamp_before
    assert row.result_summary == summary_before
    assert await _activity_rows(async_db_session) == [], "a refusal is not an accepted change"


@pytest.mark.asyncio
async def test_a_completed_row_cannot_be_set_back_to_pending(api_client, auth_headers, async_db_session):
    """The measured F-17 defect, named: `completed` -> `pending` used to answer 200."""
    task = await _seed_task(async_db_session, "completed", title="the register's probe")

    resp = await api_client.patch(
        f"{TASKS}/{task.id}/status", headers=auth_headers, json={"status": "pending"}
    )

    assert resp.status_code == 409, f"completed->pending is the defect; got {resp.status_code}"
    assert resp.json()["error"]["code"] == E_TASK_NOT_RUNNABLE
    assert resp.json()["error"]["context"]["accepted"] == []
    row = await _reload_task(async_db_session, task.id)
    assert row.status == "completed"


@pytest.mark.asyncio
async def test_the_transition_table_is_the_one_the_claim_and_release_edges_live_in(
    async_db_session,
):
    """ONE table: the API's write authority and the two atomic queue verbs agree by test."""
    from app.services.task_service import TASK_TRANSITIONS, legal_task_status_targets

    table = {
        (str(from_status), str(to_status))
        for from_status, targets in TASK_TRANSITIONS.items()
        for to_status in targets
    }
    assert table == set(LEGAL_TRANSITIONS), (
        "the service's matrix and this file's promise must be the same 14 edges"
    )
    # The two edges claim_task / release_claim implement atomically are cells of this table.
    assert ("pending", "in_progress") in table
    assert ("in_progress", "pending") in table
    # ... and the terminal states are terminal for the API writer too.
    for terminal in ("completed", "cancelled", "rejected"):
        assert legal_task_status_targets(terminal) == (), f"{terminal} must be terminal"
    assert "in_progress" in legal_task_status_targets("pending")


@pytest.mark.asyncio
async def test_an_unknown_status_value_is_still_the_value_set_refusal(
    api_client, auth_headers, async_db_session
):
    """The shipped T12/redteam pin, kept: a typo is E_VALIDATION with the value set, not a 409."""
    task = await _seed_task(async_db_session, "pending")

    resp = await api_client.patch(
        f"{TASKS}/{task.id}/status", headers=auth_headers, json={"status": "definitely_not_a_status"}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == E_VALIDATION
    assert set(resp.json()["error"]["context"]["valid"]) == set(STATUS_VALUES)
    assert (await _reload_task(async_db_session, task.id)).status == "pending"


@pytest.mark.asyncio
async def test_a_row_with_no_status_is_refused_by_name_never_crashed(
    api_client, auth_headers, async_db_session
):
    """A NULL `tasks.status` (a raw/ops writer: the column is nullable) has no matrix row.

    The refusal must be the named 409 with an EMPTY accepted set — not a 500 from
    `TaskStatus("")` — and the row must be left exactly as it was.
    """
    from sqlalchemy import text

    # Inserted raw, with status NULL: the ORM's `default="pending"` would fill the value, and a
    # fresh row is also what the route would read in production (its own session, own identity map).
    await async_db_session.execute(
        text(
            "INSERT INTO tasks (title, agent_type, source, status) "
            "VALUES ('null status row', 'finance', 'manual', NULL)"
        )
    )
    await async_db_session.commit()
    task_id = (
        await async_db_session.execute(
            text("SELECT id FROM tasks WHERE title = 'null status row'")
        )
    ).scalar_one()

    resp = await api_client.patch(
        f"{TASKS}/{task_id}/status", headers=auth_headers, json={"status": "completed"}
    )

    assert resp.status_code == 409, resp.text
    error = resp.json()["error"]
    assert error["code"] == E_TASK_NOT_RUNNABLE
    assert error["context"]["from"] == ""
    assert error["context"]["accepted"] == []
    stored = (
        await async_db_session.execute(
            text("SELECT status FROM tasks WHERE id = :id"), {"id": task_id}
        )
    ).scalar_one()
    assert stored is None, "a refused transition publishes nothing, not even a default"


@pytest.mark.asyncio
async def test_a_missing_task_is_still_the_named_404(api_client, auth_headers):
    resp = await api_client.patch(
        f"{TASKS}/987654/status", headers=auth_headers, json={"status": "cancelled"}
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == E_NOT_FOUND


# ── D6-R2: a refused `.delay()` RE-ARMS the row ─────────────────────────────────────────────


def _dead_delay(*_args, **_kwargs):
    raise ConnectionError("Error 61 connecting to localhost:6379. Connection refused.")


async def _trigger_with_a_dead_broker(api_client, auth_headers, monkeypatch, title: str):
    import celery_app.tasks.agent_tasks as agent_tasks

    monkeypatch.setattr(agent_tasks.run_agent_task, "delay", _dead_delay, raising=False)
    return await api_client.post(
        "/api/v1/agents/social_media/trigger", headers=auth_headers, json={"task_title": title}
    )


@pytest.mark.asyncio
async def test_a_refused_delay_re_arms_the_row_instead_of_writing_failed(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """The row stays `pending` + `metadata.rearmed_at`; it is never terminalised on a guess."""
    resp = await _trigger_with_a_dead_broker(
        api_client, auth_headers, monkeypatch, "C1 re-arm probe"
    )

    assert resp.status_code == 503, "a broker outage is still a loud, named refusal"
    error = resp.json()["error"]
    assert error["code"] == E_REDIS
    assert error["how_to_fix"]
    task_id = error["context"]["task_id"]
    # The response says the honest thing: accepted (the row exists, still queued), not dispatched.
    assert error["context"]["dispatched"] is False
    assert error["context"]["status"] == "pending"
    assert "not dispatched" in error["message"].lower()
    assert "re-dispatched" in error["message"] or "re-dispatch" in error["message"].lower()

    row = await _reload_task(async_db_session, task_id)
    assert row.status == "pending", "a publish refusal cannot prove nothing was delivered"
    assert "E_REDIS" in (row.error_message or ""), (
        "the row keeps the refusal's name as the diagnostic, without a terminal status"
    )
    metadata = row.metadata_json or {}
    assert "rearmed_at" in metadata, "the reconciler's marker is what makes the retry bounded"
    assert metadata["rearm_reason"] == "enqueue_refused:ConnectionError"
    # It parses as the instant the reconciler compares against its window.
    assert datetime.fromisoformat(metadata["rearmed_at"]) is not None


@pytest.mark.asyncio
async def test_the_rearmed_row_can_still_be_run_by_a_second_attempt(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """The marker's whole point: the row is still claimable, so a later delivery can run it."""
    from app.services.task_service import claim_task

    resp = await _trigger_with_a_dead_broker(
        api_client, auth_headers, monkeypatch, "C1 second attempt"
    )
    task_id = resp.json()["error"]["context"]["task_id"]

    row = await _reload_task(async_db_session, task_id)
    # The three facts `_reconcile_stranded_pending` selects on (maintenance.py:358-374).
    assert row.status == "pending"
    assert row.claimed_at is None
    assert "rearmed_at" in (row.metadata_json or {})
    # ... and the worker's own claim (the only path a delivery takes) still accepts it.
    claimed = await claim_task(async_db_session, worker="second-attempt", task_id=row.id)
    assert claimed is not None
    assert claimed.status == "in_progress"
    assert claimed.attempts == 1


# ── D4-R2: the burn breakdown is USD-only, and the exclusion is labelled ────────────────────


async def _expense(db, category: str, cents: int, currency: str | None) -> ExpenseRecord:
    row = ExpenseRecord(
        category=category,
        vendor="probe",
        amount_cents=cents,
        currency=currency,
        date=today_in(),
    )
    db.add(row)
    await db.commit()
    return row


@pytest.mark.asyncio
async def test_burn_line_items_sums_only_usd_and_labels_the_excluded_rows(async_db_session):
    """Mixed USD+EUR: the EUR row is EXCLUDED and COUNTED — never priced as dollars."""
    from app.api.v1.finance import _burn_line_items

    await _expense(async_db_session, "tools", 10_000, "usd")   # $100.00
    await _expense(async_db_session, "tools", 50_000, "eur")   # EUR 500.00
    reasons: dict[str, str] = {}

    items, excluded = await _burn_line_items(async_db_session, reasons)

    assert len(items) == 1
    assert items[0]["monthly_usd"] == 100.0, "the EUR amount must not be summed as USD"
    assert excluded == 1, "the excluded row must be counted, not silently dropped"
    assert "another or unstated currency" in (items[0]["source"] or "")
    assert "1" in (items[0]["source"] or "")
    assert "burn_line_items.tools" not in reasons, "a measured (usd-only) row needs no null reason"


@pytest.mark.asyncio
async def test_a_category_with_only_foreign_rows_is_unmeasured_not_zero(async_db_session):
    """All-EUR: the row is UNMEASURED (null + reason), never a fabricated $0.00 or a fake $600."""
    from app.api.v1.finance import _burn_line_items

    await _expense(async_db_session, "tools", 50_000, "eur")
    await _expense(async_db_session, "tools", 10_000, "eur")
    reasons: dict[str, str] = {}

    items, excluded = await _burn_line_items(async_db_session, reasons)

    assert len(items) == 1
    assert items[0]["monthly_usd"] is None, "nothing was measured in USD"
    assert excluded == 2
    assert "another or unstated currency" in (items[0]["reason"] or "")
    assert "2" in (items[0]["reason"] or "")
    assert "burn_line_items.tools" in reasons


@pytest.mark.asyncio
async def test_an_all_usd_month_still_prices_every_row(async_db_session):
    """The other direction: the fix must not exclude, count or blank a plain USD ledger."""
    from app.api.v1.finance import _burn_line_items

    await _expense(async_db_session, "tools", 10_000, "usd")
    await _expense(async_db_session, "tools", 2_500, "usd")
    reasons: dict[str, str] = {}

    items, excluded = await _burn_line_items(async_db_session, reasons)

    assert items[0]["monthly_usd"] == 125.0
    assert excluded == 0
    assert "another or unstated currency" not in (items[0]["source"] or "")
    assert reasons == {}


async def _company_with_measured_inputs(db) -> CompanyConfig:
    company = CompanyConfig(
        name="C1 Co",
        owner_hours_per_week=40,
        human_hourly_rate=30.0,
        confirmed_idea={
            "name": "SEO — small business",
            "price_band": [1500, 3000],
            "one_liner": "Full SEO program",
        },
    )
    db.add(company)
    await db.commit()
    return company


@pytest.mark.asyncio
async def test_a_mixed_currency_month_refuses_break_even_and_names_the_excluded_count(
    async_db_session,
):
    """A partial burn is never a total: break-even is refused, with the excluded count named."""
    from app.api.v1.finance import build_planning_view

    await _company_with_measured_inputs(async_db_session)
    async_db_session.add(
        AgentRun(
            agent_type="social_media",
            status="completed",
            cost_usd=Decimal("60"),
            started_at=utcnow(),
            ended_at=utcnow(),
        )
    )
    await _expense(async_db_session, "tools", 10_000, "usd")
    await _expense(async_db_session, "tools", 50_000, "eur")
    await async_db_session.commit()

    view = await build_planning_view(async_db_session)

    assert view["burn_line_items"][0]["monthly_usd"] == 100.0
    assert view["capacity"]["break_even_clients"] is None, (
        "a usd-only subtotal of a mixed month cannot price a break-even"
    )
    reason = view["reasons"]["capacity.break_even_clients"]
    assert "1" in reason and "currency" in reason


@pytest.mark.asyncio
async def test_an_all_usd_month_still_prices_break_even(async_db_session):
    """The other direction: an entirely USD month must still answer a break-even number."""
    from app.api.v1.finance import build_planning_view

    await _company_with_measured_inputs(async_db_session)
    async_db_session.add(
        AgentRun(
            agent_type="social_media",
            status="completed",
            cost_usd=Decimal("60"),
            started_at=utcnow(),
            ended_at=utcnow(),
        )
    )
    await _expense(async_db_session, "tools", 10_000, "usd")   # $100 burn
    await async_db_session.commit()

    view = await build_planning_view(async_db_session)

    assert view["burn_line_items"][0]["monthly_usd"] == 100.0
    # price 1500, ai_cost 60/6 = 10, human 25 h * 30 = 750 -> contribution 740; 100/740 -> 1 client
    assert view["capacity"]["break_even_clients"] == 1, view["reasons"]
    assert "capacity.break_even_clients" not in view["reasons"]


# ── D4-R3: the 30-day AI spend is a TOTAL or it is UNMEASURED ────────────────────────────────


def _run(cost: Decimal | None, *, finished: bool = True, agent: str = "social_media") -> AgentRun:
    return AgentRun(
        agent_type=agent,
        status="completed" if finished else "running",
        cost_usd=cost,
        started_at=utcnow(),
        ended_at=utcnow() if finished else None,
    )


@pytest.mark.asyncio
async def test_the_ai_spend_window_refuses_a_partial_sum(async_db_session):
    """Priced + unpriced finished runs: the priced part is NOT the window's spend."""
    from app.api.v1.finance import _ai_spend_window_truth, _ai_spend_window_usd

    async_db_session.add_all([_run(Decimal("3.00")), _run(None)])
    await async_db_session.commit()

    truth = await _ai_spend_window_truth(async_db_session)

    assert truth.total is None, f"a partial total is not a total (measured 3.00? {truth.total})"
    assert truth.finished_unpriced == 1
    assert truth.measured_runs == 1
    assert await _ai_spend_window_usd(async_db_session) is None


@pytest.mark.asyncio
async def test_an_all_unpriced_window_is_unmeasured_not_zero(async_db_session):
    from app.api.v1.finance import _ai_spend_window_truth

    async_db_session.add_all([_run(None), _run(None, agent="finance")])
    await async_db_session.commit()

    truth = await _ai_spend_window_truth(async_db_session)

    assert truth.total is None
    assert truth.finished_unpriced == 2
    assert truth.per_agent == {}
    assert truth.measured_runs == 0


@pytest.mark.asyncio
async def test_a_fully_priced_window_is_the_measured_sum(async_db_session):
    """The other direction: when every finished run is priced, the sum IS the total."""
    from app.api.v1.finance import _ai_spend_window_truth, _ai_spend_window_usd

    async_db_session.add_all([_run(Decimal("3.00")), _run(Decimal("2.00"), agent="finance")])
    await async_db_session.commit()

    truth = await _ai_spend_window_truth(async_db_session)

    assert truth.total == Decimal("5.0000")
    assert truth.finished_unpriced == 0
    assert truth.per_agent == {"social_media": Decimal("3.0000"), "finance": Decimal("2.0000")}
    assert await _ai_spend_window_usd(async_db_session) == 5.0


@pytest.mark.asyncio
async def test_an_in_flight_run_is_counted_and_never_priced_into_the_total(async_db_session):
    """SpendTruth's own rule: an open run is bounded by the cap, not guessed into the total."""
    from app.api.v1.finance import _ai_spend_window_truth

    async_db_session.add_all([_run(Decimal("4.00")), _run(None, finished=False)])
    await async_db_session.commit()

    truth = await _ai_spend_window_truth(async_db_session)

    assert truth.total == Decimal("4.0000")
    assert truth.in_flight == 1
    assert truth.finished_unpriced == 0


@pytest.mark.asyncio
async def test_planning_refuses_ai_cost_on_a_partially_priced_window(async_db_session):
    """The wire: `margin_template.ai_cost` is null + a reason naming the unpriced count."""
    from app.api.v1.finance import build_planning_view

    await _company_with_measured_inputs(async_db_session)
    async_db_session.add_all([_run(Decimal("3.00")), _run(None)])
    await _expense(async_db_session, "tools", 10_000, "usd")
    await async_db_session.commit()

    view = await build_planning_view(async_db_session)

    assert view["margin_template"]["ai_cost"] is None
    reason = view["reasons"]["margin_template.ai_cost"]
    assert "1" in reason and "no cost" in reason
    assert view["margin_template"]["margin"] is None


@pytest.mark.asyncio
async def test_planning_prices_ai_cost_when_the_window_is_fully_measured(async_db_session):
    """The other direction: 60.00 over 6 clients at capacity is still 10.00/client."""
    from app.api.v1.finance import build_planning_view

    await _company_with_measured_inputs(async_db_session)
    async_db_session.add(_run(Decimal("60")))
    await _expense(async_db_session, "tools", 10_000, "usd")
    await async_db_session.commit()

    view = await build_planning_view(async_db_session)

    assert view["capacity"]["max_clients"] == 6
    assert view["margin_template"]["ai_cost"] == 10.0
    assert "margin_template.ai_cost" not in view["reasons"]
