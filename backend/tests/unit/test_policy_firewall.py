"""The policy firewall — the component that decides whether an agent may send, post or spend.

It had NO test in the repo while being the last gate before every external side effect: the lane that
wrote it ran its own suite in a scratch directory and only six agent-layer tests were lifted. These are
the invariants a "did the firewall get weaker?" regression must break loudly on.

Test 1 is a real regression: `sandbox_mode: bool | None = None` used to be passed straight into
`decide(sandbox_mode: bool = True)`, and `None` is falsy — so an unspecified mode silently DISABLED the
sandbox, the opposite of the default and in the fail-open direction. RED leg: revert
`_resolve_sandbox_mode` to `return sandbox_mode` and
`test_unspecified_sandbox_mode_keeps_the_sandbox_on` fails (it observes `allowed is True`).
"""
from __future__ import annotations

import app.config as app_config
from app.agents.policy_firewall import (
    AGENT_CAPABILITIES,
    ActionClass,
    ActionRequest,
    Decision,
    RateCaps,
    decide,
)
from app.agents.side_effects import evaluate_side_effect


def _req(**overrides) -> ActionRequest:
    base = dict(
        agent_type="social_media",
        action_class=ActionClass.EXTERNAL_REVERSIBLE,
        channel="twitter",
        payload={"action": "tweet", "text": "hello"},
    )
    base.update(overrides)
    return ActionRequest(**base)


# ── 1. the sandbox must survive an unspecified mode ─────────────────────────────────────────────
def test_unspecified_sandbox_mode_keeps_the_sandbox_on(monkeypatch):
    monkeypatch.setattr(app_config.settings, "sandbox_mode", True)
    decision = evaluate_side_effect(
        "social_media", ActionClass.EXTERNAL_REVERSIBLE, "twitter",
        action="tweet", sandbox_mode=None,
    )
    assert decision.allowed is False, "an unspecified mode must not let an external action through"
    assert decision.sandboxed is True, "it must be recorded as a labelled draft, not performed"
    assert decision.code.startswith("POLSIA-E")


def test_explicit_sandbox_mode_false_is_still_honoured(monkeypatch):
    """The fix must not make an explicit opt-out impossible."""
    monkeypatch.setattr(app_config.settings, "sandbox_mode", True)
    decision = evaluate_side_effect(
        "social_media", ActionClass.EXTERNAL_REVERSIBLE, "twitter",
        action="tweet", sandbox_mode=False,
    )
    assert decision.allowed is True
    assert decision.sandboxed is False


# ── 2. identity and capability checks ───────────────────────────────────────────────────────────
def test_the_phantom_tenth_agent_is_denied_by_name():
    """`deployment` is in ALL_AGENT_TYPES but has no capability entry (the documented contradiction)."""
    assert "deployment" not in AGENT_CAPABILITIES
    decision = decide(_req(agent_type="deployment"))
    assert decision.decision is Decision.DENY
    assert decision.code == "POLSIA-E114"
    assert "deployment" in decision.reason


def test_an_agent_may_not_exceed_its_action_class():
    # business_planning is internal-write only: it has no business sending anything anywhere.
    decision = decide(_req(agent_type="business_planning", channel="email"))
    assert decision.decision is Decision.DENY
    assert "action class" in decision.reason


def test_a_channel_outside_the_capability_is_denied():
    # social_media may touch twitter and linkedin — never email.
    decision = decide(_req(channel="email"))
    assert decision.decision is Decision.DENY
    assert "channel" in decision.reason


# ── 3. the hard stops ───────────────────────────────────────────────────────────────────────────
def test_kill_switch_denies_everything():
    decision = decide(_req(), kill_switch=True, sandbox_mode=False)
    assert decision.decision is Decision.DENY
    assert decision.code == "POLSIA-E118"
    assert decision.allowed is False


def test_a_channel_rate_cap_denies_at_the_limit():
    caps = RateCaps(per_channel_day={"twitter": 5}, per_channel_hour={"twitter": 2})
    at_cap = decide(_req(sends_today=5), caps=caps, sandbox_mode=False)
    assert at_cap.decision is Decision.DENY
    assert at_cap.code == "POLSIA-E117"
    under = decide(_req(sends_today=4, sends_this_hour=1), caps=caps, sandbox_mode=False)
    assert under.decision is Decision.ALLOW, "one below the cap must still go through"


# ── 4. the money path: approval, and failing closed when there is no approval channel ───────────
def test_an_action_over_the_value_threshold_needs_a_human():
    decision = decide(_req(value_usd=25.0), approval_secret=b"secret", sandbox_mode=False)
    assert decision.decision is Decision.REQUIRE_APPROVAL
    assert decision.code == "POLSIA-E115"
    assert decision.require_token is True


def test_needing_approval_with_no_approval_channel_fails_closed():
    decision = decide(_req(value_usd=100.0), approval_secret=b"", sandbox_mode=False)
    assert decision.decision is Decision.DENY, "no approval channel must deny, never allow"
    assert decision.code == "POLSIA-E114"
    assert "approval" in decision.reason


def test_a_dollar_under_the_threshold_does_not_need_approval():
    decision = decide(_req(value_usd=24.99), approval_secret=b"secret", sandbox_mode=False)
    assert decision.decision is Decision.ALLOW


# ── 5. the approved path ────────────────────────────────────────────────────────────────────────
def test_a_valid_approval_token_lets_the_action_through():
    from app.agents.policy_firewall import fingerprint, issue_approval

    secret = b"unit-test-secret"
    fp = fingerprint(ActionClass.EXTERNAL_REVERSIBLE.value, "twitter", {"action": "tweet", "text": "hello"})
    token = issue_approval(secret, action_fingerprint=fp).token
    decision = decide(
        _req(approval_token=token, value_usd=30.0), approval_secret=secret, sandbox_mode=False
    )
    assert decision.decision is Decision.ALLOW
    assert decision.audit.get("approved") is True
