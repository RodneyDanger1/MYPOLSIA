"""FIXER-5 / L3-10 — the refund identifier contract, end to end across prompt/schema/adapter/agent.

Premise (RED before the fix; raw evidence: fix-5/raw/probe_premises_l2l3.txt): the finance
agent read `event_id` (a Stripe EVENT id) and passed it as the adapter's `payment_intent`;
the adapter forwarded it unguarded; `refund_requested` — the field the agent gates on — was
named nowhere in the ROLE prompt; `FailedPayment` had no `payment_intent_id`.  An `evt_…` id
can never refund anything (Stripe refunds target a PaymentIntent/charge), so the only
money-moving call in the product could not succeed even with the approval channel building.

The firewall's approval channel has no producer today (documented owner item), so the agent
test DOUBLES the firewall decision at its boundary (`request_side_effect`), which is what the
test then holds fixed while checking the identifier that crosses the NEXT boundary — the
adapter call.  The adapter tests stub the provider edge (`stripe.Refund.create`) only.
"""
from __future__ import annotations

import json

import pytest
import stripe

import app.config as app_config
from app.agents import integrations
from app.agents.finance import agent as finance_agent
from app.agents.finance.agent import ROLE, FinanceAgent
from app.core.errors import E_VALIDATION
from app.schemas.agent_outputs import FailedPayment

EVT_ID = "evt_1NG8Du2eZvKYlo2CUI79vXWy"
PI_ID = "pi_3NkFIX5refund0001"


# ── the adapter: fail-closed prefix guard ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "bad_id",
    [EVT_ID, "failed_payment:0", "", "  ", "ch_1AbcChargeId", "pi", "xpi_1"],
)
def test_adapter_refuses_a_non_pi_id_by_name_with_no_provider_call(monkeypatch, bad_id):
    monkeypatch.setattr(app_config.settings, "stripe_secret_key", "test-value")
    calls: list[tuple] = []
    monkeypatch.setattr(
        integrations, "integration_call", lambda *a, **k: calls.append((a, k)) or {"ok": True}
    )

    out = integrations.refund_payment(payment_id=bad_id, amount_cents=1500)

    assert out["ok"] is False
    error = out["error"]
    assert error["code"] == E_VALIDATION
    assert "PaymentIntent" in error["message"] and "pi_" in error["message"]
    assert calls == [], "fail-closed: nothing may reach the provider adapter"


def test_adapter_sends_payment_intent_and_amount_for_a_pi_id(monkeypatch):
    """The kwarg the provider receives for a valid id — stubbed at `stripe.Refund.create`."""
    monkeypatch.setattr(app_config.settings, "stripe_secret_key", "test-value")
    captured: dict = {}

    def fake_create(**params):
        captured.update(params)
        return {"id": "re_123", "status": "succeeded"}

    monkeypatch.setattr(stripe.Refund, "create", fake_create)
    out = integrations.refund_payment(payment_id=PI_ID, amount_cents=2500)

    assert out["ok"] is True
    assert out["result"] == {"id": "re_123", "status": "succeeded"}
    assert captured == {"payment_intent": PI_ID, "amount": 2500}


def test_adapter_omits_amount_when_none(monkeypatch):
    """No cap supplied -> no `amount` kwarg (Stripe refunds what is left of the intent)."""
    monkeypatch.setattr(app_config.settings, "stripe_secret_key", "test-value")
    captured: dict = {}

    def fake_create(**params):
        captured.update(params)
        return {"id": "re_full", "status": "succeeded"}

    monkeypatch.setattr(stripe.Refund, "create", fake_create)
    out = integrations.refund_payment(payment_id=PI_ID)

    assert out["ok"] is True
    assert captured == {"payment_intent": PI_ID}


def test_guard_fires_before_the_credentials_check(monkeypatch):
    """A wrong-typed id is a CALLER error even when Stripe is unconfigured."""
    monkeypatch.setattr(app_config.settings, "stripe_secret_key", "")
    out = integrations.refund_payment(payment_id=EVT_ID)
    assert out["error"]["code"] == E_VALIDATION  # not E_INTEGRATION


# ── the agent: the identifier that crosses to the adapter ─────────────────────────────────────


