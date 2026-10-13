"""Polsia runtime — safety layer for an agent that can post, email, spend and merge.

Lifted from lane 05 (`research/05-runtime-100x/code/runtime/policy_firewall.py`) and wired to
`app.core.errors`, so a denied action leaves the firewall as a NAMED refusal
(`E_SANDBOX_BLOCKED` / `E_POLICY_DENIED` / `E_RATE_LIMITED`) instead of a boolean a caller can
ignore.

Threat model (verified sources, DESIGN-AGENT-RUNTIME.md §6):
  * indirect prompt injection through ingested content (competitor pages, customer emails,
    social mentions, GitHub issues) — OWASP LLM01:2025;
  * excessive agency / improper output handling — OWASP LLM06:2025, LLM05:2025;
  * unbounded consumption — OWASP LLM10:2025 (rate caps + budgets);
  * real incidents: EchoLeak (CVE-2025-32711, zero-click injection in Microsoft 365 Copilot,
    arXiv:2509.10540) and the Invariant Labs GitHub-MCP toxic flow (a malicious issue got private
    data exfiltrated into a public PR).

What is DETERMINISTIC (defence you can rely on):
  * every external side effect calls `decide()` first — allowlist, action class, caps,
    provenance, kill switch; a deny is final for the cycle;
  * untrusted content is wrapped and delimiter forgery is escaped;
  * irreversible actions require a single-use approval token when value/taint thresholds trip.

What is PROBABILISTIC (defence-in-depth, NOT a guarantee — labelled honestly):
  * `flag_injection_heuristics()` — regex cues. EchoLeak evaded a production classifier; assume
    this can be evaded too. It never *permits* anything; it only escalates to human review.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import time
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import Any

from app.core.errors import (
    E_POLICY_DENIED,
    E_RATE_LIMITED,
    E_SANDBOX_BLOCKED,
    PolsiaError,
)

# ---------------------------------------------------------------------------
# Action classes and decision objects
# ---------------------------------------------------------------------------

class ActionClass(StrEnum):
    READ = "read"
    INTERNAL_WRITE = "internal_write"
    EXTERNAL_REVERSIBLE = "external_reversible"
    EXTERNAL_IRREVERSIBLE = "external_irreversible"


class Decision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass
class PolicyDecision:
    decision: Decision
    reason: str
    code: str                            # POLSIA taxonomy code (stable, see structured_output)
    require_token: bool = False
    audit: dict = field(default_factory=dict)
    how_to_fix: str = ""                 # action-specific fix; falls back to ERROR_HINTS[code]

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW

    @property
    def sandboxed(self) -> bool:
        """A sandbox refusal is still a refusal — but the agent is allowed to keep a LABELLED
        draft instead of performing the action."""
        return bool(self.audit.get("sandbox"))

    @property
    def e_code(self) -> str:
        """The `app.core.errors` name for this decision."""
        if self.decision is Decision.ALLOW:
            return ""
        if self.sandboxed:
            return E_SANDBOX_BLOCKED
        if self.code == "POLSIA-E117":
            return E_RATE_LIMITED
        return E_POLICY_DENIED

    def to_polsia_error(self, *, agent_type: str, action: str = "") -> PolsiaError:
        from app.core.errors import ERROR_HINTS

        code = self.e_code
        return PolsiaError(
            code,
            f"{agent_type or 'agent'} action {action or self.audit.get('class', '')!r} refused: {self.reason}",
            how_to_fix=self.how_to_fix or ERROR_HINTS.get(code, ""),
            context={"decision": self.decision.value, "policy_code": self.code, **self.audit},
        )


# ---------------------------------------------------------------------------
# Per-agent tool allowlists (principle of least privilege)
# ---------------------------------------------------------------------------

@dataclass
class AgentCapability:
    allowed_tools: tuple[str, ...]          # CLI --allowedTools patterns
    denied_tools: tuple[str, ...] = ()      # CLI --disallowedTools patterns
    tools_available: str = "default"        # CLI --tools value; "" disables builtins
    action_classes: frozenset[ActionClass] = frozenset({ActionClass.READ})
    channels: tuple[str, ...] = ()          # external channels this agent may touch


# NOTE: every write-class action here still goes through decide(); allowlists restrict which
# TOOLS exist, decide() gates what they may DO.
AGENT_CAPABILITIES: dict[str, AgentCapability] = {
    "orchestrator": AgentCapability(
        allowed_tools=(), denied_tools=("*",), tools_available="",
        action_classes=frozenset({ActionClass.INTERNAL_WRITE}), channels=(),
    ),
    "business_planning": AgentCapability(
        allowed_tools=("Read",), denied_tools=("Bash", "Edit", "Write", "WebFetch", "mcp__*"),
        action_classes=frozenset({ActionClass.INTERNAL_WRITE}), channels=(),
    ),
    "competitor_research": AgentCapability(
        allowed_tools=("WebFetch", "WebSearch"),
        denied_tools=("Bash", "Edit", "Write", "Read", "mcp__*"),
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE}),
        channels=("web",),
    ),
    "social_media": AgentCapability(
        allowed_tools=(), denied_tools=("Bash", "Edit", "Write", "Read", "mcp__*"),
        tools_available="",
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE,
                                  ActionClass.EXTERNAL_REVERSIBLE}),
        channels=("twitter", "linkedin"),
    ),
    "email_outreach": AgentCapability(
        allowed_tools=(), denied_tools=("Bash", "Edit", "Write", "Read", "mcp__*"),
        tools_available="",
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE,
                                  ActionClass.EXTERNAL_REVERSIBLE}),
        channels=("email",),
    ),
    "customer_support": AgentCapability(
        allowed_tools=(), denied_tools=("Bash", "Edit", "Write", "Read", "mcp__*"),
        tools_available="",
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE,
                                  ActionClass.EXTERNAL_REVERSIBLE}),
        channels=("email",),
    ),
    "ads_management": AgentCapability(
        allowed_tools=(), denied_tools=("Bash", "Edit", "Write", "Read", "mcp__*"),
        tools_available="",
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE,
                                  ActionClass.EXTERNAL_REVERSIBLE}),
        channels=("google_ads", "meta_ads"),
    ),
    "code_generation": AgentCapability(
        allowed_tools=("Read", "Edit", "Write",
                       "Bash(git status *)", "Bash(git diff *)", "Bash(git log *)",
                       "Bash(git branch *)", "Bash(git add *)", "Bash(git commit *)"),
        denied_tools=("Bash(git push *)", "Bash(git remote *)", "Bash(rm *)",
                      "Bash(curl *)", "Bash(wget *)", "mcp__*"),
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE,
                                  ActionClass.EXTERNAL_REVERSIBLE,
                                  ActionClass.EXTERNAL_IRREVERSIBLE}),
        channels=("github",),
    ),
    "finance": AgentCapability(
        allowed_tools=(), denied_tools=("Bash", "Edit", "Write", "Read", "mcp__*"),
        tools_available="",
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE,
                                  ActionClass.EXTERNAL_IRREVERSIBLE}),  # refunds: approval-gated
        channels=("stripe",),
    ),
}


def capability_for(agent_type: str) -> AgentCapability | None:
    return AGENT_CAPABILITIES.get(agent_type)


# ---------------------------------------------------------------------------
# Untrusted-content quarantine
# ---------------------------------------------------------------------------

_FORGE = re.compile(r"</?\s*untrusted[^>]*>", re.IGNORECASE)


def wrap_untrusted(source: str, content_id: str, text: str, *, max_chars: int = 6000) -> str:
    """Wrap ingested content so the prompt can treat it as DATA.

    - delimiter forgery is escaped (`</untrusted>` inside content cannot close the block);
    - length capped (context budget + injection surface);
    - provenance (source + id) travels with the content for audit.
    """
    clipped = text[:max_chars]
    if len(text) > max_chars:
        clipped += f"\n[... clipped, {len(text) - max_chars} chars omitted ...]"
    safe = _FORGE.sub(lambda m: m.group(0).replace("<", "&lt;"), clipped)
    return (
        f'<untrusted source="{source}" content_id="{content_id}">\n'
        f"{safe}\n</untrusted>"
    )


_INJECTION_CUES = (
    re.compile(r"ignore (all |any )?(previous|prior|above) (instructions|rules)", re.I),
    re.compile(r"disregard (your|the) (instructions|system prompt)", re.I),
    re.compile(r"\byou are now\b|\bnew instructions\b|\bsystem:\s", re.I),
    re.compile(r"(send|email|post|publish|leak|exfiltrate|transfer|pay|refund)\b[^.\n]{0,60}(api[_ -]?key|password|credentials|secrets?|private)", re.I),
    re.compile(r"(api[_ -]?key|password|secret|token)\b[^.\n]{0,40}(send|email|post|include|attach|paste)", re.I),
    re.compile(r"https?://[^\s]+[?&](data|payload|q)=", re.I),
    re.compile(r"BEGIN (SYSTEM|INSTRUCTION)|###\s*instruction", re.I),
    re.compile(r"do not tell (the )?(user|human|operator)", re.I),
)


def flag_injection_heuristics(untrusted_blocks: list[str]) -> list[int]:
    """Return indexes of blocks that look instruction-like.

    PROBABILISTIC. Never used to *allow* an action; only to raise `tainted` on the run context,
    which forces irreversible actions into human review.
    """
    flagged = []
    for i, block in enumerate(untrusted_blocks):
        if any(cue.search(block) for cue in _INJECTION_CUES):
            flagged.append(i)
    return flagged


@dataclass
class RunTaint:
    tainted: bool = False
    flagged_blocks: list[int] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Rate caps
# ---------------------------------------------------------------------------

@dataclass
class RateCaps:
    """Per-channel send caps. Counters come from the DB via `app.services.channel_service`
    (`social_posts` for twitter/linkedin, `email_logs` for email) so caps survive restarts —
    a channel with NO local send record is UNKNOWN, and a cap it cannot verify is refused by
    name (`cap_unknown`, E114) rather than read as zero sends. Values are policy, not physics."""
    per_channel_day: dict[str, int] = field(default_factory=lambda: {
        "email": 50, "twitter": 12, "linkedin": 4, "github": 6,
        "google_ads": 10, "meta_ads": 10, "stripe": 5, "render": 3,
    })
    per_channel_hour: dict[str, int] = field(default_factory=lambda: {
        "email": 15, "twitter": 3, "github": 2,
    })


# ---------------------------------------------------------------------------
# Approval tokens (single-use, HMAC-signed, TTL)
# ---------------------------------------------------------------------------

@dataclass
class ApprovalToken:
    token: str
    action_fingerprint: str
    expires_at: float
    issued_to: str


def issue_approval(secret: bytes, *, action_fingerprint: str, ttl_s: int = 3600,
                   issued_to: str = "operator") -> ApprovalToken:
    exp = int(time.time()) + ttl_s
    msg = f"{action_fingerprint}|{exp}|{issued_to}".encode()
    sig = hmac.new(secret, msg, hashlib.sha256).hexdigest()[:32]
    return ApprovalToken(f"{exp}.{sig}", action_fingerprint, exp, issued_to)


def verify_approval(secret: bytes, token: str, *, action_fingerprint: str,
                    now: float | None = None, issued_to: str = "operator") -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except (ValueError, AttributeError):
        return False
    if (now or time.time()) > exp:
        return False
    want = hmac.new(secret, f"{action_fingerprint}|{exp}|{issued_to}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(want, sig)


def fingerprint(action_class: str, channel: str, payload: dict) -> str:
    canon = f"{action_class}|{channel}|{sorted(payload.items(), key=lambda kv: str(kv[0]))}"
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()[:24]


# ---------------------------------------------------------------------------
# The firewall: decide()
# ---------------------------------------------------------------------------

@dataclass
class ActionRequest:
    agent_type: str
    action_class: ActionClass
    channel: str                      # "", "email", "twitter", "github", "stripe", ...
    tool: str = ""                    # CLI tool name when the action is a tool call
    value_usd: float = 0.0            # monetary value at stake (spend, refund, budget change)
    payload: dict = field(default_factory=dict)
    taint: RunTaint | None = None
    approval_token: str = ""
    # Counters are `int | None` on purpose: `None` = the caller supplied no count = UNKNOWN, and
    # a configured cap is then refused BY NAME (POLSIA-E114, cap_unknown) instead of reading as
    # "zero sends"; `0` = a MEASURED zero (a counting source was read and found none).
    # Collapsing the two is exactly how the caps came to never fire (A2 finding F-04).
    sends_today: int | None = None
    sends_this_hour: int | None = None


APPROVAL_THRESHOLDS = {
    "value_usd": 25.0,                 # any action moving >= $25 needs a human
    "new_email_domain_batch": 10,      # first cold blast to a domain bigger than this: review
}

_CENT = Decimal("0.01")
#: The operator's configured price, as the exact whole cents the decision compares (A-T18):
#: 25.0 -> exactly 2500, byte for byte — never a float re-derivation of it.
_APPROVAL_VALUE_CENTS: int = int(
    Decimal(str(APPROVAL_THRESHOLDS["value_usd"])).quantize(_CENT, rounding=ROUND_HALF_UP) * 100
)


def value_cents(value_usd: float) -> int:
    """The ONE place a dollar float crosses into a policy decision (A-T18 / R11).

    The approval threshold is compared in exact whole cents: `Decimal(str(x))` recovers the
    decimal the value was written as (str() is the shortest round-tripping repr — measured:
    cents -> float -> cents round-trips exactly for 30,007 probe values), and the HALF_UP
    cent-quantisation absorbs sub-cent binary noise (`(0.7+0.1)*31.25` is 24.999999999999996
    in doubles but means $25.00).  Any rounding deviation is toward the human: a value within
    half a cent BELOW the threshold rounds onto it and needs approval, never the reverse.
    A non-finite value is unmeasurable and fails closed (the old float compare let `nan`
    through: `nan >= 25.0` is False — fail OPEN).
    """
    if not math.isfinite(value_usd):
        return _APPROVAL_VALUE_CENTS
    return int(Decimal(str(value_usd)).quantize(_CENT, rounding=ROUND_HALF_UP) * 100)


def _cap_count_unknown(channel: str, window: str, cap_value: int, audit: dict) -> PolicyDecision:
    """The named refusal for a configured cap whose counter has NO source (A2 finding F-04).

    `sends_today`/`sends_this_hour` is `None` when no local record exists to count — that is
    "unknown", not zero. Quietly passing the request would disable the cap entirely; a
    fabricated `0` would be a second fake gate. The safe answer is no, with the way out named.
    """
    return PolicyDecision(
        Decision.DENY,
        f"{window} cap for '{channel}' cannot be verified: no local record of '{channel}' "
        f"sends exists (count unknown)",
        "POLSIA-E114",
        audit={**audit, "cap_window": window, "cap": cap_value, "cap_unknown": True},
        how_to_fix=f"record {channel} sends in a local table the snapshot can count, or remove "
                   f"this channel's cap in RateCaps; until then the safe answer is no",
    )


def decide(
    req: ActionRequest,
    *,
    caps: RateCaps | None = None,
    approval_secret: bytes = b"",
    kill_switch: bool = False,
    sandbox_mode: bool = True,
) -> PolicyDecision:
    """The output-action firewall. EVERY external side effect calls this first.

    Order of checks (first DENY wins; REQUIRE_APPROVAL is sticky and checked after the denies):
      1. kill switch (E118)
      2. agent capability exists; action class permitted; channel permitted
      3. tool allowlist (when a tool call is the action)
      4. sandbox mode: external actions are recorded as dry-runs, nothing is sent
      5. rate caps (E117; E114 when a configured cap's count is unknown)
      6. taint/approval/value: irreversible or tainted or >= $25 requires a human (E115)
      7. approval token verification (missing/invalid -> REQUIRE_APPROVAL)
    """
    caps = caps or RateCaps()
    value_cents_now = value_cents(req.value_usd)   # the one boundary conversion (A-T18)
    audit: dict[str, Any] = {
        "agent": req.agent_type, "class": req.action_class.value, "channel": req.channel,
        "value_usd": req.value_usd, "value_cents": value_cents_now,
        # Annotation is load-bearing: `audit` holds a float (value_usd) and strings, so without it mypy
        # infers `dict[str, float | str]` and then reports `audit["fingerprint"]` as `float | str` where
        # `verify_approval` wants `str` — a narrowing artifact, not a runtime bug (fingerprint() -> str).
        "fingerprint": fingerprint(req.action_class.value, req.channel, req.payload),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if kill_switch:
        return PolicyDecision(Decision.DENY, "kill switch active", "POLSIA-E118", audit=audit)

    cap = AGENT_CAPABILITIES.get(req.agent_type)
    if cap is None:
        return PolicyDecision(Decision.DENY, f"unknown agent '{req.agent_type}'", "POLSIA-E114", audit=audit)
    if req.action_class not in cap.action_classes:
        return PolicyDecision(
            Decision.DENY,
            f"agent '{req.agent_type}' is not permitted action class '{req.action_class.value}'",
            "POLSIA-E114", audit=audit)
    if req.channel and req.channel not in cap.channels:
        return PolicyDecision(Decision.DENY,
                              f"agent '{req.agent_type}' may not touch channel '{req.channel}'",
                              "POLSIA-E114", audit=audit)

    if req.tool:
        allowed = any(_tool_match(req.tool, pat) for pat in cap.allowed_tools)
        denied = any(_tool_match(req.tool, pat) for pat in cap.denied_tools)
        if denied or not allowed:
            return PolicyDecision(Decision.DENY, f"tool '{req.tool}' not in allowlist", "POLSIA-E114", audit=audit)

    external = req.action_class in (ActionClass.EXTERNAL_REVERSIBLE, ActionClass.EXTERNAL_IRREVERSIBLE)
    # Sandbox BEFORE caps: the caps bound REAL sends, and a sandboxed run performs none. Checking
    # a (previously unwired) counter first turns a labelled draft into a run-killing denial — a
    # fail-loud where a draft is exactly the correct answer. Residual, recorded in the receipt: a
    # sandboxed over-cap attempt now records a draft instead of raising; nothing is sent either way.
    if external and sandbox_mode:
        return PolicyDecision(
            Decision.DENY,
            "SANDBOX_MODE=true: external action recorded as a labelled draft, nothing sent",
            "POLSIA-E114", audit={**audit, "sandbox": True})

    if req.channel:
        day_cap = caps.per_channel_day.get(req.channel)
        if day_cap is not None:
            if req.sends_today is None:
                return _cap_count_unknown(req.channel, "day", day_cap, audit)
            if req.sends_today >= day_cap:
                return PolicyDecision(Decision.DENY,
                                      f"daily cap for {req.channel}: {req.sends_today}/{day_cap}",
                                      "POLSIA-E117", audit=audit)
        hour_cap = caps.per_channel_hour.get(req.channel)
        if hour_cap is not None:
            if req.sends_this_hour is None:
                return _cap_count_unknown(req.channel, "hour", hour_cap, audit)
            if req.sends_this_hour >= hour_cap:
                return PolicyDecision(Decision.DENY,
                                      f"hourly cap for {req.channel}: {req.sends_this_hour}/{hour_cap}",
                                      "POLSIA-E117", audit=audit)

    needs_approval = False
    reasons: list[str] = []
    if external and req.action_class == ActionClass.EXTERNAL_IRREVERSIBLE:
        needs_approval = True                 # irreversible ALWAYS needs a human
        reasons.append("irreversible action")
        if req.taint and req.taint.tainted:
            reasons.append("context tainted by untrusted content")
    if value_cents_now >= _APPROVAL_VALUE_CENTS:
        needs_approval = True
        reasons.append(f"value ${value_cents_now / 100:.2f} >= ${APPROVAL_THRESHOLDS['value_usd']:.0f}")
    if needs_approval and not approval_secret and not sandbox_mode:
        # no approval infrastructure at all for something that needs approval => deny, fail closed
        return PolicyDecision(Decision.DENY,
                              "approval required but no approval channel configured",
                              "POLSIA-E114", audit={**audit, "reasons": reasons})

    if needs_approval:
        fp = audit["fingerprint"]
        if req.approval_token and verify_approval(approval_secret, req.approval_token, action_fingerprint=fp):
            return PolicyDecision(Decision.ALLOW, "approved by token", "POLSIA-E000",
                                  audit={**audit, "reasons": reasons, "approved": True})
        return PolicyDecision(Decision.REQUIRE_APPROVAL, "; ".join(reasons), "POLSIA-E115",
                              require_token=True, audit=audit)

    return PolicyDecision(Decision.ALLOW, "permitted", "POLSIA-E000", audit=audit)


def _tool_match(tool: str, pattern: str) -> bool:
    """Claude Code permission-rule semantics, simplified: 'Bash(git diff *)' prefix matching
    with the mandatory space before '*' (docs: without the space, `Bash(git diff*)` would also
    match `git diff-index`)."""
    if pattern in ("*",):
        return True
    if pattern.endswith(")") and "(" in pattern:
        name, inner = pattern.split("(", 1)
        inner = inner[:-1]
        if not tool.startswith(name):
            return False
        if tool == name:
            return True
        arg = tool[len(name) + 1:-1] if tool.startswith(name + "(") else ""
        if inner.endswith(" *"):
            return arg.startswith(inner[:-2])
        return arg == inner
    return tool == pattern


# ---------------------------------------------------------------------------
# enforcement helpers used by the agents
# ---------------------------------------------------------------------------

def kill_grade_from_env() -> str:
    import os

    from app.config import settings

    value = (os.getenv("POLSIA_KILL_SWITCH") or getattr(settings, "kill_switch", "") or "").strip().lower()
    if value in ("1", "freeze", "stop", "true"):
        return "freeze"
    if value in ("throttle", "reads"):
        return "throttle"
    return "run"


def enforce(
    req: ActionRequest,
    *,
    caps: RateCaps | None = None,
    approval_secret: bytes = b"",
    sandbox_mode: bool | None = None,
    kill_switch: bool | None = None,
) -> PolicyDecision:
    """`decide()` + the honest failure semantics.

    Returns the decision when it is ALLOW or REQUIRE_APPROVAL, and when it is a sandbox denial
    (the caller must then record a file-labelled draft: an operation that did not happen is not
    an error, but it is not a success either). Any other DENY raises the named refusal, because a
    policy denial must never be silently swallowed by an agent loop.
    """
    from app.config import settings

    decision = decide(
        req,
        caps=caps,
        approval_secret=approval_secret,
        kill_switch=bool(kill_switch) if kill_switch is not None
        else kill_grade_from_env() == "freeze",
        sandbox_mode=bool(settings.sandbox_mode if sandbox_mode is None else sandbox_mode),
    )
    if decision.decision is Decision.DENY and not decision.sandboxed:
        raise decision.to_polsia_error(agent_type=req.agent_type,
                                       action=req.payload.get("action") or req.action_class.value)
    return decision


def assert_sandboxed_side_effect_allowed(agent_type: str, action: str, *, decision: PolicyDecision) -> None:
    """Guard for the inverse mistake: a *successful* external action while sandboxed.

    Agents call this before writing a real result id (tweet id, PR url, Stripe id) so a
    sandboxed run can never be reportable as a success that posted something.
    """
    from app.config import settings

    if settings.sandbox_mode and decision.allowed:
        raise PolsiaError(
            E_SANDBOX_BLOCKED,
            f"{agent_type}: refusing to report a real '{action}' as done while SANDBOX_MODE=true",
            how_to_fix="the sandbox must record a labelled draft; check the firewall call order",
            context={"agent_type": agent_type, "action": action},
        )


__all__ = [
    "AGENT_CAPABILITIES",
    "APPROVAL_THRESHOLDS",
    "ActionClass",
    "ActionRequest",
    "AgentCapability",
    "ApprovalToken",
    "Decision",
    "PolicyDecision",
    "RateCaps",
    "RunTaint",
    "assert_sandboxed_side_effect_allowed",
    "capability_for",
    "decide",
    "enforce",
    "fingerprint",
    "flag_injection_heuristics",
    "issue_approval",
    "kill_grade_from_env",
    "value_cents",
    "verify_approval",
    "wrap_untrusted",
]
