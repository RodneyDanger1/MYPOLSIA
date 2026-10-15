"""V12/W-C — the W1 worker preflight hook: `_with_compliance_preflight` injects the resolved
compliance preflight under `compliance_send.PREFLIGHT_KEY` for email_outreach runs (the
`channel_sends` injection pattern; W1 OPEN(a): the per-send engine disappears once wired).

RED-first: pre-fix the captured run context carried no `compliance` key — this failed.
"""
from __future__ import annotations


def _install_agent_spy(monkeypatch, result):
    import app.agents.crew_factory as crew_factory

    calls: list = []

    def spy(agent_type, task_dict, context):
        calls.append({"agent_type": agent_type, "task_dict": task_dict, "context": context})
        return result

    monkeypatch.setattr(crew_factory, "run_agent_for_task", spy)
    return calls


def test_worker_injects_the_compliance_preflight_for_email_outreach(
    monkeypatch, queue_db, execute_body
):
    from app.agents.email_outreach.compliance_send import PREFLIGHT_KEY
    from app.models.company import CompanyConfig
    from app.models.outreach import Prospect

    calls = _install_agent_spy(monkeypatch, {"summary": "outreach done"})

    async def _seed():
        async with queue_db.factory() as db:
            db.add(
                CompanyConfig(
                    name="North Star SEO",
                    notification_email="owner@northstar.example",
                    sender_legal_name="North Star SEO LLC",
                    sender_postal_address="1 Main St, Owatonna MN",
                    website_url="https://northstar.example",
                )
            )
            db.add(Prospect(email="opt.out@example.com", status="unsubscribed"))
            db.add(Prospect(email="keep@example.com", status="new"))
            await db.commit()

    queue_db.run(_seed())

    outcome = execute_body(queue_db.create_task(agent_type="email_outreach"))
    assert outcome["status"] == "completed", outcome

    context = calls[0]["context"]
    assert PREFLIGHT_KEY in context, (
        "the worker must hand the resolved preflight to the agent (mode 1); pre-fix it fell "
        "back to the per-send loop-local engine"
    )
    preflight = context[PREFLIGHT_KEY]
    assert preflight["company"]["sender_legal_name"] == "North Star SEO LLC"
    assert "opt.out@example.com" in preflight["suppressed"]
    assert "keep@example.com" not in preflight["suppressed"]
