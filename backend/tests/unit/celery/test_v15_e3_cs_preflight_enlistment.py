"""WAVE V15 / E3 item 4-01 (H1 — E2's frozen `customer_support.send_bridge`) — the enlistment half.

E2's bridge (frozen in `wave-v15/e2-agents-comms/RECEIPT.md` §3 H1) consumes the SAME
`PREFLIGHT_KEY` the outreach sender uses; the worker hook `_with_compliance_preflight` is the
ONE producer of that key on the run context, and it admitted only `email_outreach`.  RED at
HEAD: a `customer_support` run's context carries no preflight, so the bridge's mode-1 path can
never engage (the loop-local fallback does, but the enlistment edit is E3's frozen half).

The contract pinned here:
* ONE key (`"compliance"`) for both agent types — the bridge re-exports it;
* after the flip, a CS context carries the producer's dict (sender identity + suppression);
* every OTHER agent type's context stays byte-identical (the gate must not widen silently);
* the guard names exactly the two agent types (source contract).
"""
from __future__ import annotations


def _seed_company(qdb) -> None:
    """The producer runs `footer_for_company` first — the identity trio must be real."""
    from app.models.company import CompanyConfig

    async def _go() -> None:
        async with qdb.factory() as db:
            db.add(
                CompanyConfig(
                    name="CS Co",
                    sender_legal_name="CS Co LLC",
                    sender_postal_address="1 Main St, Springfield",
                    # `footer_for_company` REQUIRES the trio: legal name, postal address AND
                    # website_url (the footer's privacy link) — all three, by name.
                    website_url="https://cs.example.com",
                )
            )
            await db.commit()

    qdb.run(_go())


def test_customer_support_runs_carry_the_compliance_preflight(queue_db):
    from app.agents.customer_support.send_bridge import PREFLIGHT_KEY as CS_KEY
    from app.agents.email_outreach.compliance_send import PREFLIGHT_KEY as OUTREACH_KEY
    from celery_app.tasks.agent_tasks import _with_compliance_preflight

    assert CS_KEY == OUTREACH_KEY == "compliance", (
        "H1: ONE preflight key serves both agent types (the bridge re-exports the outreach key)"
    )

    _seed_company(queue_db)

    async def _enrich(agent_type: str) -> dict:
        async with queue_db.factory() as db:
            return await _with_compliance_preflight(
                db, agent_type, {"company": {"name": "CS Co"}}
            )

    enriched = queue_db.run(_enrich("customer_support"))
    assert CS_KEY in enriched, (
        "the CS enlistment must inject the preflight the bridge consumes (mode 1)"
    )
    preflight = enriched[CS_KEY]
    assert preflight["company"]["sender_legal_name"] == "CS Co LLC"
    assert "suppressed" in preflight, "the suppression state travels with the preflight"

    outreach = queue_db.run(_enrich("email_outreach"))
    assert OUTREACH_KEY in outreach, "the outreach leg is unchanged by the flip"

    plain = queue_db.run(_enrich("finance"))
    assert plain == {"company": {"name": "CS Co"}}, (
        "every other agent's context stays byte-identical — the gate must not widen silently"
    )


def test_the_enlistment_gate_names_exactly_the_two_agent_types():
    """Source contract: the guard admits exactly {email_outreach, customer_support}."""
    import inspect

    import celery_app.tasks.agent_tasks as agent_tasks

    source = inspect.getsource(agent_tasks._with_compliance_preflight)
    assert '("email_outreach", "customer_support")' in source, (
        "the guard must name both agent types in one comparison (H1's frozen edit)"
    )
