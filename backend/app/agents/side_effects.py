"""Polsia runtime — sandbox semantics and third-party action execution, in one place.

The honesty rule this module enforces (SPEC-AGENTS.md §5.3, non-negotiable):

    A sandboxed action is recorded as a LABELLED DRAFT, never as a fake success.

1. The agent does every read/reasoning step normally and produces the real payload it *would*
   have sent (tweet text, email body, budget delta, PR description, refund request).
2. Instead of the mutating call it records a draft — and the returned dict carries
   `sandbox: true` + `drafted_not_sent: true` plus the refusal it received
   (`E_SANDBOX_BLOCKED`), so a dashboard can never show a non-event as a success.
3. No real id (`tweet_id`, `sendgrid_id`, `pr_url`, Stripe id) is ever fabricated: the code path
   that would perform the action is only reachable when the firewall ALLOWS it, and then it goes
   through `integration_call`, which returns a NAMED refusal when the integration is not
   installed or not configured — never a success-shaped empty result (BUILD-BRIEF §6.5).
"""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from typing import Any, Optional

from app.agents.policy_firewall import (
    ActionClass,
    ActionRequest,
    Decision,
    PolicyDecision,
    RateCaps,
    RunTaint,
    decide,
    enforce,
)
from app.core.errors import E_INTEGRATION, ERROR_HINTS, PolsiaError


def named_refusal(code: str, message: str, *, how_to_fix: str = "", **context: Any) -> dict:
    """The refusal shape carried inside an agent's output: `{"ok": False, "error": {...}}`."""
    return {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "how_to_fix": how_to_fix or ERROR_HINTS.get(code, ""),
            "context": context,
        },
    }


@dataclass
class SideEffectOutcome:
    """What the firewall said about one attempted side effect."""

    allowed: bool
    sandboxed: bool
    requires_approval: bool
    policy_code: str = ""            # POLSIA-Exxx
    e_code: str = ""                 # E_* ("" when allowed)
    reason: str = ""
    audit: dict = field(default_factory=dict)
    decision: Optional[PolicyDecision] = None

    @property
    def draft(self) -> bool:
        return self.sandboxed or self.requires_approval

    def draft_flags(self) -> dict:
        """The machine-checkable markers a drafted (not performed) action must carry."""
        if not self.draft:
            return {}
        flags = {"sandbox": bool(self.sandboxed), "drafted_not_sent": True,
                 "refused_code": self.e_code or "E_POLICY_DENIED",
                 "refused_reason": self.reason}
        if self.requires_approval:
            flags["requires_approval"] = True
        return flags

    def as_audit_record(self) -> dict:
        return {
            "decision": (self.decision.decision.value if self.decision else "unknown"),
            "policy_code": self.policy_code,
            "e_code": self.e_code,
            "reason": self.reason,
            "sandboxed": self.sandboxed,
            **{k: v for k, v in self.audit.items() if k != "ts"},
        }


def request_side_effect(
    agent_type: str,
    action_class: ActionClass,
    channel: str,
    *,
    action: str = "",
    value_usd: float = 0.0,
    payload: Optional[dict] = None,
    sends_today: int = 0,
    sends_this_hour: int = 0,
    taint: Optional[RunTaint] = None,
    approval_token: str = "",
    tool: str = "",
    sandbox_mode: Optional[bool] = None,
    caps: Optional[RateCaps] = None,
    approval_secret: bytes = b"",
) -> SideEffectOutcome:
    """Ask the firewall about one external action.

    Raises the named refusal for a policy/rate/kill denial (an agent must not continue past a
    real denial). Returns an outcome carrying `sandboxed=True` for a sandbox denial — the caller
    then records a labelled draft instead of performing the action.
    """
    req = ActionRequest(
        agent_type=agent_type,
        action_class=action_class,
        channel=channel,
        tool=tool,
        value_usd=float(value_usd),
        payload=dict(payload or {"action": action}),
        taint=taint,
        approval_token=approval_token,
        sends_today=sends_today,
        sends_this_hour=sends_this_hour,
    )
    decision = enforce(req, caps=caps, approval_secret=approval_secret, sandbox_mode=sandbox_mode)
    return SideEffectOutcome(
        allowed=decision.decision is Decision.ALLOW,
        sandboxed=decision.sandboxed,
        requires_approval=decision.decision is Decision.REQUIRE_APPROVAL,
        policy_code=decision.code,
        e_code=decision.e_code,
        reason=decision.reason,
        audit=decision.audit,
        decision=decision,
    )


