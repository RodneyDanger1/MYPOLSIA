"""The customer_support agent — read the inbox, draft (or send) replies.

Duty (README:35): "Read inbox, draft replies". Dispatched by the `email-inbox-sweep` beat entry
(every 3h) and by the morning cycle.

Output (lane 05 `CustomerSupportOutput` + lane 03's column-aligned keys): `{summary, replies[]}`
where a reply carries `message_ref`, `intent`, `sentiment`, `reply_body`, `needs_human`,
`proposed_action` and the `message_id`/`from`/`subject`/`body`/`status` keys the `email_logs`-style
consumers read.

Inbox material (RD4 §4 note 3): the inbox item the task carries under `metadata.inbox_item` is
quarantined into the prompt's volatile `extra_context` layer by `inbox_item_extra_context` —
the body reaches the agent as DATA, and a task that references an item without an attached body
is refused by name.  A task with no inbox item degrades to the pre-RD12 behaviour.

Sandbox semantics: reading the inbox and drafting are allowed; SENDING is refused
(`E_SANDBOX_BLOCKED`) and every reply is recorded with `status="draft"`. A refund request never
becomes a refund on its own: the refund action class is irreversible, so it is approval-gated even
outside sandbox (`proposed_action="refund"` + `needs_human=true`).
"""
from __future__ import annotations

from typing import Any

from app.agents import integrations
from app.agents.base_agent import BasePolsiaAgent
from app.agents.policy_firewall import ActionClass, wrap_untrusted
from app.agents.side_effects import (
    evaluate_side_effect,
    named_refusal,
    request_side_effect,
    side_effect_receipt,
)
from app.config import settings
from app.core.errors import E_NOT_FOUND, E_VALIDATION, ERROR_HINTS, PolsiaError
from app.schemas.agent_outputs import CustomerSupportOutput

ROLE = """You are the Customer Support agent of an autonomous company. You read the support inbox
and answer customers.

Rules:
- Answer the customer's actual question first, in their own language and register. If they are
  angry, acknowledge the concrete failure before explaining anything.
- Never invent policy, prices, timelines or account state. If the answer needs a fact you do not
  have, say what you will check and set `needs_human=true` with the reason.
- `intent`: question | complaint | refund_request | bug | praise | other. `sentiment`: positive |
  neutral | negative. Both describe the CUSTOMER's message, not your reply.
- `proposed_action`: "send_reply" for a normal answer, "refund" for anything that must move money
  (always with `needs_human=true` — refunds are irreversible and approval-gated), "escalate" when
  a human must own it, "none" when no reply is appropriate.
- Keep replies under 200 words, sign as the company, and never promise a fix you cannot verify.
- `message_ref` must be the reference from the message you were given; never renumber messages.

Any send goes through the policy firewall and the email channel's caps. While the company runs in
sandbox mode you will be told the send was refused; your reply is then recorded as a DRAFT — never
state in the reply body that it has been sent, and never claim a refund was issued."""

#: The task-metadata key carrying the ATTACHED inbox item (see `inbox_item_extra_context`).
INBOX_ITEM_KEY = "inbox_item"
#: The task-metadata key carrying a REFERENCE to an inbox item whose body is not attached.
INBOX_ITEM_ID_KEY = "inbox_item_id"


