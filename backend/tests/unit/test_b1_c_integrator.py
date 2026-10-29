"""Wave B1 / seat C6 (integrator) — the carries closed in this seat, pinned at the seam each failed.

Items (each test names the assertion that fails on the pre-fix tree):

  1. **D4-R1 / A2A7-01** — `AdCampaignRow.total_spent_usd` / `daily_budget_usd` defaulted to `0.0`,
     so the worker's schema DUMP carried a measured-looking zero for a campaign the model
     reported nothing about: the campaign stored `Decimal('0.0000')` and a repeat beat ERASED a
     measured `$250.00` (`_refresh_campaign_money` treats a reported value as a measurement).
     RED: `AdCampaignRow(...).model_dump()["total_spent_usd"] is None` → `assert 0.0 is None`.
  2. **F-19 / S6-R3** — `pricing.estimate_cost_usd` looked the model up with an exact key, so a
     dated / differently-capitalised SERVED id priced `None`: a run the provider charged for read
     UNMEASURED and the planner refused the day. RED: `estimate_cost_usd("deepseek-flash-2026-10")
     is not None` → `assert None is not None`.
  3. **D3-R2** — `E_RUN_CANCELLED` was module-local in `policy_firewall`; adopted into the taxonomy
     with a hint and a deliberate 409 (the status census in `test_r29_status_parity` pins the row,
     and its parity matrix now proves the status on both wires).
  4. **D3-R1** — the registry tool gate reported the generic `E_POLICY_DENIED` for a lease-lost run
     while the sibling side-effect path raised `E_RUN_CANCELLED`: one condition, two outer codes.
  5. **D6-R1** — four call-site docstrings still described the removed `failed` write; A3 re-arms.
  6. **redteam F2** — owner mail must clear `request_side_effect` on EVERY leg; `_gated_send` is the
     module's SINGLE provider call, so the ungated shape cannot be reintroduced.  PROOF here for
     both legs: sandbox on → zero provider calls; sandbox off → exactly one.

The redteam skip this seat removes lives in `tests/redteam/test_v12_sandbox_chat_injection.py`
(`test_sandbox_does_not_reach_the_owner_notification_egress`): its premise — "no
`request_side_effect` call anywhere in the module" — was already false at HEAD (the gate landed in
wave V12), so it pinned a stale source scan rather than a live defect.
"""
from __future__ import annotations

import ast
import inspect
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy import select

import app.services.notification_service as notification_service
from app.agents import integrations, pricing, tool_registry
from app.agents import policy_firewall as pf
from app.config import settings
from app.core import errors
from app.core.errors import E_POLICY_DENIED, E_RUN_CANCELLED, E_SANDBOX_BLOCKED
from app.models.ads import AdCampaign
from app.models.company import CompanyConfig
from app.models.report import ActivityLog
from app.models.task import Task
from app.schemas.agent_outputs import AdCampaignRow, AdsOutput
from app.services import ads_service

BACKEND = Path(__file__).resolve().parents[2]

#: A fixed OFF-PEAK instant (Wednesday 12:00 UTC): the alias tests compare prices, so the pricing
#: window must not depend on when the suite runs.
OFF_PEAK = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 1. the worker's schema DUMP must not fabricate a measured zero (D4-R1 / A2A7-01)
# ═══════════════════════════════════════════════════════════════════════════════════════════════


def test_the_worker_schema_dump_carries_no_measured_zero_for_an_unreported_campaign():
    """RED: the dump carried `0.0` for both money fields (`assert 0.0 is None`).

    The KEY must still be present: `ads_service` can only honour a payload that says "reported
    nothing" — an omitted key and a reported `0` were the two states the `0.0` default merged.
    """
    out = AdsOutput(
        summary="ad run: nothing measured",
        campaigns=[{"platform": "google_ads", "name": "never reported spend"}],
    )

    dumped = out.model_dump(mode="json")["campaigns"][0]

    assert "total_spent_usd" in dumped and "daily_budget_usd" in dumped
    assert dumped["total_spent_usd"] is None, (
        "the worker's schema dump fabricates a measured 0.00 for an unreported campaign"
    )
    assert dumped["daily_budget_usd"] is None


def test_a_reported_zero_survives_and_a_negative_still_refuses():
    """The `None` default must not turn a MEASURED zero into "unknown" (or accept a negative)."""
    dump = AdCampaignRow(
        platform="google_ads",
        name="reported zero",
        total_spent_usd=0.0,
        daily_budget_usd=0.0,
    ).model_dump(mode="json")

    assert dump["total_spent_usd"] == 0.0
    assert dump["daily_budget_usd"] == 0.0

    with pytest.raises(ValidationError):
        AdCampaignRow(platform="google_ads", name="negative", total_spent_usd=-1.0)


