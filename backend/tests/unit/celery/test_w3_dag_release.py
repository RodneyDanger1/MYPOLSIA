"""W3 / OR-01 — the morning plan's DAG is WIRED: a completed parent dispatches its dependent.

The defect (orchestration-hunter `BUGS.json` OR-01, P1): `daily_cycle._morning_cycle` persists
every plan edge (`metadata.depends_on_ids`) and dispatches only the DAG's ROOTS, promising that
"every dependent is released by `task_service.release_ready_dependents` when its whole set has
completed" — but that function had ZERO production callers.  The suite proved the function
WORKS (test_a3_state.py synthesised the hook) while nothing ran it, so in a default install a
dependent sat `pending` for ever and F-26's `metadata.upstream_results` was never materialised.

What this file pins (the acceptance shape from PLAN.md, seat W3):

  * a completed parent dispatches its dependent EXACTLY once, with no test helper doing the
    release — the PRODUCTION completion body (`agent_tasks._execute_agent_task`) is the driver;
  * the dependent's row carries the parent's real `result_summary` (`metadata.upstream_results`,
    the F-26 producer) and the parent's own result is unchanged;
  * a SECOND completion hook cannot dispatch the same dependent again (the release marker is
    committed with the release);
  * a terminal failure fails its dependents BY NAME — never a silent `pending` — and the reason
    names the upstream id/status; the block propagates one more hop (a chain);
  * a RETRYABLE failure (`E_INTEGRATION`, the retry is coming) settles nothing.

Every test below was RED against the pre-fix tree (no release hook at all: the dependent was
never delivered and stayed `pending`); the planted/restored transcript is in the W3 report.
"""
from __future__ import annotations

import json

from app.core.errors import E_INTEGRATION, E_VALIDATION, PolsiaError
from celery_app.tasks import agent_tasks

# ── helpers (file truth; the broker is recorded, never touched) ────────────────────────────────


def _create_task(queue_db, *, title: str, agent_type: str, metadata: dict | None = None) -> int:
    from app.services.task_service import create_task

    async def _make() -> int:
        async with queue_db.factory() as db:
            task = await create_task(
                db, title=title, agent_type=agent_type, source="orchestrator", metadata=metadata
            )
            await db.commit()
            return int(task.id)

    return queue_db.run(_make())


def _metadata(queue_db, task_id: int) -> dict:
    raw = queue_db.row(task_id).get("metadata")
    return json.loads(raw) if raw else {}


def _row(queue_db, task_id: int) -> dict:
    return queue_db.row(task_id)


def _install_delay_recorder(monkeypatch) -> list[int]:
    """Record — never perform — the broker hand-off (`run_agent_task.delay`)."""
    delivered: list[int] = []
    monkeypatch.setattr(
        agent_tasks.run_agent_task,
        "delay",
        lambda task_id: delivered.append(int(task_id)),
        raising=False,
    )
    return delivered


def _plan_dag(queue_db, *, chain: int = 2) -> list[int]:
    """A morning-plan-shaped chain A -> B (-> C): `source='orchestrator'`, edges in metadata."""
    ids: list[int] = []
    for index in range(chain):
        metadata = {"plan_day": "2026-10-28"}
        if ids:
            metadata["depends_on_ids"] = [ids[-1]]
            metadata["depends_on_titles"] = [f"Plan step {index - 1}"]
        ids.append(
            _create_task(
                queue_db,
                title=f"Plan step {index}",
                agent_type="business_planning" if index % 2 == 0 else "code_generation",
                metadata=metadata,
            )
        )
    return ids


# ── 1. the hook the product did not have ──────────────────────────────────────────────────────


