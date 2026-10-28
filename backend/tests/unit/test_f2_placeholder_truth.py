"""F2 regression: a mocked (placeholder) finance run must NEVER write a fabricated $0 snapshot.

Live-proven defect: `placeholder_payload` zero-filled every nullable `int | None` money field, so
the finance agent's snapshot builder (which treats a non-None value as MEASURED) built a snapshot
of zeros and `persist_finance_run` wrote a real `revenue_snapshots` row. The dashboard then served
MRR $0.00 as a measurement even though the run's own note said "CLAUDE_CLI_MOCK is on ... Nothing
was performed."

The contract: an unmeasured nullable field is `None` (omitted), never 0; a placeholder run writes
no snapshot at all; and an EXPLICIT 0 the model really measured is still a measurement (it must
keep persisting as 0, not be re-read as "absent").

Hardening over the first candidate fix (recorded RED/GREEN in the forge receipt): the nullable
test used to be `"none" in str(annotation).lower()`. A type's str() is a human LABEL, not a type,
so a REQUIRED `NonEmptyStr` field (whose label begins "none…") was nulled although its schema
forbids None — test_b pins that case by name. Detection now reads `typing.get_args`/`get_origin`.
"""
from __future__ import annotations

import json
from datetime import date
from typing import NewType

import pytest
from pydantic import BaseModel
from pydantic_core import PydanticUndefined
from sqlalchemy import select

from app.models.finance import RevenueSnapshot
from app.schemas import agent_outputs
from app.schemas.agent_outputs import (
    AGENT_SCHEMAS,
    UNREGISTERED_SCHEMAS,
    FinanceOutput,
    annotation_allows_none,
    placeholder_payload,
)
from app.services.finance_service import persist_finance_run

#: The note the sanctioned mock path passes (see `base_agent.call_claude_structured`); every
#: placeholder assertion below carries the same "nothing was performed" evidence.
MOCK_NOTE = (
    "CLAUDE_CLI_MOCK is on: the mock result was not JSON, so no structured output exists. "
    "Nothing was performed."
)
MOCK_SUMMARY = "Mock Claude response for testing"


def test_a_finance_placeholder_keeps_every_nullable_measurement_none() -> None:
    payload = placeholder_payload("finance", MOCK_SUMMARY, MOCK_NOTE)

    # The seven money/count columns + the nested snapshot are nullable in FinanceOutput: None =
    # not measured. Derived from the TYPING OBJECTS so a newly-added nullable column is covered.
    nullable = [
        name
        for name, field in FinanceOutput.model_fields.items()
        if annotation_allows_none(field.annotation)
    ]
    assert set(nullable) >= {
        "mrr_cents",
        "arr_cents",
        "active_subscribers",
        "churned_today",
        "new_today",
        "stripe_balance_cents",
        "total_revenue_month_cents",
    }, nullable
    for field in nullable:
        assert payload[field] is None, (field, payload[field])

    # Explicit list: a field this test silently stopped covering would be a hole in the pin.
    for field in (
        "mrr_cents",
        "arr_cents",
        "active_subscribers",
        "churned_today",
        "new_today",
        "stripe_balance_cents",
        "total_revenue_month_cents",
        "snapshot",
    ):
        assert payload[field] is None, (field, payload[field])

    # The honest parts of the placeholder are still explicit — required scalars keep their zero,
    # collections stay empty, and the reason is recorded.
    assert payload["status"] == "partial"
    assert payload["snapshot_written"] is False
    assert payload["sandbox"] is False
    assert payload["expenses"] == []
    assert payload["failed_payments"] == []
    assert payload["alerts"] == []
    assert payload["refunds_performed"] == 0
    assert any("Nothing was performed" in note for note in payload["notes"])


def test_b_a_type_that_merely_spells_none_is_not_treated_as_nullable(monkeypatch) -> None:
    """`NonEmptyStr` contains "none"; a substring test nulls it and breaks the schema.

    RED leg (recorded in the receipt): with the first candidate's
    `elif "none" in str(annotation).lower()` leg, `payload["label"]` is `None` — this test fails
    on its named assertion, and `Probe.model_validate` then refuses the placeholder.
    """
    NonEmptyStr = NewType("NonEmptyStr", str)

    class Probe(BaseModel):
        label: NonEmptyStr          # annotation's label starts "…NonEmptyStr" -> contains "none"
        none_marker: str            # a FIELD NAME containing "none" (names are not types either)
        ratio: float
        unmeasured: int | None      # the genuine nullable case, for contrast

    monkeypatch.setattr(agent_outputs, "schema_for", lambda agent_type: Probe)
    payload = placeholder_payload("probe", "s", MOCK_NOTE)

    assert payload["label"] == "", (
        "a required NonEmptyStr is NOT nullable; the substring test nulled it"
    )
    assert payload["none_marker"] == ""
    assert payload["ratio"] == 0.0
    assert payload["unmeasured"] is None, "a genuine Optional must still stay not-measured"
    # The placeholder must satisfy the very schema it was shaped from.
    Probe.model_validate(payload)


