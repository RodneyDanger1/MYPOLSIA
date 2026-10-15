"""WAVE V15 / E3 item 4-05 — the memory wiring half (read + write legs) at the worker.

RED-first pins for the repair unit (r-repair) completing E3's ready-to-execute 4-05 notes
(`wave-v15/e3-runtime-serial/RECEIPT.md` §Handoff states; interfaces = E2 receipt §3 H4):

  * read leg — the worker's context build resolves `context["memories"]` through
    `memory_service.resolve_agent_memories` (the `_with_*` pattern: a store that cannot answer
    leaves the key ABSENT and warns); `base_agent.context_memories` then renders the list into
    every agent's assembled prompt;
  * write leg — `_persist_run_artifacts` calls `remember_run` so a completed run stores ONE
    honest, run-derived memory (its own summary — never an invented one); a run without a
    summary SKIPS by name and writes nothing; the memory report is folded into the run's
    `persistence` output.

Harness: the celery conftest's file-backed `queue_db` driven on the Celery process loop through
the real `_execute_agent_task`; the vector store is the root conftest's `mock_chroma` (chromadb
is NOT installed in the unit env), and the search is faked only where determinism needs it —
the resolver's row enrichment runs for real (E2's own pattern).
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.services import memory_service

MEMORY_TOKEN = "v15-r-repair-memory-token-7c1f"


def _latest_run_output(queue_db) -> dict:
    """The run row as stored in the FILE (stdlib driver, no ORM identity map)."""
    rows = queue_db.query("SELECT output FROM agent_runs ORDER BY id DESC LIMIT 1")
    if not rows or not rows[0]["output"]:
        return {}
    return json.loads(rows[0]["output"])


def _install_agent_capture(monkeypatch, result: dict) -> list[dict]:
    """Replace `crew_factory.run_agent_for_task` with a capture of (agent, task, context)."""
    import app.agents.crew_factory as crew_factory

    calls: list[dict] = []

    def spy(agent_type, task_dict, context):
        calls.append({"agent_type": agent_type, "task_dict": task_dict, "context": context})
        return result

    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
    return calls


# ── write leg: a completed run stores its OWN summary ─────────────────────────────────────────


def test_a_completed_run_stores_its_own_summary_as_a_memory_row(
    monkeypatch, queue_db, execute_body, mock_chroma
):
    summary = f"observed {MEMORY_TOKEN}: completed the sweep"

    _install_agent_capture(monkeypatch, {"summary": summary})
    outcome = execute_body(queue_db.create_task(agent_type="orchestrator"))

    assert outcome["status"] == "completed"
    rows = queue_db.query("SELECT title, content, category, source FROM memory_entries")
    assert len(rows) == 1, "a completed run must store exactly ONE memory"
    assert rows[0]["content"] == summary, "the run's own words — never paraphrased or invented"
    assert rows[0]["category"] == "agent_run"
    assert rows[0]["source"] == "orchestrator"
    assert rows[0]["title"] == "orchestrator: queue test task"

    output = _latest_run_output(queue_db)
    memory = output["persistence"]["memory"]
    assert memory["ok"] is True, memory
    assert isinstance(memory["memory_id"], int)


def test_a_summaryless_run_writes_no_memory_and_no_persistence_report(
    monkeypatch, queue_db, execute_body, mock_chroma
):
    """No summary = nothing honest to remember: a NAMED skip, and `persistence` stays absent."""
    _install_agent_capture(monkeypatch, {"status": "ok"})
    outcome = execute_body(queue_db.create_task(agent_type="orchestrator"))

    assert outcome["status"] == "completed"
    assert queue_db.count("memory_entries") == 0, "never a fabricated memory"
    assert "persistence" not in _latest_run_output(queue_db), "nothing was written: no report"
    assert queue_db.count("activity_log", "action = ?",
                          ("run_artifacts_not_persisted",)) == 0


# ── read leg: the worker hands stored memories to the run, and the prompt shows them ──────────


def test_the_context_build_carries_stored_memories_into_the_agents_prompt(
    monkeypatch, queue_db, execute_body, mock_chroma
):
    # 1. a memory that really exists in the store (persisted and committed on the queue DB)
    async def _seed() -> int:
        async with queue_db.factory() as db:
            entry = await memory_service.store_memory(
                db, category="company", title="Flag", content=MEMORY_TOKEN, tags=["t"],
            )
            await db.commit()
            return int(entry.id)

    entry_id = queue_db.run(_seed())

    async def fake_search(db: Any, query: str, n_results: int = 5,
                          category: str | None = None) -> list[dict[str, Any]]:
        return [{"id": "hit-1", "memory_id": entry_id, "title": "Flag",
                 "content": MEMORY_TOKEN, "category": "company", "source": None,
                 "distance": 0.2}]

    monkeypatch.setattr(memory_service, "search_memory", fake_search)

    calls = _install_agent_capture(monkeypatch, {"summary": "second run"})
    outcome = execute_body(queue_db.create_task(agent_type="social_media", title="daily posts"))

    assert outcome["status"] == "completed"
    context = calls[0]["context"]
    assert context["memories"][0]["content"] == MEMORY_TOKEN, (
        "the worker must hand the resolver's list over under `context[\"memories\"]`"
    )

    # 2. the base seam renders it — the run's assembled prompt carries the stored memory
    from app.agents.social_media.agent import SocialMediaAgent

    assembled = SocialMediaAgent().assemble_prompt(calls[0]["task_dict"], context)
    assert MEMORY_TOKEN in assembled.volatile_suffix


def test_the_read_hook_leaves_memories_absent_when_the_store_cannot_answer(
    monkeypatch, queue_db, execute_body, mock_chroma, caplog
):
    """A dead store is RECORDED (warn) and the key stays ABSENT — never an injected `[]`."""
    from app.core.errors import E_DB, PolsiaError

    async def broken(db: Any, *, agent_type: str, query: str = "",
                     limit: int = 8) -> list[dict[str, Any]]:
        raise PolsiaError(E_DB, "memory store unreachable", how_to_fix="restart the store")

    monkeypatch.setattr(memory_service, "resolve_agent_memories", broken)
    calls = _install_agent_capture(monkeypatch, {"summary": "run without memories"})

    with caplog.at_level(logging.WARNING, logger="celery_app.tasks.agent_tasks"):
        outcome = execute_body(queue_db.create_task(agent_type="social_media"))

    assert outcome["status"] == "completed", "a dead store must not kill the run"
    assert "memories" not in calls[0]["context"], "unknown must stay distinguishable from none"
    assert [r for r in caplog.records if "code=agent_memories_unavailable" in r.getMessage()]


# ── the fold: the memory report rides the persistence report ─────────────────────────────────


def test_persist_run_artifacts_folds_the_memory_report(queue_db, mock_chroma):
    from celery_app.tasks.agent_tasks import _persist_run_artifacts

    async def _go() -> dict[str, Any]:
        async with queue_db.factory() as db:
            report = await _persist_run_artifacts(
                db, "orchestrator", {"summary": f"stored {MEMORY_TOKEN}"},
                task={"title": "fold leg"},
            )
            await db.commit()
            return report

    report = queue_db.run(_go())

    assert report["ok"] is True, report
    assert report["memory"]["ok"] is True
    assert report["memory"]["title"] == "orchestrator: fold leg"
    assert isinstance(report["memory"]["memory_id"], int)
    assert queue_db.count("memory_entries") == 1


def test_persist_run_artifacts_skips_the_memory_leg_without_a_summary(queue_db, mock_chroma):
    from celery_app.tasks.agent_tasks import _persist_run_artifacts

    async def _go() -> dict[str, Any]:
        async with queue_db.factory() as db:
            report = await _persist_run_artifacts(db, "orchestrator", {})
            await db.commit()
            return report

    report = queue_db.run(_go())

    assert report == {}, "nothing worth storing and nothing written: no report at all"
    assert queue_db.count("memory_entries") == 0
