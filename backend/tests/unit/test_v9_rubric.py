"""V9/B1 — the planner rubric, gates and claim verification: pure Python, no model, no HTTP.

RED-FIRST leg (OATH): written before `app/services/planner_service.py` exists; the import
below fails with ModuleNotFoundError until the service lands.  Raw pytest legs live in
`polsia-work/build/ui-reverse/wave-v9/b1-planner/` (see RECEIPT.md).

The rubric is COMPUTED IN PYTHON (plan §9 B1 rule b): the model emits structured signals
(demand_grade / competition_level / fleet_fit / days_to_first_dollar / price_band /
est_variable_cost / hours needed), and these deterministic mappings turn them into the frozen
0-5 dimensions + 0-30 total.  No model call, no I/O — a test that changes one signal changes
exactly the expected score.
"""
from __future__ import annotations

from decimal import Decimal

from app.services.planner_service import compute_gates, compute_rubric, verify_claims


def _idea(**over):
    """A complete idea in the shape the planner draft schema emits (all signals present)."""
    idea = {
        "name": "Idea",
        "one_liner": "one line",
        "buyer": "small cafes",
        "price_band": [100.0, 150.0],
        "channel": "stripe payment link",
        "unit_cost_sketch": "2h setup, no materials",
        "est_variable_cost": 20.0,
        "launch_cost_usd": 50.0,
        "days_to_first_dollar": 10,
        "competition_level": "medium",
        "fleet_fit": "high",
        "demand_grade": "likely",
        "owner_hours_per_week_needed": 4,
        "claims": [],
    }
    idea.update(over)
    return idea


# ── the six dimensions, each = one deterministic mapping ────────────────────────────────────

def test_demand_grade_mapping():
    scores = {
        grade: compute_rubric(
            _idea(demand_grade=grade), owner_hours_per_week=10, owner_budget_usd=Decimal("500")
        )["demand"]
        for grade in ("proven", "likely", "plausible", "untested")
    }
    assert scores == {"proven": 5, "likely": 4, "plausible": 2, "untested": 0}


def test_margin_scales_with_gross_ratio_of_the_low_price():
    # margin ratio = (lo - est_variable_cost) / lo, over the LOW end (conservative, same
    # number the CM gate uses).  lo = 100: ratio 1.00 -> 5, 0.80 -> 5, 0.65 -> 4, 0.50 -> 3,
    # 0.35 -> 2, 0.10 -> 1, 0.00 -> 0.
    base = {"owner_hours_per_week": 10, "owner_budget_usd": Decimal("500")}
    assert compute_rubric(_idea(est_variable_cost=0.0), **base)["margin"] == 5
    assert compute_rubric(_idea(est_variable_cost=20.0), **base)["margin"] == 5   # 0.80
    assert compute_rubric(_idea(est_variable_cost=35.0), **base)["margin"] == 4   # 0.65
    assert compute_rubric(_idea(est_variable_cost=50.0), **base)["margin"] == 3   # 0.50
    assert compute_rubric(_idea(est_variable_cost=65.0), **base)["margin"] == 2   # 0.35
    assert compute_rubric(_idea(est_variable_cost=90.0), **base)["margin"] == 1   # 0.10
    assert compute_rubric(_idea(est_variable_cost=100.0), **base)["margin"] == 0  # CM == 0


def test_margin_is_zero_when_cm_is_non_positive():
    rubric = compute_rubric(
        _idea(est_variable_cost=120.0), owner_hours_per_week=10, owner_budget_usd=Decimal("500")
    )
    assert rubric["margin"] == 0


def test_competition_mapping():
    scores = {
        level: compute_rubric(
            _idea(competition_level=level), owner_hours_per_week=10, owner_budget_usd=Decimal("500")
        )["competition"]
        for level in ("none", "low", "medium", "high", "saturated")
    }
    assert scores == {"none": 5, "low": 4, "medium": 3, "high": 1, "saturated": 0}


def test_fleet_fit_mapping():
    scores = {
        fit: compute_rubric(
            _idea(fleet_fit=fit), owner_hours_per_week=10, owner_budget_usd=Decimal("500")
        )["agent_fit"]
        for fit in ("high", "medium", "low")
    }
    assert scores == {"high": 5, "medium": 3, "low": 1}


def test_time_to_first_dollar_thresholds():
    base = {"owner_hours_per_week": 10, "owner_budget_usd": Decimal("500")}
    expected = {7: 5, 8: 4, 14: 4, 15: 3, 30: 3, 31: 2, 60: 2, 61: 1, 90: 1, 91: 0}
    got = {
        days: compute_rubric(_idea(days_to_first_dollar=days), **base)["time_to_first_dollar"]
        for days in expected
    }
    assert got == expected


# ── owner_fit: profile-aware when measured, need-scale when the profile is unmeasured ───────

def test_owner_fit_with_measured_owner_hours():
    # hours = 10: need <= 5 -> 5; <= 10 -> 4; <= 15 -> 2; <= 20 -> 1; more -> 0
    base = {"owner_hours_per_week": 10, "owner_budget_usd": Decimal("500")}
    expected = {4: 5, 5: 5, 10: 4, 15: 2, 20: 1, 25: 0}
    got = {
        need: compute_rubric(_idea(owner_hours_per_week_needed=need), **base)["owner_fit"]
        for need in expected
    }
    assert got == expected


