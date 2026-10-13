"""The finance agent — Stripe revenue sync, expenses and refunds.

Duty (README:38): "Stripe revenue sync, spend tracking". Dispatched by `ads-stripe-sync`
(every 6h) and by the morning cycle's `priority=1` "Daily revenue snapshot" task.

Output (lane 05 `FinanceOutput` + lane 03's snapshot/failed-payment keys): `{summary, mrr_cents,
arr_cents, active_subscribers, churned_today, new_today, stripe_balance_cents, snapshot{},
expenses[], failed_payments[], alerts[], snapshot_written}` — one key per column of
`revenue_snapshots`, `expense_records` and `stripe_events`.  A revenue field the model did not
measure stays `None`/absent and is OMITTED from the snapshot — never zero-filled, because
"omitted" and "measured 0" are different states.

Idempotency: `revenue_snapshots.snapshot_date` is UNIQUE, so the snapshot is an upsert keyed by
date.  `snapshot_written` in this payload is always `False`: the WRITE decides it — the worker
sets it True only when `finance_service.persist_finance_run`'s upsert really returned a row, so
an attempted write can never wear a real one and the second morning run of the day cannot
create a duplicate row.

Sandbox semantics: balance/subscription reads and the snapshot/expense writes are allowed; a
REFUND is `external_irreversible` and is refused (`E_SANDBOX_BLOCKED`) while sandboxed, and
approval-gated when it is not (`refunds_performed` stays 0 unless a token was verified).
"""
from __future__ import annotations

from typing import Any

from app.agents import integrations
from app.agents.base_agent import BasePolsiaAgent
from app.agents.policy_firewall import ActionClass, wrap_untrusted
from app.agents.side_effects import request_side_effect, side_effect_receipt
from app.config import settings
from app.core.errors import E_VALIDATION, PolsiaError
from app.core.timeutil import today_utc
from app.schemas.agent_outputs import FinanceOutput

#: The task-metadata key carrying the failed-payment records this run may act on (see
#: `failed_payments_extra_context`).  The producer is the worker-side attach: the morning cycle
#: fills it from `finance_service.recent_failed_payments` — the records the Stripe webhook
#: persisted with their extracted `payment_intent_id` (T14/D21).
FAILED_PAYMENTS_KEY = "failed_payments"

#: The seven `revenue_snapshots` money columns this agent may measure.  A field that is `None`
#: here means "not measured" and must be omitted from the row, never zero-filled — measured 0
#: and not-measured are different states.
_SNAPSHOT_MONEY_FIELDS = (
    "mrr_cents",
    "arr_cents",
    "active_subscribers",
    "churned_today",
    "new_today",
    "total_revenue_month_cents",
    "stripe_balance_cents",
)


def failed_payments_extra_context(task: dict[str, Any]) -> str | None:
    """The failed-payment records attached to this task, quarantined for the prompt — or a refusal.

    WHY (T14/D21, fix-5 §8): the refund path needs a real Stripe PaymentIntent id (`pi_…`), and
    before this the finance agent had NO way to receive one — the webhook persisted the delivery
    (with the id inside `raw_payload`) and nothing read it into the agent's context, so a model
    could only ever invent an id and the adapter's fail-closed guard refused it.  The producer is
    the task's own `metadata` (the frozen `tasks.metadata` JSON column, carried to the agent by
    the worker): the morning cycle attaches the records `finance_service.recent_failed_payments`
    returns.

        metadata = {"failed_payments": [{"event_id": "evt_…", "event_type": "charge.failed",
                                         "payment_intent_id": "pi_…" | None,
                                         "amount_cents": 1500, "currency": "usd", ...}]}

    The records are quarantined with the REAL `wrap_untrusted(source="stripe", ...)`, so they
    ride the VOLATILE `extra_context` layer as DATA: an instruction-injection string smuggled
    into a Stripe object (a description, a customer name) is scanned by the assembler and taints
    the run instead of steering it.

    Absent material: `None` — the run degrades to exactly its previous behaviour and never
    invents a payment.  A PRESENT-but-unusable attachment (not a list of objects) is a NAMED
    refusal raised BEFORE any model call: this agent cannot query Stripe (no tools, no DB
    session), so a malformed attachment is a producer bug, not something to guess around.
    An EMPTY list is not an attachment at all (`None`): "no failed payments" needs no context.
    """
    metadata = task.get("metadata")
    if not isinstance(metadata, dict) or FAILED_PAYMENTS_KEY not in metadata:
        return None
    records = metadata.get(FAILED_PAYMENTS_KEY)
    if records is None:
        return None
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise PolsiaError(
            E_VALIDATION,
            "task metadata.failed_payments is not a list of objects; the finance agent cannot "
            "read a malformed attachment (it has no Stripe access to re-fetch it)",
            how_to_fix=(
                "attach the shape finance_service.recent_failed_payments returns: a list of "
                "{event_id, event_type, payment_intent_id, amount_cents, currency, ...} objects"
            ),
            context={"type": type(records).__name__},
        )
    if not records:
        return None
    lines: list[str] = []
    for index, record in enumerate(records):
        intent = record.get("payment_intent_id")
        intent_text = (
            str(intent).strip()
            if isinstance(intent, str) and intent.strip()
            else "absent (this delivery carried no pi_… id — no refund can be requested for it)"
        )
        lines.append(
            f"- failed payment #{index}: event_id={record.get('event_id') or 'unknown'}, "
            f"event_type={record.get('event_type') or 'unknown'}, "
            f"payment_intent_id={intent_text}, "
            f"amount_cents={record.get('amount_cents') if record.get('amount_cents') is not None else 'unmeasured'}, "
            f"currency={record.get('currency') or 'unmeasured'}, "
            f"customer_id={record.get('customer_id') or 'unmeasured'}"
        )
    text = "FAILED STRIPE PAYMENTS (persisted by the webhook; refunds need the pi_… id):\n" + "\n".join(lines)
    return wrap_untrusted(source="stripe", content_id=f"failed_payments:{len(lines)}", text=text)

