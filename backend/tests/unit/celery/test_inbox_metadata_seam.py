"""RD12-csinbox — the claimed row's `metadata` payload reaches the agent (the seam that dropped it).

`_execute_agent_task` used to build `task_dict = {"id", "title", "description"}` — the frozen
`tasks.metadata` JSON column (the per-task payload channel; customer_support's attached inbox
item) was DROPPED, so no attached payload could ever reach an agent's prompt
(build/RD4-unwired/RECEIPT.md §4 note 3).  This file pins the fixed carrier end to end:

  row (`tasks.metadata`) → worker (`task_dict["metadata"]`) → real agent → real prompt assembly
  → the prompt string the model seam receives (CLAUDE_CLI_MOCK — the sanctioned agent seam).

RED leg B (measured this lane, receipt §RED): reverting the `task_dict` literal makes both the
carrier test and the row-to-prompt test fail; the restore is sha256-verified.
"""
from __future__ import annotations

ITEM = {"id": "msg-1007", "from": "customer@example.com", "subject": "order #1042",
        "body": "RD12-ROW-BODY: where is my order #1042?"}


def _capture_agent_seam(monkeypatch) -> list[dict]:
    """Record every `task_dict` the worker hands to `crew_factory.run_agent_for_task`."""
    import app.agents.crew_factory as crew_factory

    seen: list[dict] = []

    def spy(agent_type, task_dict, context):
        seen.append({"agent_type": agent_type, "task_dict": task_dict})
        return {"summary": "worker seam captured"}

    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
    return seen


def test_the_worker_carries_the_rows_metadata_to_the_agent(queue_db, execute_body, monkeypatch):
    seen = _capture_agent_seam(monkeypatch)
    task_id = queue_db.create_task(
        title="Check inbox and reply to customer emails",
        agent_type="customer_support",
        metadata={"inbox_item": ITEM},
    )

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed", outcome
    assert seen and seen[0]["agent_type"] == "customer_support"
    assert seen[0]["task_dict"]["metadata"] == {"inbox_item": ITEM}


def test_a_row_without_metadata_carries_none_never_a_fabricated_payload(
    queue_db, execute_body, monkeypatch
):
    """Absent metadata is None on the task dict — 'nothing attached', distinguishable from a body."""
    seen = _capture_agent_seam(monkeypatch)
    task_id = queue_db.create_task(
        title="Check inbox and reply to customer emails", agent_type="customer_support"
    )

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed", outcome
    assert seen[0]["task_dict"]["metadata"] is None


def test_the_body_flows_from_the_task_row_into_the_prompt(queue_db, execute_body, monkeypatch):
    """The whole chain, real agent included: the prompt the model seam receives carries the body
    inside the inbox quarantine block; the cacheable prefix does not."""
    from app.agents.base_agent import BasePolsiaAgent

    captured: list[dict] = []
    original = BasePolsiaAgent.call_claude_structured

    def recording(self, prompt, **kw):
        captured.append({"prompt": prompt, "system": kw.get("system_prompt_static", "")})
        return original(self, prompt, **kw)

    monkeypatch.setattr(BasePolsiaAgent, "call_claude_structured", recording)
    task_id = queue_db.create_task(
        title="Check inbox and reply to customer emails",
        agent_type="customer_support",
        metadata={"inbox_item": ITEM},
    )

    outcome = execute_body(task_id)

    assert outcome["status"] == "completed", outcome
    assert captured, "the real agent must reach the model seam"
    assert any('<untrusted source="inbox" content_id="msg-1007">' in c["prompt"] for c in captured)
    assert any(ITEM["body"] in c["prompt"] for c in captured)
    assert all(ITEM["body"] not in c["system"] for c in captured)
