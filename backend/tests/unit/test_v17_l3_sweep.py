"""V17/L3 backend sweep — pins for the defects confirmed by the adversarial pass.

Each test is RED at base 3873450 and green after the lane's fix; the docstring names the failure
mode, the file:line where it lived, and the contract that now refuses it BY NAME.

Territory: these are L3's own surfaces (dunning task, config schema/route, planner schema,
stripe webhook route).  Fixtures are re-used, never copied: `queue_db`/`mock_redis` come from
`tests/unit/celery/conftest.py` (re-exported by `tests/unit/conftest.py`), `api_client` from
`tests/conftest.py`, and `mail_seam`/`_seed`/`_run`/`_ledger` from the shipped dunning suite
(`tests/unit/test_v9_dunning.py`) so there is exactly ONE definition of each double.
"""
from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from tests.unit.test_v9_dunning import (  # noqa: F401  (fixture registration)
    DUE,
    _ledger,
    _run,
    _seed,
    mail_seam,
)

# ── L3-01: superseded dunning steps must not fire on a later tick ─────────────────────────────


def test_superseded_dunning_steps_never_fire_on_a_later_tick(
    queue_db, mock_redis, mail_seam  # noqa: F811 - the fixture, imported above
):
    """A late-start invoice gets ONE (latest) reminder, not a cascade of stale earlier ones.

    Failure at base: `_sent_steps` (celery_app/tasks/dunning.py:142-170) only records the step
    that fired; the steps named in `superseded` stayed owed, so the NEXT daily tick fired the
    previous rung — a pre-due "please pay by the due date" reminder arriving weeks AFTER the
    final notice.  The module docstring promises the opposite ("no cascade of stale reminders")
    and names the skipped steps as superseded, which reads as "consumed".
    """
    _seed(queue_db)  # due DUE; all five rungs are owed 30 days later
    late = DUE + timedelta(days=30)

    first = _run(queue_db, late)
    assert [f["step"] for f in first["sent"]] == ["due_plus_30"]
    fired = first["sent"][0]
    assert fired["superseded"] == ["pre_due", "due", "due_plus_5", "due_plus_15"]
    assert len(mail_seam) == 1

    for offset in (1, 2, 3, 10):
        later = _run(queue_db, late + timedelta(days=offset))
        assert later["sent"] == [], (
            f"day {offset} after the final notice fired a stale rung: {later['sent']}"
        )
    assert len(mail_seam) == 1, "exactly ONE dunning email for a late-start invoice"
    assert [row["detail"]["step"] for row in _ledger(queue_db)] == ["due_plus_30"]
    # The ladder still owes nothing: a much later tick is a bare no-op (not a replay of rung 1).
    assert _run(queue_db, late + timedelta(days=400))["sent"] == []


# ── L3-02: non-finite numbers inside free-form config JSON are refused at the wire ────────────