def test_a_completed_parent_dispatches_its_dependent_without_a_test_helper(
    queue_db, monkeypatch, install_run_spy
):
    """RED (pre-fix): `_execute_agent_task(A)` completed A and delivered NOTHING; B stayed pending."""
    delivered = _install_delay_recorder(monkeypatch)
    install_run_spy(delegate=False, result={"summary": "A produced the launch brief"})
    a_id, b_id = _plan_dag(queue_db, chain=2)

    outcome = queue_db.run(agent_tasks._execute_agent_task(a_id))

    assert outcome["status"] == "completed", outcome
    assert outcome["dag_dependents"] == {"released": [b_id], "dispatched": True}, outcome
    assert delivered == [b_id], (
        "the completed parent did not hand its dependent to the broker exactly once "
        f"(delivered={delivered!r}) — this is OR-01"
    )

    row_b = _row(queue_db, b_id)
    meta_b = _metadata(queue_db, b_id)
    assert row_b["status"] == "pending", row_b
    assert meta_b["dag_released_at"], meta_b
    assert meta_b["dag_released_by"] == a_id, meta_b
    assert meta_b["rearmed_at"], "a released row must carry the delivery-debt marker"
    # F-26's producer: the dependent RECORDS what the upstream agent produced.
    assert [item["task_id"] for item in meta_b["upstream_results"]] == [a_id], meta_b
    assert meta_b["upstream_results"][0]["result_summary"] == "A produced the launch brief"
    # The parent's own truth is untouched by the release.
    assert _row(queue_db, a_id)["result_summary"] == "A produced the launch brief"
    assert _row(queue_db, a_id)["status"] == "completed"


def test_a_second_completion_hook_cannot_dispatch_the_same_dependent_twice(
    queue_db, monkeypatch, install_run_spy
):
    """The release marker is committed with the release: a re-run of the hook delivers nothing."""
    delivered = _install_delay_recorder(monkeypatch)
    install_run_spy(delegate=False, result={"summary": "done"})
    a_id, b_id = _plan_dag(queue_db, chain=2)
    queue_db.run(agent_tasks._execute_agent_task(a_id))
    assert delivered == [b_id]

    # A redelivered terminal message / a second caller of the hook: the production helper itself.
    second = queue_db.run(agent_tasks._release_dag_dependents(a_id, agent_type="business_planning"))
    again = queue_db.run(agent_tasks._release_dag_dependents(a_id, agent_type="business_planning"))

    assert second == {"released": [], "dispatched": False}, second
    assert again == {"released": [], "dispatched": False}, again
    assert delivered == [b_id], f"the dependent was dispatched twice: {delivered!r}"


def test_a_completion_with_nothing_waiting_settles_nothing(queue_db, monkeypatch, install_run_spy):
    delivered = _install_delay_recorder(monkeypatch)
    install_run_spy(delegate=False, result={"summary": "standalone work"})
    a_id = _create_task(queue_db, title="Standalone", agent_type="finance")

    outcome = queue_db.run(agent_tasks._execute_agent_task(a_id))

    assert outcome["status"] == "completed"
    assert outcome["dag_dependents"] == {"released": [], "dispatched": False}
    assert delivered == []


# ── 2. failure semantics: a dependency that can never be satisfied is named, never pending ─────


def test_a_terminal_failure_blocks_its_dependents_by_name_and_transitively(
    queue_db, monkeypatch, install_run_spy
):
    """RED (pre-fix): B and C stayed `pending` for ever, waiting on a failed A."""
    delivered = _install_delay_recorder(monkeypatch)
    install_run_spy(
        delegate=False,
        # A NON-retryable named refusal: the ladder is not going to run A again, so A's edge can
        # never be satisfied.  (`ValueError` would be treated as transient by `is_retryable` —
        # unexpected failures get a retry — so the terminal case is driven by a named code.)
        error_factory=lambda: PolsiaError(
            E_VALIDATION, "the brief could not be drafted: the model returned no plan"
        ),
    )
    a_id, b_id, c_id = _plan_dag(queue_db, chain=3)

    outcome = queue_db.run(agent_tasks._execute_agent_task(a_id))

    assert outcome["status"] == "failed", outcome
    assert outcome["requeued"] is False, "E_VALIDATION is terminal, not retryable"
    assert outcome["dag_dependents"] == {"blocked": [b_id, c_id]}, outcome
    assert delivered == [], "a blocked dependent must never be dispatched"

    row_b, row_c = _row(queue_db, b_id), _row(queue_db, c_id)
    assert row_b["status"] == "failed", row_b
    assert row_c["status"] == "failed", row_c
    assert f"#{a_id}" in (row_b["error_message"] or ""), row_b["error_message"]
    assert "dependency_blocked" in (row_b["error_message"] or ""), row_b["error_message"]
    assert "'failed'" in (row_b["error_message"] or ""), row_b["error_message"]
    assert "no plan" in (row_b["error_message"] or ""), (
        "the blocked reason carries the upstream's own failure, not just its id"
    )
    meta_b = _metadata(queue_db, b_id)
    assert meta_b["dependency_blocked"]["upstream_id"] == a_id, meta_b
    assert meta_b["dependency_blocked"]["upstream_status"] == "failed", meta_b
    # The transitive hop names ITS direct cause (B, which was blocked by A), never a guess.
    assert f"#{b_id}" in (row_c["error_message"] or ""), row_c["error_message"]
    assert _metadata(queue_db, c_id)["dependency_blocked"]["upstream_id"] == b_id
    # One activity row per blocked task, at warning level.
    rows = queue_db.query(
        "SELECT summary, level FROM activity_log WHERE action = 'task_dependency_blocked' "
        "ORDER BY id"
    )
    assert sorted(row["summary"] for row in rows) == sorted(
        [row_b["error_message"], row_c["error_message"]]
    ), rows
    assert {row["level"] for row in rows} == {"warning"}

    # A second hook for the same outcome is a no-op (the marker stops a re-write).
    again = queue_db.run(
        agent_tasks._block_dag_dependents(
            a_id, agent_type="business_planning", upstream_status="failed", upstream_error="x"
        )
    )
    assert again == {"blocked": []}, again


