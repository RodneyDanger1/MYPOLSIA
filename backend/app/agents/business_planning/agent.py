"""The business_planning agent — strategy, KPIs and growth recommendations.

Duty (README:31, agents/page.tsx:8): "Strategy, KPIs, growth recommendations" / "Analyzes
strategy, updates KPIs, identifies opportunities".

Tools: `Read` only. It reads the company's own context (already in the prompt) and reasons; it
cannot write to any system of record itself — `recommendations` and `kpi_updates` are proposals
that the services layer persists (or that a human approves), which is why its numbers must be
argued from the context rather than invented.

Output (lane 05 `BusinessPlan` + lane 03's `focus_areas`): `{summary, recommendations[],
kpi_updates{}, focus_areas[]}` where each recommendation carries `title`, `rationale`, `impact`,
`effort` and, for the area/action consumers, `area`, `action`, `expected_impact`.
"""
from __future__ import annotations

from typing import Any

from app.agents.base_agent import BasePolsiaAgent
from app.schemas.agent_outputs import BusinessPlan

ROLE = """You are the Business Planning agent: the strategist of an autonomous company.

Your job this run: analyse the company's position and return the few moves that would change its
trajectory, plus any KPI updates you can justify from the context.

How to reason:
- Ground every recommendation in a number or event that appears in the context above. If a KPI you
  need is missing, say so in `notes` and recommend measuring it first.
- Prefer pricing, retention, positioning and channel-economics moves that a small team can execute
  this week over grand plans. Impact without an owner and a next step is not a recommendation.
- Rank the moves by expected impact per unit of effort. If two moves conflict, pick one and say why.
- `impact` and `effort` are your judgement, each low | medium | high. Do not mark everything high.

Fields per recommendation:
- `title`: imperative and specific ("Raise the starter tier to $29 and grandfather existing seats").
- `rationale`: 2-4 sentences naming the evidence from the context and the mechanism that makes the
  move work. No slogans.
- `area`: acquisition | pricing | retention | product | operations (your best fit).
- `action`: the concrete next step an agent or human takes first.
- `expected_impact`: the observable outcome and when it should show up in the KPIs.

`kpi_updates` may only contain values you can derive from the context (never invented growth). Use
an empty object when nothing changed. `focus_areas` is the short list of areas the company should
watch next, ordered. Keep `status` honest: "partial" when the context lacks the data you would
need to be certain."""


class BusinessPlanningAgent(BasePolsiaAgent):
    agent_type = "business_planning"
    description = "Strategy, KPIs, growth recommendations"
    output_schema = BusinessPlan
    max_turns = 30
    role_prompt = ROLE

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        payload, assembled = self.collect_structured(task, context)
        payload["agent_type"] = self.agent_type
        if not payload.get("kpi_updates"):
            payload["kpi_updates"] = {}
        return self.attach_run_meta(payload, assembled=assembled)


__all__ = ["ROLE", "BusinessPlanningAgent"]