@pytest.mark.asyncio
async def test_c_a_mocked_finance_run_persists_no_revenue_snapshot(async_db_session) -> None:
    """The LIVE chain, end to end: mock placeholder -> the finance snapshot builder -> persist.

    The defect was not in `persist_finance_run` alone: the agent turns a non-None top-level money
    field into a snapshot dict ("measured"), so zero-filled nullables became a real $0 row.  This
    runs the REAL agent under the sanctioned `CLAUDE_CLI_MOCK` path and persists its real result.
    """
    from app.agents.finance.agent import FinanceAgent

    result = FinanceAgent().run(
        {"title": "Daily revenue snapshot", "description": None},
        {"company": {"name": "Test Co"}},
    )

    assert result["status"] == "partial", result.get("status")
    assert result["snapshot"] is None, (
        "the agent built a snapshot from zero-filled placeholders — the live fabrication"
    )
    assert result["mrr_cents"] is None
    assert any("no revenue figures were provided" in alert for alert in result["alerts"]), (
        result.get("alerts")
    )

    report = await persist_finance_run(async_db_session, result)

    assert report["ok"] is True, report
    assert report["snapshot_written"] is False, report
    assert (await async_db_session.scalar(select(RevenueSnapshot.id).limit(1))) is None, (
        "a mock run must not fabricate a revenue snapshot row"
    )


@pytest.mark.asyncio
async def test_d_a_real_measured_zero_still_persists(async_db_session) -> None:
    """The other direction: an EXPLICIT 0 is a measurement, not "absent".

    Guards the fix from over-correcting — "None means unmeasured" must never be implemented by
    turning a model-reported zero into None.
    """
    measured = FinanceOutput.model_validate(
        {
            "summary": "Finance run: measured a real $0.00 (no charges this period)",
            "status": "ok",
            "mrr_cents": 0,
            "arr_cents": 0,
            "active_subscribers": 0,
            "churned_today": 0,
            "new_today": 0,
            "stripe_balance_cents": 0,
            "total_revenue_month_cents": 0,
            "snapshot": {
                "snapshot_date": "2026-10-28",
                "mrr_cents": 0,
                "active_subscribers": 0,
            },
        }
    ).model_dump(mode="json")

    # Validation must NOT re-read 0 as absent.
    assert measured["mrr_cents"] == 0, measured["mrr_cents"]
    assert measured["snapshot"]["mrr_cents"] == 0

    report = await persist_finance_run(async_db_session, measured)

    assert report["ok"] is True, report
    assert report["snapshot_written"] is True, report
    row = (await async_db_session.execute(select(RevenueSnapshot))).scalar_one()
    assert row.snapshot_date == date(2026, 10, 28)
    assert row.mrr_cents == 0
    assert row.active_subscribers == 0


def test_e_every_placeholder_is_json_serialisable_and_has_no_pydantic_sentinel() -> None:
    """A required field has NO default: `get_default()` is `PydanticUndefined` (not None, not ...).

    The old `default is not None and default is not ...` test copied that sentinel into the
    payload, so the orchestrator's required `key_focus` arrived as a non-JSON-serialisable object
    (latent: the mock path returns the payload unvalidated). Required fields now take the type's
    zero via `FieldInfo.is_required()`.
    """
    for agent_type in {*AGENT_SCHEMAS, *UNREGISTERED_SCHEMAS}:
        payload = placeholder_payload(agent_type, MOCK_SUMMARY, MOCK_NOTE)
        assert not any(value is PydanticUndefined for value in payload.values()), agent_type
        json.dumps(payload)  # would raise TypeError on the sentinel
        for name, field in agent_outputs.schema_for(agent_type).model_fields.items():
            if annotation_allows_none(field.annotation) and name not in {
                "summary", "notes", "status", "schema_version", "mode",
            }:
                assert payload[name] is None, (agent_type, name, payload[name])