ROLE = """You are the Finance agent of an autonomous company. You own the numbers: revenue, spend
and the alerts a founder must see.

Your job this run: reconcile the company's money position and report it in exact cents.

Rules:
- Every monetary field is an integer count of CENTS. Never write dollars in a cents field.
- `arr_cents` must be 12 x `mrr_cents` (within 2%). If you only know one, derive the other and say
  so in `notes`.
- Report ONLY numbers that appear in the context you were given or that you read from the provider
  through a tool. There is no "roughly": an unmeasured number is omitted, and its name is listed in
  `alerts` so someone measures it.
- `snapshot` mirrors the `revenue_snapshots` row for today (snapshot_date = YYYY-MM-DD, UTC) and
  carries ONLY the fields you actually measured: omit the rest, because an omitted field means
  "not measured" while a 0 is a measurement. Leave `snapshot_written` alone — the runtime
  records the real write.
- `expenses` entries are single real costs with category, vendor and amount in cents; never roll up
  guessed infrastructure spend.
- `failed_payments` lists the failed charges you actually saw, each with the charge's amount and
  its `payment_intent_id` (the Stripe PaymentIntent id, `pi_…`).  When this task carries a
  "FAILED STRIPE PAYMENTS" block, those records are the failed charges the platform already
  persisted — use THEIR `payment_intent_id` values verbatim; never re-type, guess or "complete"
  one.  Keep `event_id` when you know it as PROVENANCE only: a refund is created against a
  PaymentIntent, never against an event, so a request whose `payment_intent_id` is missing (or is
  not a `pi_…` id) is refused by name and no refund is sent.  Set `refund_requested=true` only for
  a charge you saw fail and want refunded — it is your PROPOSAL; the policy firewall still decides
  whether the refund is sent.
- `alerts` are the things a human must act on today: a failed charge above the usual size, a churn
  spike, a balance that cannot cover the payroll window. If nothing is wrong, return an empty list
  rather than inventing concern.
- Refunds and charges move money irreversibly. Never issue one yourself: set `refunds_performed=0`
  and describe the refund you would make in `alerts`. The runtime refuses an unapproved refund,
  and in sandbox mode it records the request as a labelled draft instead of sending it."""


