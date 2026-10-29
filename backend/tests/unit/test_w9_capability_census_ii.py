"""W9/CAPABILITY-AUDITOR — CAPABILITY CENSUS II (waves 4-8 surfaces).

WHY this file exists (audit II, after waves 4-8).  Wave 4's census
(`test_w4_capability_census.py`) pinned the fleet registry, the plan contract, the task→agent
capability gate, the tool action-class table, the required-argument behaviour, the manifest
payload and the prompt-vs-runtime tool names.  Between then and wave 9 the tree grew four
capability surfaces that census never covered:

  * the **provider-adapter layer** (waves 7-8): each agent's declared CHANNEL now has a shipped
    adapter that either performs when configured or refuses BY NAME — and nothing asserted that
    every declared channel has one, or that an unconfigured adapter can never report success;
  * the **module-docstring capability claims** in `app/agents/**`: they were written before
    V12/W-B added the REGISTRY tool allowlist, and several still tell a reader that an agent has
    "no tools" while the firewall that runs it grants real registry reads (the model is told the
    truth by the TOOL DOCTRINE layer; the developer-facing docstring was the stale copy);
  * the **direct-instantiation routing seams**: `planner_service` builds two agent classes
    directly instead of going through `crew_factory.run_agent_for_task`, so it can skip the
    fleet gates — a census must keep that seam set CLOSED and make the gates run there too;
  * the **email lifecycle vocabulary** and the **taint/approval transports** (waves 7-8), whose
    claims ("this tuple mirrors the schema", "every external action sees the run's taint and its
    approval") had no pin of their own.

Every pin below is DERIVED from the module that enforces it (no hand-typed second copy), and
every one of them fails when the thing it describes drifts.
"""
from __future__ import annotations

import asyncio
import importlib
import inspect
import pathlib
import re

import pytest

from app.agents import integrations, tool_registry
from app.agents import policy_firewall as pf
from app.agents.crew_factory import AGENT_MAP, get_agent_class
from app.config import settings
from app.core.errors import PolsiaError
from app.schemas import agent_outputs
from app.schemas.agent_outputs import EmailDraft
from app.services import (
    capability_manifest,
    deploy_service,
    email_lifecycle,
    planner_service,
    secret_store,
)

BACKEND = pathlib.Path(__file__).resolve().parents[2]
APP = BACKEND / "app"
CELERY = BACKEND / "celery_app"


def _agent_modules() -> dict[str, str]:
    """Every agent the capability map describes → its module path (derived, no second copy)."""
    modules = {agent_type: module for agent_type, (module, _cls) in AGENT_MAP.items()}
    modules["company_planner"] = "app.agents.company_planner.agent"
    return modules


# ─────────────────────────────────────────────────────────────────────────────────────────────
# A. module-docstring capability claims vs the registry that runs the agent
# ─────────────────────────────────────────────────────────────────────────────────────────────

#: Phrases that assert an agent has NO tools.  A docstring may say this only when the firewall's
#: registry allowlist for that agent really is empty — otherwise the prompt (TOOL DOCTRINE) and
#: the module that documents it disagree about the same run.
TOOLLESS_CLAIMS: tuple[str, ...] = (
    "tools: none",
    "tool-less",
    "has no tools",
    "(no tools",
    "no tools, no",
    "tools: `read` only",
    "cannot fetch, post or spend",
)