def test_owner_fit_without_a_measured_profile_falls_back_to_the_need_scale():
    # owner hours unmeasured (company row has NULL): the score is the idea's own weekly need
    # on a fixed scale <=2 -> 5, <=5 -> 4, <=10 -> 3, <=20 -> 1, more -> 0.  Never fabricated
    # from an owner figure that does not exist.
    base = {"owner_hours_per_week": None, "owner_budget_usd": None}
    expected = {2: 5, 5: 4, 10: 3, 20: 1, 40: 0}
    got = {
        need: compute_rubric(_idea(owner_hours_per_week_needed=need), **base)["owner_fit"]
        for need in expected
    }
    assert got == expected


def test_total_is_the_sum_of_the_six_dimensions():
    rubric = compute_rubric(
        _idea(), owner_hours_per_week=10, owner_budget_usd=Decimal("500")
    )
    six = [
        rubric["demand"], rubric["margin"], rubric["competition"],
        rubric["agent_fit"], rubric["time_to_first_dollar"], rubric["owner_fit"],
    ]
    assert all(isinstance(score, int) and 0 <= score <= 5 for score in six)
    assert rubric["total"] == sum(six)
    assert 0 <= rubric["total"] <= 30


# ── gates: the hard CM gate + the two profile-dependent null-honest gates ───────────────────

def test_cm_positive_is_strictly_greater_than_zero():
    assert compute_gates(_idea(est_variable_cost=99.99), owner_hours_per_week=10, owner_budget_usd=None)["cm_positive"] is True
    assert compute_gates(_idea(est_variable_cost=100.0), owner_hours_per_week=10, owner_budget_usd=None)["cm_positive"] is False
    assert compute_gates(_idea(est_variable_cost=120.0), owner_hours_per_week=10, owner_budget_usd=None)["cm_positive"] is False


def test_budget_fit_is_null_when_the_owner_budget_is_unmeasured():
    gates = compute_gates(_idea(), owner_hours_per_week=10, owner_budget_usd=None)
    assert gates["budget_fit"] is None


def test_budget_fit_compares_launch_cost_to_the_owner_budget():
    assert compute_gates(_idea(launch_cost_usd=100.0), owner_hours_per_week=10, owner_budget_usd=Decimal("500"))["budget_fit"] is True
    assert compute_gates(_idea(launch_cost_usd=500.0), owner_hours_per_week=10, owner_budget_usd=Decimal("500"))["budget_fit"] is True   # <= fits
    assert compute_gates(_idea(launch_cost_usd=500.01), owner_hours_per_week=10, owner_budget_usd=Decimal("500"))["budget_fit"] is False


def test_hours_fit_is_null_when_the_owner_hours_are_unmeasured():
    gates = compute_gates(_idea(), owner_hours_per_week=None, owner_budget_usd=Decimal("500"))
    assert gates["hours_fit"] is None


def test_hours_fit_compares_the_needed_hours_to_the_owner_hours():
    assert compute_gates(_idea(owner_hours_per_week_needed=10), owner_hours_per_week=10, owner_budget_usd=None)["hours_fit"] is True
    assert compute_gates(_idea(owner_hours_per_week_needed=11), owner_hours_per_week=10, owner_budget_usd=None)["hours_fit"] is False


# ── claims: cited-only keeping (provenance) ─────────────────────────────────────────────────

FETCHED = "https://research.example/pricing"


def test_verify_claims_keeps_only_claims_citing_a_fetched_url():
    claims = [
        {"claim": "cafes pay 100+", "source_url": FETCHED, "quote": "from $100"},
        {"claim": "the market is huge", "source_url": "", "quote": ""},
        {"claim": "another shop charges 250", "source_url": "https://never-fetched.example/x", "quote": "250"},
    ]
    kept, dropped = verify_claims(claims, fetched_urls={FETCHED}, fetched_at="2026-10-14T12:00:00+00:00")
    assert dropped == 2
    assert len(kept) == 1
    (only,) = kept
    assert only["claim"] == "cafes pay 100+"
    assert only["source_url"] == FETCHED
    assert only["fetched_at"] == "2026-10-14T12:00:00+00:00"
    assert only["quote"] == "from $100"
    assert only["verified"] is True


def test_verify_claims_matches_urls_with_surrounding_whitespace():
    claims = [{"claim": "x", "source_url": f"  {FETCHED}  ", "quote": "q"}]
    kept, dropped = verify_claims(claims, fetched_urls={FETCHED}, fetched_at="t")
    assert dropped == 0
    assert kept[0]["source_url"] == FETCHED


def test_verify_claims_on_a_clean_list_is_all_kept_zero_dropped():
    claims = [{"claim": "x", "source_url": FETCHED, "quote": "q"}]
    kept, dropped = verify_claims(claims, fetched_urls={FETCHED}, fetched_at="t")
    assert dropped == 0 and len(kept) == 1
