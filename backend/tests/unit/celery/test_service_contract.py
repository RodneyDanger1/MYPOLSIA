"""SIGNATURE CONTRACT — the caller and the service must keep agreeing on the call shape.

`celery_app/tasks/agent_tasks.py` was written against a *requested* claim API and then
reconciled by the integration pass against the service that actually landed
(`app/services/task_service.py`, probe-verified): `claim_task` is KEYWORD-ONLY and RAISES on an
explicit unrunnable `task_id` instead of returning None, `heartbeat_task` reports a lost claim
by raising, and the outcome-and-release call the caller was written against does not exist —
the outcome is written with `update_task_status` and the claim fields are cleared by the caller.

The integration pass had no regression test.  Each side could drift silently: a renamed service
parameter or a changed call site only shows up as a `TypeError` inside a live worker.  These
tests pin the agreement from BOTH directions:

* the caller's real call sites are parsed out of its SOURCE (`ast`) and bound against the
  service's real signatures (`inspect.signature`) — `bind()` rejects an unknown/missing/duplicate
  parameter and a too-large positional arity, and the required-parameter check rejects a call
  that omits something the service needs;
* the names the caller imports are pinned (the five reconciled service functions + T17's single
  money boundary `_as_column_decimal`), and `claim_task`'s keyword-only kinds are asserted
  explicitly (the shape the parent reconciled).

`binding_problems(service)` is a module-level function on purpose: the red-leg harness runs it
against a mutated service module to prove the check can fail without editing any service file.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import celery_app.tasks.agent_tasks as agent_tasks
from app.services import task_service

CALLER_PATH = Path(agent_tasks.__file__)

#: Exactly what the caller imports from the service: the reconciled five-function contract
#: plus T17's single money boundary.  `_apply_run_usage` writes `agent_runs.cost_usd` through
#: `_as_column_decimal` (A-T17 single-site rule) instead of the old in-place `float(cost)`.
#: Old pin (pre-T17): the five functions only — both sides agree on every name here.
IMPORTED = {
    "claim_task",
    "create_agent_run",
    "finish_agent_run",
    "heartbeat_task",
    "update_task_status",
    "_as_column_decimal",
}

CLAIM_TASK_PARAMETERS = ["db", "worker", "lease_seconds", "task_id", "agent_type"]


def _tree() -> ast.Module:
    return ast.parse(CALLER_PATH.read_text(encoding="utf-8"))


def _imported_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.services.task_service":
            names.update(alias.name for alias in node.names)
    return names


def _call_sites(tree: ast.Module, names: set[str]) -> list[tuple[str, ast.Call]]:
    sites: list[tuple[str, ast.Call]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in names:
            sites.append((func.id, node))
        elif isinstance(func, ast.Attribute) and func.attr in names:
            sites.append((func.attr, node))
    return sites


def binding_problems(service) -> list[str]:
    """Every way the caller's call sites fail to bind against `service`'s signatures."""
    tree = _tree()
    imported = _imported_names(tree)
    problems: list[str] = []

    for name in sorted(imported):
        function = getattr(service, name, None)
        if not callable(function):
            problems.append(f"{name}: not a callable on the service module")

    for name, call in _call_sites(tree, imported):
        function = getattr(service, name, None)
        if not callable(function):
            continue  # already reported above
        if any(isinstance(arg, ast.Starred) for arg in call.args) or any(
            keyword.arg is None for keyword in call.keywords
        ):
            problems.append(f"{name} (line {call.lineno}): *args/**kwargs call cannot be checked")
            continue
        signature = inspect.signature(function)
        args = [object() for _ in call.args]
        kwargs = {keyword.arg: object() for keyword in call.keywords if keyword.arg}
        try:
            bound = signature.bind(*args, **kwargs)
        except TypeError as exc:
            problems.append(f"{name} (line {call.lineno}): {exc}")
            continue
        for parameter_name, parameter in signature.parameters.items():
            if parameter.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):
                continue
            if parameter.default is inspect.Parameter.empty and parameter_name not in bound.arguments:
                problems.append(
                    f"{name} (line {call.lineno}): call omits required parameter {parameter_name!r}"
                )
    return problems


# ─────────────────────────────────────────────────────────────────────────────
# the contract
# ─────────────────────────────────────────────────────────────────────────────
def test_caller_imports_exactly_the_service_names_it_uses():
    """RED leg: add any name to the caller's import that the service does not define.

    Renamed by T17 from `..._the_five_service_functions_...`: the caller now imports a sixth
    name — `_as_column_decimal`, the single money boundary — and the pin moved deliberately
    with it (old pin: the five functions; new pin: those five + the boundary).
    """
    imported = _imported_names(_tree())
    assert imported == IMPORTED, f"the caller's service imports changed: {sorted(imported)}"
    for name in sorted(imported):
        assert callable(getattr(task_service, name, None)), f"the service has no callable {name!r}"


def test_every_service_call_in_the_caller_binds_to_the_real_signature():
    """RED legs, both sides: rename a parameter in the service, or change a call in the caller.

    (Either the service file or `celery_app/tasks/agent_tasks.py` is enough; both were exercised
    in the receipt's red-leg runs.)
    """
    problems = binding_problems(task_service)
    assert problems == [], "the caller's calls do not bind to the service's signatures:\n" + "\n".join(
        problems
    )


def test_claim_task_is_keyword_only_because_the_caller_calls_it_that_way():
    """The reconciled shape: `claim_task(db, *, worker, lease_seconds, task_id, agent_type)`."""
    parameters = inspect.signature(task_service.claim_task).parameters
    assert list(parameters) == CLAIM_TASK_PARAMETERS, (
        "claim_task's parameter list changed — the caller calls these names"
    )
    assert parameters["db"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    for name in ("worker", "lease_seconds", "task_id", "agent_type"):
        assert parameters[name].kind is inspect.Parameter.KEYWORD_ONLY, (
            f"{name} must stay keyword-only: the caller passes it by keyword"
        )


def test_the_callers_claim_call_uses_the_reconciled_keywords():
    """The caller must pass `worker`/`task_id`/`lease_seconds` by keyword and only `db` positionally."""
    calls = [call for name, call in _call_sites(_tree(), {"claim_task"})]
    assert calls, "the caller no longer calls claim_task — this contract test is stale"
    for call in calls:
        keywords = {keyword.arg for keyword in call.keywords if keyword.arg}
        assert {"worker", "task_id", "lease_seconds"} <= keywords, (
            f"claim_task call at line {call.lineno} lost a keyword: {sorted(keywords)}"
        )
        assert len(call.args) == 1, "only the session `db` may be positional in the claim call"


def test_the_other_four_service_signatures_are_frozen():
    """Renaming any parameter the caller uses (or reordering) fails here."""
    expected = {
        "heartbeat_task": ["db", "task_id", "worker"],
        "update_task_status": ["db", "task_id", "status", "result_summary", "error_message"],
        "create_agent_run": [
            "db",
            "agent_type",
            "task_id",
            "input_context",
            "run_type",
            "cli_session_id",
            "tokens_input",
            "tokens_output",
            "cost_usd",
            "usage_json",
        ],
        "finish_agent_run": [
            "db",
            "run_id",
            "status",
            "output",
            "duration_secs",
            "error_message",
            "cli_session_id",
            "tokens_input",
            "tokens_output",
            "cost_usd",
            "usage_json",
        ],
    }
    for name, parameters in expected.items():
        actual = list(inspect.signature(getattr(task_service, name)).parameters)
        assert actual == parameters, f"{name} changed: {actual}"
