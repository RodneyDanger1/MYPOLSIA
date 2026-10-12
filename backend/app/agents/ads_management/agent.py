"""The ads_management agent — Google Ads + Meta campaign optimisation.

Duty (README:36): "Google + Meta campaign optimization". Dispatched by the `ads-stripe-sync` beat
entry (every 6h) and by ad-hoc tasks.

Output (lane 05 `AdsOutput` + lane 03's campaign/metric rows): `{summary, recommendations[],
metrics_snapshot{}, campaigns[], metrics[]}` feeding `ad_campaigns` / `ad_metrics`.

Sandbox semantics: reading metrics and writing `ad_campaigns`/`ad_metrics` rows is allowed; every
BUDGET OR STATUS MUTATION on a live platform is refused (`E_SANDBOX_BLOCKED`) and returned as a
recommendation with `applied=false` plus the `sandbox`/`drafted_not_sent` markers. Budget changes
move real money, so any single change of $25 or more needs human approval even outside sandbox.
"""
from __future__ import annotations

from typing import Any

from app.agents import integrations
from app.agents.base_agent import BasePolsiaAgent
from app.agents.policy_firewall import ActionClass
from app.agents.side_effects import request_side_effect, side_effect_receipt
from app.config import settings
from app.schemas.agent_outputs import AdsOutput

ROLE = """You are the Ads Management agent of an autonomous company. You run paid acquisition on
Google Ads and Meta.

Your job this run: read the campaign metrics, then recommend (and, when permitted, apply) the
changes that improve return on ad spend without starving the funnel.

Decision rules:
- Compare each campaign against its own baseline, not against a universal target. A campaign with
  ROAS below its goal for 3+ consecutive days is a pause or a cut; one that is profitable and
  capped by budget is a scale-up.
- Never increase a budget by more than 25% in one step — learning phases reset and the CPA spikes.
- One recommendation per campaign, best-first, with `rationale` naming the metric values that
  drove it. "Improve performance" is not a rationale.
- `budget_change_usd` is the signed change you propose (negative = cut). Use 0.0 for pause/resume.
- `campaign_ref` must be an id or name from the metrics you were given; never invent one.
- Put the headline numbers you actually read in `metrics_snapshot` (spend, roas, cpa as keys).
  If a metric is missing, omit it — do not estimate.
- `new_campaign` is allowed only when the context shows an untested channel with budget headroom,
  and its rationale must state the test's budget and success condition.

Every status or budget mutation is a real money action: it goes through the policy firewall, and
while the company runs in sandbox mode it will be refused and recorded as a recommendation with
`applied=false`. Never write that a change was applied unless the runtime confirms it."""


class AdsManagementAgent(BasePolsiaAgent):
    agent_type = "ads_management"
    description = "Manages Google and Meta ad campaigns"
    output_schema = AdsOutput
    max_turns = 30
    role_prompt = ROLE

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        payload, assembled = self.collect_structured(task, context)
        payload["agent_type"] = self.agent_type
        receipts: list[dict[str, Any]] = []

        for index, rec in enumerate(list(payload.get("recommendations") or [])):
            platform = str(rec.get("platform") or "google_ads")
            action = str(rec.get("action") or "noop")
            campaign_ref = str(rec.get("campaign_ref") or f"campaign:{index}")
            delta = float(rec.get("budget_change_usd") or 0.0)
            if action == "noop":
                rec["applied"] = False
                continue
            counts = ((context or {}).get("channel_sends") or {}).get(platform) or {}
            outcome = request_side_effect(
                self.agent_type,
                ActionClass.EXTERNAL_REVERSIBLE,
                platform,
                action=action,
                value_usd=abs(delta),
                payload={"action": action, "campaign_ref": campaign_ref,
                         "budget_change_usd": delta},
                sends_today=int(counts.get("today") or 0),
                sends_this_hour=int(counts.get("hour") or 0),
            )
            receipt = side_effect_receipt(outcome, action=action, ref=campaign_ref,
                                          extra={"platform": platform, "budget_change_usd": delta})
            if outcome.allowed:                                   # only outside sandbox
                result = integrations.update_ad_budget(
                    platform=platform, campaign_ref=campaign_ref, budget_change_usd=delta
                )
                receipt["integration"] = result
                rec["applied"] = bool(result.get("ok"))
                if not result.get("ok"):
                    payload.setdefault("refusals", []).append(result)
            else:
                rec["applied"] = False
                if outcome.sandboxed:
                    payload["sandbox"] = True
                payload["drafted_not_sent"] = True
                if outcome.requires_approval:
                    payload.setdefault("refusals", []).append({
                        "ok": False,
                        "error": {"code": "E_POLICY_DENIED",
                                  "message": f"{action} on {campaign_ref} needs approval: {outcome.reason}",
                                  "how_to_fix": "an operator approves the budget change, then re-run",
                                  "context": {"policy_code": outcome.policy_code}},
                    })
            receipts.append(receipt)

        payload["side_effects"] = receipts
        payload.setdefault("refusals", [])
        payload.setdefault("recommendations", [])
        payload.setdefault("metrics_snapshot", {})
        if settings.sandbox_mode:
            payload["sandbox"] = True
            payload["drafted_not_sent"] = True
            for rec in payload["recommendations"]:
                rec["applied"] = False
        return self.attach_run_meta(payload, assembled=assembled)


__all__ = ["ROLE", "AdsManagementAgent"]