@pytest.mark.asyncio
async def test_the_schema_dump_through_record_ads_run_reads_null_not_zero(async_db_session):
    """The D4-R1 settlement: feed the schema's own dump through the writer the worker calls."""
    dumped = AdCampaignRow(
        platform="google_ads", external_id="camp-c6-unreported", name="Spring launch"
    ).model_dump(mode="json")

    report = await ads_service.record_ads_run(
        async_db_session, {"campaigns": [dumped], "metrics": []}
    )

    assert report["ok"] is True, report
    stored = (await async_db_session.execute(select(AdCampaign))).scalars().one()
    assert stored.total_spent_usd is None, "an unreported spend is UNKNOWN, not a measured zero"
    assert stored.daily_budget_usd is None


@pytest.mark.asyncio
async def test_a_repeat_beat_that_omits_the_money_cannot_erase_a_measured_total(async_db_session):
    """A2A7-01's worst limb: the 6-hourly beat used to reset a measured $250.00 to $0.00."""
    first = AdCampaignRow(
        platform="google_ads",
        external_id="camp-c6-measured",
        name="Spring launch",
        total_spent_usd=250.0,
        daily_budget_usd=50.0,
    ).model_dump(mode="json")
    await ads_service.record_ads_run(async_db_session, {"campaigns": [first], "metrics": []})

    # The next beat's model reports the campaign but omits the money — the schema fills None.
    second = AdCampaignRow(
        platform="google_ads", external_id="camp-c6-measured", name="Spring launch"
    ).model_dump(mode="json")
    report = await ads_service.record_ads_run(
        async_db_session, {"campaigns": [second], "metrics": []}
    )

    stored = (await async_db_session.execute(select(AdCampaign))).scalars().one()
    assert stored.total_spent_usd == Decimal("250.0000"), "the repeat beat erased a measured total"
    assert stored.daily_budget_usd == Decimal("50.0000")
    assert report["campaigns_updated"] == 0, "an unreported value is not a write"


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 2. a SERVED model alias prices from the pinned catalog (F-19 / S6-R3)
# ═══════════════════════════════════════════════════════════════════════════════════════════════


def test_a_served_alias_prices_exactly_like_the_pinned_model():
    """RED: `estimate_cost_usd("deepseek-flash-2026-10")` was None (`assert None is not None`)."""
    tokens = {"cache_hit_tokens": 0, "cache_miss_tokens": 1000, "output_tokens": 100}
    exact = pricing.estimate_cost_usd("deepseek-flash", now=OFF_PEAK, **tokens)
    assert exact is not None, "control: the pinned id must price"

    for served in (
        "deepseek-flash-2026-10",          # a dated release
        "DeepSeek-Flash",                  # a differently-cased echo
        "  deepseek-flash-2026-10  ",      # provider padding
        "deepseek-flash-latest",           # a moving alias
    ):
        priced = pricing.estimate_cost_usd(served, now=OFF_PEAK, **tokens)
        assert priced == pytest.approx(exact), f"{served!r} priced differently from its catalog entry"

    assert pricing.rates_for("deepseek-v4-pro-2026-10") is pricing.MODEL_RATES["deepseek-v4-pro"]


def test_an_unknown_or_separatorless_id_stays_unmeasured():
    """The alias rule resolves to a PINNED entry or answers None — it never guesses a rate.

    The separator is part of the match: `deepseek-flashx` is a DIFFERENT model id, not a variant
    of `deepseek-flash`, and pricing it as one would fabricate a cost.
    """
    for unknown in ("deepseek-flashx", "deepseek-f", "gpt-5", "", "   ", None):
        assert pricing.rates_for(unknown) is None, f"{unknown!r} resolved to a pinned rate"

    assert pricing.estimate_cost_usd(
        "deepseek-flashx", cache_hit_tokens=0, cache_miss_tokens=10, output_tokens=1, now=OFF_PEAK
    ) is None


def test_an_unmeasured_token_count_still_makes_the_whole_estimate_none():
    """The honesty rule the alias fix must not erode: a partial sum is fabrication."""
    assert pricing.estimate_cost_usd(
        "deepseek-flash-2026-10",
        cache_hit_tokens=None,
        cache_miss_tokens=1000,
        output_tokens=100,
        now=OFF_PEAK,
    ) is None


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 3. E_RUN_CANCELLED has ONE canonical home (D3-R2)
# ═══════════════════════════════════════════════════════════════════════════════════════════════