async def test_config_put_refuses_non_finite_numbers_in_json_columns(api_client, async_db_session):
    """`PUT /config` accepted NaN inside `confirmed_idea` and persisted it (SQLite JSON text).

    Failure at base: `CompanyConfigUpdate` (app/schemas/company.py) typed the free-form JSON
    columns as `dict[str, Any]`, so `{"confirmed_idea": {"price_band": [NaN, 100]}}` answered 200
    and stored the non-standard `NaN` token in `company_config.confirmed_idea`.  Every later read
    then LAUNDERED it: pydantic's serializer turns NaN into `null` on the wire, so the planning
    view reported `price: null` with NO reason beside it (the reason contract says every null
    carries its cause), and any service doing float/Decimal math on the stored value inherited a
    non-finite number.  The S4-F1 rule (a non-finite number is not a measurement) is now enforced
    at THIS writer too: named 422 `E_VALIDATION`, nothing stored.
    """
    from app.models.company import CompanyConfig

    async_db_session.add(CompanyConfig(name="Acme"))
    await async_db_session.commit()

    resp = await api_client.put(
        "/api/v1/config",
        content='{"confirmed_idea": {"name": "x", "price_band": [NaN, 100]}}',
        headers={"X-API-Key": "test-key", "Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_VALIDATION"
    assert resp.headers.get("X-Polsia-Error-Code") == "E_VALIDATION"

    stored = await async_db_session.get(CompanyConfig, 1)
    assert stored is not None and stored.confirmed_idea is None, "nothing non-finite was stored"


async def test_config_read_refuses_a_poisoned_row_by_name(api_client, async_db_session):
    """A row already carrying NaN (raw SQL / pre-fix write) must NOT read as a clean 200.

    Failure at base: `GET /config` rendered the poisoned JSON with NaN serialized to `null` — the
    caller saw a normal 200 whose `confirmed_idea.price_band[0]` read as "unmeasured" and whose
    planning view carried no reason.  The invoices precedent (app/api/v1/invoices.py:87-110) is
    the doctrine: a stored non-finite number is corruption, refused BY NAME (`E_DB` + header),
    never laundered into null.
    """
    from app.models.company import CompanyConfig

    async_db_session.add(
        CompanyConfig(name="Acme", confirmed_idea={"name": "x", "price_band": [float("nan"), 100.0]})
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/config", headers={"X-API-Key": "test-key"})
    assert resp.status_code == 503, resp.text[:300]
    body = resp.json()
    assert body["error"]["code"] == "E_DB"
    assert resp.headers.get("X-Polsia-Error-Code") == "E_DB"
    assert "non-finite" in body["error"]["message"]


# ── L3-03: the planner's model-output schema refuses non-finite numbers ───────────────────────


def test_planner_idea_draft_refuses_non_finite_numbers():
    """`PlannerIdeaDraft` accepted NaN/Infinity from the MODEL and the values reached the ledger.

    Failure at base: the draft's float fields (and `price_band`'s elements) used pydantic's
    default `allow_inf_nan=True`.  `_assemble_candidate` (app/services/planner_service.py:477-483)
    does `float(band[0])`, so a NaN survived into the proposal's activity detail — and `confirm`
    copies that candidate verbatim into `company_config.confirmed_idea`, the exact poisoned-row
    shape L3-02's read guard then has to refuse.  The model is untrusted input; non-finite numbers
    are now a schema failure (`E_AGENT_BAD_OUTPUT` on the agent path), never a stored value.
    """
    from app.schemas.planner import PlannerIdeaDraft

    base = {
        "name": "Bookkeeping for clinics",
        "est_variable_cost": 12.0,
        "launch_cost_usd": 300.0,
        "days_to_first_dollar": 14,
        "competition_level": "low",
        "fleet_fit": "high",
        "demand_grade": "proven",
        "owner_hours_per_week_needed": 6,
    }
    for bad in (
        {"price_band": [float("nan"), 100.0]},
        {"price_band": [10.0, float("inf")]},
        {"est_variable_cost": float("nan")},
        {"launch_cost_usd": float("-inf")},
    ):
        with pytest.raises(ValidationError):
            PlannerIdeaDraft(**{**base, **bad})


# ── L3-04: the public Stripe webhook answers a misconfiguration in the named envelope ────────


async def test_stripe_webhook_missing_secret_is_the_named_e_config_refusal(
    api_client, monkeypatch
):
    """`POST /finance/stripe/webhook` with no secret answered an UNCODED 500.

    Failure at base: app/api/v1/finance.py raised `HTTPException(500, detail="Stripe webhook
    secret not configured")` — a plain-string detail, so neither the body envelope nor
    `X-Polsia-Error-Code` named the fault, and every Stripe delivery (public route) was an
    unexplained 500.  The same condition elsewhere is `E_CONFIG` (app/api/v1/data.py:513-522):
    the service cannot take the work, and the operator gets the code + the fix hint.  503 is the
    canonical status for E_CONFIG (app/core/errors.py:153).
    """
    from app.config import settings

    monkeypatch.setattr(settings, "stripe_webhook_secret", "")
    resp = await api_client.post(
        "/api/v1/finance/stripe/webhook", content=b"{}", headers={"X-API-Key": "test-key"}
    )
    assert resp.status_code == 503, resp.text[:300]
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_CONFIG"
    assert body["error"]["how_to_fix"]
    assert resp.headers.get("X-Polsia-Error-Code") == "E_CONFIG"
