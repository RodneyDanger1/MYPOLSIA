"""T14b (FU-3) — the `failed_payments` attach at the OTHER two finance dispatch sites.

T14/D21 wired the producer half of the refund path on the MORNING cycle only (its receipt §7 /
wave-W3 REQUESTS §4 record the seam): the fixed finance task gets
`metadata={"failed_payments": await recent_failed_payments(db)}` (non-empty only), and the finance
agent renders the records into its quarantined volatile context so a refund can name a real `pi_…`
id.  The other two sites that dispatch the finance agent did NOT attach anything:

  1. `celery_app/tasks/agent_tasks.py::_ads_stripe_sync` — the 6-hourly *"Check for failed Stripe
     payments and update revenue snapshot"* task;
  2. `app/api/v1/agents.py` manual "Run Now" trigger — the owner's ad-hoc finance run.

This file pins both, mirroring the morning cycle's exact shape and the T14 harness:

  * records exist -> the finance task's `metadata.failed_payments` carries them (with the
    `pi_…` id), and the OTHER task created by the same call carries nothing;
  * no records -> metadata is exactly what the site always wrote (SQL NULL — untouched);
  * the MANUAL trigger attaches for finance only (a non-finance trigger with the same records on
    disk attaches nothing);
  * the finance agent's volatile context receives the attached records (the consumer leg).

RED legs (driver: wave-W5/run_red_legs.py, byte-safe restore): the attach removed at EACH site ->
that site's task metadata lacks the key (both legs measured separately).
"""
from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.config import settings

PI_FROM_CHARGE = "pi_3MtwBwLkdIwHu7ix28a3tqPa"

CHARGE_SHAPE = {
    "id": "ch_3MmlLrLkdIwHu7ix0snN0B15",
    "object": "charge",
    "amount": 1500,
    "currency": "usd",
    "customer": "cus_t14b",
    "payment_intent": PI_FROM_CHARGE,
    "status": "failed",
}


# ── fixtures (the queue FILE is the DB both the route and the workers read) ────────────────────


@pytest_asyncio.fixture
async def trigger_client(queue_db, monkeypatch):
    """An ASGI client whose requests talk to the queue FILE — `get_db`'s contract mirrored."""
    from app.core.database import get_db
    from app.main import app

    monkeypatch.setattr(settings, "api_key", "test-key")

    async def _get_db():
        async with queue_db.factory() as db:
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    app.dependency_overrides[get_db] = _get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client
    app.dependency_overrides.clear()


# ── helpers ───────────────────────────────────────────────────────────────────────────────────


def _seed_failed_payment(queue_db, event_id: str = "evt_t14b") -> None:
    from app.services.finance_service import record_stripe_event

    async def _seed() -> None:
        async with queue_db.factory() as db:
            await record_stripe_event(
                db,
                stripe_event_id=event_id,
                event_type="charge.failed",
                raw_payload={"id": event_id, "data": {"object": CHARGE_SHAPE}},
                customer_id="cus_t14b",
                amount_cents=1500,
                currency="usd",
                payment_intent_id=PI_FROM_CHARGE,
            )
            await db.commit()

    queue_db.run(_seed())


class _Group:
    """The celery group seam (the sync creates TWO tasks -> `enqueue_created`'s group path)."""

    def __call__(self, sigs):  # noqa: ANN001
        return self

    def apply_async(self) -> None:
        return None


def _metadata_by_agent(queue_db) -> dict[str, Any]:
    rows = queue_db.query("SELECT agent_type, metadata FROM tasks")
    return {row["agent_type"]: row["metadata"] for row in rows}


def _install_delay_recorder(monkeypatch) -> list[int]:
    import celery_app.tasks.agent_tasks as agent_tasks

    delivered: list[int] = []
    monkeypatch.setattr(
        agent_tasks.run_agent_task,
        "delay",
        lambda task_id: delivered.append(task_id),
        raising=False,
    )
    return delivered


def _parsed(metadata: Any) -> dict[str, Any]:
    return json.loads(metadata) if metadata and metadata != "null" else {}


# ── 1. site one: the 6-hourly `_ads_stripe_sync` ──────────────────────────────────────────────


def test_the_ads_stripe_sync_attaches_the_failed_payments_to_the_finance_task(queue_db, monkeypatch):
    from celery_app.tasks.agent_tasks import _ads_stripe_sync

    _seed_failed_payment(queue_db, "evt_t14b_sync")
    monkeypatch.setattr("celery.group", _Group())

    result = queue_db.run(_ads_stripe_sync())

    assert result["status"] == "queued", result
    by_agent = _metadata_by_agent(queue_db)
    assert set(by_agent) == {"ads_management", "finance"}
    attached = _parsed(by_agent["finance"]).get("failed_payments")
    assert attached, "the finance task must carry the records its refund path needs"
    assert attached[0]["payment_intent_id"] == PI_FROM_CHARGE
    assert attached[0]["event_id"] == "evt_t14b_sync"
    # V15 re-pin (r-repair unit, 4-03): the finance task ALSO carries the Stripe revenue-facts
    # attach (H3) merged into the SAME metadata dict — `failed_payments` is never replaced.
    assert "revenue_facts" in _parsed(by_agent["finance"])
    # V15 re-pin (r-repair unit, 4-02): the ads task carries its ad-metrics attach (H2) — the
    # configured platform's envelope or its named refusal, never nothing.
    assert "ad_metrics" in _parsed(by_agent["ads_management"])


