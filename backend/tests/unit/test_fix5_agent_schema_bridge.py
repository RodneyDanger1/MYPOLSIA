"""FIXER-5 / L1-06 — the local validator and the CLI-schema bridge agree about agents.

Premise (RED before the fix; raw evidence: fix-5/raw/probe_premises_l1.txt):
`cli_schema_for('deployment')` emitted a 7-property draft-07 schema for the documented,
deliberately-unregistered tenth agent (the X-01 note keeps `UNREGISTERED_SCHEMAS`), while
`validate_agent_output('deployment', ...)` answered `unknown agent_type` — the bridge and
the validator could disagree about which agents exist, breaking the module docstring's
promise that "the schema is sent to the CLI via --json-schema AND re-validated locally with
the same model".

The fix makes `validate_agent_output` use `schema_for` (accepting the unregistered schema
on BOTH sides); an agent_type with no schema at all keeps answering the same unknown-agent
string it always did, so nothing gains a new raise path.
"""
from __future__ import annotations

from app.schemas.agent_outputs import (
    AGENT_SCHEMAS,
    ALL_AGENT_TYPES,
    UNREGISTERED_SCHEMAS,
    DeploymentOutput,
    FinanceOutput,
    cli_schema_for,
    schema_for,
    validate_agent_output,
)


def test_deployment_validates_with_the_same_model_the_cli_bridge_sends():
    ok, model = validate_agent_output("deployment", {"summary": "planned a deploy"})
    assert ok is True
    assert isinstance(model, DeploymentOutput)
    assert schema_for("deployment") is DeploymentOutput
    assert "deployments" in (cli_schema_for("deployment").get("properties") or {})


def test_every_documented_agent_type_is_locally_validatable():
    """ALL_AGENT_TYPES (incl. `deployment`) must never answer 'unknown agent_type' again."""
    for agent_type in ALL_AGENT_TYPES:
        ok, result = validate_agent_output(agent_type, {})
        assert ok is False, agent_type  # empty payload is invalid for every schema
        assert not any("unknown agent_type" in error for error in result), (agent_type, result)


def test_unknown_agent_type_keeps_the_exact_unknown_string():
    ok, errors = validate_agent_output("not_an_agent", {})
    assert ok is False
    assert errors == ["unknown agent_type 'not_an_agent'"]


def test_registered_agent_payloads_still_validate():
    ok, model = validate_agent_output("finance", {"summary": "numbers reconciled"})
    assert ok is True
    assert isinstance(model, FinanceOutput)


def test_invalid_payload_reports_field_errors_not_unknown_agent():
    ok, errors = validate_agent_output("deployment", {})
    assert ok is False
    assert any("summary" in error for error in errors), errors


def test_validator_and_cli_bridge_expose_the_same_root_properties():
    """Per agent: the schema sent to the CLI and the model re-validated locally are one."""
    for agent_type in ALL_AGENT_TYPES:
        model = schema_for(agent_type)
        properties = set(cli_schema_for(agent_type).get("properties") or {})
        assert properties == set(model.model_fields), agent_type


def test_registry_and_unregistered_maps_are_unchanged_by_the_bridge_fix():
    """The fix changed the VALIDATOR, not the registry: nine registered + deployment."""
    assert set(AGENT_SCHEMAS) == set(ALL_AGENT_TYPES) - {"deployment"}
    assert {"deployment": DeploymentOutput} == UNREGISTERED_SCHEMAS
