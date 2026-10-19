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
import logging
import math
import re
import time
from contextvars import ContextVar
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

logger = logging.getLogger(__name__)

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
    # V12/W-B — the REGISTRY-tool allowlist: the `tool_registry` names this agent may CALL.
    # Deliberately a SEPARATE namespace from the two CLI fields above: those carry the Claude
    # CLI argv's permission-rule language (`"*"`, `"mcp__*"`, `WebFetch` — consumed by
    # `--allowedTools/--disallowedTools`), while `agent_tools` carries registry names
    # (`web_fetch`, ...) consumed by `decide()`'s tool leg and `tool_registry.execute_tool`.
    # Mixing them would deny the orchestrator everything (its `denied_tools=("*",)` is a CLI
    # rule) and would hand the CLI subprocess argv it cannot interpret.
    agent_tools: tuple[str, ...] = ()


# NOTE: every write-class action here still goes through decide(); allowlists restrict which
# TOOLS exist, decide() gates what they may DO.  `agent_tools` (V12/W-B) is enforced by
# `decide()`'s tool leg — `tool_registry.execute_tool` consults it on EVERY call.
AGENT_CAPABILITIES: dict[str, AgentCapability] = {
    "orchestrator": AgentCapability(
        allowed_tools=(), denied_tools=("*",), tools_available="",
        # V12/W-B: READ added — the frozen mapping grants this agent read-class REGISTRY tools
        # (macro_snapshot/pricing_catalog); external classes stay absent as before.
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE}), channels=(),
        # `denied_tools=("*",)` denies every CLI tool; the registry namespace below is separate
        # (frozen V12 mapping: the orchestrator reasons over macro + pricing facts).
        # V20/L2 — the owner asked that "agents/orchestrator can see/use the phone": the
        # orchestrator holds the READ action class and plans from the owner's real situation,
        # so the read-only `phone_read` joins its registry toolbox.  Codes are NEVER reachable
        # through it (`tool_registry`: no codes field exists; code-shaped bodies are withheld).
        # V24/L2 — DECISION: the orchestrator gets NO code_read/code_list.  Its own module
        # docstring is the evidence: "Tools: NONE ... The orchestrator is a planner: it reads
        # the context it is given and emits a plan. It cannot fetch, post or spend, which is the
        # entire point of keeping it tool-less (a planner that can act is a planner that can be
        # injected into acting)."  Its prompts name what it is given — company profile, KPIs,
        # yesterday's summary, the task board and the budget — and never the workspace, so a
        # workspace reader would widen a planner into an actor for no planning benefit.
        agent_tools=("macro_snapshot", "pricing_catalog", "phone_read"),
    ),
    "business_planning": AgentCapability(
        allowed_tools=("Read",), denied_tools=("Bash", "Edit", "Write", "WebFetch", "mcp__*"),
        # V12/W-B: READ added for the macro/pricing registry reads (frozen mapping); external
        # classes stay absent — reads + internal writes only.
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE}), channels=(),
        # V12/W-B: plans must read the macro snapshot + market-rate catalog before claiming
        # numbers (frozen mapping).
        agent_tools=("macro_snapshot", "pricing_catalog"),
    ),
    "competitor_research": AgentCapability(
        # V12/W-B: the stale ("WebFetch", "WebSearch") pair replaced HONESTLY.  `WebSearch` is
        # removed because the registry namespace is where a real call lives; advertising a CLI
        # search the DeepSeek runtime cannot run was a capability this build could not honor.
        # The CLI `WebFetch` entry stays: it is the CLI argv's own tool name; the DeepSeek
        # runtime's registry surface is the `agent_tools` below.
        allowed_tools=("WebFetch",),
        denied_tools=("Bash", "Edit", "Write", "Read", "mcp__*"),
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE}),
        channels=("web",),
        # V24/L2 — `web_search` SHIPS (the wave's frozen interface: DeepSeek
        # `POST https://api.deepseek.com/v1/tools/search`).  This is the ONE agent whose own
        # ROLE already instructed a search — "Use WebSearch to find candidates and WebFetch to
        # read the pages that matter (pricing pages, positioning pages, launch posts, funding
        # notes)" — so until V24 its prompt demanded a capability the runtime did not have.
        # Least privilege keeps every other agent's allowlist unchanged: `company_planner`
        # explicitly says "never ask for or attempt fetches of your own", `business_planning`
        # reasons from the context it is given, and the orchestrator's docstring is "It cannot
        # fetch, post or spend ... a planner that can act is a planner that can be injected into
        # acting".  `web_fetch` stays first (append-only re-pin).
        agent_tools=("web_fetch", "web_search"),
    ),
    # V12/W-B: company_planner WAS ABSENT from this map — `decide()` denied it as "unknown
    # agent" before the tool leg could even run, so the frozen mapping (macro_snapshot +
    # pricing_catalog for the planner) could not exist without an entry.  Least privilege:
    # read + internal writes, no channels, and the CLI fields mirror the old base_agent
    # fallback exactly (no CLI tool surface).
    "company_planner": AgentCapability(
        allowed_tools=(), denied_tools=(),
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE}), channels=(),
        agent_tools=("macro_snapshot", "pricing_catalog"),
    ),
    "social_media": AgentCapability(
        allowed_tools=(), denied_tools=("Bash", "Edit", "Write", "Read", "mcp__*"),
        tools_available="",
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE,
                                  ActionClass.EXTERNAL_REVERSIBLE}),
        # U5/P1 + V8/C4: every social platform the plan names. linkedin/facebook/instagram ship
        # adapters since U5; the tiktok/youtube adapters SHIPPED IN V8 (Content Posting API /
        # videos.insert) and `channel_service` now counts their published rows — measurable after
        # the first real publish, while an attempted send still refuses BY NAME until creds are
        # configured.
        channels=("twitter", "linkedin", "facebook", "instagram", "tiktok", "youtube"),
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
        # V20/L2 — this agent's whole job is reading the owner's inbound correspondence (its
        # `email` channel is the owner's support inbox), so it holds the same owner-data READ
        # class the orchestrator does and gets the read-only `phone_read`.  Every other
        # tool-less agent stays tool-less: the market/macro readers and the outbound senders do
        # not read the owner's private correspondence, so least privilege keeps them empty.
        # Codes are NEVER reachable through the tool (see `tool_registry.PHONE_CODE_WITHHELD`).
        agent_tools=("phone_read",),
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
        # V24/L2 — the registry was the hole in this agent's story: its duty is "Ship features"
        # / "implement small, verifiable changes", yet `agent_tools` was EMPTY, so on the
        # DeepSeek runtime it could only describe code in prose.  All three tools are bounded to
        # `settings.code_workspace_dir` (text only, ≤256 KiB, traversal-proof) and `code_write`
        # is gated as `internal_write`; they are NOT the repository, NOT a shell and NOT the
        # network — the existing git/PR walls above are unchanged.
        # V25/L2 — the four GitHub tools join THIS row and only this row (least privilege,
        # evidence quoted):
        #   * V25-PLAN "Remaining work" row 2: "**GitHub tools** — repo/branch/commit/PR behind
        #     the firewalled registry + secret store ... The code agent can actually ship code;
        #     source history like Polsia" — the recipient is named by the plan: the CODE agent.
        #   * This is the only agent whose ROLE is "Ship features" / "implement small,
        #     verifiable changes", the only one that already holds the `github` channel and the
        #     CLI `Bash(git status|diff|log|branch|add|commit *)` allowlist (and still denies
        #     `Bash(git push *)`, so the CLI path cannot publish either).
        #   * Its own V24 prompt doctrine ("write the ACTUAL deliverable ... prefer it over
        #     describing code") is what a commit completes; no other agent's prompt asks for a
        #     repository.  The outbound-channel agents, the planners and finance reason from the
        #     context they are given, and the orchestrator "cannot fetch, post or spend ... a
        #     planner that can act is a planner that can be injected into acting".  A
        #     source-control write for any of those would be privilege without a task.
        #   * Order is the use order (read -> branch -> commit -> PR); append-only, so the V24
        #     names keep their positions for every pre-V25 consumer.
        #   * The mutation tools are gated `external_reversible` in
        #     `tool_registry._TOOL_ACTION_CLASS`, so the firewall's sandbox/taint legs govern a
        #     commit exactly as they govern a post; `dry_run` defaults to true on top of that.
        agent_tools=("code_write", "code_read", "code_list",
                     "github_repo", "github_branch", "github_commit", "github_pr"),
    ),
    "finance": AgentCapability(
        allowed_tools=(), denied_tools=("Bash", "Edit", "Write", "Read", "mcp__*"),
        tools_available="",
        action_classes=frozenset({ActionClass.READ, ActionClass.INTERNAL_WRITE,
                                  ActionClass.EXTERNAL_IRREVERSIBLE}),  # refunds: approval-gated
        channels=("stripe",),
        # V12/W-B: budget/pricing reasoning reads the macro snapshot + catalog (frozen mapping).
        agent_tools=("macro_snapshot", "pricing_catalog"),
    ),
}


