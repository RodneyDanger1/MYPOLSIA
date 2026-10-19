"""V12/W-B — the agent→tool allowlist wiring: policy_firewall + registry + doctrine.

Pins the frozen V12 mapping (competitor_research: web_fetch; business_planning / finance /
orchestrator / company_planner: macro_snapshot + pricing_catalog; everyone else: none), the
now-REAL tool leg of `decide()`, `execute_tool`'s denial path, the base_agent tool-doctrine
renderer in both runtime cases, and the invocation seam W-A's tool loop reads.

V24/L2 re-pin (deliberate): `web_search` now SHIPS and is granted to `competitor_research`
(its ROLE already instructed a search — "Use WebSearch to find candidates and WebFetch to read
the pages"), and `code_generation` — previously the tool-less "code" agent — holds
`code_write`/`code_read`/`code_list` confined to `settings.code_workspace_dir`.  The
`web_search does NOT ship` V12 pin is REVERSED here, by name, because the V24 plan freezes the
provider interface; every other agent's allowlist is unchanged.
"""
from __future__ import annotations

from app.agents import policy_firewall as pf
from app.agents import tool_registry
from app.agents.base_agent import BasePolsiaAgent
from app.agents.policy_firewall import ActionClass, ActionRequest, Decision, decide

#: V20/L2: `phone_read` joined the registry (read-only, codes structurally absent).
#: V24/L2: `web_search` + the three code tools joined — eight frozen names.
#: The V12 seam deliberately goes red when a tool joins; this is that re-pin, with the wave
#: recorded.
FROZEN = {"web_fetch", "web_search", "macro_snapshot", "pricing_catalog",
          "phone_read", "code_write", "code_read", "code_list"}
READ_MACRO_AGENTS = ("business_planning", "finance", "company_planner")
#: V24/L2: `code_generation` LEFT this list — it now owns the three code tools.
NO_TOOL_AGENTS = ("social_media", "email_outreach", "ads_management")


def _probe(agent_type: str) -> BasePolsiaAgent:
    class _Probe(BasePolsiaAgent):
        def run(self, task, context):
            return {"summary": "probe"}

    _Probe.agent_type = agent_type
    return _Probe()


# ── the mapping ──────────────────────────────────────────────────────────────────────────────

def test_the_frozen_agent_tool_mapping_is_wired():
    # V24/L2: the market-intelligence agent holds the search tool after `web_fetch` (append-only).
    assert tool_registry.tools_for("competitor_research") == ("web_fetch", "web_search")
    for agent_type in READ_MACRO_AGENTS:
        assert tool_registry.tools_for(agent_type) == ("macro_snapshot", "pricing_catalog")
    # V20/L2: the orchestrator already held owner-data reads, so `phone_read` joins its toolbox.
    assert tool_registry.tools_for("orchestrator") == (
        "macro_snapshot", "pricing_catalog", "phone_read")
    # V24/L2: the code agent finally has a filesystem surface (workspace-confined).
    assert tool_registry.tools_for("code_generation") == (
        "code_write", "code_read", "code_list")
    # V20/L2: customer_support's only toolbox entry is the read-only phone tool.
    assert tool_registry.tools_for("customer_support") == ("phone_read",)
    for agent_type in NO_TOOL_AGENTS:
        assert tool_registry.tools_for(agent_type) == (), agent_type
    assert tool_registry.tools_for("no_such_agent") == ()


def test_every_allowlisted_name_is_a_frozen_tool_spec():
    for capability in pf.AGENT_CAPABILITIES.values():
        assert set(capability.agent_tools) <= FROZEN


def test_web_search_ships_and_only_the_market_intelligence_agent_holds_it():
    """V24/L2 REVERSED the V12 "web_search does NOT ship" rule: the DeepSeek Search API is a
    real provider now, and the wave plan freezes the interface.  It is granted to exactly one
    agent — competitor_research, whose ROLE already said "Use WebSearch to find candidates" —
    so the other tool-less agents stay tool-less (least privilege, evidence in the RECEIPT)."""
    holders = {agent_type for agent_type, capability in pf.AGENT_CAPABILITIES.items()
               if "web_search" in capability.agent_tools}
    assert holders == {"competitor_research"}
    payload = tool_registry.execute_tool(
        "web_search", {"query": "x"}, agent_type="social_media")
    assert '"E_POLICY_DENIED"' in payload, "the firewall refuses it by name for a non-holder"