def test_e_run_cancelled_is_registered_with_a_hint_and_a_deliberate_409():
    """The taxonomy row exists, says how to proceed, and is 409 — not the unlisted-500 fallback."""
    assert errors.E_RUN_CANCELLED == pf.E_RUN_CANCELLED, (
        "the firewall's code and the taxonomy's must be the same string, or a caller branching on "
        "one misses the other"
    )
    assert errors.HTTP_STATUS_BY_CODE[E_RUN_CANCELLED] == 409, (
        "a cancelled run is not an internal error: another worker owns the row"
    )
    assert errors.http_status_for(E_RUN_CANCELLED) == 409
    assert errors.ERROR_HINTS[E_RUN_CANCELLED].strip(), "every mapped code needs a howToFix"


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 4. the registry tool gate reports the code the firewall decided (D3-R1)
# ═══════════════════════════════════════════════════════════════════════════════════════════════


def test_the_tool_gate_reports_the_cancellation_code_the_firewall_decided():
    """RED: the outer code was `E_POLICY_DENIED` while the message named MERIDIAN-E120."""
    pf.begin_run_cancellation()
    try:
        assert pf.cancel_run("claim lease lost (E_TASK_CLAIMED)") is True
        payload = json.loads(
            tool_registry.execute_tool("signals_read", {"topics": ["ai"]}, agent_type="social_media")
        )
    finally:
        pf.end_run_cancellation()

    assert payload["ok"] is False, "a lease-lost run's tool call must be refused"
    assert payload["error"]["code"] == E_RUN_CANCELLED, (
        "the tool path and the side-effect path must answer the same outer code"
    )
    assert payload["error"]["context"]["policy_code"] == "MERIDIAN-E120"
    assert payload["error"]["how_to_fix"], "a named refusal always says how to proceed"


def test_every_other_gate_denial_keeps_the_generic_policy_code():
    """The re-map is ONE code wide: a capability denial must not borrow the cancellation code."""
    pf.end_run_cancellation()  # no run published a guard: nothing is cancelled here

    payload = json.loads(
        tool_registry.execute_tool("github_commit", {}, agent_type="social_media")
    )

    assert payload["ok"] is False
    assert payload["error"]["code"] == E_POLICY_DENIED
    assert payload["error"]["context"]["policy_code"] != "MERIDIAN-E120"


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 5. the scheduler call sites describe the RE-ARM contract (D6-R1)
# ═══════════════════════════════════════════════════════════════════════════════════════════════


def test_the_scheduler_call_sites_no_longer_claim_the_rows_are_marked_failed():
    """A3 re-arms (`pending` + `metadata.rearmed_at`); a docstring that says `failed` is a lie."""
    modules = {
        "cadence": BACKEND / "celery_app" / "tasks" / "cadence.py",
        "followups": BACKEND / "celery_app" / "tasks" / "followups.py",
        "night_task": BACKEND / "celery_app" / "tasks" / "night_task.py",
    }

    for name, path in modules.items():
        source = path.read_text(encoding="utf-8")
        assert "marks the rows failed" not in source, f"{name}: stale `failed` contract"
        assert "re-arm" in source.lower(), f"{name}: the re-arm contract is not documented"


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# 6. the owner-notification egress goes through the ONE gated send (redteam F2)
# ═══════════════════════════════════════════════════════════════════════════════════════════════


def _dotted(node: ast.expr) -> str:
    """`ast.Attribute` chain → dotted text (`integrations.send_email`)."""
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    if isinstance(node, ast.Name):
        return node.id
    return ""


def test_the_only_provider_call_in_notification_service_sits_behind_the_gate():
    """Structural proof: ONE `integrations.send_email` call, inside `_gated_send`, after the gate.

    This is the shape redteam F2 needs: a second, ungated send site cannot be added without this
    test failing, and the single site cannot reach the provider before `request_side_effect`.
    """
    tree = ast.parse(inspect.getsource(notification_service))

    sends = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _dotted(node.func) == "integrations.send_email"
    ]
    assert len(sends) == 1, "the module has more than one provider call: one of them is ungated"

    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    owner = parent[sends[0]]
    while owner is not None and not isinstance(owner, ast.FunctionDef | ast.AsyncFunctionDef):
        owner = parent.get(owner)
    assert isinstance(owner, ast.AsyncFunctionDef) and owner.name == "_gated_send", (
        "the provider call must live in `_gated_send`, the module's single gated send"
    )

    calls = [node for node in ast.walk(owner) if isinstance(node, ast.Call)]
    gate_calls = [call for call in calls if _dotted(call.func) == "_firewall_clearance"]
    assert gate_calls, "the send helper never asks the side-effect gate"
    assert gate_calls[0].lineno < sends[0].lineno, (
        "the provider call runs BEFORE the gate: a sandboxed run could reach the wire"
    )


