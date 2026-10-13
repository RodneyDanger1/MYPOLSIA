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
from typing import Any

from app.agents.policy_firewall import (
    ActionClass,
    ActionRequest,
    Decision,
    PolicyDecision,
    RateCaps,
    RunTaint,
    current_run_taint,
    decide,
    enforce,
)
from app.core.errors import E_INTEGRATION, ERROR_HINTS, PolsiaError


def _resolve_taint(taint: RunTaint | None) -> RunTaint | None:
    """The taint a side-effect request must carry: the explicit one, else the RUN's.

    RD4 item 3 / audit L2-F05 — the consumer half of the taint chain.  `context_assembler.assemble()`
    publishes the run's taint on the `RUN_TAINT` contextvar (set from the real content the prompt
    carried, through `flag_injection_heuristics`); every agent call site below would otherwise have
    to accept and thread a `taint=` argument.  Reading it here means all six callers
    (ads_management, code_generation, customer_support, email_outreach, finance, social_media)
    inherit it with no per-agent edit, an explicit `taint=` still WINS (callers that know better,
    and every existing test, keep their behaviour), and a run that assembled no context at all
    (no taint published) is `None` — "not known to be tainted", which the firewall treats exactly
    as it did before this change.
    """
    return taint if taint is not None else current_run_taint()


def _resolve_sandbox_mode(sandbox_mode: bool | None) -> bool:
    """Resolve "unspecified" to the configured default.

    Passing `None` straight through to `decide()` OVERRODE its fail-safe `sandbox_mode: bool = True`
    default: `decide(sandbox_mode=None)` makes every `if sandbox_mode:` check falsy, so an unspecified
    mode silently behaved as sandbox OFF — the opposite of the default, and in the fail-open direction.
    Resolving here keeps the firewall's own default meaningful.
    """
    # Local import on purpose: this module keeps `app.config` out of its import graph (same pattern
    # further down). Without it this helper raises NameError the first time it runs with
    # sandbox_mode=None — a bug the test that covers it caught immediately.
    from app.config import settings

    return settings.sandbox_mode if sandbox_mode is None else bool(sandbox_mode)


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
    decision: PolicyDecision | None = None

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
    payload: dict | None = None,
    sends_today: int | None = None,
    sends_this_hour: int | None = None,
    taint: RunTaint | None = None,
    approval_token: str = "",
    tool: str = "",
    sandbox_mode: bool | None = None,
    caps: RateCaps | None = None,
    approval_secret: bytes = b"",
) -> SideEffectOutcome:
    """Ask the firewall about one external action.

    `sends_today`/`sends_this_hour` carry a MEASURED count, or `None` = "no count was supplied =
    unknown" — the firewall refuses a configured cap whose count is unknown rather than assuming
    zero sends.

    `taint` is the run's taint when the caller passes it explicitly, else whatever
    `context_assembler.assemble()` published for THIS run (`_resolve_taint`).

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
        taint=_resolve_taint(taint),
        approval_token=approval_token,
        sends_today=sends_today,
        sends_this_hour=sends_this_hour,
    )
    decision = enforce(
        req, caps=caps, approval_secret=approval_secret,
        sandbox_mode=_resolve_sandbox_mode(sandbox_mode),
    )
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
    payload: dict | None = None,
    sends_today: int | None = None,
    sends_this_hour: int | None = None,
    taint: RunTaint | None = None,
    sandbox_mode: bool | None = None,
    caps: RateCaps | None = None,
    approval_secret: bytes = b"",
) -> PolicyDecision:
    """The firewall's ANSWER without enforcement.

    `sends_today`/`sends_this_hour` carry a MEASURED count, or `None` = "unknown" (a configured
    cap is then refused by name instead of being read as zero sends).

    `taint` (RD4 item 3): this path used to build its `ActionRequest` with NO taint at all, so the
    customer_support refund escalation — the caller that reaches `evaluate_side_effect` — could
    never see the run's taint.  It now resolves like `request_side_effect`: explicit argument first,
    else the run's published taint.

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
            taint=_resolve_taint(taint),
            sends_today=sends_today,
            sends_this_hour=sends_this_hour,
        ),
        caps=caps,
        approval_secret=approval_secret,
        sandbox_mode=_resolve_sandbox_mode(sandbox_mode),
    )


def side_effect_receipt(outcome: SideEffectOutcome, *, action: str, ref: str = "",
                        extra: dict | None = None) -> dict:
    """One audit line for an attempted side effect (the shape `action_audit` stores)."""
    return {
        "action": action,
        "ref": ref,
        "performed": bool(outcome.allowed),
        **outcome.as_audit_record(),
        **(extra or {}),
    }


def capped_item_refusal(
    exc: PolsiaError,
    *,
    agent_type: str,
    action: str,
    ref: str,
    channel: str,
) -> tuple[dict, dict]:
    """Receipt + refusal pair for one item a CHANNEL CAP refused mid-batch (L3-05).

    WHY this exists: the channel caps are check-and-consume — the agents forward
    `snapshot + performed` (what THIS run has already really performed) to the firewall, so a
    batch can now hit its channel cap part-way through.  `enforce()` RAISES the named refusal for
    a rate denial, and for the FIRST attempted item of a run that is exactly right: nothing was
    performed, so the run may fail loudly.  But once this run HAS performed at least one real
    send, letting the raise escape would (a) discard the performed work — only a completed run
    persists its artifacts — and (b) re-execute the batch on the retry (`E_RATE_LIMITED` is
    retryable), performing yet another real send against the stale pre-run snapshot.  So the
    loops record this pair instead: the item never reaches the provider, and the refusal is
    recorded BY NAME in `refusals[]` — never swallowed.

    Callers gate on `exc.code == E_RATE_LIMITED` (a cap denial) and re-raise anything else; this
    function only shapes the record, it never decides.  The receipt mirrors
    `side_effect_receipt(..., performed=False)` for a refused item.
    """
    context = dict(exc.context or {})
    receipt = {
        "action": action,
        "ref": ref,
        "performed": False,
        "decision": str(context.get("decision") or "deny"),
        "policy_code": str(context.get("policy_code") or ""),
        "e_code": exc.code,
        "reason": exc.message,
        "agent": agent_type,
        "channel": channel,
        **{k: v for k, v in context.items() if k not in ("decision", "policy_code")},
    }
    refusal = {
        "ok": False,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "how_to_fix": exc.how_to_fix or ERROR_HINTS.get(exc.code, ""),
            "context": context,
        },
    }
    return receipt, refusal


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
    "capped_item_refusal",
    "evaluate_side_effect",
    "integration_call",
    "named_refusal",
    "request_side_effect",
    "side_effect_receipt",
]