def test_a_retryable_failure_settles_nothing_because_the_retry_is_coming(
    queue_db, monkeypatch, install_run_spy
):
    delivered = _install_delay_recorder(monkeypatch)

    def _retryable() -> Exception:
        return PolsiaError(E_INTEGRATION, "the provider answered 503")

    install_run_spy(delegate=False, error_factory=_retryable)
    a_id, b_id = _plan_dag(queue_db, chain=2)

    outcome = queue_db.run(agent_tasks._execute_agent_task(a_id))

    assert outcome["status"] == "failed", outcome
    assert outcome["retryable"] is True, outcome
    assert outcome["requeued"] is True, outcome
    assert outcome["dag_dependents"] is None, outcome
    assert _row(queue_db, a_id)["status"] == "pending", "the retry re-queues the parent"
    assert _row(queue_db, b_id)["status"] == "pending", (
        "a still-retrying parent must not block its dependent"
    )
    assert _metadata(queue_db, b_id).get("dependency_blocked") is None
    assert delivered == []


# ── 3. the release function itself stays idempotent (no worker involved) ──────────────────────


def test_the_release_function_releases_each_dependent_once(queue_db):
    from app.services import task_service

    a_id = _create_task(queue_db, title="Parent", agent_type="competitor_research")
    b_id = _create_task(
        queue_db,
        title="Child",
        agent_type="code_generation",
        metadata={"depends_on_ids": [a_id]},
    )

    async def _complete() -> None:
        async with queue_db.factory() as db:
            await task_service.update_task_status(db, a_id, "completed", result_summary="parent out")
            await db.commit()

    async def _release() -> list[int]:
        async with queue_db.factory() as db:
            ready = await task_service.release_ready_dependents(db, completed_id=a_id)
            await db.commit()
            return ready

    queue_db.run(_complete())
    assert queue_db.run(_release()) == [b_id]
    assert queue_db.run(_release()) == [], "a second release pass must not re-release the row"
    meta = _metadata(queue_db, b_id)
    assert meta["dag_released_by"] == a_id
    assert meta["upstream_results"][0]["result_summary"] == "parent out"


# ── 4. F2 finisher: the SERVICE's own terminal writers settle the DAG too ────────────────────
# `PATCH /tasks/{id}/status` (`task_service.transition_task_status`) is a PRODUCTION
# completion/termination writer that never enters the worker body.  OR-01's acceptance is
# "named outcome, never silent pending" for a failed/cancelled upstream and a released
# dependent for a completed one — that has to hold on this path too, or an owner who cancels a
# plan step strands its dependents exactly as before the fix.


