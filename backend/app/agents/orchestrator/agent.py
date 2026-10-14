"""The orchestrator agent — the morning plan and the evening summary.

Duty (README:30): "Writes morning plan + evening summary". Called in-process by
`celery_app.tasks.daily_cycle` for both cycles, so the same class serves two prompts and its
output shape depends on which one ran:

  * morning  -> `{summary, key_focus, tasks[], mode:"morning"}` — the task plan the morning cycle
    queues DIRECTLY (`daily_cycle._morning_cycle` reads `plan["tasks"]`; corrected in wave U3 —
    this line used to claim "the DAG planner materialises" it, but `app/orchestration/planner.py`
    is deliberately unwired and materialises nothing on this path; auditor H-04);
  * evening  -> `{summary, insights[], mode:"evening"}` — the run-ledger narrative the evening
    cycle writes into `daily_reports`.

Tools: NONE (`--tools ""`). The orchestrator is a planner: it reads the context it is given and
emits a plan. It cannot fetch, post or spend, which is the entire point of keeping it tool-less
(a planner that can act is a planner that can be injected into acting).
"""
from __future__ import annotations

from typing import Any

from app.agents.base_agent import BasePolsiaAgent
from app.config import settings
from app.schemas.agent_outputs import OrchestratorPlan

MORNING_PROMPT = """You are the Orchestrator of an autonomous company operated by a fleet of agents.

Your job this run: produce TODAY'S PLAN as a dependency-aware task list.

What you are given: the company profile, its KPIs and goals, yesterday's summary, the tasks already
on the board, and the money budget for the day. Reason over those and decide what actually moves
the business forward today.

Rules for the plan:
- 3 to 8 tasks (never more), highest-leverage first. A day with one task is a day wasted; a day
  with twenty is a day with none.
- Each task must be runnable by exactly one agent: orchestrator, business_planning,
  competitor_research, social_media, email_outreach, customer_support, ads_management,
  code_generation, finance.
- Set `depends_on` to the TITLES of tasks that must finish first (e.g. every customer-facing task
  depends on the morning research task). Use dependencies to express real ordering, not decoration.
- `priority` 1 = do first, 5 = do last. Reserve 1 for the thing that unblocks the others.
- `side_effect` marks what the task does in the world: read, internal_write, external_reversible
  (a post or an email) or external_irreversible (a refund, a merge, a deploy).
- `estimate_usd` is your honest cost estimate for the agent run (0.02-2.00) and
  `estimate_seconds` its wall-clock estimate (5-7200). Estimate from the task's scope; the runtime
  uses these for scheduling and for the pre-dispatch budget gate.
- `acceptance` states the observable condition that means the task succeeded.
- Never plan work with no KPIs or revenue link, and never plan the same task twice in a day.

Set `key_focus` to the single theme that matters most today and `summary` to a 2-4 sentence plan
narrative a founder could read on a dashboard."""

EVENING_PROMPT = """You are the Orchestrator of an autonomous company. This run is the EVENING close.

Your job: turn the day's run ledger into an honest summary.

What you are given: the company profile and KPIs, yesterday's summary, and today's task board with
each row's final state (completed, failed, still pending) plus the day's spend.

Write `summary` as 3-6 sentences that a founder can act on tomorrow morning: what actually shipped,
what stalled and why, and what the numbers say. Cite only what the ledger contains — if a number
is not in the context, say it is unmeasured rather than estimating it.

Write `insights` as 3-6 short, specific observations: one per line, each naming the evidence it
comes from (a task, a metric, a failure). No filler, no motivational language, no advice you
cannot tie to a row above.

Set `tasks` to [] (planning happens in the morning) and `key_focus` to the single thing to watch
tomorrow. Use status="partial" if the ledger you were given is incomplete."""


class OrchestratorAgent(BasePolsiaAgent):
    agent_type = "orchestrator"
    description = "Writes the morning plan and evening summary"
    output_schema = OrchestratorPlan
    max_turns = 40
    role_prompt = MORNING_PROMPT

    # ------------------------------------------------------------------ mode
    @staticmethod
    def plan_mode(task: dict[str, Any]) -> str:
        """Which of the two prompts this run is.

        L3-06: the CALLER knows — `daily_cycle` builds both task dicts and passes
        `metadata={"cycle": "morning"|"evening"}` — so the declared cycle is read FIRST and is
        the authority.  The word sniff over title/description stays only as the fallback for
        ad-hoc tasks (it reads free text, and the shipped cycle titles still resolve correctly:
        "Morning planning cycle" has no trigger word, "Evening reporting cycle" names itself).
        Sniffing first meant a MORNING plan whose (model-authored) wording contained "close",
        "report", "summary" or "insights" silently became an evening run and threw the plan away.
        """
        metadata = task.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        declared = str(metadata.get("cycle") or "").strip().lower()
        if declared in ("morning", "evening"):
            return declared
        text = f"{task.get('title') or ''} {task.get('description') or ''}".lower()
        if any(word in text for word in ("evening", "report", "close", "summary", "insights")):
            return "evening"
        return "morning"

    def run(self, task: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        mode = self.plan_mode(task)
        self.role_prompt = EVENING_PROMPT if mode == "evening" else MORNING_PROMPT

        payload, assembled = self.collect_structured(task, context)
        payload["agent_type"] = self.agent_type
        model_mode = str(payload.get("mode") or "").strip().lower()
        payload["mode"] = mode
        if model_mode and model_mode != mode:
            # L3-06: the model's own claim disagreed with the computed mode — recorded, never
            # silently overwritten (the caller's declaration wins; the disagreement is evidence).
            payload.setdefault("notes", []).append(
                f"model declared mode={model_mode!r}; computed mode={mode!r} is authoritative"
            )

        if mode == "morning":
            tasks = list(payload.get("tasks") or [])
            limit = max(1, int(settings.planner_max_nodes))
            if len(tasks) > limit:
                payload.setdefault("notes", []).append(
                    f"plan truncated to planner_max_nodes={limit} (model proposed {len(tasks)})"
                )
                tasks = tasks[:limit]
            payload["tasks"] = tasks
            payload.setdefault("insights", [])
            payload["planned_task_count"] = len(tasks)
            payload["plan_titles"] = [t.get("title") for t in tasks]
        else:
            discarded = [t for t in (payload.get("tasks") or []) if isinstance(t, dict)]
            payload["tasks"] = []
            if discarded:
                # L3-06: the evening branch exists to REPORT, not to plan — but a non-empty
                # tasks[] it drops must not vanish without a trace (it used to: no error, no
                # note). The titles are recorded so the loss is visible on the tasks board.
                titles = [str(t.get("title") or "") for t in discarded][:8]
                payload.setdefault("notes", []).append(
                    f"evening mode discarded {len(discarded)} planned task(s): {titles}"
                )
            payload.setdefault("insights", [])
        return self.attach_run_meta(payload, assembled=assembled)


__all__ = ["EVENING_PROMPT", "MORNING_PROMPT", "OrchestratorAgent"]
