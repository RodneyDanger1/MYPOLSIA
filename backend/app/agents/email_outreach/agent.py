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

Run-action semantics (L3-02): only `action="send"` attempts a send; `"find"` (prospects only),
`"draft"` (emails written, nothing sent) and `"noop"` runs record every email as a labelled draft
and never touch the firewall or the provider. The channel caps keep their check-and-consume
semantics inside a batch (in-run `performed` counter, L3-05).
"""
from __future__ import annotations

import logging
from typing import Any

from app.agents import integrations
from app.agents.base_agent import BasePolsiaAgent
from app.agents.policy_firewall import ActionClass
from app.agents.side_effects import capped_item_refusal, request_side_effect, side_effect_receipt
from app.config import settings
from app.core.errors import E_RATE_LIMITED, PolsiaError
from app.schemas.agent_outputs import EmailOutreachOutput

logger = logging.getLogger(__name__)

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
        # int | None: None = "no local record of sends" -> the firewall refuses the send on a
        # capped channel (fail closed); it must never be collapsed to 0 here.
        counts = ((context or {}).get("channel_sends") or {}).get("email") or {}
        sends_today = counts.get("today")
        sends_hour = counts.get("hour")
        receipts: list[dict[str, Any]] = []
        # L3-02: only "send" attempts an external send. "find" (prospects only), "draft" (emails
        # written, nothing sent) and "noop" record each email as the labelled draft the sandbox
        # branch already produces — no firewall call, no provider, no cap slot consumed.
        sending = action == "send"
        # L3-05: what THIS run has really performed on the email channel (successful provider
        # sends only). The caps are check-and-consume; the snapshot is read once per run, so the
        # firewall must see snapshot + performed or one batch overshoots the day/hour cap.
        performed = 0

        for _index, email in enumerate(list(payload.get("emails") or [])):
            if not sending:
                email["status"] = "draft"
                email["sendgrid_id"] = None
                payload["drafted_not_sent"] = True
                continue
            to = str(email.get("prospect_email") or "")
            sends_today_now = None if sends_today is None else sends_today + performed
            sends_hour_now = None if sends_hour is None else sends_hour + performed
            try:
                outcome = request_side_effect(
                    self.agent_type,
                    ActionClass.EXTERNAL_REVERSIBLE,
                    "email",
                    action=action,
                    payload={"action": action, "subject": str(email.get("subject") or "")[:200],
                             "to_domain": to.split("@")[-1] if "@" in to else ""},
                    sends_today=sends_today_now,
                    sends_this_hour=sends_hour_now,
                )
            except PolsiaError as exc:
                # A cap denial with nothing performed yet stays fatal; after this run performed a
                # real send it is recorded and the email stays a draft (see
                # `capped_item_refusal`). Anything that is not a cap denial re-raises unchanged.
                if exc.code != E_RATE_LIMITED or performed == 0:
                    raise
                receipt, refusal = capped_item_refusal(
                    exc, agent_type=self.agent_type, action=action, ref=to, channel="email",
                )
                receipts.append(receipt)
                payload.setdefault("refusals", []).append(refusal)
                email["status"] = "draft"
                email["sendgrid_id"] = None
                payload["drafted_not_sent"] = True
                continue
            receipt = side_effect_receipt(outcome, action=action, ref=to)
            if outcome.allowed:                                  # only outside sandbox
                result = integrations.send_email(
                    to=to, subject=str(email.get("subject") or ""), body=str(email.get("body") or "")
                )
                receipt["integration"] = result
                if result.get("ok"):
                    email["status"] = "sent"
                    performed += 1
                    # RD30/RD14: the cap's dedupe key is the provider's OWN per-message id — the
                    # SAME capture as the CS producer (customer_support/agent.py) so the two
                    # cannot drift apart again. A status code is not an id: the old
                    # `message_id or status_code` fallback keyed every no-header send on "202",
                    # so distinct real sends collapsed onto ONE counted row (R27 M3, measured).
                    sendgrid_id = str((result.get("result") or {}).get("message_id") or "")
                    email["sendgrid_id"] = sendgrid_id
                    if not sendgrid_id:
                        # Performed but provably uncountable: name the gap — never fake a key
                        # (record_sent_emails refuses items without a real provider id) and
                        # never stay silent about the undercount it causes.
                        logger.warning(
                            "code=email_sent_without_provider_id: prospect=%s performed but "
                            "the provider returned no X-Message-Id; record_sent_emails will "
                            "not count this send (email cap undercount is named, not silent)",
                            to,
                        )
                        payload.setdefault("uncounted_sends", []).append({
                            "reason": "no_provider_id",
                            "prospect_email": to,
                            "provider_status": (result.get("result") or {}).get("status_code"),
                        })
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