def test_competitor_research_holds_web_fetch_and_web_search_and_drops_the_stale_cli_search():
    cap = pf.AGENT_CAPABILITIES["competitor_research"]
    # V24/L2: append-only — the existing fetch stays first, the shipped search joins it.
    assert cap.agent_tools == ("web_fetch", "web_search")
    assert "WebSearch" not in cap.allowed_tools, (
        "CLI search was never honorable under the DeepSeek runtime; the REGISTRY tool replaced it"
    )
    assert "WebFetch" in cap.allowed_tools, "the CLI namespace keeps the CLI's own tool name"


def test_code_generation_gains_the_workspace_tools_without_gaining_a_cli_shell():
    """V24/L2: the code agent's registry surface is the workspace (write/read/list); its CLI
    walls are UNCHANGED — no push, no remote, no rm, no curl/wget, no MCP."""
    cap = pf.AGENT_CAPABILITIES["code_generation"]
    assert cap.agent_tools == ("code_write", "code_read", "code_list")
    assert "Bash(git push *)" in cap.denied_tools and "Bash(rm *)" in cap.denied_tools
    assert "mcp__*" in cap.denied_tools


# ── decide(): the (formerly dormant) tool leg ────────────────────────────────────────────────

def _tool_decision(agent_type: str, tool: str) -> pf.PolicyDecision:
    return decide(ActionRequest(agent_type=agent_type, action_class=ActionClass.READ,
                                channel="", tool=tool))


def test_registry_tools_are_allowed_for_mapped_agents():
    for agent_type, tool in (
        ("competitor_research", "web_fetch"),
        ("business_planning", "macro_snapshot"),
        ("finance", "pricing_catalog"),
        ("orchestrator", "macro_snapshot"),
        ("company_planner", "pricing_catalog"),
    ):
        decision = _tool_decision(agent_type, tool)
        assert decision.decision is Decision.ALLOW, (agent_type, tool, decision.reason)


def test_registry_tools_are_denied_for_unmapped_agents():
    for agent_type, tool in (
        ("social_media", "web_fetch"),
        ("code_generation", "web_fetch"),
        ("email_outreach", "macro_snapshot"),
        ("customer_support", "pricing_catalog"),
    ):
        decision = _tool_decision(agent_type, tool)
        assert decision.decision is Decision.DENY, (agent_type, tool)
        assert "not in allowlist" in decision.reason


def test_the_orchestrators_cli_deny_glob_does_not_gate_registry_tools():
    """`orchestrator` denies ALL CLI tools (`denied_tools=("*",)`) yet the frozen mapping gives
    it macro_snapshot/pricing_catalog: the registry namespace is its own gate."""
    assert _tool_decision("orchestrator", "macro_snapshot").decision is Decision.ALLOW
    assert _tool_decision("orchestrator", "Bash").decision is Decision.DENY


def test_an_exact_deny_entry_still_beats_registry_membership(monkeypatch):
    """Fail closed: if a future capability both lists a registry tool AND denies it by exact
    name, the deny wins."""
    from dataclasses import replace

    monkeypatch.setitem(
        pf.AGENT_CAPABILITIES, "probe_agent",
        replace(pf.AGENT_CAPABILITIES["competitor_research"],
                agent_tools=("web_fetch",), denied_tools=("web_fetch",)),
    )
    decision = _tool_decision("probe_agent", "web_fetch")
    assert decision.decision is Decision.DENY
    assert "not in allowlist" in decision.reason


def test_the_cli_pattern_leg_still_works_untouched():
    from app.agents.policy_firewall import decide

    assert _tool_decision("business_planning", "Read").decision is Decision.ALLOW
    assert _tool_decision("competitor_research", "WebFetch").decision is Decision.ALLOW
    denied = decide(ActionRequest(agent_type="code_generation", action_class=ActionClass.READ,
                                  channel="github", tool="mcp__github__create_issue"))
    assert denied.decision is Decision.DENY, "the L3-08 CLI semantics are unchanged"


# ── execute_tool denial path ─────────────────────────────────────────────────────────────────

def test_execute_tool_refuses_a_denied_agent_by_name():
    import json

    payload = json.loads(tool_registry.execute_tool("web_fetch", {"url": "http://x"}, agent_type="social_media"))
    assert payload["ok"] is False
    error = payload["error"]
    assert error["code"] == "E_POLICY_DENIED"
    assert "web_fetch" in error["message"] and "social_media" in error["message"]
    assert error["how_to_fix"]


# ── doctrine rendering (runtime-can-call vs no-tools) ────────────────────────────────────────

def _doctrine(agent_type: str, *, deepseek: bool, monkeypatch) -> str:
    from app.agents import deepseek_runtime

    monkeypatch.setattr(deepseek_runtime, "available", lambda: deepseek)
    agent = _probe(agent_type)
    return agent.layers_for({"title": "t"}, {"company": {"name": "Co"}})["tool_doctrine"].text