def capability_for(agent_type: str) -> AgentCapability | None:
    """Allowlist lookup by agent type.

    ADOPTED (V12/W-B, 2026-10-15): the R25 note below described the pre-V12 state — this
    function now has a PRODUCTION caller, `tool_registry.tools_for` (the tool allowlist read
    behind `execute_tool` + the base_agent tool doctrine), which resolves the R25-1
    adoption question instead of leaving it open.

    Historical note (R25 bounded scan, 2026-10-13; raw grep evidence:
    polsia-work/build/R25-deadhelpers/raw/scan_public_funcs.txt): exported in `__all__`
    but then called by nothing (production or tests); consumers read `AGENT_CAPABILITIES`
    directly.
    """
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
# Run-level taint: the contextvar the PRODUCER sets and the CONSUMER reads
# ---------------------------------------------------------------------------

#: The taint of the run executing in this context.
#:
#: Why a contextvar instead of a parameter (RD4 item 3, audit L2-F05 "the taint chain is dead"):
#: the content that taints a run is assembled in `context_assembler.assemble()`, which every agent
#: reaches through `base_agent.collect_structured` — but the side-effect request that must SEE the
#: taint happens later, inside per-agent code, at six call sites that would each have to accept and
#: thread a new argument.  A contextvar carries it through the whole call with zero call-site
#: edits: `assemble()` sets it from the real content, `request_side_effect` /
#: `evaluate_side_effect` read it whenever the caller supplied no explicit `taint=`.
#:
#: Isolation: a contextvar is per thread/task, so one run's verdict cannot bleed into an unrelated
#: concurrent run.  Each `assemble()` call OVERWRITES the value (clean content writes
#: `tainted=False`), so a tainted run cannot taint the next run in the same worker either;
#: `reset_run_taint()` restores the no-run state for tests and one-off callers.
#:
#: It lives HERE (not in context_assembler) because this module is the taint's home and both the
#: producer and the consumer already import it — no new import edge, no cycle.
RUN_TAINT: ContextVar[RunTaint | None] = ContextVar("polsia_run_taint", default=None)