def test_an_owner_cancelled_parent_fails_its_dependents_by_name(queue_db):
    """F2 RED (pre-fix): the two dependents of a cancelled parent stayed `pending` for ever."""
    from app.services import task_service

    a_id, b_id, c_id = _plan_dag(queue_db, chain=3)

    async def _cancel() -> None:
        async with queue_db.factory() as db:
            await task_service.transition_task_status(db, a_id, "cancelled", actor="owner")
            await db.commit()

    queue_db.run(_cancel())

    assert _row(queue_db, a_id)["status"] == "cancelled"
    row_b, row_c = _row(queue_db, b_id), _row(queue_db, c_id)
    assert row_b["status"] == "failed", row_b
    assert row_c["status"] == "failed", row_c
    assert "dependency_blocked" in (row_b["error_message"] or ""), row_b["error_message"]
    assert f"#{a_id}" in (row_b["error_message"] or ""), row_b["error_message"]
    assert "'cancelled'" in (row_b["error_message"] or ""), row_b["error_message"]
    assert _metadata(queue_db, b_id)["dependency_blocked"]["upstream_status"] == "cancelled"
    assert f"#{b_id}" in (row_c["error_message"] or ""), row_c["error_message"]
    actions = [record["action"] for record in queue_db.query("SELECT action FROM activity_log ORDER BY id")]
    assert actions.count("task_dependency_blocked") == 2, actions


def test_an_operator_completed_parent_rearms_its_dependent_for_delivery(queue_db):
    """F2 RED (pre-fix): an operator-completed parent released nothing (its dependent stranded)."""
    from app.services import task_service

    a_id, b_id = _plan_dag(queue_db, chain=2)

    async def _complete() -> None:
        async with queue_db.factory() as db:
            await task_service.transition_task_status(
                db, a_id, "completed", result_summary="operator finished the brief", actor="owner"
            )
            await db.commit()

    queue_db.run(_complete())

    row_b, meta_b = _row(queue_db, b_id), _metadata(queue_db, b_id)
    assert row_b["status"] == "pending", row_b
    assert meta_b["dag_released_at"], meta_b
    assert meta_b["dag_released_by"] == a_id, meta_b
    assert meta_b["rearmed_at"], "a released row must carry the delivery-debt marker"
    assert [item["task_id"] for item in meta_b["upstream_results"]] == [a_id], meta_b
    assert meta_b["upstream_results"][0]["result_summary"] == "operator finished the brief"


# ── 5. F2 finisher: the batch WINDOW must not hide this parent's dependents ──────────────────
# The release/block sweeps read a bounded page (`DAG_RELEASE_BATCH` / `_DAG_BLOCK_BATCH` = 200)
# ordered by id.  A backlog of unrelated pending dependent rows ahead of this plan's rows used to
# consume the whole window, so the parent's OWN dependent was never examined: it stranded
# `pending` with no marker and no reconciler could see it.  These three tests pin the property the
# bounds must preserve — "the completed/failed parent's dependent is found, whatever the backlog".


def _seed_pending_backlog(queue_db, count: int, *, depends_on: int = 999_999) -> None:
    """`count` unrelated pending rows carrying an edge nothing will ever satisfy."""
    from app.services.task_service import create_task

    async def _seed() -> None:
        async with queue_db.factory() as db:
            for index in range(count):
                await create_task(
                    db,
                    title=f"Backlog {index}",
                    agent_type="finance",
                    source="orchestrator",
                    metadata={"depends_on_ids": [depends_on]},
                )
            await db.commit()

    queue_db.run(_seed())


def test_a_completed_parent_releases_a_dependent_beyond_the_global_batch(
    queue_db, monkeypatch, install_run_spy
):
    """F2 RED: the single 200-row window was all backlog; the dependent was never released."""
    delivered = _install_delay_recorder(monkeypatch)
    install_run_spy(delegate=False, result={"summary": "A produced the launch brief"})
    _seed_pending_backlog(queue_db, 200)
    a_id, b_id = _plan_dag(queue_db, chain=2)

    outcome = queue_db.run(agent_tasks._execute_agent_task(a_id))

    assert outcome["status"] == "completed", outcome
    assert delivered == [b_id], (
        "the completed parent did not release the dependent behind a 200-row backlog "
        f"(delivered={delivered!r})"
    )
    row_b, meta_b = _row(queue_db, b_id), _metadata(queue_db, b_id)
    assert row_b["status"] == "pending", row_b
    assert meta_b["dag_released_at"], meta_b
    assert meta_b["dag_released_by"] == a_id, meta_b