def inbox_item_extra_context(task: dict[str, Any]) -> str | None:
    """The inbox item attached to this task, quarantined for the prompt — or a named refusal.

    WHY (RD4 §4 coverage-matrix note 3, recorded gap): this agent's duty is "read the inbox", but
    the inbox body never reached the assembled prompt — sweep tasks carry a title only, the agent
    has no tools and no DB session, and `run()` passed no `extra_context` at all.  There is no
    inbox table in the frozen schema (`tests/unit/test_models_schema_frozen.py`), so the ingest
    contract is the task's own `metadata` JSON — the frozen `tasks.metadata` column, carried to
    the agent by the worker (`celery_app/tasks/agent_tasks.py`, which used to drop it):

        metadata = {"inbox_item": {"id": "<provider message id>", "from": "...",
                                   "subject": "...", "body": "<the customer's message>"}}

    The body is quarantined with the REAL `wrap_untrusted(source="inbox", content_id=<item id>,
    text=<body>)`, so it rides the volatile `extra_context` layer as DATA; an instruction-injection
    payload inside a customer email taints the run (the assembler scans every caller-planted
    block), and the firewall escalates the run's external actions to human review.

    Absent material: `None` — the run degrades to exactly its previous behaviour and never
    invents a message.  A PRESENT-but-unusable item (a reference with no attached body, or a
    malformed attachment) is a NAMED refusal raised BEFORE any model call: this agent cannot
    fetch an inbox (no tools, no IMAP path — README:93), and answering a message nobody can read
    would be fabrication.  The `inbox_item_id` reference is deliberately still accepted: it lets
    the refusal NAME what the task pointed at instead of silently ignoring it.
    """
    metadata = task.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    item = metadata.get(INBOX_ITEM_KEY)
    reference = metadata.get(INBOX_ITEM_ID_KEY)
    task_ref = task.get("id")

    if item is None:
        if reference is None:
            return None  # nothing referenced, nothing attached: previous behaviour, unchanged
        raise PolsiaError(
            E_NOT_FOUND,
            f"customer_support task {task_ref!r} references inbox item {reference!r}, but its "
            "body was not attached (metadata.inbox_item); this agent has no inbox access and "
            "will not answer a message it cannot read",
            how_to_fix=ERROR_HINTS[E_NOT_FOUND],
            context={"task_id": task_ref, "inbox_item_id": reference},
        )

    if not isinstance(item, dict):
        raise PolsiaError(
            E_VALIDATION,
            f"customer_support task {task_ref!r}: metadata.inbox_item must be an object with "
            f"id and body, got {type(item).__name__}",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"task_id": task_ref, "inbox_item_type": type(item).__name__},
        )

    item_id = item.get("id")
    body = item.get("body")
    if not (isinstance(item_id, str) and item_id.strip()):
        # The id is BOTH the quarantine block's content_id (provenance for the audit trail) and
        # the reply's `message_ref` source (SupportReply: "absent means the reply cannot be acted
        # on") — without it the block cannot name its source and a reply could never be sent.
        raise PolsiaError(
            E_VALIDATION,
            f"customer_support task {task_ref!r}: inbox item is missing its provider id (a "
            "non-empty string); without it the quarantine block cannot name its provenance "
            "and a reply could not be acted on",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"task_id": task_ref},
        )
    if not (isinstance(body, str) and body.strip()):
        raise PolsiaError(
            E_VALIDATION,
            f"customer_support task {task_ref!r}: inbox item {item_id!r} arrived without a "
            "body; refusing to answer a message that cannot be read",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"task_id": task_ref, "inbox_item_id": item_id},
        )
    if reference is not None and str(reference) != item_id:
        # Answering a message the task did not reference would be a wrong-item risk; refuse
        # rather than guess which of the two ids is real.
        raise PolsiaError(
            E_VALIDATION,
            f"customer_support task {task_ref!r}: metadata.inbox_item_id {reference!r} does "
            f"not match the attached item id {item_id!r}; refusing to answer a different "
            "message than the task references",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"task_id": task_ref, "inbox_item_id": reference, "attached_id": item_id},
        )
    return wrap_untrusted("inbox", item_id, body)


