"""V8 / seat C1 — `chat_service` unit legs: the deterministic router, redaction, tokens, renders.

These are the brief's "router unit legs": pattern matching (kind + rule for each intent family),
the substring-level secret redaction, confirm-token mint/verify round trips (expiry, tamper,
wrong-secret, malformed), and the bounded answers rendering REAL reads or their honest
placeholder (`not measured` / `none yet` — never a fabricated 0).

Endpoint-shaped legs (201/409/401, exactly-once confirm, history shape, secret echo over HTTP)
live in `tests/unit/api/test_v8_chat.py`.
"""
from __future__ import annotations

import time as time_module

import pytest
from sqlalchemy import select

from app.agents.policy_firewall import configured_approval_secret
from app.config import settings
from app.models.chat import ChatMessage
from app.services import chat_service as chat
from app.services.task_service import create_task

CONFIRM_SECRET = b"v8-unit-confirm-secret"


# ── deterministic pattern matching ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("message", "kind", "rule"),
    [
        ("", "answer", "blank"),
        ("   ", "answer", "blank"),
        ("status of task 12", "answer", "task_lookup"),
        ("what is task #7?", "answer", "task_lookup"),
        ("tell me about #42", "answer", "task_lookup"),
        ("pay the contractor $500", "refused", "money_external"),
        ("buy ads on google", "refused", "money_external"),
        ("please send the invoice to our lawyer", "refused", "money_external"),
        ("withdraw $200 from stripe", "refused", "money_external"),
        ("transfer one hundred dollars to savings", "refused", "money_external"),
        ("issue a refund for the last order", "refused", "money_external"),
        ("create a task to research competitor pricing", "create_task", "create_task"),
        ("make a task for the social agent", "create_task", "create_task"),
        ("run the finance agent", "create_task", "create_task"),
        ("have the research agent summarize the niche", "create_task", "create_task"),
        ("what is our revenue this month", "answer", "finance"),
        ("how much cash do we have", "answer", "finance"),
        ("show me the runs ledger", "answer", "runs"),
        ("what are the latest runs", "answer", "runs"),
        ("what does the latest daily report say", "answer", "report"),
        ("how are things going today", "answer", "dashboard"),
        ("status", "answer", "dashboard"),
        ("write me a poem about cats", "answer", "fallback"),
    ],
)
def test_classifier_matches_the_frozen_rules(message, kind, rule):
    verdict = chat.classify_message(message)
    assert (verdict.kind, verdict.rule) == (kind, rule)


def test_task_lookup_carries_the_id():
    assert chat.classify_message("status of task 12").task_id == 12
    assert chat.classify_message("tell me about #42").task_id == 42


def test_the_money_guard_beats_the_create_cues():
    """"create a task to pay X" must REFUSE — never be silently turned into a task."""
    verdict = chat.classify_message("create a task to pay the electrician")
    assert verdict.kind == "refused"
    assert verdict.rule == "money_external"
    assert "pay" in verdict.matched


@pytest.mark.parametrize(
    ("message", "agent_type"),
    [
        ("create a task to research the market", "competitor_research"),
        ("run the social agent", "social_media"),
        ("create a task to email prospects", "email_outreach"),
        ("run the finance agent", "finance"),
        ("create a task to tidy the workspace", "orchestrator"),
    ],
)
def test_agent_routing_is_deterministic(message, agent_type):
    assert chat.route_agent_type(message) == agent_type


# ── secret redaction (load-bearing invariant) ────────────────────────────────


def test_redact_replaces_key_material():
    out = chat.redact_secrets("my key is sk-live-deadbeef1234, keep it safe")
    assert "sk-live-deadbeef1234" not in out
    assert "sk-" not in out
    assert "[redacted]" in out


@pytest.mark.parametrize(
    "text",
    [
        "disk-space is full",
        "risk-management review",
        "task-list for next week",
        "bare prefix sk-",
        "SK-UPPER-case-9876",
        "a key sk-abc123 buried in prose",
    ],
)
def test_redaction_invariant_no_sk_substring_survives(text):
    """Substring-level on purpose: no `sk-` byte-sequence may survive redaction, ever."""
    assert "sk-" not in chat.redact_secrets(text).lower()


# ── confirm tokens: the approvals mint pattern ───────────────────────────────


def test_confirm_token_roundtrip():
    token = chat.issue_confirm_token(CONFIRM_SECRET, chat_message_id=7)
    check = chat.check_confirm_token(CONFIRM_SECRET, token)
    assert check.ok is True
    assert check.chat_message_id == 7


def test_confirm_token_is_bound_to_its_row():
    token = chat.issue_confirm_token(CONFIRM_SECRET, chat_message_id=7)
    forged = "8." + token.split(".", 1)[1]
    check = chat.check_confirm_token(CONFIRM_SECRET, forged)
    assert check.ok is False
    assert check.reason == "invalid"


