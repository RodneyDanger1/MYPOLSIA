"""W8 / W7-S2 — taint is STICKY: an explicit CLEAN verdict may not clear the run's published taint.

The seam this file pins (wave-7 finding W7-S2, measured live): `side_effects._resolve_taint` used
to return the caller's explicit `RunTaint` unchecked, i.e. an explicit `RunTaint(tainted=False)`
OVERRIDDEN the run's published taint from `context_assembler.assemble()`.  A caller that passed a
clean verdict after scanning one narrow slice of its own input (the shape the approved-payload seam
uses) could therefore erase a taint the rest of the run's context had already produced — a
confused-deputy hole in the one consumer half of the taint chain.

The contract this file makes executable:

* an explicit verdict can ESCALATE (clean run + explicit taint) and it may carry EXTRA provenance
  (both tainted: union of flagged blocks/sources), but it can NEVER DE-ESCALATE (`tainted` is the
  OR of the run's verdict and the explicit one) — taint is sticky for the life of the run;
* `None` still means "use the run's verdict" (byte-identical to the pre-wave-8 behaviour every
  shipped caller relies on: `base_agent.consume_approved_payload` passes `None` unless its own
  scan found taint);
* a run with NO published verdict keeps the old explicit-wins behaviour (nothing to merge).

RED leg (before the fix): `test_an_explicit_clean_verdict_cannot_clear_the_runs_published_taint`
and `test_two_tainted_verdicts_merge_their_provenance` fail; the control tests pass either way.
"""
from __future__ import annotations

import pytest

from app.agents import policy_firewall as pf
from app.agents.policy_firewall import ActionClass, RateCaps, RunTaint
from app.agents.side_effects import _resolve_taint, evaluate_side_effect, request_side_effect

SECRET = b"w8-taint-test-secret"
CAPS = RateCaps(per_channel_day={}, per_channel_hour={})
EMAIL = dict(
    agent_type="email_outreach",
    action_class=ActionClass.EXTERNAL_REVERSIBLE,
    channel="email",
    action="send",
    payload={"to": "customer@example.com", "subject": "Your order", "body": "It ships tomorrow."},
    sends_today=0,
    sends_this_hour=0,
    sandbox_mode=False,
    caps=CAPS,
)


@pytest.fixture(autouse=True)
def _isolate_run_scope():
    def _clear() -> None:
        pf.reset_run_taint()
        pf.reset_run_approval()

    _clear()
    yield
    _clear()


# ── the defect: an explicit CLEAN verdict must not clear a published taint ────────────────────

def test_an_explicit_clean_verdict_cannot_clear_the_runs_published_taint():
    """RED before the fix: the side effect is ALLOWED because the explicit clean verdict wins."""
    pf.set_run_taint(RunTaint(tainted=True, flagged_blocks=[0], sources=["layer:task_brief"]))

    resolved = _resolve_taint(RunTaint(tainted=False))
    assert resolved is not None
    assert resolved.tainted is True, "an explicit CLEAN verdict cleared the run's published taint"

    outcome = request_side_effect(taint=RunTaint(tainted=False), approval_secret=SECRET, **EMAIL)

    assert outcome.allowed is False, outcome.reason
    assert outcome.requires_approval is True, outcome.reason
    assert "context tainted by untrusted content" in outcome.reason
    assert outcome.audit["taint"]["tainted"] is True
    assert outcome.audit["taint"]["sources"] == ["layer:task_brief"]


def test_the_propose_only_path_cannot_be_cleared_by_an_explicit_clean_verdict():
    """`evaluate_side_effect` (customer_support's refund escalation) shares the same resolver."""
    pf.set_run_taint(RunTaint(tainted=True, flagged_blocks=[2], sources=["inbox"]))

    decision = evaluate_side_effect(
        "email_outreach", ActionClass.EXTERNAL_REVERSIBLE, "email",
        action="send", sandbox_mode=False, caps=CAPS,
        taint=RunTaint(tainted=False),
    )

    assert decision.decision is pf.Decision.DENY, decision.reason
    # No approval channel was passed to the propose-only path, so the DENY names that absence; the
    # TAINT evidence rides the audit, and it must name the run's own source (not the clean verdict).
    assert decision.audit["reasons"] == ["context tainted by untrusted content"]
    assert decision.audit["taint"]["tainted"] is True
    assert decision.audit["taint"]["sources"] == ["inbox"]


def test_two_tainted_verdicts_merge_their_provenance_instead_of_dropping_one():
    """RED before the fix: the explicit verdict REPLACED the run's blocks/sources."""
    pf.set_run_taint(RunTaint(tainted=True, flagged_blocks=[0], sources=["layer:task_brief"]))

    resolved = _resolve_taint(RunTaint(tainted=True, flagged_blocks=[3],
                                       sources=["approved_payload:fp-1"]))

    assert resolved is not None and resolved.tainted is True
    assert sorted(resolved.flagged_blocks) == [0, 3]
    assert resolved.sources == ["layer:task_brief", "approved_payload:fp-1"]

    outcome = request_side_effect(
        taint=RunTaint(tainted=True, flagged_blocks=[3], sources=["approved_payload:fp-1"]),
        approval_secret=SECRET, **EMAIL,
    )
    assert outcome.allowed is False
    assert sorted(outcome.audit["taint"]["flagged_blocks"]) == [0, 3]
    assert set(outcome.audit["taint"]["sources"]) == {"layer:task_brief",
                                                      "approved_payload:fp-1"}


# ── controls: the escalation direction and the old contracts stay intact ──────────────────────

def test_an_explicit_tainted_verdict_still_escalates_a_clean_run():
    """An explicit verdict may always ESCALATE; the fix must not weaken this direction."""
    pf.set_run_taint(RunTaint(tainted=False))

    outcome = request_side_effect(
        taint=RunTaint(tainted=True, flagged_blocks=[1], sources=["approved_payload:fp-2"]),
        approval_secret=SECRET, **EMAIL,
    )

    assert outcome.allowed is False
    assert outcome.requires_approval is True
    assert "context tainted by untrusted content" in outcome.reason


def test_no_explicit_verdict_still_reads_the_runs_published_taint():
    """The shipped caller (`consume_approved_payload`) passes `None`; behaviour must not change."""
    published = RunTaint(tainted=True, flagged_blocks=[0], sources=["layer:task_brief"])
    pf.set_run_taint(published)

    assert _resolve_taint(None) is published

    outcome = request_side_effect(approval_secret=SECRET, **EMAIL)
    assert outcome.allowed is False
    assert outcome.audit["taint"]["sources"] == ["layer:task_brief"]


def test_no_published_verdict_keeps_the_explicit_verdict_verbatim():
    """A run that assembled no context has no verdict to merge; explicit still applies."""
    assert pf.current_run_taint() is None
    explicit = RunTaint(tainted=False, sources=["memory"])
    assert _resolve_taint(explicit) is explicit
    assert _resolve_taint(None) is None


def test_a_clean_run_with_an_explicit_clean_verdict_is_still_allowed():
    """The negative control: the fix must not turn ordinary clean sends into escalations."""
    pf.set_run_taint(RunTaint(tainted=False))

    outcome = request_side_effect(taint=RunTaint(tainted=False), approval_secret=SECRET, **EMAIL)

    assert outcome.allowed is True, outcome.reason
    assert outcome.e_code == ""
