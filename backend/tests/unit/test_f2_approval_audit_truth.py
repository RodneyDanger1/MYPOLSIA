"""F2 regression: the approval-clear audit keeps FINGERPRINTS, not stringified record dicts.

Live-proven on the Deny probe: `_clear_parked_approval` wrote
`"fingerprints": ["{'ref': ..., 'fingerprint': 'f2denyprobe…'}"]` — a Python dict repr, not an
identifier. The owner/API can neither clear nor audit that value. The parked set is a list of
record dicts today (and of fingerprint strings in legacy rows); both must reduce to the fingerprint.
"""
from __future__ import annotations

from app.models.task import Task
from app.services.task_service import APPROVAL_TOKEN_METADATA_KEY, _clear_parked_approval


def _task(metadata: dict) -> Task:
    return Task(title="audit probe", agent_type="email_outreach", metadata_json=metadata)


def test_a_parked_record_audits_its_fingerprint_not_the_dict_repr() -> None:
    task = _task({
        "approval_required": [
            {"action": "send", "ref": "a@example.invalid", "channel": "email",
             "fingerprint": "fp-one", "reason": "parked"},
            {"action": "send", "ref": "b@example.invalid", "channel": "email",
             "fingerprint": "fp-two", "reason": "parked"},
        ],
        APPROVAL_TOKEN_METADATA_KEY: "a-bearer-token",
    })

    assert _clear_parked_approval(task, status="cancelled", actor="owner") is True
    meta = task.metadata_json or {}

    assert meta["approval_required"] == []
    assert meta["approval_cleared"]["fingerprints"] == ["fp-one", "fp-two"]
    assert meta["approval_cleared"]["status"] == "cancelled"
    # The token dies with the set it was minted for.
    assert APPROVAL_TOKEN_METADATA_KEY not in meta


def test_legacy_string_fingerprints_are_kept_verbatim() -> None:
    task = _task({"approval_required": ["legacy-fp"], APPROVAL_TOKEN_METADATA_KEY: "tok"})

    assert _clear_parked_approval(task, status="failed", actor="update_task_status") is True

    assert task.metadata_json["approval_cleared"]["fingerprints"] == ["legacy-fp"]