def test_confirm_token_fails_under_a_different_secret():
    token = chat.issue_confirm_token(CONFIRM_SECRET, chat_message_id=7)
    check = chat.check_confirm_token(b"some-other-secret", token)
    assert check.ok is False
    assert check.reason == "invalid"


def test_confirm_token_expires(monkeypatch):
    token = chat.issue_confirm_token(CONFIRM_SECRET, chat_message_id=7, ttl_s=1)
    future = time_module.time() + 2
    monkeypatch.setattr(chat.time, "time", lambda: future)
    check = chat.check_confirm_token(CONFIRM_SECRET, token)
    assert check.ok is False
    assert check.reason == "expired"
    assert check.chat_message_id == 7


def test_confirm_token_tampered_signature_fails():
    token = chat.issue_confirm_token(CONFIRM_SECRET, chat_message_id=7)
    header, exp, sig = token.split(".", 2)
    check = chat.check_confirm_token(CONFIRM_SECRET, f"{header}.{exp}.{'0' * len(sig)}")
    assert check.ok is False
    assert check.reason == "invalid"


def test_confirm_token_malformed():
    check = chat.check_confirm_token(CONFIRM_SECRET, "not-a-token")
    assert check.ok is False
    assert check.reason == "malformed"


# ── bounded answers render real reads or the honest placeholder ──────────────


@pytest.mark.asyncio
async def test_answers_render_real_reads_or_placeholders(async_db_session, mock_redis):
    """Empty DB: COUNT readings print measured zeros; unmeasured values stay honest."""
    dashboard = await chat.handle_message(async_db_session, "status")
    assert dashboard.intent.source == "dashboard summary"
    assert "0 task(s) created" in dashboard.reply.text
    assert "none yet" in dashboard.reply.text

    finance = await chat.handle_message(async_db_session, "finance")
    assert finance.intent.source == "finance summary"
    assert "not measured" in finance.reply.text, "unmeasured money must never render $0.00"

    runs = await chat.handle_message(async_db_session, "runs")
    assert runs.intent.source == "runs ledger"
    assert "empty" in runs.reply.text

    report = await chat.handle_message(async_db_session, "report")
    assert report.intent.source == "latest daily report"
    assert report.reply.text == "No daily report has been written yet."

    lookup = await chat.handle_message(async_db_session, "task 999")
    assert lookup.intent.source == "task lookup"
    assert "no task #999" in lookup.reply.text


@pytest.mark.asyncio
async def test_task_lookup_renders_a_real_row(async_db_session, mock_redis):
    task = await create_task(async_db_session, title="Seed task", agent_type="finance", source="manual")
    await async_db_session.flush()
    answer = await chat.handle_message(async_db_session, f"task {task.id}")
    assert f"Task #{task.id}" in answer.reply.text
    assert "finance" in answer.reply.text


@pytest.mark.asyncio
async def test_create_task_proposal_mints_a_verifiable_token(async_db_session, mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "approval_secret", "svc-level-secret")
    resp = await chat.handle_message(async_db_session, "create a task to research the market")
    assert resp.intent.kind == "create_task"
    assert resp.intent.agent_type == "competitor_research"
    assert resp.requires_confirm is True and resp.confirm_token

    check = chat.check_confirm_token(configured_approval_secret(), resp.confirm_token)
    assert check.ok is True and check.chat_message_id is not None
    row = (
        await async_db_session.execute(select(ChatMessage).where(ChatMessage.id == check.chat_message_id))
    ).scalar_one()
    assert row.role == "owner"
    assert row.intent["kind"] == "create_task" and row.intent["state"] == "pending"
    assert row.intent["plan"]["agent_type"] == "competitor_research"


@pytest.mark.asyncio
async def test_create_task_without_secret_refuses_by_name(async_db_session, mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "approval_secret", "")
    resp = await chat.handle_message(async_db_session, "create a task to research the market")
    assert resp.intent.kind == "refused"
    assert resp.refusal is not None and resp.refusal.code == "E_CONFIG"
    assert resp.confirm_token is None, "a token that could never verify must never be minted"


@pytest.mark.asyncio
async def test_money_refusal_names_policy_and_sandbox(async_db_session, mock_redis, monkeypatch):
    monkeypatch.setattr(settings, "sandbox_mode", True)
    resp = await chat.handle_message(async_db_session, "pay the invoice")
    assert resp.intent.kind == "refused"
    assert resp.refusal.code == "E_POLICY_DENIED"
    assert "approvals" in resp.refusal.how_to_fix
    assert "SANDBOX_MODE" in resp.refusal.how_to_fix


@pytest.mark.asyncio
async def test_answer_never_echoes_key_material(async_db_session, mock_redis):
    resp = await chat.handle_message(async_db_session, "my key is sk-live-deadbeef1234 — what is our status")
    assert resp.intent.kind == "answer"
    assert "sk-" not in resp.reply.text.lower()
    assert "sk-" not in resp.model_dump_json().lower()