def set_run_taint(taint: RunTaint | None) -> None:
    """Publish the taint of the run being assembled (producer side — `context_assembler`)."""
    RUN_TAINT.set(taint)


def current_run_taint() -> RunTaint | None:
    """The taint of the run executing in this context; None when no run assembled one."""
    return RUN_TAINT.get()


def reset_run_taint() -> None:
    """Clear the run taint (tests, or a caller that must not inherit a previous run's verdict).

    TEST-SUPPORT reset; NO PRODUCTION CALLER (R25 bounded scan, 2026-10-13; raw grep
    evidence: polsia-work/build/R25-deadhelpers/raw/scan_public_funcs.txt) — only tests
    call it (tests/unit/test_taint_chain.py, test_csinbox_prompt.py,
    test_extra_context_layer.py reset the contextvar between cases).  The production
    publish/read pair is `set_run_taint` (context_assembler.assemble) /
    `current_run_taint` (side_effects).  Removal vs adoption is an owner decision
    (REQUESTS R25-1).
    """
    RUN_TAINT.set(None)


# ---------------------------------------------------------------------------
# Run-level approval: the contextvar the WORKER sets and the firewall CONSUMER reads
# ---------------------------------------------------------------------------

@dataclass
class RunApproval:
    """The approval material ONE run executes with: the operator's secret + the supplied token.

    Both default to the exact "nothing" values the firewall's parameters already used (`b""` /
    `""`), so a run that publishes no approval is byte-identical to the pre-channel behaviour.
    """

    secret: bytes = b""
    token: str = ""


