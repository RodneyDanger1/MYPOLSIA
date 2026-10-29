"""WAVE 4 / F-29 — a chat "FILE THIS TASK" confirm must disclose the PAID run BEFORE the click.

THE DEFECT: `POST /api/v1/chat/confirm` is not a filing step.  `chat_service.confirm_task` creates
the task row, commits it, and calls `run_agent_task.delay(task.id)` — a worker then runs the agent
against the model provider.  The confirm card said "CONFIRM files it through the same path the
agents page uses" and nothing about money, so one click could start a billed run the owner was
never told about.

THE FIX UNDER TEST (backend half): the create_task 201 reply carries the disclosure facts as
ADDITIVE `intent` extras — `paid_run`, `run_cost_cap_usd`, `day_cost_cap_usd` — read from the SAME
`accounting.budget_policy_from_settings()` object the dispatch/run guards enforce (never a
client-side constant).  Every other branch leaves them `None`: an answer or a refusal dispatches
nothing, so it must not carry a cost claim.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services import chat_service as chat


@pytest.mark.asyncio
async def test_the_create_task_reply_declares_the_paid_run_and_the_enforced_caps(
    async_db_session, mock_redis, monkeypatch
) -> None:
    monkeypatch.setattr(settings, "approval_secret", "f29-secret")
    # Distinctive values: the assertion proves the wire reads the POLICY, not a hardcoded number.
    monkeypatch.setattr(settings, "agent_run_cost_cap_usd", 3.5)
    monkeypatch.setattr(settings, "agent_daily_cost_cap_usd", 33.0)

    resp = await chat.handle_message(async_db_session, "create a task to research the market")

    assert resp.intent.kind == "create_task"
    assert resp.requires_confirm is True
    assert resp.intent.paid_run is True
    assert resp.intent.run_cost_cap_usd == 3.5
    assert resp.intent.day_cost_cap_usd == 33.0

    # The disclosure is a fact about the confirm path, not about the reply text: the caps are the
    # same numbers `accounting.authorize` compares an estimated cost against.
    from app.agents.accounting import budget_policy_from_settings

    policy = budget_policy_from_settings()
    assert resp.intent.run_cost_cap_usd == float(policy.per_run_cap_usd)
    assert resp.intent.day_cost_cap_usd == float(policy.day_limit_usd)


@pytest.mark.asyncio
async def test_an_answer_and_a_refusal_carry_no_cost_claim(async_db_session, mock_redis) -> None:
    answer = await chat.handle_message(async_db_session, "status")
    assert answer.intent.kind == "answer"
    assert answer.intent.paid_run is None
    assert answer.intent.run_cost_cap_usd is None
    assert answer.intent.day_cost_cap_usd is None

    refusal = await chat.handle_message(async_db_session, "send an email to our customers")
    assert refusal.intent.kind == "refused"
    assert refusal.intent.paid_run is None
    assert refusal.intent.run_cost_cap_usd is None


@pytest.mark.asyncio
async def test_the_201_wire_body_carries_the_disclosure_before_any_confirm(
    api_client, async_db_session, auth_headers, monkeypatch
) -> None:
    """The card reads the 201; this is the exact body it gets, BEFORE the confirm POST exists."""
    monkeypatch.setattr(settings, "approval_secret", "f29-secret")
    monkeypatch.setattr(settings, "agent_run_cost_cap_usd", 2.25)
    monkeypatch.setattr(settings, "agent_daily_cost_cap_usd", 44.0)

    resp = await api_client.post(
        "/api/v1/chat",
        json={"message": "create a task to draft the launch checklist"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["requires_confirm"] is True
    assert body["intent"]["kind"] == "create_task"
    assert body["intent"]["paid_run"] is True
    assert body["intent"]["run_cost_cap_usd"] == 2.25
    assert body["intent"]["day_cost_cap_usd"] == 44.0
