"""The email_outreach agent — prospect finding, cold sequences, follow-ups.

Duty (README:34): "Prospect finding + cold email sequences". Writes `prospects` (unique email),
`email_campaigns` and `email_logs` (sequence_step default 1, status default `sent` — which is
exactly why a sandboxed run must write `draft` instead).

Output (lane 05 `EmailOutreachOutput` + lane 03's `follow_ups_scheduled`): `{summary, action,
prospects[], emails[], follow_ups_scheduled}`.

Sandbox semantics: while `SANDBOX_MODE=true` every send is REFUSED (`E_SANDBOX_BLOCKED`) and each
email is recorded with `status="draft"` and `sendgrid_id=null` plus the `sandbox`/`drafted_not_sent`
markers; the same is true for the un-subscribe-safe follow-up scheduling. Only an operator with
`SANDBOX_MODE=false` reaches `integrations.send_email`, which either sends or refuses by name.
"""
from __future__ import annotations

from typing import Any

from app.agents import integrations
from app.agents.base_agent import BasePolsiaAgent
from app.agents.policy_firewall import ActionClass
from app.agents.side_effects import request_side_effect, side_effect_receipt
from app.config import settings
from app.schemas.agent_outputs import EmailOutreachOutput

ROLE = """You are the Email Outreach agent of an autonomous company. You find prospects that fit
the company's ideal customer profile and write the first email of a sequence.

Rules for prospects (return at most 25):
- One entry per real, business email address you can justify from the context you were given (a
  prior list, a public company page, a directory). NEVER invent an address, a name or a title.
- `source` says where the contact came from. An entry without a source is not usable.

Rules for emails (at most 10, one per prospect):
- Under 120 words. One specific reason you are writing to THIS company, one sentence on what you
  do that is relevant to them, one low-friction question. No "I hope this finds you well".
- `subject`: <= 60 characters, lowercase, no emoji, no fake "Re:" — it must be true.
- No false urgency, no invented results, no claims about the prospect you cannot see in the
  context. Compliance matters: if the context shows no unsubscribe path, note it and do not send.
- `sequence_step` is 1 for a first touch. Say in `follow_ups_scheduled` how many follow-ups this
  batch implies (0 when none).

Set `action` to "find" (prospects only), "draft" (emails written, nothing sent), "send" (publish
this batch) or "noop" when there is nothing worth sending.

Every send goes through the company's policy firewall and the channel's daily/hourly caps. If an
action is refused you will be told the reason; your output is then recorded as a labelled draft
(status="draft", no provider id). Never write an id such as a SendGrid message id yourself, and
never claim an email was delivered when the runtime did not report a real send."""


class EmailOutreachAgent(BasePolsiaAgent):
    agent_type = "email_outreach"
    description = "Finds prospects and sends cold emails"
    output_schema = EmailOutreachOutput
    max_turns = 30
    role_prompt = ROLE

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        payload, assembled = self.collect_structured(task, context)
        payload["agent_type"] = self.agent_type
        action = str(payload.get("action") or "draft")
        counts = ((context or {}).get("channel_sends") or {}).get("email") or {}
        sends_today = int(counts.get("today") or 0)
        sends_hour = int(counts.get("hour") or 0)
        receipts: list[dict[str, Any]] = []

        for index, email in enumerate(list(payload.get("emails") or [])):
            to = str(email.get("prospect_email") or "")
            outcome = request_side_effect(
                self.agent_type,
                ActionClass.EXTERNAL_REVERSIBLE,
                "email",
                action=action,
                payload={"action": action, "subject": str(email.get("subject") or "")[:200],
                         "to_domain": to.split("@")[-1] if "@" in to else ""},
                sends_today=sends_today,
                sends_this_hour=sends_hour,
            )
            receipt = side_effect_receipt(outcome, action=action, ref=to)
            if outcome.allowed:                                  # only outside sandbox
                result = integrations.send_email(
                    to=to, subject=str(email.get("subject") or ""), body=str(email.get("body") or "")
                )
                receipt["integration"] = result
                if result.get("ok"):
                    email["status"] = "sent"
                    email["sendgrid_id"] = str((result.get("result") or {}).get("message_id")
                                               or (result.get("result") or {}).get("status_code") or "")
                else:
                    email["status"] = "failed"
                    payload.setdefault("refusals", []).append(result)
            else:
                # Refused (sandbox / cap / approval): the honest representation is a draft.
                email["status"] = "draft"
                email["sendgrid_id"] = None
                if outcome.sandboxed:
                    payload["sandbox"] = True
                payload["drafted_not_sent"] = True
            receipts.append(receipt)

        payload["side_effects"] = receipts
        payload.setdefault("refusals", [])
        payload.setdefault("prospects", [])
        payload.setdefault("emails", [])
        payload.setdefault("follow_ups_scheduled", 0)
        if settings.sandbox_mode:
            payload["sandbox"] = True
            payload["drafted_not_sent"] = True
            for email in payload["emails"]:
                email["sendgrid_id"] = None
                if email.get("status") == "sent":
                    email["status"] = "draft"
        payload["action"] = action
        return self.attach_run_meta(payload, assembled=assembled)


__all__ = ["ROLE", "EmailOutreachAgent"]