def integration_call(
    module_name: str,
    attribute: str,
    *,
    agent_type: str,
    install_hint: str,
    call: Any = None,
    **kwargs: Any,
) -> dict:
    """Lazily import a third-party integration and make one call, honestly.

    Returns `{"ok": True, "result": ...}` or a named refusal:
      * `E_INTEGRATION` when the library is not installed or its credentials are unset — the
        deployment is not configured for this action, which is NOT a success;
      * `E_INTEGRATION` when the provider call itself fails, with the exception text and hint.

    Lazy import is mandatory (CLAUDE.md:116): importing tweepy/stripe/sendgrid at module import
    time would break every unit test that touches the registry.
    """
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        return named_refusal(
            E_INTEGRATION,
            f"{agent_type}: the {module_name!r} integration is not installed, so this action "
            f"cannot be performed for real",
            how_to_fix=install_hint,
            module=module_name,
            error=str(exc),
        )
    target = call if callable(call) else getattr(module, attribute, None)
    if target is None:
        return named_refusal(
            E_INTEGRATION,
            f"{agent_type}: {module_name}.{attribute} does not exist in the installed version",
            how_to_fix=install_hint,
            module=module_name,
            attribute=attribute,
        )
    try:
        result = target(**kwargs)
    except Exception as exc:  # provider failure: named, never swallowed
        return named_refusal(
            E_INTEGRATION,
            f"{agent_type}: {module_name}.{attribute} failed: {exc}",
            how_to_fix=ERROR_HINTS[E_INTEGRATION],
            module=module_name,
            attribute=attribute,
        )
    return {"ok": True, "result": result}


def evaluate_side_effect(
    agent_type: str,
    action_class: ActionClass,
    channel: str,
    *,
    action: str = "",
    value_usd: float = 0.0,
    payload: Optional[dict] = None,
    sends_today: int = 0,
    sends_this_hour: int = 0,
    sandbox_mode: Optional[bool] = None,
    caps: Optional[RateCaps] = None,
    approval_secret: bytes = b"",
) -> PolicyDecision:
    """The firewall's ANSWER without enforcement.

    Used where an agent must *record* what policy says rather than perform the action — e.g.
    customer_support proposing a refund: money never moves from the support agent (its capability
    has no `external_irreversible` class), so the decision is attached to the output as a named
    refusal and escalated, instead of raising and killing the run.
    """
    return decide(
        ActionRequest(
            agent_type=agent_type,
            action_class=action_class,
            channel=channel,
            value_usd=float(value_usd),
            payload=dict(payload or {"action": action}),
            sends_today=sends_today,
            sends_this_hour=sends_this_hour,
        ),
        caps=caps,
        approval_secret=approval_secret,
        sandbox_mode=sandbox_mode,
    )


def side_effect_receipt(outcome: "SideEffectOutcome", *, action: str, ref: str = "",
                        extra: Optional[dict] = None) -> dict:
    """One audit line for an attempted side effect (the shape `action_audit` stores)."""
    return {
        "action": action,
        "ref": ref,
        "performed": bool(outcome.allowed),
        **outcome.as_audit_record(),
        **(extra or {}),
    }


def assert_not_reporting_real_side_effect(payload: dict, *, agent_type: str, keys: tuple[str, ...]) -> None:
    """Fail loudly if sandbox mode is on and the payload still claims a real provider id.

    Called by the agents that could otherwise leak a fabricated `tweet_id`/`pr_url`; a sandboxed
    run must be impossible to mistake for a real one.
    """
    from app.config import settings

    if not settings.sandbox_mode:
        return
    present = [k for k in keys if payload.get(k)]
    if present:
        raise PolsiaError(
            E_INTEGRATION,
            f"{agent_type}: payload claims real provider ids {present} while SANDBOX_MODE=true",
            how_to_fix="record a labelled draft (sandbox/drafted_not_sent) instead of a real id",
            context={"agent_type": agent_type, "keys": present},
        )


__all__ = [
    "SideEffectOutcome",
    "assert_not_reporting_real_side_effect",
    "evaluate_side_effect",
    "integration_call",
    "named_refusal",
    "request_side_effect",
    "side_effect_receipt",
]
