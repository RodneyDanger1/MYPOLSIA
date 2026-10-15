"""R30 — the email producer at the WORKER level: real `_execute_agent_task` on a file-backed DB.

Same harness as `test_cs_send_cap_producer.py` / `test_send_cap_producer_worker_e2e.py`: the real
worker body (`queue_db` + `execute_body` fixtures), the real agent, the real policy firewall (the
worker injects the real `channel_sends` snapshot), the real adapter (`integrations.send_email` ->
`_sendgrid_send`), and the real producer (`_persist_run_artifacts` -> `record_sent_emails`).
Only the CLI seam (`collect_structured`) and the provider SDK (a fake `sendgrid` module whose
response mirrors SendGrid's — 202, with or without `X-Message-Id`) are doubles.

Pins the two R30 fixes where they must ultimately hold — the production call chain:

  * M3 — a performed send the provider gave no id for is NOT counted and the gap is NAMED on the
    run output (`uncounted_sends`); it is never folded onto the HTTP status code.
  * M5 — a batch whose first performed send carries a malformed address is skipped BY NAME
    (`skipped_malformed` on the run output + the worker's existing `run_artifacts_not_persisted`
    warning path, honoured by real activity rows) and the send AFTER it is still counted.

RED legs (receipt): restore `... or status_code ...` in the agent -> leg 1 fails (id "202", one
collapsed row); restore the batch-abort in `record_sent_emails` -> leg 2's `email_logs` count
stays 0 and the run output carries no `skipped_malformed`.
"""
from __future__ import annotations

import json
import sys
import types
from datetime import UTC, datetime
from typing import Any

import app.config as app_config
from app.agents.email_outreach.agent import EmailOutreachAgent

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


def _latest_run_output(queue_db) -> dict[str, Any]:
    """The last run row's stored output, read from the FILE (stdlib driver, no ORM)."""
    rows = queue_db.query("SELECT output FROM agent_runs ORDER BY id DESC LIMIT 1")
    if not rows or not rows[0]["output"]:
        return {}
    return json.loads(rows[0]["output"])


class _FakeSendGrid:
    """Provider double: 202 + a UNIQUE `X-Message-Id` per send — or NO header when told to."""

    def __init__(self, *, send_ids: bool = True) -> None:
        self.sent_message_ids: list[str] = []
        self._send_ids = send_ids

    def install(self, monkeypatch: Any) -> _FakeSendGrid:
        fake = self

        class _Response:
            def __init__(self, message_id: str) -> None:
                self.status_code = 202
                self.headers = {"X-Message-Id": message_id} if fake._send_ids else {}

        class _Client:
            def __init__(self, api_key: str) -> None:
                self.api_key = api_key

            def send(self, message: Any) -> _Response:
                message_id = f"sg-msg-{len(fake.sent_message_ids) + 1}"
                fake.sent_message_ids.append(message_id)
                return _Response(message_id)

        sendgrid_mod = types.ModuleType("sendgrid")
        helpers = types.ModuleType("sendgrid.helpers")
        mail_mod = types.ModuleType("sendgrid.helpers.mail")
        sendgrid_mod.SendGridAPIClient = _Client            # type: ignore[attr-defined]
        mail_mod.Mail = lambda **kw: {"mail": kw}            # type: ignore[attr-defined]
        helpers.mail = mail_mod                              # type: ignore[attr-defined]
        sendgrid_mod.helpers = helpers                       # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "sendgrid", sendgrid_mod)
        monkeypatch.setitem(sys.modules, "sendgrid.helpers", helpers)
        monkeypatch.setitem(sys.modules, "sendgrid.helpers.mail", mail_mod)
        return fake


#: V11/W1: the worker-resolved compliance preflight a real `email_outreach` run carries — the
#: same `channel_sends` pattern (the worker owns the DB, the agent does not). The worker leg
#: hands it through the context the run assembles, so the sends below are compliant like every
#: other real send. No assertion changed.
_COMPLIANCE = {"company": {"sender_legal_name": "Acme Robotics LLC",
                          "sender_postal_address": "123 Main St, Minneapolis, MN 55401, USA",
                          "website_url": "https://acme.example"},
               "suppressed": []}


