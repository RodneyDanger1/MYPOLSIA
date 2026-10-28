"""F2-FEED-1 (P1): a connectivity run must be able to answer, and a refusal must not "complete".

Live evidence (Live feed): task 296 "Connectivity check - model key" was marked completed by
`business_planning` while the run's own summary said it had "no visibility into whether a model key
is configured in dispatch".

Two legs, both pinned here:

1. the run's context now carries a NON-SECRET runtime status block (provider, model id,
   model_key_configured + source, sandbox_mode, mock_mode), read from the SAME seams dispatch uses
   — so the run can answer its own question, and the block never carries the key (not even the
   store's masked display form);
2. a run whose envelope says `status="refused"` is recorded as a FAILED task, by name, never as a
   completed one.  The worker derives the task's terminal status from whether
   `run_agent_for_task` raised and never reads the payload's status, so the refusal is raised at
   that seam (`E_VALIDATION`, the code the repo's own failure taxonomy maps a REFUSAL to).

RED legs (raw output in the forge receipt): removing the block from the doctrine fails test 1 by
name; removing `_refuse_a_run_that_refused_its_task` makes the refused run's task land `completed`
and fails test 4 by name.
"""
from __future__ import annotations

import json

import pytest

from app.agents import crew_factory, deepseek_runtime
from app.agents import runtime_status as rs
from app.agents.base_agent import BasePolsiaAgent
from app.agents.business_planning.agent import BusinessPlanningAgent
from app.config import settings
from app.core.errors import E_VALIDATION, PolsiaError

CONNECTIVITY_TASK = {
    "title": "Connectivity check - model key",
    "description": "Confirm the model runtime dispatch uses is configured and live.",
}


def _doctrine(monkeypatch, *, deepseek: bool, mock_mode: bool) -> str:
    """The `tool_doctrine` body for a business_planning run under a chosen runtime."""
    monkeypatch.setattr(deepseek_runtime, "available", lambda: deepseek)
    monkeypatch.setattr("app.agents.base_agent.is_mock_mode", lambda: mock_mode)
    agent = BusinessPlanningAgent()
    return agent.layers_for(CONNECTIVITY_TASK, {})["tool_doctrine"].text


# ─────────────────────────────────────────────────────────────────────────────
# 1. the non-secret runtime status block reaches the agent's prompt
# ─────────────────────────────────────────────────────────────────────────────
def test_the_prompt_carries_the_live_runtime_facts(monkeypatch):
    monkeypatch.setattr(deepseek_runtime, "configured_model", lambda: "deepseek-v4.1-flash")
    monkeypatch.setattr(
        "app.services.secret_store.deepseek_key_status",
        lambda: {"configured": True, "source": "stored", "masked": "sk-…wxyz"},
    )
    monkeypatch.setattr(settings, "sandbox_mode", False)

    agent = BusinessPlanningAgent()
    doctrine = _doctrine(monkeypatch, deepseek=True, mock_mode=False)
    # The four facts the live task needed, plus the mock-mode honesty flag.
    assert "- provider: deepseek" in doctrine, doctrine
    assert "- model: deepseek-v4.1-flash" in doctrine, doctrine
    assert "model_key_configured: true (a DeepSeek API key is present; source: stored)" in doctrine
    assert "- sandbox_mode: false" in doctrine, doctrine
    assert "- mock_mode: false (a real model call is made)" in doctrine, doctrine
    assert "RUNTIME STATUS" in doctrine

    # The ASSEMBLED prompt carries it too — not merely a layer object nobody renders.
    assembled = agent.assemble_prompt(CONNECTIVITY_TASK, {})
    assert "model_key_configured: true" in assembled.full_prompt
    assert "deepseek-v4.1-flash" in assembled.full_prompt


def test_the_block_is_honest_when_no_key_is_configured_and_the_run_is_mocked(monkeypatch):
    monkeypatch.setenv("POLSIA_CLAUDE_MODEL", "claude-pinned-by-deployment")
    doctrine = _doctrine(monkeypatch, deepseek=False, mock_mode=True)

    assert "- provider: claude_cli" in doctrine, doctrine
    assert "- model: claude-pinned-by-deployment" in doctrine, doctrine
    assert "model_key_configured: false" in doctrine, doctrine
    assert "- mock_mode: true (CLAUDE_CLI_MOCK — no model call is made)" in doctrine, doctrine