class FinanceAgent(BasePolsiaAgent):
    agent_type = "finance"
    description = "Monitors Stripe revenue, expenses and alerts"
    output_schema = FinanceOutput
    max_turns = 30
    role_prompt = ROLE

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        # T14/D21: the failed-payment records attached to this task ride the VOLATILE
        # `extra_context` layer (quarantined as DATA), which is what lets the model emit a REAL
        # `pi_…` id below instead of an invented one.  No attachment -> None -> behaviour
        # unchanged.
        payload, assembled = self.collect_structured(
            task, context, extra_context=failed_payments_extra_context(task or {})
        )
        payload["agent_type"] = self.agent_type
        receipts: list[dict[str, Any]] = []

        # The snapshot row is built ONLY from values this run actually measured — from the
        # top-level money fields and/or a model-provided `snapshot` dict.  The schema's money
        # fields are nullable, so an omitted field is `None` ("did not measure") while an
        # explicit 0 is a measured zero; only the measured keys enter the row (the columns are
        # nullable, and zero-filling them here was the fabrication this replaces).
        model_snapshot = payload.get("snapshot")
        provided: dict[str, Any] = {}
        if isinstance(model_snapshot, dict):
            provided.update(
                {key: model_snapshot[key] for key in _SNAPSHOT_MONEY_FIELDS
                 if model_snapshot.get(key) is not None}
            )
        provided.update(
            {key: payload[key] for key in _SNAPSHOT_MONEY_FIELDS
             if key not in provided and payload.get(key) is not None}
        )
        if provided:
            # The date logic is unchanged: the model's date wins, UTC-today otherwise, so the
            # UNIQUE(snapshot_date) upsert in the services layer can never drift from the payload.
            snapshot: dict[str, Any] | None = {
                "snapshot_date": (
                    model_snapshot.get("snapshot_date") if isinstance(model_snapshot, dict) else None
                )
                or today_utc().isoformat(),
                **provided,
            }
        else:
            snapshot = None
            # ONE alert naming every unmeasured field, so a human measures them.  Nothing was
            # written and nothing may claim otherwise — the write decides `snapshot_written`.
            payload.setdefault("alerts", []).append(
                "no revenue figures were provided; no snapshot was written — unmeasured: "
                + ", ".join(_SNAPSHOT_MONEY_FIELDS)
            )
        payload["snapshot"] = snapshot

        # Refunds: irreversible, so the firewall decides — never the model.
        for index, item in enumerate(list(payload.get("failed_payments") or [])):
            refundable = bool(item.get("refund_requested"))
            if not refundable:
                continue
            # L3-10: the refund call needs the PaymentIntent id (`pi_…`) — Stripe creates a
            # refund against a PaymentIntent/charge, and the event id this used to pass as
            # `payment_intent` could only ever answer a provider error.  `event_id` rides along
            # as provenance in the side-effect payload; the ADAPTER's prefix guard is the
            # single fail-closed enforcement point for the id's type.
            ref = str(item.get("payment_intent_id") or "").strip()
            event_id = item.get("event_id")
            if not ref:
                # `str(item.get("event_id") or f"failed_payment:{index}")` used to fabricate a
                # payment id that then fed `integrations.refund_payment(payment_id=ref)`: a
                # refund must NEVER be requested against a made-up id — refuse by name and
                # build no request at all.  Same refusal when the id present is an event id:
                # it is the wrong RESOURCE TYPE for a refund (see the adapter's guard).
                payload.setdefault("refusals", []).append({
                    "ok": False,
                    "error": {
                        "code": E_VALIDATION,
                        "message": (
                            f"failed payment #{index} cannot be refunded: no "
                            "payment_intent_id (the Stripe pi_… id a refund needs)"
                        ),
                        "how_to_fix": (
                            "read the failed charge's PaymentIntent id (pi_…) from Stripe "
                            "before requesting a refund; an event id is provenance and can "
                            "never refund a charge"
                        ),
                        "context": {"index": index, "event_id": event_id},
                    },
                })
                continue
            outcome = request_side_effect(
                self.agent_type,
                ActionClass.EXTERNAL_IRREVERSIBLE,
                "stripe",
                action="refund",
                value_usd=float(item.get("amount_cents") or 0) / 100.0,
                payload={"action": "refund", "ref": ref, "event_id": event_id},
            )
            receipt = side_effect_receipt(outcome, action="refund", ref=ref)
            if outcome.allowed:                                   # approval token verified
                result = integrations.refund_payment(
                    payment_id=ref,
                    # `or None`: a missing/0 amount means the model measured no cap, and Stripe
                    # then refunds what is left on the PaymentIntent — an explicit 0 would be
                    # sent as `amount=0` and rejected.
                    amount_cents=item.get("amount_cents") or None,
                )
                receipt["integration"] = result
                if result.get("ok"):
                    payload["refunds_performed"] = int(payload.get("refunds_performed") or 0) + 1
                else:
                    payload.setdefault("refusals", []).append(result)
            else:
                if outcome.sandboxed:
                    payload["sandbox"] = True
                payload["drafted_not_sent"] = True
                payload.setdefault("alerts", []).append(
                    f"refund of {(item.get('amount_cents') or 0) / 100:.2f} USD for {ref} "
                    f"was refused ({outcome.e_code or outcome.policy_code}); approve it to proceed"
                )
            receipts.append(receipt)

        payload["side_effects"] = receipts
        payload.setdefault("refusals", [])
        payload.setdefault("expenses", [])
        payload.setdefault("alerts", [])
        payload.setdefault("failed_payments", [])
        # The WRITE decides this flag, not the model and not this payload: the agent only
        # proposes a snapshot.  The worker sets it True only when persist_finance_run's upsert
        # really returned a row.
        payload["snapshot_written"] = False
        if settings.sandbox_mode:
            payload["sandbox"] = True
            payload["drafted_not_sent"] = True
            payload["refunds_performed"] = 0
        return self.attach_run_meta(payload, assembled=assembled)


__all__ = ["FAILED_PAYMENTS_KEY", "ROLE", "FinanceAgent", "failed_payments_extra_context"]
