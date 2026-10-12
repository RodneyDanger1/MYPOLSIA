"""AGENT_MAP registry - maps an agent_type to its class, and dispatches it.

Frozen contract (CLAUDE.md:79, tests/unit/celery/test_agent_tasks.py, tests/parity):

  * `AGENT_MAP` values are `(module_path, class_name)` 2-tuples of strings - the exact form the
    docs tell contributors to extend;
  * resolution is LAZY (`importlib.import_module` at call time), which is what makes
    `patch("app.agents.social_media.agent.SocialMediaAgent.run")` effective and keeps unit tests
    from importing Tweepy/Stripe/PyGithub;
  * an unknown type raises `ValueError` whose message matches "Unknown agent".

Beyond parity, dispatch now goes through two runtime gates:

  * the **policy firewall's kill switch** - a frozen fleet refuses dispatch before anything is
    instantiated (an operator with shell access can stop the fleet without touching the DB);
  * `timed_run`, so every run reports a float `duration_secs` (the value
    `agent_runs.duration_secs` stores) and the captured CLI usage rides along under the
    documented keys (`base_agent.RUN_META_KEYS`).

Per-action gating (post / email / ad change / payment / PR) happens inside each agent through
`app.agents.side_effects.request_side_effect`, i.e. before every external side effect.
"""
from __future__ import annotations

import importlib
import logging
from typing import Any

from app.agents.base_agent import BasePolsiaAgent
from app.agents.policy_firewall import AGENT_CAPABILITIES, ActionClass, kill_grade_from_env
from app.core.errors import E_POLICY_DENIED, ERROR_HINTS, PolsiaError

logger = logging.getLogger(__name__)

# (module_path, class_name) - CLAUDE.md: "Add `"<name>": ("app.agents.<name>.agent",
# "<ClassName>")` to AGENT_MAP in crew_factory.py"
AGENT_MAP: dict[str, tuple[str, str]] = {
    "orchestrator": ("app.agents.orchestrator.agent", "OrchestratorAgent"),
    "business_planning": ("app.agents.business_planning.agent", "BusinessPlanningAgent"),
    "competitor_research": ("app.agents.competitor_research.agent", "CompetitorResearchAgent"),
    "social_media": ("app.agents.social_media.agent", "SocialMediaAgent"),
    "ads_management": ("app.agents.ads_management.agent", "AdsManagementAgent"),
    "email_outreach": ("app.agents.email_outreach.agent", "EmailOutreachAgent"),
    "code_generation": ("app.agents.code_generation.agent", "CodeGenerationAgent"),
    "customer_support": ("app.agents.customer_support.agent", "CustomerSupportAgent"),
    "finance": ("app.agents.finance.agent", "FinanceAgent"),
}

# Deployment agent is documented (CLAUDE.md/docs) but has no model, table, route or
# test; adding it here would break GET /agents/status == 9. See FINDINGS.md.
VALID_AGENT_TYPES: tuple[str, ...] = tuple(AGENT_MAP.keys())


def get_agent_class(agent_type: str):
    try:
        module_path, class_name = AGENT_MAP[agent_type]
    except KeyError:
        raise ValueError(
            f"Unknown agent type: {agent_type!r}. Valid types: {', '.join(VALID_AGENT_TYPES)}"
        ) from None
    module = importlib.import_module(module_path)
    return getattr(module, class_name)


def dispatch_preflight(agent_type: str) -> list[str]:
    """Fleet-level gates that must stop dispatch BEFORE an agent is constructed.

    `freeze` stops everything; `throttle` stops the agents whose purpose is an external side
    effect (they have nothing to do with reads only). The kill switch is read from the
    environment first, so an operator can stop the fleet without a database round-trip.
    """
    grade = kill_grade_from_env()
    if grade == "freeze":
        return [f"kill switch freeze: {agent_type} dispatch refused"]
    if grade == "throttle":
        cap = AGENT_CAPABILITIES.get(agent_type)
        if cap and any(c in cap.action_classes for c in (
            ActionClass.EXTERNAL_REVERSIBLE, ActionClass.EXTERNAL_IRREVERSIBLE
        )):
            return [f"kill switch throttle: {agent_type} performs external actions; reads only"]
    return []


def run_agent_for_task(
    agent_type: str,
    task_dict: dict[str, Any] | None,
    context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Instantiate the mapped agent and run it.  Sync by design (CLI subprocess).

    Returns the agent's dict **plus** `duration_secs` and the usage/cost/session keys the queue
    persists into `agent_runs` (additive: the agent's own keys are untouched).
    """
    agent_class = get_agent_class(agent_type)          # ValueError("Unknown agent ...") here
    problems = dispatch_preflight(agent_type)
    if problems:
        raise PolsiaError(
            E_POLICY_DENIED,
            f"dispatch refused for {agent_type}: {'; '.join(problems)}",
            how_to_fix=ERROR_HINTS[E_POLICY_DENIED],
            context={"agent_type": agent_type, "kill_grade": kill_grade_from_env()},
        )
    agent = agent_class()
    if not isinstance(agent, BasePolsiaAgent):
        raise PolsiaError(
            E_POLICY_DENIED,
            f"agent class for {agent_type!r} is not a BasePolsiaAgent",
            how_to_fix="check AGENT_MAP; every entry must be a BasePolsiaAgent subclass",
            context={"agent_type": agent_type, "class": type(agent).__name__},
        )
    result = agent.timed_run(task_dict or {}, context or {})
    if not isinstance(result, dict):
        result = {"result": result}
    result.setdefault("agent_type", agent_type)
    return result


__all__ = [
    "AGENT_MAP",
    "VALID_AGENT_TYPES",
    "dispatch_preflight",
    "get_agent_class",
    "run_agent_for_task",
]