#: The approval context of the run executing in this context (self-approval channel, D1/T02).
#:
#: Why a contextvar, same reasoning as RUN_TAINT above: the run's approval material is decided by
#: the WORKER (it owns `settings.approval_secret` and the task row carrying `metadata.approval_token`),
#: while the side-effect request that must SEE it happens later inside per-agent code, at call sites
#: that would each have to accept and thread a new argument.  A contextvar carries it through the
#: whole call with zero per-agent edits: `agent_tasks._execute_agent_task` sets it before invoking
#: the agent (`asyncio.to_thread` copies the context into the run's thread — pinned by
#: tests/unit/api/test_t02_approvals.py), `request_side_effect` / `evaluate_side_effect` read it
#: whenever the caller supplied no explicit `approval_secret=`/`approval_token=`.  Each worker
#: invocation SETS it and RESETS it in a `finally`, so one run's token cannot leak into the next
#: run in the same worker process (the RUN_TAINT isolation argument).
RUN_APPROVAL: ContextVar[RunApproval | None] = ContextVar("polsia_run_approval", default=None)


def set_run_approval(approval: RunApproval | None) -> None:
    """Publish the approval material of the run about to execute (producer side — the worker)."""
    RUN_APPROVAL.set(approval)


def current_run_approval() -> RunApproval | None:
    """The approval material of the run executing in this context; None when no run published one."""
    return RUN_APPROVAL.get()


def reset_run_approval() -> None:
    """Clear the run approval (a worker's `finally`, or tests that must not inherit one).

    Clearing publishes `None` — "no approval channel for this context" — which the resolvers treat
    exactly as they treat a run that never set one, never as an error.
    """
    RUN_APPROVAL.set(None)


def configured_approval_secret() -> bytes:
    """The operator's `APPROVAL_SECRET`, read DEFENSIVELY at call time.

    `getattr` on purpose (the same pattern `kill_grade_from_env` uses for `kill_switch`): this read
    path keeps working against a config surface that predates the field instead of raising at
    dispatch, and an unset/empty value is `b""` — "no approval channel", which the firewall treats
    as the fail-closed DENY it always was (policy_firewall `decide()`: "approval required but no
    approval channel configured").
    """
    from app.config import settings

    raw = getattr(settings, "approval_secret", "")
    return raw.encode("utf-8") if isinstance(raw, str) and raw else b""


# ---------------------------------------------------------------------------
# Rate caps
# ---------------------------------------------------------------------------