def _arm_email(monkeypatch, payload: dict[str, Any], *, send_ids: bool) -> _FakeSendGrid:
    """Real send path for the worker leg: no sandbox, configured provider, pinned clock."""
    monkeypatch.setattr(app_config.settings, "company_timezone", "UTC")
    monkeypatch.setattr(app_config.settings, "sandbox_mode", False)
    monkeypatch.setattr(app_config.settings, "sendgrid_api_key", "SG.test-key")
    monkeypatch.setattr(app_config.settings, "sendgrid_from_email", "ops@example.com")
    monkeypatch.setattr("app.services.channel_service.now_utc", lambda: NOW)
    # V11/W1: the compliance preflight rides the context the run assembles — exactly where the
    # worker hook adds it next to `_with_channel_sends`. The real builder is still used.
    from app.services.company_service import get_full_context as _real_full_context  # noqa: E501

    async def _with_compliance(*args, **kwargs):
        assembled = await _real_full_context(*args, **kwargs)
        assembled["compliance"] = _COMPLIANCE
        return assembled

    # the worker resolves it at CALL time (`from app.services.company_service import
    # get_full_context` inside `_execute_agent_task`), so this is the name to patch.
    monkeypatch.setattr("app.services.company_service.get_full_context", _with_compliance)
    monkeypatch.setattr("app.services.outreach_service.utcnow", lambda: NOW)
    monkeypatch.setattr(
        EmailOutreachAgent,
        "collect_structured",
        lambda self, task, context, **kw: (dict(payload), None),
    )
    return _FakeSendGrid(send_ids=send_ids).install(monkeypatch)


def _email_task(queue_db) -> int:
    return queue_db.create_task(
        agent_type="email_outreach", title="Send the outreach sequence"
    )


def test_a_no_provider_id_send_is_named_on_the_run_and_never_counted(monkeypatch, queue_db,
                                                                     execute_body):
    """M3 through the worker: the send really happened, no id came back, the run names the gap."""
    _arm_email(monkeypatch, {"action": "send", "emails": [
        {"prospect_email": "prospect0@example.com", "subject": "s0", "body": "b0"},
    ]}, send_ids=False)

    outcome = execute_body(_email_task(queue_db))
    assert outcome["status"] == "completed"

    out = _latest_run_output(queue_db)
    assert out["emails"][0]["status"] == "sent"
    assert out["emails"][0]["sendgrid_id"] == ""               # a status code is not an id
    assert out["uncounted_sends"] == [{
        "reason": "no_provider_id",
        "prospect_email": "prospect0@example.com",
        "provider_status": 202,
    }]
    assert queue_db.count("email_logs") == 0                   # WAS 1: the collapsed "202" row
    assert out["persistence"]["emails_written"] == 0
    assert queue_db.count("activity_log", "action = ?", ("run_artifacts_not_persisted",)) == 0


def test_a_malformed_head_item_is_skipped_and_the_send_after_it_still_counts(
    monkeypatch, queue_db, execute_body,
):
    """M5 through the worker: both sends really happened; the malformed one is skipped BY NAME."""
    fake = _arm_email(monkeypatch, {"action": "send", "emails": [
        {"prospect_email": "no-at-sign", "subject": "s0", "body": "b0"},
        {"prospect_email": "prospect1@example.com", "subject": "s1", "body": "b1"},
    ]}, send_ids=True)

    outcome = execute_body(_email_task(queue_db))
    assert outcome["status"] == "completed"                    # the run succeeded; the persistence
    assert fake.sent_message_ids == ["sg-msg-1", "sg-msg-2"]   # leg skipped one item, by name

    assert queue_db.count("email_logs") == 1                   # WAS 0: the batch aborted at item 0
    row = queue_db.query("SELECT * FROM email_logs")[0]
    assert (row["sendgrid_id"], row["status"]) == ("sg-msg-2", "sent")

    out = _latest_run_output(queue_db)
    persisted = out["persistence"]
    assert persisted["ok"] is False                            # the skip is never silent...
    assert persisted["emails_written"] == 1                    # ...but the later send IS counted
    assert persisted["skipped_malformed"] == [{
        "index": 0,
        "prospect_email": "no-at-sign",
        "sendgrid_id": "sg-msg-1",
        "code": "E_VALIDATION",
        "message": "prospect email 'no-at-sign' is not a usable address",
    }]
    # the worker's existing warning path fired for the partial persistence (real activity row)
    assert queue_db.count("activity_log", "action = ?", ("run_artifacts_not_persisted",)) == 1
    assert queue_db.count("prospects", "email = ?", ("prospect1@example.com",)) == 1

    # usable afterwards: a follow-up run on the same DB still sends and counts
    _arm_email(monkeypatch, {"action": "send", "emails": [
        {"prospect_email": "prospect2@example.com", "subject": "s2", "body": "b2"},
    ]}, send_ids=True)
    second = execute_body(_email_task(queue_db))
    assert second["status"] == "completed"
    assert queue_db.count("email_logs") == 2