def test_doctrine_names_the_registry_tools_under_the_deepseek_runtime(monkeypatch):
    doctrine = _doctrine("competitor_research", deepseek=True, monkeypatch=monkeypatch)

    assert "web_fetch" in doctrine, "the callable registry tool must be named"
    assert "WebFetch" not in doctrine, "no CLI-tool claim survives under DeepSeek"
    assert tool_registry.TOOL_GUIDANCE["web_fetch"].split(",")[0][:30] in doctrine, (
        "a short WHEN-to-use line per tool"
    )


def test_doctrine_names_both_macro_tools_for_the_finance_family(monkeypatch):
    doctrine = _doctrine("finance", deepseek=True, monkeypatch=monkeypatch)

    assert "macro_snapshot" in doctrine and "pricing_catalog" in doctrine


def test_doctrine_keeps_the_honest_no_tools_text_when_the_list_is_empty(monkeypatch):
    doctrine = _doctrine("social_media", deepseek=True, monkeypatch=monkeypatch)

    assert "web_fetch" not in doctrine and "none" in doctrine
    assert "no tool" in doctrine.lower()


def test_doctrine_under_the_cli_runtime_lists_the_cli_names(monkeypatch):
    doctrine = _doctrine("competitor_research", deepseek=False, monkeypatch=monkeypatch)

    assert "WebFetch" in doctrine, "the CLI runtime's real tools are listed as before"
    assert "web_fetch" not in doctrine, "registry names are the DeepSeek runtime's namespace"


def test_doctrine_for_an_empty_cli_agent_says_none(monkeypatch):
    doctrine = _doctrine("social_media", deepseek=False, monkeypatch=monkeypatch)

    assert "none" in doctrine and "web_fetch" not in doctrine


# ── the invocation seam W-A's loop reads ─────────────────────────────────────────────────────

def _fake_roundtrip(monkeypatch, *, capture: dict):
    """Monkeypatch a whole fake round-trip; returns a runner that hooks `_build_invocation`."""
    from app.agents import claude_cli, deepseek_runtime

    def fake(*args, **kwargs):
        return claude_cli.InvokeResult(stdout='{"result": "ok"}', stderr="",
                                       exit_code=0, wall_seconds=0.0)

    monkeypatch.setattr(claude_cli, "run_with_retries", lambda dispatch, classify: (dispatch(), 1))
    monkeypatch.setattr(claude_cli, "invoke", fake)
    monkeypatch.setattr(deepseek_runtime, "invoke", fake)

    def run(agent, prompt):
        original = agent._build_invocation

        def build(prompt, **kwargs):
            inv = original(prompt, **kwargs)
            capture["inv"] = inv
            return inv

        agent._build_invocation = build  # type: ignore[method-assign]
        try:
            agent._real_call(prompt)
        finally:
            agent._build_invocation = original  # type: ignore[method-assign]

    return run


def _allowed_tools(inv) -> tuple[str, ...]:
    """The tool list riding the invocation — the `--allowedTools` argv value (W-A's seam:
    `plan_from_invocation` recovers flags from the argv; an absent flag means no tools)."""
    argv = inv.argv
    if "--allowedTools" not in argv:
        return ()
    return tuple(argv[argv.index("--allowedTools") + 1].split(","))


def test_real_call_hands_the_deepseek_loop_the_registry_tool_list(monkeypatch):
    from app.agents import deepseek_runtime

    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)
    capture: dict = {}
    run = _fake_roundtrip(monkeypatch, capture=capture)
    run(_probe("competitor_research"), "prompt")

    # V24/L2: the registry list rides the seam in allowlist order (fetch, then search).
    assert _allowed_tools(capture["inv"]) == ("web_fetch", "web_search"), (
        "the invocation carries the REGISTRY tools under the DeepSeek runtime"
    )


def test_real_call_keeps_the_cli_patterns_under_the_cli_runtime(monkeypatch):
    from app.agents import deepseek_runtime

    monkeypatch.setattr(deepseek_runtime, "available", lambda: False)
    capture: dict = {}
    run = _fake_roundtrip(monkeypatch, capture=capture)
    run(_probe("competitor_research"), "prompt")

    assert _allowed_tools(capture["inv"]) == ("WebFetch",), "CLI argv keeps CLI names"


def test_real_call_leaves_a_no_tool_agent_empty_under_deepseek(monkeypatch):
    from app.agents import deepseek_runtime

    monkeypatch.setattr(deepseek_runtime, "available", lambda: True)
    capture: dict = {}
    run = _fake_roundtrip(monkeypatch, capture=capture)
    run(_probe("social_media"), "prompt")

    assert _allowed_tools(capture["inv"]) == (), (
        "no --allowedTools flag == W-A's runtime behaves exactly as today"
    )
