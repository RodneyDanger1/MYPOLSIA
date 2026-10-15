"""Wave V12 / G2 — REPEAT: the completed-task re-run spec (closer C2).

What this file pins (the frozen interface of `app/services/task_repeat.py`):

* Original evidence: the task detail modal carries an outlined `[⟳ REPEAT]` control on a
  COMPLETED task (`agent2-code/cap-06.md`).  Ours has no repeat seam on `/api/v1/tasks`
  (list/get/create/status-patch only) — this module is the pure spec half; route wiring is
  HANDOFF G2-H1.
* `repeat_spec(completed_task)` builds the create payload for the repeat: title / description /
  agent_type / priority copied, `status` NOT smuggled in (the store's `pending` default is the
  contract), and the source row's `metadata` NOT copied — one-shot provenance refs (the RD12
  `inbox_item` contract) must not be re-dispatched by a repeat.  The new task's metadata
  carries ONLY `repeat_of` + `repeat_kind`.
* Every non-completed state is refused BY NAME (E_VALIDATION + non-empty how_to_fix) with the
  ONE truthful alternative in the hint (aid3-beat/MATCH-VS-BEAT.md B3 grammar: failed -> retry,
  rejected -> edit & requeue, running states wait, approval states go through approvals).
* Malformed sources (missing/blank title, missing agent_type, non-int or boolean id) refuse by
  name too — a repeat never fabricates a field.
"""
from __future__ import annotations

from typing import Any

import pytest

from app.core.errors import E_VALIDATION, PolsiaError
from app.models.task import TaskStatus
from app.services.task_repeat import REPEATABLE_STATUS, repeat_spec, repeatable

#: the ONE truthful alternative each non-repeatable state's hint must name (loosely matched)
_ALTERNATIVE_KEYWORDS = {
    TaskStatus.PENDING.value: "run",
    TaskStatus.IN_PROGRESS.value: "wait",
    TaskStatus.FAILED.value: "retry",
    TaskStatus.REJECTED.value: "edit",
    TaskStatus.CANCELLED.value: "new task",
    TaskStatus.AWAITING_APPROVAL.value: "approve",
}


def _completed(**overrides: Any) -> dict[str, Any]:
    source: dict[str, Any] = {
        "id": 7,
        "status": TaskStatus.COMPLETED.value,
        "title": "Ship the landing page",
        "description": "Build and deploy the first version.",
        "agent_type": "code_generation",
        "priority": 2,
        "metadata": {"inbox_item": {"provider_id": "abc-1"}},
    }
    source.update(overrides)
    return source


# ── the completed case ────────────────────────────────────────────────────────


def test_completed_task_repeats_into_a_fresh_create_spec() -> None:
    spec = repeat_spec(_completed())
    create = spec["task_create"]
    assert create["title"] == "Ship the landing page"
    assert create["description"] == "Build and deploy the first version."
    assert create["agent_type"] == "code_generation"
    assert create["priority"] == 2
    assert spec["source_status"] == REPEATABLE_STATUS


def test_repeat_never_smuggles_status_or_source_metadata() -> None:
    spec = repeat_spec(_completed())
    assert "status" not in spec["task_create"]  # the store's pending default is the contract
    assert "metadata" not in spec["task_create"]
    assert spec["metadata"] == {"repeat_of": 7, "repeat_kind": "owner_repeat"}
    assert "inbox_item" not in spec["metadata"]  # one-shot provenance is NOT re-dispatched
    assert spec["notes"] and all(note.strip() for note in spec["notes"])


def test_optional_fields_omitted_honestly() -> None:
    spec = repeat_spec(_completed(description=None, priority=None))
    assert "description" not in spec["task_create"]  # absent source field -> absent, not ""
    assert "priority" not in spec["task_create"]


def test_repeatable_helper_matches_the_contract() -> None:
    assert repeatable(TaskStatus.COMPLETED.value) is True
    for status in _ALTERNATIVE_KEYWORDS:
        assert repeatable(status) is False
    assert repeatable("made_up") is False
    assert REPEATABLE_STATUS in {member.value for member in TaskStatus}


# ── refusals ──────────────────────────────────────────────────────────────────


def test_non_completed_states_refuse_by_name_with_the_one_alternative() -> None:
    for status, keyword in _ALTERNATIVE_KEYWORDS.items():
        with pytest.raises(PolsiaError) as refusal:
            repeat_spec(_completed(status=status))
        assert refusal.value.code == E_VALIDATION, status
        assert refusal.value.how_to_fix, status
        assert keyword in refusal.value.how_to_fix.lower(), status


def test_malformed_source_refuses_by_name() -> None:
    for bad in (
        _completed(id=None),
        _completed(id=True),      # bool is not an id
        _completed(id="7"),       # a typed refusal, no coercion
        _completed(id=0),
        _completed(title="   "),
        _completed(title=None),
        _completed(agent_type=None),
        _completed(status=None),
        _completed(status="mystery_state"),
    ):
        with pytest.raises(PolsiaError) as refusal:
            repeat_spec(bad)
        assert refusal.value.code == E_VALIDATION
        assert refusal.value.how_to_fix
