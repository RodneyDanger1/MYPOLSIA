"""F-07 remainder (P1): a run's own verdict must reach EVERY seam, in text and in data.

WAVE-1 STATE (measured): the `[partial] ` label and `payload_status` existed at exactly ONE seam —
`crew_factory.run_agent_for_task`.  The planner's two agent steps call `agent.timed_run(...)`
DIRECTLY (`app/services/planner_service.py:_research_sync` / `_proposal_sync`), so a partial or
refused planner run reached its consumer as clean text with no verdict at all, and `_proposal_sync`
would go on to persist "candidates" from a refusal.

THE RULE (decided here, not deferred; see `app/agents/run_verdict.py`): `AgentEnvelope.status` is
the ONLY authority.  There is NO prose scan — the trial's `status: ok` runs each carried 3-7 honest
`notes` caveats, so a keyword rule would relabel genuine completions `[partial]` and fake a verdict.
`refused` raises the named `E_VALIDATION` (the task is FAILED, never completed); `partial` writes the
structured verdict (`payload_status` + `verdict`) and prefixes the visible `summary` once; `ok` is
untouched.  It is applied in `BasePolsiaAgent.timed_run`, the seam every run passes through.

RED leg (forge receipt): with `apply_run_verdict` removed from `timed_run`, the direct-seam tests
fail by name (no `payload_status`, unlabelled summary, a refused run returned instead of raising).
"""
from __future__ import annotations

import pytest

from app.agents import crew_factory
from app.agents.base_agent import BasePolsiaAgent
from app.agents.run_verdict import (
    PARTIAL_SUMMARY_PREFIX,
    PAYLOAD_STATUS_KEY,
    VERDICT_KEY,
    apply_run_verdict,
)
from app.core.errors import E_VALIDATION, PolsiaError

PARTIAL_PAYLOAD = {
    "summary": "Research run: supplier prices are quote-only; the positioning analysis is complete.",
    "status": "partial",
    "notes": ["three supplier pages are quote-only"],
}

REFUSED_PAYLOAD = {
    "summary": "Cannot honestly draft this RFQ: no supplier addresses exist in context.",
    "status": "refused",
    "notes": ["zero verified supplier email addresses in context"],
}


class _StubAgent(BasePolsiaAgent):
    """A shipped-shaped agent whose `run()` returns an exact payload (no model, no network).

    The DEFAULT payload is the partial one so the class itself is a valid no-arg agent class for
    `crew_factory.get_agent_class` (which instantiates `agent_class()`).
    """

    agent_type = "business_planning"
    output_schema = None
    default_payload: dict = PARTIAL_PAYLOAD

    def __init__(self, payload: dict | None = None) -> None:
        super().__init__()
        self._payload = dict(payload if payload is not None else type(self).default_payload)

    def run(self, task, context):  # noqa: ANN001 - the frozen BasePolsiaAgent seam
        return dict(self._payload)


@pytest.fixture(autouse=True)
def _real_run_labels(monkeypatch):
    """These tests pin the REAL-run ledger shape; the sanctioned mock seam is exempt from the label
    (its summary is a pinned fixture — see `run_verdict._label_summary_once`)."""
    monkeypatch.setattr("app.agents.base_agent.is_mock_mode", lambda: False)


PARTIAL_PAYLOAD = {
    "summary": "Research run: supplier prices are quote-only; the positioning analysis is complete.",
    "status": "partial",
    "notes": ["three supplier pages are quote-only"],
}

REFUSED_PAYLOAD = {
    "summary": "Cannot honestly draft this RFQ: no supplier addresses exist in context.",
    "status": "refused",
    "notes": ["zero verified supplier email addresses in context"],
}


# ─────────────────────────────────────────────────────────────────────────────────────────────
# the seam the planner uses: `agent.timed_run(...)` directly
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_a_partial_run_is_verdict_labelled_on_the_direct_timed_run_seam():
    result = _StubAgent(PARTIAL_PAYLOAD).timed_run({"title": "t"}, {})

    assert result["summary"].startswith(PARTIAL_SUMMARY_PREFIX), result["summary"]
    assert result[PAYLOAD_STATUS_KEY] == "partial"
    verdict = result[VERDICT_KEY]
    assert verdict["status"] == "partial"
    assert verdict["label"] == PARTIAL_SUMMARY_PREFIX.strip()
    assert verdict["source"] == "agent_envelope.status"
    assert "quote-only" in verdict["reason"]          # the run's own notes, not prose inference


