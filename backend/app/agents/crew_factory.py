"""AGENT_MAP registry - maps an agent_type to its class, and dispatches it.

Frozen contract (CLAUDE.md:79, tests/unit/celery/test_agent_tasks.py, tests/parity):

  * `AGENT_MAP` values are `(module_path, class_name)` 2-tuples of strings - the exact form the
    docs tell contributors to extend;
  * resolution is LAZY (`importlib.import_module` at call time), which is what makes
    `patch("app.agents.social_media.agent.SocialMediaAgent.run")` effective and keeps unit tests
    from importing Tweepy/Stripe/PyGithub;
  * an unknown type raises `ValueError` whose message matches "Unknown agent".

Beyond parity, dispatch now goes through three runtime gates:

  * the **policy firewall's kill switch** - a frozen fleet refuses dispatch before anything is
    instantiated (an operator with shell access can stop the fleet without touching the DB);
  * the **spawn-level safety preflight** (`claude_cli.safety_preflight`, RD4 item 2): the CLI
    binary and the CLI's auth state are checked before a worker slot is committed, with inputs
    that describe the runtime a dispatch would ACTUALLY take (`_spawn_facts`) -- mock mode and a
    non-CLI backend never spawn a CLI, so a missing binary is only a hard failure when the
    claude-cli path is the path;
  * `timed_run`, so every run reports a float `duration_secs` (the value
    `agent_runs.duration_secs` stores) and the captured CLI usage rides along under the
    documented keys (`base_agent.RUN_META_KEYS`).

Per-action gating (post / email / ad change / payment / PR) happens inside each agent through
`app.agents.side_effects.request_side_effect`, i.e. before every external side effect.
"""
from __future__ import annotations

import importlib
import logging
import os
import re
import shutil
import subprocess
from typing import Any

from app.agents.base_agent import BasePolsiaAgent, is_mock_mode
from app.agents.claude_cli import cli_path, safety_preflight
from app.agents.policy_firewall import AGENT_CAPABILITIES, ActionClass, kill_grade_from_env
from app.config import settings
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


# ── spawn facts for the pre-dispatch safety checks (RD4 item 2; audit L2-F08) ──────────────────
# `claude_cli.safety_preflight` is the documented fail-closed check (kill switch / CLI binary /
# auth) and had ZERO callers: a missing binary surfaced as E_CLI_MISSING from `invoke()`, and a
# logged-out CLI as an envelope failure (CLI_AUTH_FAILED -> E_CLI_EXIT) — both AFTER a worker slot
# and a budget were already committed to the run.  It is wired into `dispatch_preflight` below
# (the seam that already runs before every `run_agent_for_task`) with inputs that describe THIS
# runtime instead of assumed ones:
#   * mock mode never spawns a process, so the binary/auth legs are NOT APPLICABLE — reported
#     clear WITH the reason recorded, because failing a path nothing takes would be a fictional
#     refusal, and passing silently would be a fake one;
#   * a non-CLI backend (when one is configured) does not need the CLI binary either;
#   * otherwise the CLI IS the path: binary presence is probed for real, and auth is probed for
#     real once per process.

_LOGGED_OUT_RE = re.compile(
    r"not\s+(?:logged|signed)\s+in|logged\s+out|no\s+(?:account|credentials)|"
    r"run\s+`?claude\s+login`?|authentication\s+required",
    re.IGNORECASE,
)

#: Probe cache keyed by the resolved CLI path: one bounded subprocess per worker process.
_CLI_AUTH_VERDICTS: dict[str, tuple[bool, str]] = {}


def _cli_binary_present() -> bool:
    """Is the configured CLI actually resolvable on this host?  (Real probe, no guessing.)"""
    path = cli_path()
    if os.path.isabs(path) or "/" in path or "\\" in path:
        return os.path.isfile(path)
    return shutil.which(path) is not None