@dataclass
class RateCaps:
    """Per-channel send caps. Counters come from the DB via `app.services.channel_service`
    (`social_posts` for twitter/linkedin/facebook/instagram, `email_logs` for email) so caps
    survive restarts — a channel with NO local send record is UNKNOWN, and a cap it cannot verify
    is refused by name (`cap_unknown`, E114) rather than read as zero sends. Values are policy,
    not physics."""
    per_channel_day: dict[str, int] = field(default_factory=lambda: {
        # D5 (owner decision, V8 — code-gap §10): the email numbers stay the CLONE's — 50/day
        # (+ 15/h below) — deliberately NOT the live product's published two-tier model ("one
        # email a day on the shared Polsia sending domain, five a day once you connect and
        # verify your own custom sending domain", dossier §8 line 99).  The tier needs a source
        # of truth for "own domain verified" that this build does not have (code-gap §10 item 1
        # is the queued resolver); shipping invented tier numbers would be policy theatre.
        # D6 (owner decision, V8 — code-gap §12): X/Twitter stays FLAT 12/day (+ 3/h below).
        # The live product nuances two things this flat number cannot express: the first "free
        # launch tweet" is exempt from the daily limit, and posting from Polsia's SHARED
        # account gets the smaller allowance while connecting your own X account raises it
        # (dossier §8 line 98).  The live numbers themselves are NOT FINDABLE — only the shape
        # is — so a split needs owner-decided numbers (code-gap §12); until then one documented
        # flat clone policy applies, stated here rather than implied.
        "email": 50, "twitter": 12, "linkedin": 4, "github": 6,
        "google_ads": 10, "meta_ads": 10, "stripe": 5, "render": 3,
        # U5/P1 + V8/C4 social platform coverage — the documented numbers (the caps route
        # auto-enumerates this map, its B9 companion). All six platforms now ship adapters;
        # tiktok/youtube joined the counting source in V8/C4, so their counts become measurable
        # once a real publish writes a row — before that an attempted send is refused by name,
        # never allowed on a fabricated zero.
        "facebook": 3, "instagram": 5, "tiktok": 10, "youtube": 3,
    })
    per_channel_hour: dict[str, int] = field(default_factory=lambda: {
        # D5/D6: the hourly numbers keep the same decisions as the day caps above.
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
    """Mint a single-use HMAC approval token for a gated action (TTL-bounded).

    The ISSUER is now live: `POST /api/v1/approvals` (owner-authenticated, D1/T02) calls this for a
    named action fingerprint and returns the token + expiry, and `agent_tasks` publishes
    `settings.approval_secret` plus the task's supplied token as the run's approval context, so a
    token that verifies lets the gated action PROCEED (`decide()`'s token branch).  Until D1/T02
    landed there was no production issuer and the firewall's "approval required but no approval
    channel configured" DENY was final for gated actions.

    The safety shape is unchanged and deliberate: a token is scoped to ONE fingerprint (it verifies
    only against the exact action+payload class it was minted for), TTL-bounded, and only useful when
    the operator has actually configured `APPROVAL_SECRET` — with no secret the resolver yields
    `b""`, `verify_approval` cannot match anything, and the DENY stands (fail closed).

    NOTE (still true): the token is bearer-style — anyone holding it AND the secret's cooperation
    can approve that fingerprint.  Delivery to the owner is the operator's channel.
    """
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
      3. tool allowlist (when a tool call is the action — i.e. the caller supplied `req.tool`).
         REAL since V12/W-B: `tool_registry.execute_tool` consults this leg on EVERY tool call.
         A `tool_registry` name is gated by `AgentCapability.agent_tools` (membership IS the
         allowlist; an exact deny entry wins); every other tool name keeps the CLI
         permission-rule language (allow globs must be anchored, deny globs always apply)
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
    if req.taint is not None:
        # The EVIDENCE, not just the verdict: an action_audit row (or a refusal a human reads) must
        # show WHY the run was escalated — which blocks were flagged and where they came from.
        audit["taint"] = {
            "tainted": req.taint.tainted,
            "flagged_blocks": list(req.taint.flagged_blocks),
            "sources": list(req.taint.sources),
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
        # V12/W-B — registry names first: when `req.tool` is one of the agent's `agent_tools`
        # (the tool_registry namespace) membership IS the allowlist, and the CLI globs below
        # deliberately do not speak about it — the orchestrator denies every CLI tool
        # (`denied_tools=("*",)`) yet the frozen V12 mapping lets it read macro/pricing.  An
        # EXACT deny entry for the same name still wins (fail closed).
        registry_ok = req.tool in cap.agent_tools and req.tool not in cap.denied_tools
        if not registry_ok:
            # Claude Code permission-rule parity (L3-08). DENY rules keep the wider glob
            # semantics (`"*"` and `mcp__*` are legal denies); an ALLOW rule that is an
            # unanchored glob (`"*"`, `"B*"`, `"mcp__*"`) is SKIPPED WITH A WARNING — it never
            # auto-approves anything, and a `mcp__`-with-parentheses rule is skipped the same
            # way. Fail closed: skipping shrinks the allowlist, never widens it.
            usable_allow: list[str] = []
            for pattern in cap.allowed_tools:
                if _allow_rule_usable(pattern):
                    usable_allow.append(pattern)
                else:
                    logger.warning(
                        "code=tool_allowlist_unanchored_glob: allow rule %r for %s is skipped "
                        "(unanchored globs never auto-approve; anchor it with a literal "
                        "mcp__<server>__ prefix or name the tool exactly)",
                        pattern,
                        req.agent_type,
                    )
            allowed = any(_tool_match(req.tool, pat) for pat in usable_allow)
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
    if external and req.taint is not None and req.taint.tainted:
        # RD4 item 3 / audit L2-F05 — this is the leg that was dead.  The check list this function
        # documents (see the docstring: "irreversible or tainted or >= $25 requires a human")
        # promised that a TAINTED run escalates; the code only *mentioned* taint inside the
        # irreversible branch, so a tainted run could still send an ordinary reversible email or
        # post.  A tainted run is one whose context (task text, memories, fetched content) carried
        # instruction-like content: escalating every external action out of that run to human
        # review is the entire point of the heuristic, which is explicitly never-permitting.
        # The heuristic is probabilistic, so over-escalation is the safe direction — and it is the
        # only direction implemented: REQUIRE_APPROVAL here, or the hard DENY below when no
        # approval channel exists.  It can never turn a deny into an allow.
        needs_approval = True
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


def _allow_rule_usable(pattern: str) -> bool:
    """May an ALLOW rule auto-approve?  (Claude Code permission-rule parity, L3-08.)

    The CLI's documented rule language: DENY rules accept globs in the tool-name position, but
    ALLOW rules accept a tool-name glob ONLY after a literal `mcp__<server>__` prefix — an
    unanchored allow glob (`"*"`, `"B*"`, `"mcp__*"`) is "skipped with a warning and doesn't
    auto-approve anything", and any `mcp__` rule with parentheses is skipped too.  Exact tool
    names and `Tool(specifier)` patterns are always usable.
    """
    if pattern.startswith("mcp__") and "(" in pattern:
        return False                      # Claude Code skips any mcp__ rule with parentheses
    if "(" in pattern:
        return pattern.endswith(")")      # Tool(specifier) form
    if "*" not in pattern:
        return True                       # exact tool name / bare mcp__<server>
    # A glob in the tool-name position: only legal after a literal mcp__<server>__ prefix.
    return pattern.startswith("mcp__") and "__" in pattern[5:]


def _tool_match(tool: str, pattern: str) -> bool:
    """Claude Code permission-rule semantics, completed (L3-08).

    * `"*"` matches every tool;
    * `'Bash(git diff *)'` prefix matching with the mandatory space before '*' (docs: without the
      space, `Bash(git diff*)` would also match `git diff-index`);
    * `mcp__<server>` matches that server's tools (`tool == pattern or tool.startswith(server + "__")`);
    * a prefix glob (`mcp__github__create*`) matches by `startswith` — the same meaning the CLI
      implements, so firewall-side and CLI-side tool policy agree about what `mcp__github__*` is;
    * otherwise exact equality (fail closed for shapes this matcher does not understand).

    The old three-shape version fell through to `tool == pattern` for globs, so `"mcp__*"` — the
    deny entry every capability carries — matched only the literal string `"mcp__*"`.
    """
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
        if inner.endswith("*"):
            # Trailing-star semantics per the docs: `Bash(git diff *)` — with the mandatory
            # space — matches `git diff HEAD` but NOT `Bash(git diff-index)` (the space is a
            # word boundary); without the space, `Bash(git diff*)` IS a raw prefix (it would
            # match `git diff-index`). The old `inner[:-2]` dropped the space, so the with-space
            # form wrongly matched `git diff-index` — firewall and CLI disagreed about it.
            return arg.startswith(inner[:-1])
        return arg == inner
    if pattern.startswith("mcp__") and "*" not in pattern:
        # The bare server form: `mcp__github` is every tool on that server.
        return tool == pattern or tool.startswith(pattern + "__")
    if pattern.endswith("*"):
        return tool.startswith(pattern[:-1])
    return tool == pattern


# ---------------------------------------------------------------------------
# enforcement helpers used by the agents
# ---------------------------------------------------------------------------

def kill_grade_from_env() -> str:
    """The operator's kill grade, read at CALL time: `POLSIA_KILL_SWITCH` env, then settings.

    Audit L2 (item RD4-1): the settings fallback read `settings.kill_switch`, a field `config.py`
    did not carry — so it was permanently `""` and an operator could set the switch in config and
    have it never read.  `config.py` now carries `kill_switch: str = ""` (added alongside this
    wave); the `getattr` fallback stays on purpose, so this read path keeps working against a
    config surface that predates (or differs from) that field instead of raising at dispatch.

    Precedence is deliberate — env first, settings second: an operator's shell-level stop is the
    fastest one and must win even while a settings file still says "run".  Both paths are pinned by
    tests/unit/test_kill_switch_settings.py, including a monkeypatched-settings leg that the
    missing field made impossible to write before.
    """
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

    ZERO CALLERS (R25 bounded scan, 2026-10-13; raw grep evidence:
    polsia-work/build/R25-deadhelpers/raw/scan_public_funcs.txt) — nothing imports or
    calls it (production or tests).  The wired form of this guard is
    `side_effects.assert_not_reporting_real_side_effect` (called at
    social_media/agent.py:167).  Removal vs adoption is an owner decision
    (REQUESTS R25-1).
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
    "RunApproval",
    "RunTaint",
    "assert_sandboxed_side_effect_allowed",
    "capability_for",
    "configured_approval_secret",
    "current_run_approval",
    "current_run_taint",
    "decide",
    "enforce",
    "fingerprint",
    "flag_injection_heuristics",
    "issue_approval",
    "kill_grade_from_env",
    "reset_run_approval",
    "reset_run_taint",
    "set_run_approval",
    "set_run_taint",
    "value_cents",
    "verify_approval",
    "wrap_untrusted",
]