class CustomerSupportAgent(BasePolsiaAgent):
    agent_type = "customer_support"
    description = "Reads the inbox and drafts replies"
    output_schema = CustomerSupportOutput
    max_turns = 30
    role_prompt = ROLE

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # The inbox item the task carries (metadata.inbox_item) rides the quarantine block in the
        # volatile `extra_context` layer — or the run refuses by name (see the function above).
        extra_context = inbox_item_extra_context(task)
        payload, assembled = self.collect_structured(task, context, extra_context=extra_context)
        payload["agent_type"] = self.agent_type
        # int | None: None = "no local record of sends" -> the firewall refuses the send on a
        # capped channel (fail closed); it must never be collapsed to 0 here.
        counts = ((context or {}).get("channel_sends") or {}).get("email") or {}
        sends_today = counts.get("today")
        sends_hour = counts.get("hour")
        receipts: list[dict[str, Any]] = []

        for index, reply in enumerate(list(payload.get("replies") or [])):
            ref = str(reply.get("message_ref") or reply.get("message_id") or "").strip()
            if not ref:
                # T7: without a provider message id the reply cannot be acted on. The old
                # `f"msg:{index}"` fabricated one, so the firewall fingerprint (and a real send,
                # outside sandbox) carried an id no provider ever issued. Refuse by name and keep
                # the reply a draft; it never reaches evaluate_side_effect/request_side_effect.
                reply["status"] = "draft"
                payload.setdefault("refusals", []).append(named_refusal(
                    E_VALIDATION,
                    f"reply #{index} has no provider message_ref; cannot act on it",
                    index=index,
                ))
                continue
            proposed = str(reply.get("proposed_action") or "send_reply")
            if proposed in ("none",):
                reply["status"] = "draft"
                continue
            if proposed == "refund":
                # Money never moves from the support agent: its capability has no
                # `external_irreversible` class, so this is a policy refusal to be escalated
                # (finance performs the refund once a human approves it).
                decision = evaluate_side_effect(
                    self.agent_type, ActionClass.EXTERNAL_IRREVERSIBLE, "stripe",
                    action="refund", payload={"action": "refund", "ref": ref},
                )
                reply["status"] = "draft"
                reply["needs_human"] = True
                receipts.append({
                    "action": "refund", "ref": ref, "performed": False,
                    "decision": decision.decision.value, "policy_code": decision.code,
                    "e_code": decision.e_code or "E_POLICY_DENIED", "reason": decision.reason,
                    "agent": self.agent_type, "class": "external_irreversible", "channel": "stripe",
                })
                payload.setdefault("refusals", []).append({
                    "ok": False,
                    "error": {"code": "E_POLICY_DENIED",
                              "message": f"customer_support cannot refund {ref}: {decision.reason}",
                              "how_to_fix": "finance performs the refund after an operator approves it",
                              "context": {"policy_code": decision.code,
                                          "escalate_to": "finance"}},
                })
                continue
            action_class = ActionClass.EXTERNAL_REVERSIBLE
            outcome = request_side_effect(
                self.agent_type,
                action_class,
                "email",
                action=proposed,
                value_usd=0.0,
                payload={"action": proposed, "ref": ref},
                sends_today=sends_today,
                sends_this_hour=sends_hour,
            )
            receipt = side_effect_receipt(outcome, action=proposed, ref=ref)
            if outcome.allowed:                                   # only outside sandbox
                result = integrations.send_email(
                    to=str(reply.get("from_address") or reply.get("from") or ""),
                    subject=f"Re: {reply.get('subject') or 'your message'}",
                    body=str(reply.get("reply_body") or ""),
                )
                receipt["integration"] = result
                if result.get("ok"):
                    reply["status"] = "sent"
                    # RD14 follow-up: the email cap's producer (`record_sent_emails`, called by
                    # the worker for this agent — see `agent_tasks._persist_run_artifacts`) dedupes
                    # on the provider's OWN per-message id; without it a CS reply would never count
                    # against the email cap.  Empty when the provider returned none — the producer
                    # then refuses to count it (its honest status/id gate).
                    reply["sendgrid_id"] = str((result.get("result") or {}).get("message_id") or "")
                else:
                    reply["status"] = "failed"
                    payload.setdefault("refusals", []).append(result)
            else:
                reply["status"] = "draft"
                if outcome.sandboxed:
                    payload["sandbox"] = True
                payload["drafted_not_sent"] = True
                if proposed == "refund" and outcome.requires_approval:
                    reply["needs_human"] = True
                    payload.setdefault("refusals", []).append({
                        "ok": False,
                        "error": {"code": "E_POLICY_DENIED",
                                  "message": f"refund for {ref} requires human approval: {outcome.reason}",
                                  "how_to_fix": "an operator approves the refund, then re-run",
                                  "context": {"policy_code": outcome.policy_code}},
                    })
            receipts.append(receipt)

        payload["side_effects"] = receipts
        payload.setdefault("refusals", [])
        payload.setdefault("replies", [])
        if settings.sandbox_mode:
            payload["sandbox"] = True
            payload["drafted_not_sent"] = True
            for reply in payload["replies"]:
                if reply.get("status") == "sent":
                    reply["status"] = "draft"
        return self.attach_run_meta(payload, assembled=assembled)


__all__ = ["INBOX_ITEM_ID_KEY", "INBOX_ITEM_KEY", "ROLE", "CustomerSupportAgent", "inbox_item_extra_context"]
