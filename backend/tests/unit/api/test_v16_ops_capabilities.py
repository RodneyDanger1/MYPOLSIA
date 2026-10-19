"""V16/E6 — GET /api/v1/ops/capabilities (frozen interface H-C1).

The endpoint is the READ-ONLY operator/frontend surface of the capability-visibility family:
every agent's REAL tools (name + the registry's own description), action classes and channels,
plus the registry tool catalog — DERIVED at request time from `policy_firewall.AGENT_CAPABILITIES`
and `tool_registry` (never hand-copied; `app/services/capability_manifest.py`).  This file pins
the wire contract byte-for-byte against an INDEPENDENT reconstruction from the two sources, so
the endpoint cannot silently diverge from the registry the agents actually execute against.
"""
from __future__ import annotations

import pytest

from app.agents import policy_firewall as pf
from app.agents import tool_registry


def _spec_description(name: str) -> str:
    return next(
        spec["function"]["description"]
        for spec in tool_registry.TOOL_SPECS
        if spec["function"]["name"] == name
    )


def _expected_payload() -> dict:
    """H-C1 rebuilt from the two sources HERE — not by calling the service under test."""
    agents = []
    for agent_type in sorted(pf.AGENT_CAPABILITIES):
        cap = pf.AGENT_CAPABILITIES[agent_type]
        agents.append(
            {
                "agent_type": agent_type,
                "tools": [
                    {"name": name, "description": _spec_description(name)}
                    for name in cap.agent_tools
                ],
                "action_classes": sorted(str(c) for c in cap.action_classes),
                "channels": list(cap.channels),
            }
        )
    registry_tools = [
        {"name": spec["function"]["name"], "description": spec["function"]["description"]}
        for spec in sorted(tool_registry.TOOL_SPECS, key=lambda s: s["function"]["name"])
    ]
    return {"agents": agents, "registry_tools": registry_tools}


@pytest.mark.asyncio
async def test_requires_the_owner_key(api_client):
    resp = await api_client.get("/api/v1/ops/capabilities")
    assert resp.status_code == 401
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_AUTH"


@pytest.mark.asyncio
async def test_payload_is_derived_from_the_two_registries(api_client, auth_headers):
    resp = await api_client.get("/api/v1/ops/capabilities", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == _expected_payload()


@pytest.mark.asyncio
async def test_payload_shape_is_exactly_the_frozen_one(api_client, auth_headers):
    resp = await api_client.get("/api/v1/ops/capabilities", headers=auth_headers)
    data = resp.json()
    assert set(data) == {"agents", "registry_tools"}
    for row in data["agents"]:
        assert set(row) == {"agent_type", "tools", "action_classes", "channels"}, row
        for tool in row["tools"]:
            assert set(tool) == {"name", "description"}
    for tool in data["registry_tools"]:
        assert set(tool) == {"name", "description"}
    assert {"orchestrator", "company_planner", "finance",
            "competitor_research"} <= {row["agent_type"] for row in data["agents"]}


@pytest.mark.asyncio
async def test_finance_tools_carry_the_registry_descriptions(api_client, auth_headers):
    resp = await api_client.get("/api/v1/ops/capabilities", headers=auth_headers)
    finance = next(r for r in resp.json()["agents"] if r["agent_type"] == "finance")
    assert finance["tools"] == [
        {"name": "macro_snapshot", "description": _spec_description("macro_snapshot")},
        {"name": "pricing_catalog", "description": _spec_description("pricing_catalog")},
    ]
    assert finance["channels"] == ["stripe"]


@pytest.mark.asyncio
async def test_phone_read_is_listed_exactly_where_the_allowlist_says(api_client, auth_headers):
    """V20/L2 DRIFT PIN — the H-C1 payload gains ONE tool, deliberately.

    Wave V20 added the read-only `phone_read` registry tool and granted it to exactly two
    agents (the orchestrator — the owner: "agents/orchestrator can see/use the phone" — and
    customer_support, which already reads the owner's inbound correspondence).  This row is the
    wire-level re-pin: the two holders name it with the registry's OWN description, no other
    agent does, and `registry_tools` still lists every tool sorted by name.  A future allowlist
    or spec change fails here until this payload expectation is re-pinned on purpose.
    """
    resp = await api_client.get("/api/v1/ops/capabilities", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()

    rows = {row["agent_type"]: row for row in data["agents"]}
    holders = {
        agent_type for agent_type, row in rows.items()
        if "phone_read" in [tool["name"] for tool in row["tools"]]
    }
    assert holders == {"orchestrator", "customer_support"}
    assert [tool["name"] for tool in rows["orchestrator"]["tools"]] == [
        "macro_snapshot", "pricing_catalog", "phone_read"]
    assert [tool["name"] for tool in rows["customer_support"]["tools"]] == ["phone_read"]
    assert rows["customer_support"]["tools"][0]["description"] == _spec_description("phone_read")

    registry_tools = {tool["name"]: tool["description"] for tool in data["registry_tools"]}
    assert registry_tools["phone_read"] == _spec_description("phone_read")
    assert list(registry_tools) == sorted(registry_tools), "registry_tools stays sorted by name"


@pytest.mark.asyncio
async def test_v24_tools_are_listed_exactly_where_the_allowlist_says(api_client, auth_headers):
    """V24/L2 DRIFT PIN — the H-C1 payload gains FOUR tools and TWO agent rows, deliberately.

    Wave V24 shipped `web_search` (DeepSeek Search API → competitor_research, after web_fetch)
    and `code_write`/`code_read`/`code_list` (the workspace trio → code_generation, which was
    tool-less before).  This is the wire-level re-pin: the holders name them with the registry's
    OWN descriptions, the orchestrator names NONE of them (the evidence-based decision), and
    `registry_tools` still lists every tool sorted by name.  A future spec/allowlist change
    fails here until this payload expectation is re-pinned on purpose.
    """
    resp = await api_client.get("/api/v1/ops/capabilities", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()

    rows = {row["agent_type"]: row for row in data["agents"]}
    assert [tool["name"] for tool in rows["competitor_research"]["tools"]] == [
        "web_fetch", "web_search"]
    assert [tool["name"] for tool in rows["code_generation"]["tools"]] == [
        "code_write", "code_read", "code_list"]
    assert [tool["name"] for tool in rows["orchestrator"]["tools"]] == [
        "macro_snapshot", "pricing_catalog", "phone_read"]

    registry_tools = {tool["name"]: tool["description"] for tool in data["registry_tools"]}
    for name in ("web_search", "code_write", "code_read", "code_list"):
        assert registry_tools[name] == _spec_description(name), name
    assert len(registry_tools) == len(tool_registry.TOOL_SPECS)
    assert list(registry_tools) == sorted(registry_tools)
