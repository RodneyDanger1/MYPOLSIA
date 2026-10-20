"""V29 parent wiring pins: five tools, their grants, and their refusal plumbing."""

from __future__ import annotations

import pytest

from app.agents import policy_firewall
from app.agents.tool_registry import _RUNNERS, TOOL_SPECS, tools_for
from app.core.errors import E_UPSTREAM, E_VALIDATION, PolsiaError

NEW_TOOLS = ("research_fetch", "fx_convert", "signals_read", "domain_health", "link_status")


def test_spec_and_runner_counts_match() -> None:
    assert len(TOOL_SPECS) == 17
    assert len(_RUNNERS) == 17
    assert {spec["function"]["name"] for spec in TOOL_SPECS} == set(_RUNNERS)


@pytest.mark.parametrize("name", NEW_TOOLS)
def test_new_specs_are_closed_objects(name: str) -> None:
    spec = next(s for s in TOOL_SPECS if s["function"]["name"] == name)
    params = spec["function"]["parameters"]
    assert params["type"] == "object"
    assert params["additionalProperties"] is False
    assert spec["function"]["description"].strip()


def test_new_grants_are_exact_and_least_privilege() -> None:
    assert {"signals_read", "fx_convert"} <= set(tools_for("orchestrator"))
    assert {"fx_convert"} <= set(tools_for("business_planning"))
    assert {"research_fetch", "signals_read", "domain_health", "link_status"} <= set(
        tools_for("competitor_research")
    )
    assert {"fx_convert"} <= set(tools_for("finance"))
    assert {"signals_read"} <= set(tools_for("social_media"))
    assert {"link_status"} <= set(tools_for("email_outreach"))

    for agent in ("customer_support", "ads_management", "code_generation", "company_planner"):
        granted = set(tools_for(agent))
        assert not (granted & set(NEW_TOOLS)), f"{agent} must not get {granted & set(NEW_TOOLS)}"


@pytest.mark.parametrize("name", ("research_fetch", "fx_convert", "domain_health", "link_status"))
def test_missing_args_are_named_validation(name: str) -> None:
    out = _RUNNERS[name](agent_type="finance", arguments={})
    assert out["ok"] is False
    assert out["error"]["code"] == E_VALIDATION
    assert out["error"]["how_to_fix"]


def test_signals_runner_passes_a_named_service_refusal(monkeypatch) -> None:
    from app.services import signals_service

    def boom(*, limit: int):
        raise PolsiaError(E_UPSTREAM, "feed unavailable", how_to_fix="retry")

    monkeypatch.setattr(signals_service, "read", boom)
    out = _RUNNERS["signals_read"](agent_type="social_media", arguments={"limit": 5})
    assert out["ok"] is False
    assert out["error"]["code"] == E_UPSTREAM
    assert out["error"]["context"]["tool"] == "signals_read"


@pytest.mark.parametrize(
    ("name", "module", "attr", "args"),
    (
        ("research_fetch", "research_fetch_service", "fetch", {"url": "https://example.com"}),
        ("fx_convert", "fx_service", "convert", {"amount": 1, "base": "EUR", "quote": "USD"}),
        ("domain_health", "trust_service", "check_domain", {"host": "example.com"}),
        ("link_status", "trust_service", "check_link", {"url": "https://example.com"}),
    ),
)
def test_service_errors_become_named_refusals(monkeypatch, name, module, attr, args) -> None:
    import importlib

    svc = importlib.import_module(f"app.services.{module}")

    def boom(*a, **k):
        raise PolsiaError(E_UPSTREAM, "upstream down", how_to_fix="retry")

    monkeypatch.setattr(svc, attr, boom)
    out = _RUNNERS[name](agent_type="competitor_research", arguments=args)
    assert out["ok"] is False
    assert out["error"]["code"] == E_UPSTREAM
    assert out["error"]["context"]["tool"] == name


def test_policy_map_is_the_single_grant_source() -> None:
    # tools_for reads policy_firewall; this pins that no second map exists.
    for agent in policy_firewall.AGENT_CAPABILITIES:
        assert tools_for(agent) == policy_firewall.AGENT_CAPABILITIES[agent].agent_tools
