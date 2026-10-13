"""T18b (FU-2) — the customer_support per-run reply budget of 10 (D22, dossier §8).

The live product documents "One run answers up to 10 unanswered emails and uses one credit"
(`polsia-behavior-dossier.md` §8).  W2 R-W2-1 established (and this lane re-verified BEFORE
editing — raw evidence `wave-W5/raw/fu2_verify_greps.txt`) that NO per-run counter existed
anywhere: greps for `per_run|PER_RUN|per-run` over the CS agent, `side_effects.py`,
`policy_firewall.py`, `config.py` and `agent_tasks.py` return nothing, and the only bound in the
CS agent was `max_turns = 30` (model-loop turns, not replies).  `RateCaps` is day/hour only and
the `email` channel is SHARED with email_outreach, so the documented bound had no home there
(D22: "keep our other defaults").  This file pins the implemented behaviour:

  * exactly 10 replies -> all 10 are attempted (the budget is a CEILING, not a target);
  * an 11th reply in the same run is SKIPPED BY NAME — refused, never attempted: its named
    refusal (`E_RATE_LIMITED`, index/ref/budget/count in the message + context) lands in
    `refusals[]`, the run summary carries the count (`replies_skipped_by_budget`) and a note in
    the summary text, and the reply stays a draft;
  * `none`/`refund`/missing-ref replies perform no email action and consume NO budget slot;
  * the cap holds on the REAL firewall path (sandbox drafts count as attempts) and inside a real
    worker run (`_execute_agent_task`, file-backed `queue_db`);
  * every `RateCaps` default value is untouched (D22 pin).

RED leg (driver: wave-W5/run_red_legs.py, byte-safe restore): the budget guard removed -> the
11th reply IS attempted (spy sees 11 calls) and no skip record exists.
"""
from __future__ import annotations

import json
from typing import Any

import app.config as app_config
from app.agents.customer_support.agent import MAX_REPLIES_PER_RUN, CustomerSupportAgent
from app.agents.policy_firewall import RateCaps
from app.agents.side_effects import SideEffectOutcome
from app.core.errors import E_POLICY_DENIED, E_RATE_LIMITED

# ── helpers ───────────────────────────────────────────────────────────────────────────────────


class _FirewallSpy:
    """Records every `request_side_effect` call; answers with a sandbox refusal so a reply
    records a labelled draft and the provider integration is never reached."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> SideEffectOutcome:
        self.calls.append((args, kwargs))
        return SideEffectOutcome(
            allowed=False,
            sandboxed=True,
            requires_approval=False,
            policy_code="POLSIA-E114",
            e_code="E_SANDBOX_BLOCKED",
            reason="test double: sandbox refusal",
            audit={"sandbox": True},
        )

    @property
    def refs(self) -> list[str]:
        return [kwargs["payload"]["ref"] for _args, kwargs in self.calls]


def _stub_collection(agent: Any, payload: dict[str, Any]) -> Any:
    """Give the agent the payload its CLI collection would have produced (mock seam only)."""
    agent.collect_structured = lambda task, context, **kw: (payload, None)
    return agent


def _replies(count: int) -> list[dict[str, Any]]:
    return [
        {
            "message_ref": f"msg-{i}",
            "proposed_action": "send_reply",
            "from": f"customer{i}@example.com",
            "subject": f"question {i}",
            "reply_body": f"answer {i}",
        }
        for i in range(count)
    ]


def _payload(count: int) -> dict[str, Any]:
    return {"summary": f"{count} unanswered emails", "replies": _replies(count)}


def _budget_refusals(out: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        r for r in out.get("refusals", [])
        if r.get("error", {}).get("code") == E_RATE_LIMITED
    ]


# ── 1. exactly 10 completes; the 11th is refused, not attempted ───────────────────────────────


def test_exactly_ten_replies_are_all_attempted_and_none_skipped(monkeypatch):
    spy = _FirewallSpy()
    monkeypatch.setattr("app.agents.customer_support.agent.request_side_effect", spy)
    agent = _stub_collection(CustomerSupportAgent(), _payload(10))

    out = agent.run({"title": "reply"}, {})

    assert len(spy.calls) == 10, "the documented budget is a ceiling, not a target"
    assert spy.refs == [f"msg-{i}" for i in range(10)]
    assert _budget_refusals(out) == []
    assert "replies_skipped_by_budget" not in out
    assert len(out["side_effects"]) == 10


def test_the_eleventh_reply_is_skipped_by_name_and_never_attempted(monkeypatch):
    spy = _FirewallSpy()
    monkeypatch.setattr("app.agents.customer_support.agent.request_side_effect", spy)
    agent = _stub_collection(CustomerSupportAgent(), _payload(11))

    out = agent.run({"title": "reply"}, {})

    assert len(spy.calls) == 10, "the 11th reply must be REFUSED, not attempted"
    assert "msg-10" not in spy.refs, "the over-budget reply never reaches the firewall/provider"

    assert out["replies_skipped_by_budget"] == 1, "the run summary carries the COUNT"
    refusals = _budget_refusals(out)
    assert len(refusals) == 1
    error = refusals[0]["error"]
    assert "reply #10" in error["message"]
    assert f"budget of {MAX_REPLIES_PER_RUN}" in error["message"]
    assert "10 replies already attempted" in error["message"]
    assert error["context"] == {
        "index": 10,
        "ref": "msg-10",
        "budget": MAX_REPLIES_PER_RUN,
        "attempted": 10,
    }
    assert error["how_to_fix"], "a named refusal carries its own fix"
    assert "run the agent again" in error["how_to_fix"]

    assert out["replies"][10]["status"] == "draft", "the skipped reply is never reported sent"
    assert "1 reply(ies) skipped: the per-run reply budget of 10 was exhausted" in out["summary"]
    assert out["drafted_not_sent"] is True


def test_many_replies_are_capped_with_a_count_and_one_record_each(monkeypatch):
    spy = _FirewallSpy()
    monkeypatch.setattr("app.agents.customer_support.agent.request_side_effect", spy)
    agent = _stub_collection(CustomerSupportAgent(), _payload(25))

    out = agent.run({"title": "reply"}, {})

    assert len(spy.calls) == 10
    assert out["replies_skipped_by_budget"] == 15
    assert len(_budget_refusals(out)) == 15
    assert [r["error"]["context"]["index"] for r in _budget_refusals(out)] == list(range(10, 25))


# ── 2. the budget on the REAL firewall path (sandbox drafts count as attempts) ────────────────


def test_the_budget_holds_on_the_real_firewall_with_sandbox_drafts(monkeypatch):
    """No spy: the real `request_side_effect`.  In sandbox every send is a labelled draft — an
    attempt all the same, so the 10/run bound still holds and nothing beyond it is attempted."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", True)
    agent = _stub_collection(CustomerSupportAgent(), _payload(12))

    out = agent.run({"title": "reply"}, {})

    assert len(out["side_effects"]) == 10, "exactly the budget's worth of attempts"
    assert all(r["sandboxed"] is True for r in out["side_effects"])
    assert out["replies_skipped_by_budget"] == 2
    assert out["sandbox"] is True
    assert all(r["status"] == "draft" for r in out["replies"])