def test_the_ads_stripe_sync_leaves_metadata_untouched_when_no_records_exist(queue_db, monkeypatch):
    """V15 re-pin (r-repair unit): an empty `failed_payments` read still attaches NOTHING for
    that key — but this site now always attaches the two H2/H3 producer envelopes (`ad_metrics`
    on the ads task; `revenue_facts` on finance), so the old "all metadata NULL" assertion
    narrowed to exactly that empty-read contract for `failed_payments`."""
    from celery_app.tasks.agent_tasks import _ads_stripe_sync

    monkeypatch.setattr("celery.group", _Group())

    result = queue_db.run(_ads_stripe_sync())

    assert result["status"] == "queued", result
    by_agent = _metadata_by_agent(queue_db)
    assert set(by_agent) == {"ads_management", "finance"}
    assert "failed_payments" not in _parsed(by_agent["finance"]), (
        "an empty read attaches NOTHING — `failed_payments` stays exactly what this site "
        "always wrote for it"
    )
    assert set(_parsed(by_agent["finance"])) == {"revenue_facts"}
    assert "ad_metrics" in _parsed(by_agent["ads_management"])


# ── 2. site two: the manual "Run Now" trigger ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_manual_finance_trigger_attaches_the_failed_payments(
    queue_db, trigger_client, auth_headers, monkeypatch
):
    _seed_failed_payment(queue_db, "evt_t14b_manual")
    delivered = _install_delay_recorder(monkeypatch)

    resp = await trigger_client.post("/api/v1/agents/finance/trigger", headers=auth_headers)

    assert resp.status_code == 202, resp.text
    task_id = resp.json()["task_id"]
    assert delivered == [task_id]
    row = queue_db.row(task_id)
    assert row["source"] == "manual"
    attached = _parsed(row["metadata"]).get("failed_payments")
    assert attached, "the owner's ad-hoc finance run must see the same records"
    assert attached[0]["payment_intent_id"] == PI_FROM_CHARGE
    assert attached[0]["event_id"] == "evt_t14b_manual"


@pytest.mark.asyncio
async def test_the_manual_finance_trigger_leaves_metadata_untouched_when_no_records_exist(
    queue_db, trigger_client, auth_headers, monkeypatch
):
    _install_delay_recorder(monkeypatch)

    resp = await trigger_client.post("/api/v1/agents/finance/trigger", headers=auth_headers)

    assert resp.status_code == 202, resp.text
    row = queue_db.row(resp.json()["task_id"])
    assert row["metadata"] in (None, "null"), "an empty read attaches nothing"


@pytest.mark.asyncio
async def test_other_agents_never_get_the_attachment_even_with_records_on_disk(
    queue_db, trigger_client, auth_headers, monkeypatch
):
    _seed_failed_payment(queue_db, "evt_t14b_other")
    _install_delay_recorder(monkeypatch)

    resp = await trigger_client.post("/api/v1/agents/social_media/trigger", headers=auth_headers)

    assert resp.status_code == 202, resp.text
    row = queue_db.row(resp.json()["task_id"])
    assert row["metadata"] in (None, "null"), "only the finance run consumes the records"


# ── 3. the consumer: the finance agent's volatile context receives them ────────────────────────


def _run_finance(task: dict) -> tuple[dict, dict]:
    """Drive the REAL `FinanceAgent.run()`; capture what it passed and what the model saw."""
    from app.agents.finance.agent import FinanceAgent

    agent = FinanceAgent()
    captured: dict[str, Any] = {}
    real_collect = agent.collect_structured
    real_call = agent.call_claude_structured

    def collect(task_, context_, **kw):
        captured["extra_context"] = kw.get("extra_context")
        payload, assembled = real_collect(task_, context_, **kw)
        captured["assembled"] = assembled
        return payload, assembled

    def call(prompt, **kw):
        captured["prompt"] = prompt
        captured["system"] = kw.get("system_prompt_static", "")
        return real_call(prompt, **kw)

    agent.collect_structured = collect
    agent.call_claude_structured = call
    return agent.run(task, {}), captured


def test_the_records_attached_by_the_sync_reach_the_finance_runs_context(
    queue_db, monkeypatch
):
    """The site-to-agent leg end to end: `_ads_stripe_sync`'s attach -> the finance agent's run
    renders the `pi_…` id into the quarantined VOLATILE context (never the stable prefix)."""
    from celery_app.tasks.agent_tasks import _ads_stripe_sync

    monkeypatch.setenv("CLAUDE_CLI_MOCK", "true")
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE",
        json.dumps({"result": json.dumps({"summary": "saw the failed payment"})}),
    )
    _seed_failed_payment(queue_db, "evt_t14b_context")
    monkeypatch.setattr("celery.group", _Group())
    queue_db.run(_ads_stripe_sync())

    rows = queue_db.query("SELECT id, title, metadata FROM tasks WHERE agent_type = 'finance'")
    assert len(rows) == 1
    task = {"id": rows[0]["id"], "title": rows[0]["title"], "metadata": _parsed(rows[0]["metadata"])}

    _payload, captured = _run_finance(task)

    assert captured["extra_context"], "the records must reach the prompt's extra_context layer"
    assert PI_FROM_CHARGE in captured["extra_context"]
    assembled = captured["assembled"]
    assert '<untrusted source="stripe"' in assembled.volatile_suffix
    assert PI_FROM_CHARGE in captured["prompt"]
    assert PI_FROM_CHARGE not in captured["system"], "per-run material must not enter the prefix"