def _record_provider(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """The provider recorder: any entry here is a REAL message on the wire."""
    sent: list[dict[str, Any]] = []

    def fake_send_email(*, to: str, subject: str, body: str) -> dict[str, Any]:
        sent.append({"to": to, "subject": subject, "body": body})
        return {"ok": True, "result": {"status_code": 202, "message_id": "sg-c6"}}

    monkeypatch.setattr(integrations, "send_email", fake_send_email)
    return sent


async def _seed_owner_task(db: Any) -> Task:
    db.add(CompanyConfig(name="Acme", notification_email="owner@example.com", notify_on_failed=True))
    task = Task(title="notify me", agent_type="finance", status="failed")
    db.add(task)
    await db.commit()
    return task


async def _activity_rows(db: Any, action: str) -> list[ActivityLog]:
    result = await db.execute(select(ActivityLog).where(ActivityLog.action == action))
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_sandbox_on_puts_zero_provider_calls_on_the_terminal_outcome_leg(
    async_db_session, monkeypatch, mock_redis
):
    """PROOF (sandbox ON): zero provider calls, and ONE named draft row."""
    monkeypatch.setattr(settings, "sandbox_mode", True)
    sent = _record_provider(monkeypatch)
    task = await _seed_owner_task(async_db_session)

    await notification_service.notify_owner_for_terminal_outcome(
        async_db_session, task.id, "failed"
    )

    assert sent == [], "SANDBOX_MODE=true put a real message on the wire"
    (row,) = await _activity_rows(async_db_session, "owner_notification_failed")
    assert row.detail["error"]["code"] == E_SANDBOX_BLOCKED
    assert row.detail["error"]["sandbox"] is True
    assert row.detail["error"]["drafted_not_sent"] is True


@pytest.mark.asyncio
async def test_sandbox_off_puts_exactly_one_provider_call_on_the_terminal_outcome_leg(
    async_db_session, monkeypatch
):
    """PROOF (sandbox OFF): exactly one provider call — the gate is the sandbox, nothing else."""
    monkeypatch.setattr(settings, "sandbox_mode", False)
    sent = _record_provider(monkeypatch)
    task = await _seed_owner_task(async_db_session)

    await notification_service.notify_owner_for_terminal_outcome(
        async_db_session, task.id, "failed"
    )

    (call,) = sent
    assert call["to"] == "owner@example.com"
    assert await _activity_rows(async_db_session, "owner_notification_failed") == []


@pytest.mark.asyncio
async def test_sandbox_on_puts_zero_provider_calls_on_the_cycle_report_leg(
    async_db_session, monkeypatch, mock_redis
):
    """PROOF (sandbox ON, the second owner-mail leg): zero provider calls, one named draft row."""
    monkeypatch.setattr(settings, "sandbox_mode", True)
    sent = _record_provider(monkeypatch)
    async_db_session.add(
        CompanyConfig(name="Acme", notification_email="owner@example.com", cycle_report_email=True)
    )
    await async_db_session.commit()

    await notification_service.notify_owner_cycle_report(
        async_db_session, kind="morning", report_date=date(2026, 10, 15), summary="all good"
    )

    assert sent == []
    (row,) = await _activity_rows(async_db_session, "cycle_report_email_failed")
    assert row.detail["error"]["code"] == E_SANDBOX_BLOCKED
    assert row.detail["error"]["drafted_not_sent"] is True


@pytest.mark.asyncio
async def test_sandbox_off_puts_exactly_one_provider_call_on_the_cycle_report_leg(
    async_db_session, monkeypatch
):
    """PROOF (sandbox OFF, the second owner-mail leg): exactly one provider call."""
    monkeypatch.setattr(settings, "sandbox_mode", False)
    sent = _record_provider(monkeypatch)
    async_db_session.add(
        CompanyConfig(name="Acme", notification_email="owner@example.com", cycle_report_email=True)
    )
    await async_db_session.commit()

    await notification_service.notify_owner_cycle_report(
        async_db_session, kind="evening", report_date=date(2026, 10, 15), summary="all good"
    )

    (call,) = sent
    assert call["to"] == "owner@example.com"
    assert "Evening" in call["subject"]