def test_no_agent_module_claims_tool_lessness_while_the_registry_grants_it():
    """The developer-facing docstring must agree with the allowlist the runtime enforces.

    MEASURED (wave 9, before the fix): four modules claimed tool-lessness while holding registry
    tools — `orchestrator` ("Tools: NONE … It cannot fetch, post or spend", 5 tools),
    `business_planning` ("Tools: `Read` only", 3), `customer_support` ("has no tools", 1:
    `phone_read`) and `finance` ("no tools", 3).  The model was told the truth by
    `base_agent.layers_for`; only the docstring was the stale copy, which is exactly the drift
    class this census exists to stop.
    """
    offenders: list[str] = []
    for agent_type, module_path in _agent_modules().items():
        granted = tool_registry.tools_for(agent_type)
        if not granted:
            continue                       # a genuinely tool-less agent may say so
        doc = importlib.import_module(module_path).__doc__ or ""
        lowered = doc.lower()
        hits = [phrase for phrase in TOOLLESS_CLAIMS if phrase in lowered]
        if hits:
            offenders.append(
                f"{agent_type} ({module_path}): claims {hits} but its registry allowlist is "
                f"{sorted(granted)}"
            )
    assert not offenders, (
        "module docstrings claim tool-lessness for agents that CAN call registry tools:\n  "
        + "\n  ".join(offenders)
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────
# B. every declared CHANNEL has a shipped performer — and never reports fake success
# ─────────────────────────────────────────────────────────────────────────────────────────────

class _Performer:
    """One declared channel's shipped entry point, derived from the module that owns it."""

    __slots__ = ("channel", "resolve", "status", "evidence")

    def __init__(self, channel: str, resolve, status: str, evidence: str) -> None:
        self.channel = channel
        self.resolve = resolve
        self.status = status          # performs_when_configured | refuses_when_configured | tool_backed
        self.evidence = evidence


def _platform(platform: str):
    return lambda: integrations._PLATFORM_ADAPTERS[platform]


#: channel → the ONE entry point that can act on it.  Derived from the dispatch tables
#: (`integrations._PLATFORM_ADAPTERS` / the adapter functions / the registry runner / the deploy
#: service), never from prose.  `status` is the measured truth this audit reports:
#:   * performs_when_configured — a real provider call behind a credential gate;
#:   * refuses_when_configured  — the adapter exists but ALWAYS answers with a named refusal
#:     (this deployment ships no account-scoped write client: integrations.py:898-904);
#:   * tool_backed              — the channel is reached through a registry tool, not an adapter.
CHANNEL_PERFORMERS: dict[str, _Performer] = {
    **{
        platform: _Performer(platform, _platform(platform), "performs_when_configured",
                             "app/agents/integrations.py:600-607")
        for platform in ("twitter", "linkedin", "facebook", "instagram", "tiktok", "youtube")
    },
    "email": _Performer("email", lambda: integrations.send_email, "performs_when_configured",
                        "app/agents/integrations.py:663 (reply path: customer_support/send_bridge.py)"),
    "github": _Performer("github", lambda: integrations.open_pull_request,
                         "performs_when_configured", "app/agents/integrations.py:768"),
    "stripe": _Performer("stripe", lambda: integrations.refund_payment,
                         "performs_when_configured", "app/agents/integrations.py:804"),
    "google_ads": _Performer("google_ads", lambda: integrations.update_ad_budget,
                             "refuses_when_configured", "app/agents/integrations.py:870-904"),
    "meta_ads": _Performer("meta_ads", lambda: integrations.update_ad_budget,
                           "refuses_when_configured", "app/agents/integrations.py:870-904"),
    "render": _Performer("render", lambda: deploy_service.deploy_site, "performs_when_configured",
                         "app/services/deploy_service.py:deploy_site (dry-run by default)"),
    "web": _Performer("web", lambda: tool_registry._RUNNERS["web_fetch"], "tool_backed",
                      "app/agents/tool_registry.py:_run_web_fetch (web_search is the search leg)"),
}


def _declared_channels() -> set[str]:
    declared = {c for cap in pf.AGENT_CAPABILITIES.values() for c in cap.channels}
    declared |= {c for cap in pf.DIRECT_CALLER_CAPABILITIES.values() for c in cap.channels}
    return declared


def test_every_declared_channel_has_exactly_one_shipped_performer():
    """A channel is a capability CLAIM: without a performer the firewall would permit a send no
    code can make.  Both directions are asserted, so a removed adapter and an undeclared adapter
    each fail here."""
    declared = _declared_channels()
    served = set(CHANNEL_PERFORMERS)
    assert declared == served, (
        f"declared channels with no performer: {sorted(declared - served)}; "
        f"performers for channels no agent may touch: {sorted(served - declared)}"
    )


def test_every_channel_performer_resolves_to_a_real_callable():
    """No dead pointer: the table above must resolve through the shipped module."""
    for channel, performer in CHANNEL_PERFORMERS.items():
        target = performer.resolve()
        assert callable(target), f"{channel}: {performer.evidence} does not resolve to a callable"


def test_the_web_channel_is_really_tool_backed():
    """The one channel that is not an adapter is served by the registry tools its holder holds."""
    holders = [a for a, cap in pf.AGENT_CAPABILITIES.items() if "web" in cap.channels]
    assert holders, "no agent declares the 'web' channel"
    search_tools = {"web_fetch", "web_search"}
    for agent_type in holders:
        granted = set(tool_registry.tools_for(agent_type))
        assert granted & search_tools == search_tools, (
            f"{agent_type} declares the 'web' channel but holds {sorted(granted & search_tools)} "
            f"of the web tools — a channel with no callable behind it"
        )
        specs = {spec["function"]["name"] for spec in tool_registry.TOOL_SPECS}
        assert search_tools <= specs


_MUTATING_CALLS: dict[str, callable] = {
    # text-only legs answer at the CREDENTIAL gate (twitter/linkedin/facebook cannot upload media
    # yet — a separate, separately-named refusal); the three media-only platforms get a URL
    # because their API has no text-only publish.
    "twitter": lambda: integrations.post_tweet("hello", platform="twitter"),
    "linkedin": lambda: integrations.post_tweet("hello", platform="linkedin"),
    "facebook": lambda: integrations.post_tweet("hello", platform="facebook"),
    "instagram": lambda: integrations.post_tweet(
        "hello", platform="instagram", image_url="https://cdn.example.com/x.jpg"),
    "tiktok": lambda: integrations.post_tweet(
        "hello", platform="tiktok", image_url="https://cdn.example.com/x.jpg"),
    "youtube": lambda: integrations.post_tweet(
        "hello", platform="youtube", image_url="https://cdn.example.com/x.jpg"),
    "email": lambda: integrations.send_email(to="a@example.com", subject="s", body="b"),
    "github": lambda: integrations.open_pull_request(
        repo="owner/name", branch="b", title="t", body="b"),
    "stripe": lambda: integrations.refund_payment(payment_id="pi_test"),
    "google_ads": lambda: integrations.update_ad_budget(
        platform="google_ads", campaign_ref="c", budget_change_usd=1.0),
    "meta_ads": lambda: integrations.update_ad_budget(
        platform="meta_ads", campaign_ref="c", budget_change_usd=1.0),
    "render": lambda: asyncio.run(deploy_service.deploy_site("no-such-site")),
}

_BLANK_SETTINGS = (
    "sendgrid_api_key", "sendgrid_from_email", "twitter_api_key", "twitter_api_secret",
    "twitter_access_token", "twitter_access_secret", "linkedin_access_token",
    "linkedin_author_urn", "facebook_page_id", "facebook_page_access_token",
    "instagram_user_id", "instagram_access_token", "tiktok_access_token",
    "tiktok_open_id", "youtube_access_token", "github_token", "stripe_secret_key",
    "google_ads_developer_token", "google_ads_client_id", "google_ads_client_secret",
    "google_ads_refresh_token", "google_ads_customer_id", "meta_ads_access_token",
    "meta_ads_account_id", "render_api_key", "render_service_id",
)


@pytest.fixture()
def unconfigured(monkeypatch):
    """Every provider credential blank (settings + secret store), so each adapter answers at its
    own gate: this is the ONLY way to exercise all twelve adapters offline without a network."""
    for name in _BLANK_SETTINGS:
        if hasattr(settings, name):
            monkeypatch.setattr(settings, name, "", raising=False)
    monkeypatch.setattr(secret_store, "get_secret", lambda *_a, **_k: None)


def _refusal_shape(payload: object) -> tuple[bool, str, str]:
    """`(is_success, code, how_to_fix)` from an adapter answer (dict or a raised PolsiaError)."""
    if isinstance(payload, dict):
        error = payload.get("error") or {}
        return bool(payload.get("ok")), str(error.get("code") or ""), str(
            error.get("how_to_fix") or ""
        )
    return True, "", ""


@pytest.mark.parametrize("channel", sorted(_MUTATING_CALLS))
def test_an_unconfigured_channel_refuses_by_name_and_never_reports_success(channel, unconfigured):
    """The wave-7/8 claim under test: "each adapter refuses by name when it is not configured".

    A fabricated success here is the worst possible defect (a send that never happened, recorded
    as done), so the assertion is on the ANSWER, not on an exception being raised.
    """
    try:
        payload = _MUTATING_CALLS[channel]()
    except PolsiaError as exc:
        # A raised refusal is acceptable ONLY if it is named and explains the fix.
        assert exc.code.startswith("E_"), f"{channel}: unnamed failure {exc.code!r}"
        assert str(exc.message).strip(), f"{channel}: empty refusal message"
        return
    ok, code, how_to_fix = _refusal_shape(payload)
    assert ok is False, f"{channel} reported SUCCESS with no credentials configured: {payload!r}"
    assert code, f"{channel}: refusal carries no error code: {payload!r}"
    assert how_to_fix.strip(), f"{channel}: refusal names no way forward: {payload!r}"


def test_a_refuses_when_configured_channel_really_has_no_provider_client():
    """`update_ad_budget` is the one adapter that answers with a refusal even when configured —
    "this deployment ships no account-scoped write client" (integrations.py:898-904).  The claim
    is only honest while no provider client is reachable from it."""
    source = inspect.getsource(integrations.update_ad_budget)
    assert "named_refusal" in source, (
        "update_ad_budget must answer with a NAMED refusal, never a silent no-op"
    )
    for client in ("google.ads", "facebook_business", "FacebookAdsApi", "GoogleAdsClient"):
        assert client not in source, (
            f"update_ad_budget now reaches {client!r} — the documented "
            "'no account-scoped write client' refusal (README:279-280) is stale"
        )


# ─────────────────────────────────────────────────────────────────────────────────────────────
# C. routing / delegation: the dispatch seams are CLOSED, and the direct ones pass the gates
# ─────────────────────────────────────────────────────────────────────────────────────────────

#: The ONLY modules allowed to construct an agent class directly instead of dispatching through
#: `crew_factory.run_agent_for_task`.  Each entry is a service seam that runs the agent on the
#: caller's own session/thread, with a reason; a new module here is a new dispatch path and must
#: be audited deliberately.
DIRECT_INSTANTIATION_SEAMS: dict[str, str] = {
    "app/services/planner_service.py": (
        "the planner's fetch/plan steps run the shipped agents on the REQUEST's thread "
        "(asyncio.to_thread), budget-gated per step; they are not crew tasks"
    ),
}


def _direct_instantiation_modules() -> dict[str, list[int]]:
    """Modules under app/ + celery_app/ that write `XxxAgent()` (a direct construction)."""
    found: dict[str, list[int]] = {}
    pattern = re.compile(r"\b([A-Z][A-Za-z0-9]*Agent)\(\)")
    for base in (APP, CELERY):
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            lines = [text[: m.start()].count("\n") + 1 for m in pattern.finditer(text)]
            if lines:
                found[str(path.relative_to(BACKEND)).replace("\\", "/")] = lines
    return found


def test_the_direct_agent_construction_seams_are_exactly_the_documented_ones():
    """Every dispatch goes through ONE seam (`run_agent_for_task`) unless it is listed here.

    A new direct construction is a dispatch path that skips the kill switch, the spawn-level
    safety preflight and the task→agent capability gate — exactly the class of hole wave 4
    closed for the worker.  This pin makes it a failing test instead of a silent bypass.
    """
    found = _direct_instantiation_modules()
    assert set(found) == set(DIRECT_INSTANTIATION_SEAMS), (
        "direct agent construction outside the documented seams: "
        f"{sorted(set(found) - set(DIRECT_INSTANTIATION_SEAMS))}; "
        f"documented seams that no longer construct: "
        f"{sorted(set(DIRECT_INSTANTIATION_SEAMS) - set(found))}"
    )


@pytest.mark.parametrize(("attr", "agent_type"), [
    ("_research_sync", "competitor_research"),
    ("_proposal_sync", "company_planner"),
])
def test_a_direct_planner_seam_passes_the_fleet_gates_before_constructing(monkeypatch, attr, agent_type):
    """W9/CAPABILITY-AUDITOR — the planner seams skipped `dispatch_preflight` entirely.

    MEASURED (before the fix): with `POLSIA_KILL_SWITCH=freeze`, `planner_service._proposal_sync`
    still constructed `CompanyPlannerAgent` and ran it — the fleet freeze was enforced only by the
    separate budget gate, and the spawn-level safety legs (CLI binary/auth facts) were never asked
    at all.  Now the SAME `crew_factory.dispatch_preflight` runs before the class exists.
    """
    monkeypatch.setenv("POLSIA_KILL_SWITCH", "freeze")
    built: list[str] = []

    class _Exploding:
        def __init__(self) -> None:  # pragma: no cover - the gate must fire first
            built.append(agent_type)
            raise AssertionError(
                f"{agent_type} was CONSTRUCTED before the fleet gate: the dispatch seam skipped "
                "dispatch_preflight"
            )

    if attr == "_research_sync":
        monkeypatch.setattr(planner_service, "CompetitorResearchAgent", _Exploding)
        call = lambda: planner_service._research_sync({"name": "Co"})  # noqa: E731
    else:
        monkeypatch.setattr(planner_service, "CompanyPlannerAgent", _Exploding)
        call = lambda: planner_service._proposal_sync({"name": "Co"}, {})  # noqa: E731

    with pytest.raises(PolsiaError) as excinfo:
        call()
    assert excinfo.value.code == "E_POLICY_DENIED", (
        f"expected the named fleet refusal, got {excinfo.value.code!r}: {excinfo.value.message}"
    )
    assert "freeze" in excinfo.value.message.lower(), excinfo.value.message
    assert built == [], "the agent class was constructed before the fleet gate refused"


def test_the_planner_seam_refusal_names_the_measured_reason():
    """The refusal a human reads must carry the problems, not just a code."""
    from app.agents.crew_factory import dispatch_preflight

    problems = dispatch_preflight("company_planner")
    assert isinstance(problems, list)          # the same list the seam raises as its message


def test_the_orchestrator_prompt_only_offers_agents_a_plan_may_name():
    """The morning plan's own instruction is routing material: it must name the dispatchable nine.

    MEASURED (wave 9): the prompt lists exactly `crew_factory.AGENT_MAP` and mentions neither
    `company_planner` (service-invoked — a plan naming it fails `TaskSpec` validation as a whole)
    nor `deployment` (no class).  A prompt that named either would teach the model to emit a plan
    the schema refuses — the wave-4 fleet-map defect, in prose form.
    """
    from app.agents.orchestrator.agent import EVENING_PROMPT, MORNING_PROMPT

    missing = [agent_type for agent_type in AGENT_MAP if agent_type not in MORNING_PROMPT]
    assert not missing, f"the morning plan prompt never offers {missing} — unroutable work"
    for not_a_target in set(pf.AGENT_CAPABILITIES) | set(agent_outputs.ALL_AGENT_TYPES):
        if not_a_target in AGENT_MAP:
            continue
        assert not_a_target not in MORNING_PROMPT, (
            f"the morning prompt names {not_a_target!r}, which no plan may assign"
        )
        assert not_a_target not in EVENING_PROMPT, (
            f"the evening prompt names {not_a_target!r}, which no plan may assign"
        )


# ─────────────────────────────────────────────────────────────────────────────────────────────
# D. wave-7/8: the email lifecycle vocabulary vs the agent's own output contract
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_email_attempt_vocabulary_mirrors_the_agent_output_schema():
    """`email_lifecycle.EMAIL_ATTEMPT_STATUSES` documents itself as mirroring `EmailDraft.status`.

    A drifted mirror would let a run write an attempt state the lifecycle cannot classify (or
    refuse one the schema allows) — the "sent vs tried" confusion the module exists to end.

    The mirror is a SET claim ("whatever the schema allows, the lifecycle knows"), which is why
    the two tuples are compared as sets: their ORDER differs by design (the lifecycle lists the
    durable states first; the schema lists them in prose order).  MEASURED (wave 9): the sets are
    equal — `('draft','queued','sent','failed')` vs `('draft','sent','queued','failed')` — so the
    claim holds and only an added/removed state can fail this pin.
    """
    import typing

    annotation = EmailDraft.model_fields["status"].annotation
    literal = tuple(typing.get_args(annotation))
    assert literal, f"EmailDraft.status is not a Literal: {annotation!r}"
    assert set(email_lifecycle.EMAIL_ATTEMPT_STATUSES) == set(literal), (
        "email_lifecycle.EMAIL_ATTEMPT_STATUSES does not mirror EmailDraft.status: "
        f"{email_lifecycle.EMAIL_ATTEMPT_STATUSES} vs {literal}"
    )


def test_every_attempt_state_is_a_state_the_row_layer_knows():
    attempts = set(email_lifecycle.EMAIL_ATTEMPT_STATUSES)
    rows = set(email_lifecycle.EMAIL_ROW_STATUSES)
    assert attempts <= rows, f"attempt states the row layer refuses: {sorted(attempts - rows)}"
    assert email_lifecycle.EMAIL_ROW_SENT in rows
    assert email_lifecycle.EMAIL_ROW_DEFAULT_STATUS != email_lifecycle.EMAIL_ROW_SENT, (
        "a row that states no status must never default to a SEND"
    )


# ─────────────────────────────────────────────────────────────────────────────────────────────
# E. wave-7/8: both external transports see the run's taint AND its approval material
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_both_external_transports_read_the_runs_taint_and_approval():
    """The registry tool path and the agent side-effect path must ask the SAME run facts.

    MEASURED historically (A2A9-01/A2A9-02): `decide()`'s taint leg was unreachable from the tool
    path because `_execute` published nothing, and an approval could only ever satisfy the
    side-effect path.  Both seams now read the contextvars the worker publishes; if one of them
    stops, its actions silently lose the taint/approval decision while the other keeps it.
    """
    for label, module in (("tool_registry", tool_registry),
                          ("side_effects", importlib.import_module("app.agents.side_effects"))):
        source = inspect.getsource(module)
        assert "current_run_taint(" in source, f"{label} no longer reads the run's taint"
        assert "current_run_approval(" in source, (
            f"{label} no longer reads the run's approval material"
        )


def test_every_run_fact_the_decisions_read_has_a_publisher():
    """The publisher side: a read that nothing publishes is a dead decision leg.

    MEASURED (wave 9): the taint is published by the prompt assembler and by
    `BasePolsiaAgent.timed_run` (the seam every run passes through), while the approval material
    and the channel-send snapshot are published by the WORKER before it starts the run — because
    only the worker holds the task row's token and the day's counts.  The census asserts each
    fact still has its publisher; it does not dictate which module that is.
    """
    assembler = (APP / "agents" / "context_assembler.py").read_text(encoding="utf-8")
    base = (APP / "agents" / "base_agent.py").read_text(encoding="utf-8")
    worker = (CELERY / "tasks" / "agent_tasks.py").read_text(encoding="utf-8")
    assert "set_run_taint(" in assembler, "the prompt assembler no longer publishes the run taint"
    assert "set_run_taint(" in base, "BasePolsiaAgent no longer publishes the run taint"
    for publisher in ("set_run_approval(", "set_run_channel_sends("):
        assert publisher in worker, f"the worker no longer calls {publisher}"


# ─────────────────────────────────────────────────────────────────────────────────────────────
# F. per-agent output schema ↔ the prompt's own output contract
# ─────────────────────────────────────────────────────────────────────────────────────────────

def _agents_for_schema_audit() -> list[str]:
    return sorted(list(AGENT_MAP) + ["company_planner"])


def test_every_agents_output_contract_names_exactly_its_schemas_status_vocabulary():
    """`base_agent._outcome_contract` tells the model which `status` values are legal.

    The words come from the SCHEMA (the validator that will judge the answer), so a subclass that
    narrows or widens `status` without the contract agreeing is a prompt that teaches the model
    to fail validation — and a schema with NO `status` field must not be told to set one.

    MEASURED (wave 9, before the fix): `PlannerProposalDraft` declares only `ideas`, yet its
    contract said "Set status to 'ok' | 'partial' | 'refused' (required: )" — a vocabulary and a
    required-list the schema does not have.  Extra keys are ignored there, so it was a stale
    instruction rather than a validation failure; it is corrected at the renderer so no future
    schema can drift the same way.
    """
    import typing

    from app.schemas.agent_outputs import AgentEnvelope

    for agent_type in _agents_for_schema_audit():
        agent = (get_agent_class(agent_type) if agent_type in AGENT_MAP
                 else importlib.import_module("app.agents.company_planner.agent").CompanyPlannerAgent)()
        model = agent.output_model()
        contract = agent._outcome_contract()
        status_field = model.model_fields.get("status")
        if status_field is None:
            assert "set status" not in contract.lower(), (
                f"{agent_type}: {model.__name__} declares no status field, but the output "
                f"contract tells the model to set one: {contract!r}"
            )
        else:
            allowed = typing.get_args(status_field.annotation)
            assert allowed, (
                f"{agent_type}: {model.__name__}.status is not a Literal: "
                f"{status_field.annotation!r}"
            )
            for word in allowed:
                assert str(word) in contract, (
                    f"{agent_type}: the schema allows status {word!r} but the output contract "
                    f"does not name it: {contract!r}"
                )
        # and the contract's own required-field list must be the schema's required list
        required = model.model_json_schema().get("required") or []
        for field in required:
            assert field in contract, (
                f"{agent_type}: {model.__name__} requires {field!r} but the contract omits it"
            )
        assert "required: )" not in contract, (
            f"{agent_type}: the contract renders an EMPTY required list as '(required: )' — say "
            f"'none' instead: {contract!r}"
        )
        if agent_type in AGENT_MAP:
            assert issubclass(model, AgentEnvelope)


def test_the_schema_registry_covers_every_agent_the_matrix_describes():
    for agent_type in AGENT_MAP:
        assert agent_type in agent_outputs.AGENT_SCHEMAS
    assert "deployment" in agent_outputs.UNREGISTERED_SCHEMAS
    assert "deployment" not in agent_outputs.AGENT_SCHEMAS


# ─────────────────────────────────────────────────────────────────────────────────────────────
# G. the manifest is still the code matrix after waves 4-8 (re-derived, not re-typed)
# ─────────────────────────────────────────────────────────────────────────────────────────────

def test_the_manifest_and_the_fleet_map_still_describe_the_same_runtime():
    manifest = capability_manifest.capability_manifest()
    assert {entry["agent_type"] for entry in manifest["agents"]} == set(pf.AGENT_CAPABILITIES)
    assert [tool["name"] for tool in manifest["registry_tools"]] == sorted(
        spec["function"]["name"] for spec in tool_registry.TOOL_SPECS
    )
    fleet_map = capability_manifest.render_fleet_capability_map(tools_callable=True)
    for agent_type in pf.AGENT_CAPABILITIES:
        assert f"- {agent_type}:" in fleet_map
    # a manifest tool row is the registry's own description, never a re-write
    specs = {spec["function"]["name"]: spec for spec in tool_registry.TOOL_SPECS}
    for tool in manifest["registry_tools"]:
        assert tool["description"] == specs[tool["name"]]["function"]["description"]