def _probe_cli_auth() -> tuple[bool, str]:
    """`claude auth status`, cached per process: `(auth_ok, verdict)`.

    Verdicts: `"probed_ok"` | `"logged_out"` | `"unverified"`.  Only a DEFINITIVE logged-out signal
    (the CLI naming that state) reports a problem.  An inconclusive probe — an older CLI without the
    subcommand, a timeout, unreadable output — is recorded as `unverified` rather than turned into a
    fleet-wide refusal: a check that cannot judge must not stop every run, and the spawn-time
    mapping (CLI_AUTH_FAILED -> E_CLI_EXIT, with the documented `claude login` hint) remains the
    second line of defence.  The verdict travels in the dispatch context either way, so an operator
    can see whether the leg was actually verified.
    """
    path = cli_path()
    cached = _CLI_AUTH_VERDICTS.get(path)
    if cached is not None:
        return cached
    try:
        proc = subprocess.run(
            [path, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,   # a non-zero exit is not by itself a logged-out signal (see docstring)
        )
        output = f"{proc.stdout or ''}\n{proc.stderr or ''}"
        verdict: tuple[bool, str] = (
            (False, "logged_out") if _LOGGED_OUT_RE.search(output) else (True, "probed_ok")
        )
    except (OSError, subprocess.SubprocessError):
        verdict = (True, "unverified")
    _CLI_AUTH_VERDICTS[path] = verdict
    return verdict


#: Fallback declaration of backends that do not spawn the claude CLI.  The authoritative signal is
#: the provider seam's own predicate (`deepseek_runtime.available()` — a resolvable DeepSeek key,
#: exactly what `base_agent._real_call` routes on); this tuple additionally honours a backend that
#: declares itself through `POLSIA_MODEL_BACKEND` / `settings.model_backend`.  A value in NEITHER
#: place is treated as the claude-cli path — fail closed, because an unrecognised token must never
#: silently disable the binary check.
_NON_CLI_BACKENDS: tuple[str, ...] = ("deepseek", "api", "openai", "anthropic_api", "http")


def _non_cli_backend() -> str:
    """The non-CLI runtime a dispatch would ACTUALLY use, asked of the seam that decides it.

    `base_agent._real_call` routes a run to the DeepSeek HTTP runtime when
    `deepseek_runtime.available()`; the preflight has to ask the SAME question, or it would refuse
    a dispatch that never spawns the CLI.  The import is defensive on purpose: the module arrives
    with a parallel seat, and while it is absent the claude-cli path is the path (fail closed).  A
    predicate that raises is treated identically — a broken provider probe must not take dispatch
    down with it, and the CLI check then applies.
    """
    try:
        from app.agents import deepseek_runtime
    except ImportError:
        return ""
    try:
        return "deepseek" if deepseek_runtime.available() else ""
    except Exception:  # noqa: BLE001 - see docstring: provider probe failure ⇒ CLI path
        return ""


def _spawn_facts() -> dict[str, Any]:
    """Which backend a dispatch would actually use, and what that means for the CLI legs."""
    if is_mock_mode():
        return {
            "backend": "mock", "cli_present": True, "auth_ok": True, "cli_auth": "not_applicable",
            "note": "CLAUDE_CLI_MOCK: no CLI process is spawned, so the CLI legs do not apply",
        }
    backend = _non_cli_backend() or str(
        os.getenv("POLSIA_MODEL_BACKEND") or getattr(settings, "model_backend", "") or ""
    ).strip().lower()
    if backend in _NON_CLI_BACKENDS:
        return {
            "backend": backend, "cli_present": True, "auth_ok": True, "cli_auth": "not_applicable",
            "note": f"backend {backend!r} does not spawn the claude CLI; the CLI legs do not apply",
        }
    if not _cli_binary_present():
        return {
            "backend": "claude_cli", "cli_present": False, "auth_ok": True,
            "cli_auth": "not_probed",
            "note": f"claude CLI {cli_path()!r} is not resolvable; auth was not probed",
        }
    auth_ok, verdict = _probe_cli_auth()
    return {
        "backend": "claude_cli", "cli_present": True, "auth_ok": auth_ok,
        "cli_auth": verdict, "note": "",
    }


def dispatch_preflight(agent_type: str, *, dispatch_paused: bool = False) -> list[str]:
    """Fleet-level gates that must stop dispatch BEFORE an agent is constructed.

    `freeze` stops everything; `throttle` stops the agents whose purpose is an external side
    effect (they have nothing to do with reads only). The kill switch is read from the
    environment first, so an operator can stop the fleet without a database round-trip.

    RD4 item 2: `claude_cli.safety_preflight` — built, documented fail-closed, and until now
    called by NOBODY (audit L2-F08) — runs in this same seam, against facts that describe the
    runtime a dispatch would actually take (`_spawn_facts`).  A missing CLI binary or a
    definitively logged-out CLI is therefore a named refusal BEFORE any spawn.  Both gates hold:
    the freeze/throttle semantics above are unchanged, and the safety problems are ADDED to them.
    """
    grade = kill_grade_from_env()
    problems: list[str] = []
    if grade == "freeze":
        problems.append(f"kill switch freeze: {agent_type} dispatch refused")
    elif grade == "throttle":
        cap = AGENT_CAPABILITIES.get(agent_type)
        if cap and any(c in cap.action_classes for c in (
            ActionClass.EXTERNAL_REVERSIBLE, ActionClass.EXTERNAL_IRREVERSIBLE
        )):
            problems.append(f"kill switch throttle: {agent_type} performs external actions; reads only")

    if dispatch_paused:
        # V8-D6 / V8/C5: `company_config.dispatch_paused` — the owner's Pause/Resume Switch.
        # This gate is SYNC with no session, so the flag is resolved asynchronously where a
        # session exists (`resolve_dispatch_paused` below + the worker gate in `agent_tasks`)
        # and passed in here; a direct caller bypassing the worker still hits this leg.
        problems.append(
            "company paused by owner: dispatch refused (company_config.dispatch_paused=true; "
            "resume in Settings to allow dispatch)"
        )

    facts = _spawn_facts()
    for problem in safety_preflight(
        kill_grade=grade, cli_present=bool(facts["cli_present"]), auth_ok=bool(facts["auth_ok"])
    ):
        # One refusal, one voice: the freeze/throttle gate above has already refused BY NAME for the
        # kill switch, so safety_preflight's own kill line is dropped instead of repeated.  Matched
        # on the CONCEPT ("kill switch"), not on exact wording — that function lives in another
        # seat's file and its message text is not ours to pin.
        if problem in problems or (problems and "kill switch" in problem.lower()):
            continue
        problems.append(problem)
    return problems


def run_agent_for_task(
    agent_type: str,
    task_dict: dict[str, Any] | None,
    context: dict[str, Any] | None,
    *,
    dispatch_paused: bool = False,
) -> dict[str, Any]:
    """Instantiate the mapped agent and run it.  Sync by design (CLI subprocess).

    Returns the agent's dict **plus** `duration_secs` and the usage/cost/session keys the queue
    persists into `agent_runs` (additive: the agent's own keys are untouched).
    """
    agent_class = get_agent_class(agent_type)          # ValueError("Unknown agent ...") here
    problems = dispatch_preflight(agent_type, dispatch_paused=dispatch_paused)
    if problems:
        raise PolsiaError(
            E_POLICY_DENIED,
            f"dispatch refused for {agent_type}: {'; '.join(problems)}",
            how_to_fix=ERROR_HINTS[E_POLICY_DENIED],
            # `safety` carries what the spawn-level legs actually measured (backend, binary
            # presence, auth verdict + why a leg did not apply) — a refusal a human reads must say
            # WHICH check refused, not just that one did (RD4 item 2).
            context={"agent_type": agent_type, "kill_grade": kill_grade_from_env(),
                     "safety": _spawn_facts()},
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


async def resolve_dispatch_paused(db: Any) -> bool:
    """The owner's pause flag from `company_config`, resolved where a session exists (V8-D6).

    `dispatch_preflight` is sync and holds no DB session, so the worker resolves the flag on
    its own session and PASSES it in; this helper is the one read path both the worker gate
    and tests share.  Defensive on purpose (the `_configured_recipient` pattern): a missing
    column reads as False, and a FAILED config read also resolves to False with a warning —
    the claim that immediately preceded it proved the database reachable, and a read glitch
    must not self-inflict a fleet-wide outage (the kill switch remains the fail-closed stop).
    """
    from app.services import company_service

    try:
        config = await company_service.get_company(db)
    except Exception as exc:  # noqa: BLE001 — a read glitch is not a pause (see docstring)
        logger.warning("dispatch pause: config read failed; treating as NOT paused (%s)", exc)
        return False
    if config is None:
        return False
    return bool(getattr(config, "dispatch_paused", False))


__all__ = [
    "AGENT_MAP",
    "VALID_AGENT_TYPES",
    "dispatch_preflight",
    "get_agent_class",
    "resolve_dispatch_paused",
    "run_agent_for_task",
]
