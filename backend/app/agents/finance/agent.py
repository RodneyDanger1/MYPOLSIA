"""The finance agent — Stripe revenue sync, expenses and refunds.

Duty (README:38): "Stripe revenue sync, spend tracking". Dispatched by `ads-stripe-sync`
(every 6h) and by the morning cycle's `priority=1` "Daily revenue snapshot" task.

Output (lane 05 `FinanceOutput` + lane 03's snapshot/failed-payment keys): `{summary, mrr_cents,
arr_cents, active_subscribers, churned_today, new_today, stripe_balance_cents, snapshot{},
expenses[], failed_payments[], alerts[], snapshot_written}` — one key per column of
`revenue_snapshots`, `expense_records` and `stripe_events`.

Idempotency: `revenue_snapshots.snapshot_date` is UNIQUE, so the snapshot is an upsert keyed by
date; `snapshot_written` means "this payload carries a snapshot to upsert", which is why the
second morning run of the day cannot create a duplicate row.

Sandbox semantics: balance/subscription reads and the snapshot/expense writes are allowed; a
REFUND is `external_irreversible` and is refused (`E_SANDBOX_BLOCKED`) while sandboxed, and
approval-gated when it is not (`refunds_performed` stays 0 unless a token was verified).
"""
from __future__ import annotations

from typing import Any

from app.agents import integrations
from app.agents.base_agent import BasePolsiaAgent
from app.agents.policy_firewall import ActionClass
from app.agents.side_effects import request_side_effect, side_effect_receipt
from app.config import settings
from app.core.timeutil import today_utc
from app.schemas.agent_outputs import FinanceOutput

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
- `snapshot` mirrors the `revenue_snapshots` row for today (snapshot_date = YYYY-MM-DD, UTC). Set
  `snapshot_written=true` only when you are providing that row.
- `expenses` entries are single real costs with category, vendor and amount in cents; never roll up
  guessed infrastructure spend.
- `failed_payments` lists the failed charges you actually saw, with their event id and amount.
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
        payload, assembled = self.collect_structured(task, context)
        payload["agent_type"] = self.agent_type
        receipts: list[dict[str, Any]] = []

        # A snapshot payload is the internal write this agent performs; make the date explicit so
        # the UNIQUE(snapshot_date) upsert in the services layer can never drift from the payload.
        snapshot = payload.get("snapshot")
        if not isinstance(snapshot, dict) or not snapshot.get("snapshot_date"):
            snapshot = {
                "snapshot_date": today_utc().isoformat(),
                "mrr_cents": int(payload.get("mrr_cents") or 0),
                "arr_cents": int(payload.get("arr_cents") or 0),
                "active_subscribers": int(payload.get("active_subscribers") or 0),
                "churned_today": int(payload.get("churned_today") or 0),
                "new_today": int(payload.get("new_today") or 0),
                "total_revenue_month_cents": int(payload.get("total_revenue_month_cents") or 0),
                "stripe_balance_cents": int(payload.get("stripe_balance_cents") or 0),
            }
        payload["snapshot"] = snapshot

        # Refunds: irreversible, so the firewall decides — never the model.
        for index, item in enumerate(list(payload.get("failed_payments") or [])):
            refundable = bool(item.get("refund_requested"))
            if not refundable:
                continue
            ref = str(item.get("event_id") or f"failed_payment:{index}")
            outcome = request_side_effect(
                self.agent_type,
                ActionClass.EXTERNAL_IRREVERSIBLE,
                "stripe",
                action="refund",
                value_usd=float(item.get("amount_cents") or 0) / 100.0,
                payload={"action": "refund", "ref": ref},
            )
            receipt = side_effect_receipt(outcome, action="refund", ref=ref)
            if outcome.allowed:                                   # approval token verified
                result = integrations.refund_payment(payment_id=ref)
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
        payload["snapshot_written"] = True
        if settings.sandbox_mode:
            payload["sandbox"] = True
            payload["drafted_not_sent"] = True
            payload["refunds_performed"] = 0
        return self.attach_run_meta(payload, assembled=assembled)


__all__ = ["ROLE", "FinanceAgent"]