def test_a_refused_run_raises_the_named_refusal_on_the_direct_timed_run_seam():
    with pytest.raises(PolsiaError) as excinfo:
        _StubAgent(REFUSED_PAYLOAD).timed_run({"title": "t"}, {})

    assert excinfo.value.code == E_VALIDATION
    context = excinfo.value.context
    assert context["refused"] is True
    assert context[PAYLOAD_STATUS_KEY] == "refused"
    assert context[VERDICT_KEY]["status"] == "refused"
    assert "no supplier addresses" in context["summary"]
    assert context["notes"] == ["zero verified supplier email addresses in context"]


def test_the_planner_proposal_step_cannot_consume_a_refused_run(monkeypatch):
    """The measured bypass: `_proposal_sync` runs the agent through `timed_run`, not the worker."""
    from app.services import planner_service

    monkeypatch.setattr(
        planner_service, "CompanyPlannerAgent", lambda: _StubAgent(REFUSED_PAYLOAD)
    )

    with pytest.raises(PolsiaError) as excinfo:
        planner_service._proposal_sync({"name": "Copper & Pine"}, {"competitors": []})

    assert excinfo.value.code == E_VALIDATION
    assert excinfo.value.context["refused"] is True


def test_the_planner_research_step_labels_a_partial_run(monkeypatch):
    from app.services import planner_service

    monkeypatch.setattr(
        planner_service, "CompetitorResearchAgent", lambda: _StubAgent(PARTIAL_PAYLOAD)
    )

    result = planner_service._research_sync({"name": "Copper & Pine"})

    assert result["summary"].startswith(PARTIAL_SUMMARY_PREFIX)
    assert result[PAYLOAD_STATUS_KEY] == "partial"


def test_an_ok_run_is_untouched_on_the_direct_seam():
    result = _StubAgent(
        {"summary": "Four competitors profiled with live sources.", "status": "ok", "notes": []}
    ).timed_run({"title": "t"}, {})

    assert result["summary"] == "Four competitors profiled with live sources."
    assert PAYLOAD_STATUS_KEY not in result
    assert VERDICT_KEY not in result


def test_an_absent_status_is_untouched_too():
    """No status is the contract's own default (`ok`); the guard invents no verdict."""
    result = _StubAgent({"summary": "done"}).timed_run({"title": "t"}, {})

    assert result["summary"] == "done"
    assert PAYLOAD_STATUS_KEY not in result


# ─────────────────────────────────────────────────────────────────────────────────────────────
# the rule itself: idempotence, mock-mode discipline, and the worker seam still covered
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_visible_label_is_applied_exactly_once():
    result = {"summary": "[partial] already labelled by the run", "status": "partial", "notes": []}

    apply_run_verdict("business_planning", result)
    apply_run_verdict("business_planning", result)

    assert result["summary"] == "[partial] already labelled by the run"


def test_mock_mode_keeps_the_pinned_summary_text_but_still_records_the_verdict(monkeypatch):
    """The sanctioned `CLAUDE_CLI_MOCK` summary is a pinned fixture; its VERDICT is still recorded."""
    monkeypatch.setattr("app.agents.base_agent.is_mock_mode", lambda: True)

    result = {"summary": "Mock Claude response for testing", "status": "partial", "notes": ["x"]}

    apply_run_verdict("business_planning", result)

    assert result["summary"] == "Mock Claude response for testing"
    assert result[PAYLOAD_STATUS_KEY] == "partial"
    assert result[VERDICT_KEY]["status"] == "partial"


def test_the_worker_seam_still_applies_the_rule_once(monkeypatch):
    """`run_agent_for_task` keeps its idempotent application (same payload, not double-labelled)."""
    monkeypatch.setattr(crew_factory, "get_agent_class", lambda agent_type: _StubAgent)
    monkeypatch.setattr(crew_factory, "dispatch_preflight", lambda agent_type, dispatch_paused=False: [])

    result = crew_factory.run_agent_for_task("business_planning", {"title": "t"}, {})

    assert result["summary"].count(PARTIAL_SUMMARY_PREFIX.strip()) == 1
    assert result[PAYLOAD_STATUS_KEY] == "partial"


def test_a_long_summary_labelled_on_the_direct_seam_keeps_the_full_text():
    payload = dict(PARTIAL_PAYLOAD, summary="x" * 3900)

    result = _StubAgent(payload).timed_run({"title": "t"}, {})

    assert result["summary"] == f"{PARTIAL_SUMMARY_PREFIX}{'x' * 3900}"
