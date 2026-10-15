"""REPEAT — the completed-task re-run spec (wave V12 / G2 engine lane).

Why this module exists (original-vs-ours, engine side — GAP-LEDGER row E10):

* Original evidence: the task detail modal carries an outlined ``[⟳ REPEAT]`` control on a
  Completed task (``agent2-code/cap-06.md``); tasks "run on the nightly cycle, or you can run
  them right away" (<https://help.polsia.com/create-a-task>, fetched 2026-10-15).
* Ours today: ``/api/v1/tasks`` exposes list / get / create / status-patch
  (``app/api/v1/tasks.py:72/:86/:113/:131``) — a completed task has no re-run seam, so the
  card's REPEAT has nothing to call.

This module is that seam's PURE half: validate the source and build the create-spec for the
repeat.  It persists NOTHING (route/service wiring is HANDOFF G2-H1) and it does NOT copy the
source row's ``metadata`` — provenance refs are one-shot contracts (the RD12 ``inbox_item``
reference in particular: copying one would re-dispatch a stale inbox item).  The new task's
metadata carries ONLY ``repeat_of`` + ``repeat_kind``; the task itself starts fresh under the
store's ``pending`` default (``status`` is never smuggled into the create payload).

State grammar (aid3-beat/MATCH-VS-BEAT.md B3, one truthful action per state): completed ->
REPEAT is this module; every other state refuses BY NAME with the correct alternative in
``how_to_fix`` — failed -> retry (the runtime's requeue path), rejected -> edit & requeue,
running states wait, approval states go through the approvals channel.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from app.core.errors import E_VALIDATION, ERROR_HINTS, PolsiaError
from app.models.task import TaskStatus

#: Only a COMPLETED task may be repeated (cap-06.md shows REPEAT on Completed).
REPEATABLE_STATUS = TaskStatus.COMPLETED.value

#: The ONE truthful alternative per non-repeatable state (B3 grammar; matched loosely by tests).
_ALTERNATIVE: dict[str, str] = {
    TaskStatus.PENDING.value: (
        "it is queued — a repeat would duplicate it; run it with the manual agent run, "
        "or leave it for the nightly cycle"
    ),
    TaskStatus.IN_PROGRESS.value: "it is already running — wait for the run to finish",
    TaskStatus.FAILED.value: (
        "a failed run is not repeated by cloning — use the retry/requeue path for its own error"
    ),
    TaskStatus.REJECTED.value: "edit the task and create a corrected copy instead",
    TaskStatus.CANCELLED.value: "create a new task instead",
    TaskStatus.AWAITING_APPROVAL.value: (
        "approve it (POST /api/v1/approvals) or cancel — repeating it would bypass the "
        "approval gate"
    ),
}

_STATUS_VALUES = tuple(member.value for member in TaskStatus)


def _refuse(field: str, value: object, expected: str, *, how_to_fix: str = "") -> NoReturn:
    raise PolsiaError(
        E_VALIDATION,
        f"task repeat refused: {field} {value!r} is invalid: {expected}",
        how_to_fix=how_to_fix or ERROR_HINTS[E_VALIDATION],
        context={"field": field, "value": value, "expected": expected},
    )


def _invalid_id(task_id: object) -> bool:
    return isinstance(task_id, bool) or not isinstance(task_id, int) or task_id <= 0


def repeatable(status: object) -> bool:
    """True only for the one repeatable state — the check the UI can pin a control to."""
    return status == REPEATABLE_STATUS


def repeat_spec(source: Mapping[str, Any]) -> dict[str, Any]:
    """Build the create-spec repeating a COMPLETED task, or refuse by name.

    ``source`` is a task row read as a mapping (the route passes the ORM row's fields).  The
    result::

        {
          "task_create": {"title", "agent_type", ["description"], ["priority"]},
          "metadata": {"repeat_of": <id>, "repeat_kind": "owner_repeat"},
          "notes": [...],               # what is deliberately NOT copied, in words
          "source_status": "completed",
        }

    ``task_create`` never carries ``status`` (the store's ``pending`` default is the contract)
    nor the source's ``metadata`` (one-shot provenance must not be re-dispatched).  A missing
    optional field is OMITTED, never fabricated as an empty string.
    """
    task_id = source.get("id")
    if _invalid_id(task_id):
        _refuse("id", task_id, "a positive integer task id")

    status = source.get("status")
    if not isinstance(status, str) or status not in _STATUS_VALUES:
        _refuse("status", status, f"one of the task statuses ({', '.join(_STATUS_VALUES)})")
    if status != REPEATABLE_STATUS:
        raise PolsiaError(
            E_VALIDATION,
            f"task {task_id} is {status!r} — only a completed task can be repeated",
            how_to_fix=_ALTERNATIVE.get(status, "check the task's status before repeating"),
            context={"task_id": task_id, "status": status},
        )

    title = source.get("title")
    if not isinstance(title, str) or not title.strip():
        _refuse("title", title, "a non-empty task title")
    agent_type = source.get("agent_type")
    if not isinstance(agent_type, str) or not agent_type.strip():
        _refuse("agent_type", agent_type, "a non-empty agent type")

    task_create: dict[str, Any] = {"title": title, "agent_type": agent_type}
    description = source.get("description")
    if isinstance(description, str) and description.strip():
        task_create["description"] = description
    priority = source.get("priority")
    if isinstance(priority, int) and not isinstance(priority, bool):
        task_create["priority"] = priority

    return {
        "task_create": task_create,
        "metadata": {"repeat_of": task_id, "repeat_kind": "owner_repeat"},
        "notes": [
            "the repeated task starts fresh (pending); run/result fields of the source are "
            "not copied",
            "source metadata is not copied — provenance refs (e.g. an inbox_item) are one-shot",
        ],
        "source_status": REPEATABLE_STATUS,
    }


__all__ = ["REPEATABLE_STATUS", "repeat_spec", "repeatable"]
