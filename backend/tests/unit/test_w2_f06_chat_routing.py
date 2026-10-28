"""WAVE 2 / F-06 — the chat router must FILE the four live briefs it used to answer or refuse.

The four reproductions are the Visuals' live trial messages (VISUALS-FINDINGS F-06, P1):

  1. "Research the US candle market … estimated revenue …" matched the finance ANSWER stem
     `revenue` and was never filed.
  2. "… model the unit economics … payment fees …" was REFUSED as a money action, so no finance
     planning could ever be filed.
  3. "… three Meta ad campaigns …" fell through to `orchestrator`: the keyword `campaign` was
     word-anchored and "campaigns" (plural) did not match.
  4. "… the launch social plan: 10 Instagram captions …" filed for `business_planning`: the fixed
     table order put `plan` ahead of `social`/`instagram`.

Every test here is a REAL filing check: the service classification/routing is asserted AND the
confirm-token path is driven to an actual `Task` row (the broker hand-off is recorded, never
performed — the permitted test double list of the operating contract).  No fake success: a
message that is not a work request must still be answered, and a money ACTION must still refuse.
"""
from __future__ import annotations

import pytest
from sqlalchemy import select

import celery_app.tasks.agent_tasks as agent_tasks
from app.config import settings
from app.models.task import Task
from app.services import chat_service as chat

SECRET = "w2-f06-chat-secret"


# ── the four live reproductions ─────────────────────────────────────────────


REPROS = [
    (
        "Research the US candle market and estimate the revenue opportunity for a premium "
        "coconut-soy candle brand at $24-$38 retail",
        "competitor_research",
    ),
    (
        "Model the unit economics for the candle launch including payment fees, the co-packer "
        "unit cost and the 3PL fulfilment cost",
        "finance",
    ),
    (
        "Create a task to launch three Meta ad campaigns for the candle launch month",
        "ads_management",
    ),
    (
        "Draft the launch social plan: 10 Instagram captions and 5 TikTok scripts for the "
        "candle launch",
        "social_media",
    ),
]


@pytest.mark.parametrize(("message", "agent_type"), REPROS)
def test_repro_is_classified_as_a_task_request(message, agent_type):
    verdict = chat.classify_message(message)
    assert verdict.kind == "create_task", (
        f"F-06: {message!r} must be FILED, got kind={verdict.kind!r} rule={verdict.rule!r}"
    )
    decision = chat.route_agent_decision(message)
    assert decision.understood is True
    assert decision.agent_type == agent_type, (
        f"F-06: {message!r} routed to {decision.agent_type!r} via {decision.matched!r}, "
        f"expected {agent_type!r}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(("message", "agent_type"), REPROS)
async def test_repro_files_a_real_task_through_the_confirm_token(
    async_db_session, mock_redis, monkeypatch, message, agent_type
):
    """Message -> confirm card -> confirm -> a REAL Task row for the right agent."""
    monkeypatch.setattr(settings, "approval_secret", SECRET)
    sent: list[int] = []
    monkeypatch.setattr(
        agent_tasks.run_agent_task, "delay", lambda tid: sent.append(tid), raising=False
    )

    proposal = await chat.handle_message(async_db_session, message)
    assert proposal.intent.kind == "create_task"
    assert proposal.requires_confirm is True and proposal.confirm_token
    assert proposal.intent.agent_type == agent_type
    assert proposal.task_id is None, "nothing exists before the owner confirms"

    task_id = await chat.confirm_task(async_db_session, proposal.confirm_token)
    row = (
        await async_db_session.execute(select(Task).where(Task.id == task_id))
    ).scalar_one()
    assert row.agent_type == agent_type
    assert row.source == chat.CHAT_TASK_SOURCE
    assert sent == [task_id], "the confirmed task is handed to the worker exactly once"


# ── the money guard: ACTIONS still refuse, TOPIC nouns no longer do ─────────


@pytest.mark.parametrize(
    "message",
    [
        "pay the contractor $500",
        "create a task to pay the electrician",
        "make a payment of $500 to the co-packer",
        "send a payment to the supplier",
        "buy ads on google",
        "issue a refund for the last order",
        "transfer one hundred dollars to savings",
        "withdraw $200 from stripe",
        "please send the invoice to our lawyer",
    ],
)
def test_money_actions_still_refuse_before_anything_is_filed(message):
    verdict = chat.classify_message(message)
    assert (verdict.kind, verdict.rule) == ("refused", "money_external")


@pytest.mark.parametrize(
    "message",
    [
        "Model the unit economics including payment fees and the 3PL cost",
        "Analyse our payment processing fees against the co-packer quote",
        "what are our payment fees this month?",
        "Research the payment terms our supplier offers",
    ],
)
def test_payment_as_a_topic_is_not_a_money_action(message):
    """F-06 repro 2: a brief ABOUT fees/payment terms must be fileable (or answerable), never refused."""
    assert chat.classify_message(message).kind != "refused"


def test_payment_topic_brief_routes_to_finance():
    decision = chat.route_agent_decision(
        "Analyse our payment processing fees against the co-packer quote"
    )
    assert decision.understood is True
    assert decision.agent_type == "finance"


# ── questions and non-work imperatives are still ANSWERED ───────────────────


@pytest.mark.parametrize(
    ("message", "rule"),
    [
        ("what is our revenue this month", "finance"),
        ("how much cash do we have?", "finance"),
        ("write me a poem about cats", "fallback"),
        ("status", "dashboard"),
    ],
)
def test_question_shapes_are_answers_not_filings(message, rule):
    verdict = chat.classify_message(message)
    assert (verdict.kind, verdict.rule) == ("answer", rule)


# ── F-04 regressions the specificity rule must preserve ────────────────────


@pytest.mark.parametrize(
    ("message", "agent_type"),
    [
        ("create a task to find new leads for the candle launch", "email_outreach"),
        ("create a task to email the research team about the supplier list", "competitor_research"),
        ("create a task to research the market", "competitor_research"),
        ("create a task to write the launch social plan", "social_media"),
        ("create a task to plan the launch strategy", "business_planning"),
        ("create a task to launch three Meta ad campaigns", "ads_management"),
        ("create a task to tidy the workspace", "orchestrator"),
    ],
)
def test_specificity_routes_domain_words_over_generic_ones(message, agent_type):
    assert chat.route_agent_type(message) == agent_type
