"""RD14 follow-up — the customer_support leg of the send-cap producer E2E.

Drives the REAL `_execute_agent_task` body on the file-backed `queue_db` (same harness as
`test_send_cap_producer_worker_e2e.py`), so every step is production code: the CS agent run,
the policy firewall (email cap via the worker's real snapshot), the new CS producer branch
(`_persist_run_artifacts` -> `record_sent_emails`), and the email_logs rows it writes.

WHY this lane: CS replies send through the same SendGrid path as email_outreach, but nothing
persisted them — the email channel counter undercounted every reply (RD14 receipt §7 REQUESTS 1).

RED leg (observed): the CS branch in `_persist_run_artifacts` removed (or the agent's
`reply["sendgrid_id"]` capture dropped) -> leg 1's email_logs count stays 0 and the cap leg's
second run is never refused; restore sha256-verified -> green.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import app.config as app_config
from app.agents.customer_support.agent import CustomerSupportAgent

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)
TWO_HOURS_AGO = NOW - timedelta(hours=2)

#: The email channel's day/hour caps are policy (policy_firewall RateCaps): email 50/day, 15/hour.
EMAIL_DAY_CAP = 50

INBOX = {
    "id": "msg-1",
    "from": "cust@example.com",
    "subject": "Billing question",
    "body": "How do I update my card on file?",
}


def _latest_run_output(queue_db) -> dict[str, Any]:
    """The last run row's stored output, read from the FILE (stdlib driver, no ORM)."""
    rows = queue_db.query("SELECT output FROM agent_runs ORDER BY id DESC LIMIT 1")
    if not rows or not rows[0]["output"]:
        return {}
    return json.loads(rows[0]["output"])


def _pin_clock(monkeypatch, holder: dict[str, datetime]) -> None:
    patch = monkeypatch.setattr
    patch("app.services.channel_service.now_utc", lambda: NOW)
    patch("app.services.outreach_service.utcnow", lambda: holder["now"])


def _arm_cs(monkeypatch, holder: dict[str, datetime]) -> list[dict[str, Any]]:
    """Real CS run, provider call double only; returns the provider-call recorder."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    _pin_clock(monkeypatch, holder)

    calls: list[dict[str, Any]] = []

    def fake_send_email(*, to: str, subject: str, body: str) -> dict[str, Any]:
        calls.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "result": {"status_code": 202, "message_id": f"sg-cs-{len(calls)}"}}

    monkeypatch.setattr("app.agents.integrations.send_email", fake_send_email)
    monkeypatch.setattr(
        CustomerSupportAgent,
        "collect_structured",
        lambda self, task, context, **kw: (
            {"summary": "one reply", "replies": [{
                "message_ref": "msg-1",
                "from_address": "cust@example.com",
                "subject": "Billing question",
                "reply_body": "You can update it under Settings → Billing.",
                "intent": "billing",
                "sentiment": "neutral",
                "needs_human": False,
            }]},
            None,
        ),
    )
    return calls


def _seed_email_logs(queue_db, count: int) -> dict[str, Any]:
    """Seed `count` sent-email rows through the REAL producer (email_logs is the email counter)."""
    from app.services.outreach_service import record_sent_emails

    async def _seed() -> dict[str, Any]:
        async with queue_db.factory() as db:
            report = await record_sent_emails(db, {"emails": [
                {
                    "prospect_email": f"seed{i}@example.com",
                    "subject": "seed",
                    "body": "seed",
                    "status": "sent",
                    "sendgrid_id": f"seed-{i}",
                }
                for i in range(count)
            ]})
            await db.commit()
            return report

    return queue_db.run(_seed())


def _cs_task(queue_db) -> int:
    return queue_db.create_task(
        agent_type="customer_support",
        title="Reply to the inbox item",
        metadata={"inbox_item": dict(INBOX)},
    )


def test_a_real_cs_send_is_counted_then_the_next_one_refuses_at_the_cap(
    monkeypatch, queue_db, execute_body
):
    """49 seeded + run 1 counted (50) -> run 2 refused by the EXISTING caps, no provider call."""
    holder = {"now": TWO_HOURS_AGO}
    calls = _arm_cs(monkeypatch, holder)

    assert _seed_email_logs(queue_db, EMAIL_DAY_CAP - 1)["emails_written"] == EMAIL_DAY_CAP - 1
    assert queue_db.count("email_logs") == EMAIL_DAY_CAP - 1

    # run 1 — under the cap: completes, really sends, and the worker counts it
    holder["now"] = NOW
    first = execute_body(_cs_task(queue_db))
    assert first["status"] == "completed"
    assert len(calls) == 1
    assert queue_db.count("email_logs") == EMAIL_DAY_CAP
    assert _latest_run_output(queue_db)["persistence"]["emails_written"] == 1

    # run 2 — the reply sees 50/50 and is refused BY THE EXISTING CAPS, before any send; the
    # run fails with the named refusal (the same contract the social worker leg proves).
    second = execute_body(_cs_task(queue_db))
    assert second["status"] == "failed"
    assert "E_RATE_LIMITED" in (second["error"] or "")
    assert "50/50" in (second["error"] or "")
    assert len(calls) == 1, "the refused attempt never reached the provider"
    assert queue_db.count("email_logs") == EMAIL_DAY_CAP


def test_a_failed_cs_send_adds_no_row(monkeypatch, queue_db, execute_body):
    """The producer's honest gate: a failed send is marked failed and writes nothing."""
    holder = {"now": NOW}
    _arm_cs(monkeypatch, holder)

    def failing_send_email(*, to: str, subject: str, body: str) -> dict[str, Any]:
        return {"ok": False, "error": {"code": "E_INTEGRATION", "message": "provider down"}}

    monkeypatch.setattr("app.agents.integrations.send_email", failing_send_email)

    outcome = execute_body(_cs_task(queue_db))

    assert queue_db.count("email_logs") == 0, "a failed send must not fake a send row"
    assert _latest_run_output(queue_db)["persistence"]["emails_written"] == 0
    assert outcome["status"] in ("completed", "failed")  # the run itself is honest either way