def test_the_runtime_block_never_carries_the_key_or_the_masked_form(monkeypatch):
    secret = "sk-f2feed1SECRETvalue0123456789abcdef"
    masked = "sk-…cdef"
    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)
    monkeypatch.setattr("app.agents.base_agent.is_mock_mode", lambda: False)
    monkeypatch.setattr(
        "app.services.secret_store.deepseek_key_status",
        lambda: {"configured": True, "source": "stored", "masked": masked},
    )

    status = rs.runtime_status(deepseek=True, mock_mode=False)

    # The dict carries exactly the non-secret facts — nothing from the key store can leak by
    # accident because only `configured`/`source` are ever read out of it.
    assert set(status) == {
        "provider", "model", "model_key_configured", "model_key_source",
        "sandbox_mode", "mock_mode",
    }
    assert status["model_key_configured"] is True
    assert status["model_key_source"] == "stored"

    block = rs.render_runtime_status_block(status)
    assert secret not in block
    assert masked not in block, "the store's masked display form is not a runtime STATUS fact"

    agent = BusinessPlanningAgent()
    prompt = agent.assemble_prompt(CONNECTIVITY_TASK, {}).full_prompt
    assert secret not in prompt
    assert masked not in prompt


def test_a_key_status_read_failure_is_reported_as_unknown_not_invented(monkeypatch):
    def boom():
        raise RuntimeError("secret store unreadable")

    monkeypatch.setattr("app.services.secret_store.deepseek_key_status", boom)

    status = rs.runtime_status(deepseek=True, mock_mode=False)

    assert status["model_key_source"] == "unknown", (
        "a failed status read must not be reported as a fabricated stored/env fact"
    )


# ─────────────────────────────────────────────────────────────────────────────
# 2. a REFUSED run is a failed task, never a completed one
# ─────────────────────────────────────────────────────────────────────────────
def _install_agent(monkeypatch, status: str, notes: list[str], summary: str | None = None) -> None:
    """Install a stub agent whose run returns the given envelope status/notes."""

    class StubAgent(BasePolsiaAgent):
        agent_type = "business_planning"
        output_schema = None

        def run(self, task, context):  # noqa: ANN001 - the frozen BasePolsiaAgent seam
            return {
                "summary": summary or (
                    "Connectivity check: could not see the dispatch configuration"
                ),
                "status": status,
                "notes": notes,
            }

    monkeypatch.setattr(crew_factory, "get_agent_class", lambda agent_type: StubAgent)


def test_run_agent_for_task_raises_by_name_when_the_run_refused(monkeypatch):
    _install_agent(monkeypatch, "refused", ["no visibility into whether a model key is configured"])

    with pytest.raises(PolsiaError) as refusal:
        crew_factory.run_agent_for_task("business_planning", CONNECTIVITY_TASK, {})

    assert refusal.value.code == E_VALIDATION
    assert "refused its task" in refusal.value.message
    assert "no visibility into whether a model key is configured" in refusal.value.message
    assert refusal.value.context["refused"] is True
    assert refusal.value.context["notes"] == [
        "no visibility into whether a model key is configured"
    ]
    assert refusal.value.how_to_fix, "a named refusal must say how to fix it"


def test_a_refused_run_is_recorded_as_a_failed_task_never_completed(
    monkeypatch, queue_db, execute_body, mock_redis
):
    """The live defect, end to end through the REAL worker body."""
    _install_agent(monkeypatch, "refused", ["the runtime status block named no model key"])
    task_id = queue_db.create_task(
        title=CONNECTIVITY_TASK["title"], agent_type="business_planning"
    )

    outcome = execute_body(task_id)

    row = queue_db.row(task_id)
    assert row["status"] == "failed", row
    assert row["status"] != "completed"
    assert "refused its task" in (row["error_message"] or ""), row
    assert "no model key" in (row["error_message"] or ""), row
    assert row["result_summary"] in (None, ""), (
        "a failed run must not carry a success summary"
    )
    assert outcome.get("status") == "failed", outcome


def test_a_partial_run_is_still_a_completed_task_and_keeps_its_honest_status(
    monkeypatch, queue_db, execute_body, mock_redis
):
    """Guard against over-correction: `partial` is honest WORK, not a refusal."""
    _install_agent(monkeypatch, "partial", ["two supplier pages were unreachable"])
    task_id = queue_db.create_task(
        title=CONNECTIVITY_TASK["title"], agent_type="business_planning"
    )

    outcome = execute_body(task_id)

    row = queue_db.row(task_id)
    assert row["status"] == "completed", row
    assert outcome.get("status") == "completed", outcome
    run = queue_db.query("SELECT status, output FROM agent_runs ORDER BY id DESC LIMIT 1")[0]
    assert run["status"] == "completed"
    stored = json.loads(run["output"] or "{}")
    assert stored["status"] == "partial", "the run's own honesty flag must survive in the ledger"