# ── 3. non-send replies consume no budget ─────────────────────────────────────────────────────


def test_none_and_refund_replies_do_not_consume_the_budget(monkeypatch):
    """The budget bounds SEND attempts: `none` answers nothing and `refund` moves no email — both
    are processed AFTER the budget would have been exhausted without consuming a slot."""
    spy = _FirewallSpy()
    monkeypatch.setattr("app.agents.customer_support.agent.request_side_effect", spy)
    payload = _payload(10)
    payload["replies"] += [
        {"message_ref": "msg-refund", "proposed_action": "refund", "reply_body": "refund it"},
        {"message_ref": "msg-none", "proposed_action": "none", "reply_body": ""},
    ]
    agent = _stub_collection(CustomerSupportAgent(), payload)

    out = agent.run({"title": "reply"}, {})

    assert len(spy.calls) == 10, "only the send replies are attempted"
    assert "replies_skipped_by_budget" not in out, "no slot was consumed by non-send replies"
    refunds = [r for r in out["refusals"] if r.get("error", {}).get("code") == E_POLICY_DENIED]
    assert any("msg-refund" in r["error"]["message"] for r in refunds), "the refund was processed"
    assert out["replies"][10]["status"] == "draft" and out["replies"][10]["needs_human"] is True
    assert out["replies"][11]["status"] == "draft"


# ── 4. the same bound inside a real worker run (stored output) ─────────────────────────────────


def test_a_real_worker_run_records_the_budget_skips_in_its_stored_output(queue_db, monkeypatch):
    """`_execute_agent_task` end to end: the run completes, and the skipped replies are visible in
    the run's own stored output (`agent_runs.output`) — the record the dashboard reads."""
    from celery_app.tasks.agent_tasks import _execute_agent_task

    monkeypatch.setattr(app_config.settings, "sandbox_mode", True)
    monkeypatch.setattr(
        CustomerSupportAgent,
        "collect_structured",
        lambda self, task, context, **kw: (_payload(11), None),
    )
    task_id = queue_db.create_task(
        agent_type="customer_support", title="Reply to the inbox item"
    )

    outcome = queue_db.run(_execute_agent_task(task_id))

    assert outcome["status"] == "completed"
    stored = json.loads(
        queue_db.query(
            "SELECT output FROM agent_runs WHERE task_id = ? ORDER BY id DESC LIMIT 1", (task_id,)
        )[0]["output"]
    )
    assert stored["replies_skipped_by_budget"] == 1
    assert [r["error"]["code"] for r in stored["refusals"]] == [E_RATE_LIMITED]
    assert queue_db.count("email_logs") == 0, "sandbox drafted; nothing was sent or counted"


# ── 5. D22 pin: the budget did not become a RateCaps edit ─────────────────────────────────────


def test_ratecaps_defaults_are_untouched_by_the_budget():
    """D22: only the DOCUMENTED per-run bound was implemented; every shipped cap default stands
    exactly as W2 measured it (`policy_firewall.py:378-384`)."""
    caps = RateCaps()
    assert caps.per_channel_day == {
        "email": 50, "twitter": 12, "linkedin": 4, "github": 6,
        "google_ads": 10, "meta_ads": 10, "stripe": 5, "render": 3,
    }
    assert caps.per_channel_hour == {"email": 15, "twitter": 3, "github": 2}
    assert MAX_REPLIES_PER_RUN == 10, "the documented bound (dossier §8)"