def test_a_terminal_failure_blocks_a_dependent_beyond_the_global_batch(
    queue_db, monkeypatch, install_run_spy
):
    """F2 RED: the block sweep stopped after a backlog-only page; the dependent stayed pending."""
    delivered = _install_delay_recorder(monkeypatch)
    install_run_spy(
        delegate=False,
        error_factory=lambda: PolsiaError(E_VALIDATION, "the model returned no plan"),
    )
    _seed_pending_backlog(queue_db, 200, depends_on=999_998)
    a_id, b_id = _plan_dag(queue_db, chain=2)

    outcome = queue_db.run(agent_tasks._execute_agent_task(a_id))

    assert outcome["status"] == "failed", outcome
    assert outcome["requeued"] is False, outcome
    assert outcome["dag_dependents"] == {"blocked": [b_id]}, outcome
    row_b = _row(queue_db, b_id)
    assert row_b["status"] == "failed", row_b
    assert f"#{a_id}" in (row_b["error_message"] or ""), row_b["error_message"]
    assert delivered == []


def test_a_cancelled_parent_fails_a_dependent_beyond_the_global_batch(queue_db):
    """F2 RED: the service leg's first page was all backlog; the dependent stayed pending."""
    from app.services import task_service

    _seed_pending_backlog(queue_db, 200)
    a_id, b_id = _plan_dag(queue_db, chain=2)

    async def _cancel() -> None:
        async with queue_db.factory() as db:
            await task_service.transition_task_status(db, a_id, "cancelled", actor="owner")
            await db.commit()

    queue_db.run(_cancel())

    assert _row(queue_db, a_id)["status"] == "cancelled"
    row_b = _row(queue_db, b_id)
    assert row_b["status"] == "failed", row_b
    assert "dependency_blocked" in (row_b["error_message"] or ""), row_b["error_message"]
    assert f"#{a_id}" in (row_b["error_message"] or ""), row_b["error_message"]


# ── 6. F2 finisher: the worker's PRE-EXECUTION terminal refusal legs settle the DAG ──────────
# W3 wired `_block_dag_dependents` into the budget gate and the replay-refusal gate, but neither
# leg had a test.  Both write `rejected` (terminal) instead of `failed`; their dependents can never
# run and must be named, not left pending.


def test_a_budget_rejected_parent_fails_its_dependents_by_name(queue_db, monkeypatch):
    from app.agents.accounting import BudgetStop

    a_id, b_id = _plan_dag(queue_db, chain=2)

    async def _deny(_agent_type):  # noqa: ANN001 - the gate's own signature
        raise BudgetStop("the day's spend cap is reached")

    monkeypatch.setattr(agent_tasks, "assert_budget_allows", _deny)

    outcome = queue_db.run(agent_tasks._execute_agent_task(a_id))

    assert outcome["status"] == "skipped", outcome
    assert outcome["task_status"] == "rejected", outcome
    assert outcome["dag_dependents"] == {"blocked": [b_id]}, outcome
    assert _row(queue_db, a_id)["status"] == "rejected"
    row_b = _row(queue_db, b_id)
    assert row_b["status"] == "failed", row_b
    assert "dependency_blocked" in (row_b["error_message"] or ""), row_b["error_message"]
    assert "'rejected'" in (row_b["error_message"] or ""), row_b["error_message"]
    assert _metadata(queue_db, b_id)["dependency_blocked"]["upstream_status"] == "rejected"


def test_a_replay_refused_parent_fails_its_dependents_by_name(queue_db):
    """The E_ACTION_REPLAY_UNAVAILABLE refusal is terminal too (it lands `rejected`)."""
    from app.services import task_service

    a_id, b_id = _plan_dag(queue_db, chain=2)

    async def _park_without_payload() -> None:
        async with queue_db.factory() as db:
            row = await task_service.get_task(db, a_id)
            row.metadata_json = {
                **(row.metadata_json or {}),
                "approval_required": [{"fingerprint": "fp-with-no-stored-payload"}],
                "approval_token": "not-a-real-token",
            }
            await db.commit()

    queue_db.run(_park_without_payload())

    outcome = queue_db.run(agent_tasks._execute_agent_task(a_id))

    assert outcome["status"] == "skipped", outcome
    assert outcome["reason"] == "E_ACTION_REPLAY_UNAVAILABLE", outcome
    assert outcome["task_status"] == "rejected", outcome
    assert outcome["dag_dependents"] == {"blocked": [b_id]}, outcome
    row_b = _row(queue_db, b_id)
    assert row_b["status"] == "failed", row_b
    assert "dependency_blocked" in (row_b["error_message"] or ""), row_b["error_message"]
    assert "'rejected'" in (row_b["error_message"] or ""), row_b["error_message"]