class _AllowedOutcome:
    """The firewall's ALLOW shape, doubled at the boundary (no approval producer exists)."""

    allowed = True
    sandboxed = False
    requires_approval = False
    e_code = ""
    policy_code = ""
    reason = ""
    audit: dict = {}

    def as_audit_record(self) -> dict:
        return {}


def _run_agent(monkeypatch, failed_payments: list[dict]) -> dict:
    monkeypatch.setenv(
        "CLAUDE_CLI_MOCK_RESPONSE",
        json.dumps({"summary": "one failed charge", "failed_payments": failed_payments}),
    )
    monkeypatch.setattr(
        finance_agent, "request_side_effect", lambda *a, **k: _AllowedOutcome()
    )
    return FinanceAgent().run({}, {})


def test_agent_passes_the_payment_intent_id_and_amount_to_the_adapter(monkeypatch):
    monkeypatch.setattr(finance_agent.settings, "sandbox_mode", False)

    recorded: dict = {}

    def fake_refund(*, payment_id, amount_cents=None):
        recorded["payment_id"] = payment_id
        recorded["amount_cents"] = amount_cents
        return {"ok": True, "result": {"id": "re_agent", "status": "succeeded"}}

    monkeypatch.setattr(finance_agent.integrations, "refund_payment", fake_refund)
    out = _run_agent(
        monkeypatch,
        [{"amount_cents": 1500, "currency": "usd", "refund_requested": True,
          "payment_intent_id": PI_ID, "event_id": EVT_ID}],
    )

    assert out["refunds_performed"] == 1
    assert recorded == {"payment_id": PI_ID, "amount_cents": 1500}
    assert out["refusals"] == []
    assert out["side_effects"][0]["ref"] == PI_ID  # the audit names the real target


def test_agent_refuses_a_refund_request_with_no_payment_intent_id(monkeypatch):
    """An event id (or nothing) can never refund: named refusal, NO adapter call, no fabrication."""
    called: list = []
    monkeypatch.setattr(
        finance_agent.integrations,
        "refund_payment",
        lambda **k: called.append(k) or {"ok": True},
    )
    out = _run_agent(
        monkeypatch,
        [{"amount_cents": 1500, "currency": "usd", "refund_requested": True, "event_id": EVT_ID}],
    )

    assert called == []
    (refusal,) = out["refusals"]
    error = refusal["error"]
    assert error["code"] == E_VALIDATION
    assert "payment_intent_id" in error["message"]
    assert error["context"]["event_id"] == EVT_ID  # provenance recorded, never used as the target
    assert out["refunds_performed"] == 0
    assert "failed_payment:" not in json.dumps(out)


def test_agent_leaves_a_non_refundable_failure_alone(monkeypatch):
    called: list = []
    monkeypatch.setattr(
        finance_agent.integrations,
        "refund_payment",
        lambda **k: called.append(k) or {"ok": True},
    )
    out = _run_agent(monkeypatch, [{"amount_cents": 1500, "currency": "usd"}])
    assert called == [] and out["refusals"] == [] and out["side_effects"] == []


# ── the doc contract: prompt + schema + adapter name the same identifiers ─────────────────────


def test_prompt_names_the_gate_and_the_identifier_the_adapter_needs():
    assert "refund_requested" in ROLE
    assert "payment_intent_id" in ROLE
    assert "pi_" in ROLE
    assert "PROVENANCE" in ROLE


def test_schema_descriptions_name_the_same_identifiers():
    payment_intent = FailedPayment.model_fields["payment_intent_id"]
    event = FailedPayment.model_fields["event_id"]
    assert payment_intent.default is None
    assert "pi_" in (payment_intent.description or "")
    assert "PROVENANCE" in (event.description or "")
    assert FailedPayment.model_fields["refund_requested"].default is False


def test_adapter_docstring_and_guard_agree_with_the_schema_prefix():
    """One identifier set: the schema says `pi_…`, the guard enforces `pi_` exactly."""
    doc = integrations.refund_payment.__doc__ or ""
    assert "pi_" in doc
    assert (FailedPayment.model_fields["payment_intent_id"].description or "").count("pi_") >= 1
