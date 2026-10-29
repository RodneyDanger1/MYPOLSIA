"""A-W4-04 (wave 5) — the capability matrix's own cell arithmetic, derived from CODE.

Wave 4's capability artefact headlined *"agent x tool cells: 9 x 17 = 153 -> 29 granted, 124
denied"*.  That line cannot be true under any row set: the 29 grants include `company_planner`'s 2,
i.e. a 10th capability row.  The internally consistent statements are

    10 rows x 17 tools = 170 cells -> 29 granted, 141 denied          (all capability rows)
     9 dispatchable rows x 17 = 153 cells -> 27 granted, 126 denied   (the plan-targetable fleet)

and the live `/ops/capabilities` payload agrees (10 agents, 17 tools, 29 grants; recomputed in
`forge/raw/w5/capability-arithmetic.txt`).  This file pins the derivation so a future grant change
fails a named check instead of silently making a document's number wrong — the wave-5 correction of
`CAPABILITY-MATRIX.md` cites these identities.
"""
from __future__ import annotations

from app.agents.crew_factory import VALID_AGENT_TYPES
from app.agents.policy_firewall import AGENT_CAPABILITIES
from app.agents.tool_registry import TOOL_SPECS, tools_for


def _rows() -> dict[str, list[str]]:
    return {agent_type: list(tools_for(agent_type)) for agent_type in sorted(AGENT_CAPABILITIES)}


def test_the_capability_matrix_arithmetic_is_internally_consistent() -> None:
    rows = _rows()
    tool_count = len(TOOL_SPECS)
    grants = sum(len(names) for names in rows.values())
    dispatchable = {agent_type for agent_type in rows if agent_type in set(VALID_AGENT_TYPES)}

    # The two identities the corrected artefact states.
    assert (len(rows), tool_count, grants) == (10, 17, 29), (len(rows), tool_count, grants)
    assert len(rows) * tool_count == 170
    assert len(rows) * tool_count - grants == 141
    assert len(dispatchable) * tool_count == 153
    dispatchable_grants = sum(len(rows[agent_type]) for agent_type in dispatchable)
    assert dispatchable_grants == 27
    assert len(dispatchable) * tool_count - dispatchable_grants == 126

    # WHY the old "9 x 17 = 153 -> 29" was wrong: the planner's 2 grants are the 10th row.
    assert dispatchable == set(VALID_AGENT_TYPES)
    assert "company_planner" in rows and "company_planner" not in dispatchable
    assert len(rows["company_planner"]) == 2
    assert dispatchable_grants + len(rows["company_planner"]) == grants


def test_every_grant_names_a_shipped_tool_and_every_tool_has_a_holder() -> None:
    rows = _rows()
    shipped = {str(spec["function"]["name"]) for spec in TOOL_SPECS}
    granted = {name for names in rows.values() for name in names}
    assert granted <= shipped, f"grants naming an unshipped tool: {sorted(granted - shipped)}"
    assert shipped <= granted, f"registry tools with no holder: {sorted(shipped - granted)}"
