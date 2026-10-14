"""V9/B1 — the planner routes: propose / confirm / status (frozen contract, plan §9 B1).

RED-FIRST leg (OATH): written before `app/api/v1/planner.py`, `app/services/planner_service.py`
and `app/agents/company_planner/` exist; collection fails with ModuleNotFoundError until they
land.  Raw pytest legs live in `polsia-work/build/ui-reverse/wave-v9/b1-planner/` (RECEIPT.md).

The router is NOT wired into `main.py` (V9-D3 — the parent wires planner + invoices at settle),
so these tests build a test app that includes the router DIRECTLY, with the conftest's
in-memory session as the `get_db` override — the same seam every shipped api test uses.

MOCKED MODEL + MOCKED FETCH (both seams, per the brief): `_research_sync` (the fetch step) and
`_proposal_sync` (the model step) are monkeypatched; the real code paths they stand in for run
through `BasePolsiaAgent.collect_structured` (the SAME seam every agent uses to reach the
model).  One test drives the REAL `_research_sync` in `CLAUDE_CLI_MOCK` mode to pin the budget
leg end to end.
"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.services import planner_service

FETCHED = "https://research.example/pricing"

BACKEND = Path(__file__).resolve().parents[3]

AUTH = {"X-API-Key": "test-key"}

FROZEN_CANDIDATE_KEYS = {
    "id", "name", "one_liner", "buyer", "price_band", "channel", "unit_cost_sketch",
    "rubric", "gates", "claims", "claims_unverified",   # claims_unverified: plan rule (a)
}
FROZEN_CLAIM_KEYS = {"claim", "source_url", "fetched_at", "quote", "verified"}
RUBRIC_KEYS = {
    "demand", "margin", "competition", "agent_fit", "time_to_first_dollar", "owner_fit", "total",
}
GATE_KEYS = {"cm_positive", "budget_fit", "hours_fit"}


def research_payload(queries: int = 2) -> dict:
    """The shape the competitor_research run returns (the planner's fetch step output)."""
    return {
        "summary": "Landscape notes",
        "competitors": [
            {"name": "Incumbent", "source_urls": [FETCHED], "sources": [FETCHED]},
        ],
        "findings": ["Incumbent prices from $100."],
        "search_queries": [f"query {i}" for i in range(queries)],
    }


def idea(**over) -> dict:
    """A complete idea in the planner draft shape, citing one actually-fetched URL."""
    value = {
        "name": "Cafe launch kit",
        "one_liner": "one page + email sequence for one cafe",
        "buyer": "independent cafe owners",
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
        "claims": [
            {"claim": "cafes already pay $100+ for this", "source_url": FETCHED, "quote": "from $100"},
        ],
    }
    value.update(over)
    return value


def ideas(n: int = 3, **over) -> list[dict]:
    out = []
    for i in range(n):
        value = idea(**over)
        value["name"] = f"{value['name']} {i + 1}"
        out.append(value)
    return out


@pytest_asyncio.fixture
async def planner_client(async_db_session, mock_redis, monkeypatch):
    """A test app that includes ONLY the planner router (main.py wiring is the parent's, V9-D3)."""
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    from app.api.deps import get_db
    from app.api.v1 import planner
    from app.config import settings
    from app.core.errors import PolsiaError, http_status_for, to_http_detail

    monkeypatch.setattr(settings, "api_key", "test-key")
    app = FastAPI()
    app.include_router(planner.router, prefix="/api/v1")
    app.dependency_overrides[get_db] = lambda: async_db_session

    # Mirror main.py's named-refusal handler (dependency-raised refusals, e.g. E_AUTH, must
    # answer the same body/headers they do in the real app rather than escaping the test app).
    async def _polsia_handler(request, exc: PolsiaError):
        return JSONResponse(
            status_code=http_status_for(exc.code),
            content=to_http_detail(exc),
            headers={"X-Polsia-Error-Code": exc.code},
        )

    app.add_exception_handler(PolsiaError, _polsia_handler)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


@pytest_asyncio.fixture
async def company(async_db_session):
    from app.models.company import CompanyConfig

    row = CompanyConfig(
        name="Planner Test Co",
        owner_hours_per_week=10,
        owner_budget_usd=Decimal("500.00"),
    )
    async_db_session.add(row)
    await async_db_session.commit()
    return row


@pytest.fixture
def mock_fetch(monkeypatch):
    """MOCKED fetch step: the research run's output, including the actually-fetched URLs."""
    monkeypatch.setattr(planner_service, "_research_sync", lambda profile: research_payload())


def install_model(monkeypatch, returned: list[dict]) -> None:
    """MOCKED model step: what the planner agent's validated draft payload yields."""
    monkeypatch.setattr(
        planner_service,
        "_proposal_sync",
        lambda profile, research: {"ideas": returned, "agent_type": "company_planner"},
    )


async def propose(client) -> object:
    return await client.post("/api/v1/planner/propose", headers=AUTH)


async def count_rows(db, model) -> int:
    from sqlalchemy import func, select

    return int((await db.execute(select(func.count()).select_from(model))).scalar() or 0)


# ── propose: 201 + the frozen candidate shape ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_propose_answers_201_with_frozen_candidates(
    planner_client, company, async_db_session, mock_fetch, monkeypatch
):
    install_model(monkeypatch, ideas(3))

    resp = await propose(planner_client)

    assert resp.status_code == 201, resp.text
    candidates = resp.json()["candidates"]
    assert len(candidates) == 3
    ids = set()
    for candidate in candidates:
        assert set(candidate) == FROZEN_CANDIDATE_KEYS
        assert isinstance(candidate["id"], str) and candidate["id"]
        ids.add(candidate["id"])
        assert isinstance(candidate["price_band"], list) and len(candidate["price_band"]) == 2
        assert candidate["buyer"] and candidate["channel"]
        rubric = candidate["rubric"]
        assert set(rubric) == RUBRIC_KEYS
        assert 0 <= rubric["total"] <= 30
        assert rubric["total"] == sum(
            rubric[k] for k in RUBRIC_KEYS if k != "total"
        )
        gates = candidate["gates"]
        assert set(gates) == GATE_KEYS
        assert gates["cm_positive"] is True           # lo 100 - cost 20 > 0
        assert gates["budget_fit"] is True            # launch 50 <= owner budget 500
        assert gates["hours_fit"] is True             # need 4 <= owner hours 10
        assert candidate["claims_unverified"] == 0
        assert len(candidate["claims"]) == 1
        assert set(candidate["claims"][0]) == FROZEN_CLAIM_KEYS
        assert candidate["claims"][0]["verified"] is True
        assert candidate["claims"][0]["fetched_at"]
        assert candidate["claims"][0]["source_url"] == FETCHED
    assert len(ids) == 3, "candidate ids must be unique"


@pytest.mark.asyncio
async def test_uncited_claims_are_stripped_and_counted(
    planner_client, company, mock_fetch, monkeypatch
):
    """Rule (a): only claims citing web-research fetch outputs survive; the rest are counted."""
    dirty = idea()
    dirty["claims"] = [
        {"claim": "cited", "source_url": FETCHED, "quote": "from $100"},
        {"claim": "uncited", "source_url": "", "quote": ""},
        {"claim": "cites a page nobody fetched", "source_url": "https://made-up.example/x", "quote": ""},
    ]
    install_model(monkeypatch, [dirty, *ideas(2)])

    resp = await propose(planner_client)

    assert resp.status_code == 201, resp.text
    first = resp.json()["candidates"][0]
    assert first["claims_unverified"] == 2
    assert [c["claim"] for c in first["claims"]] == ["cited"]


@pytest.mark.asyncio
async def test_cm_non_positive_candidate_is_still_returned_with_gate_false(
    planner_client, company, mock_fetch, monkeypatch
):
    """Rule (b): a CM <= 0 candidate is NEVER silently dropped — it renders cm_positive false."""
    negative = idea(price_band=[10.0, 20.0], est_variable_cost=12.0)
    negative["name"] = "CM-negative idea"
    install_model(monkeypatch, [negative, *ideas(2)])

    resp = await propose(planner_client)

    assert resp.status_code == 201, resp.text
    candidates = resp.json()["candidates"]
    assert len(candidates) == 3, "the gated candidate must still be present"
    gated = next(c for c in candidates if c["name"] == "CM-negative idea")
    assert gated["gates"]["cm_positive"] is False
    assert gated["rubric"]["margin"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("buyer", ""),
        ("price_band", []),
        ("channel", ""),
    ],
)
async def test_who_pays_pre_gate_refuses_422(
    planner_client, company, mock_fetch, monkeypatch, field, value
):
    """Rule (c): missing buyer / price / payment path fails validation with 422 E_VALIDATION."""
    broken = [idea(**{field: value}), *ideas(2)]
    install_model(monkeypatch, broken)

    resp = await propose(planner_client)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["code"] == "E_VALIDATION"
    assert resp.headers.get("X-Polsia-Error-Code") == "E_VALIDATION"


@pytest.mark.asyncio
async def test_candidate_count_outside_three_to_five_is_refused(
    planner_client, company, mock_fetch, monkeypatch
):
    install_model(monkeypatch, ideas(2))

    resp = await propose(planner_client)

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"]["code"] == "E_VALIDATION"


# ── NOTHING SPENDS: the full cycle touches no run, no post, no send ─────────────────────────

@pytest.mark.asyncio
async def test_full_propose_confirm_cycle_creates_zero_agent_runs_posts_and_email_logs(
    planner_client, company, async_db_session, mock_fetch, monkeypatch
):
    """Rule (d): nothing spends.  Counts before/after across the WHOLE cycle."""
    from sqlalchemy import select

    from app.models.outreach import EmailLog
    from app.models.report import ActivityLog
    from app.models.social import SocialPost
    from app.models.task import AgentRun

    install_model(monkeypatch, ideas(3))
    before = {
        model: await count_rows(async_db_session, model)
        for model in (AgentRun, SocialPost, EmailLog)
    }
    assert sum(before.values()) == 0

    resp = await propose(planner_client)
    assert resp.status_code == 201, resp.text
    candidate_id = resp.json()["candidates"][0]["id"]

    confirm = await planner_client.post(
        "/api/v1/planner/confirm", headers=AUTH, json={"candidate_id": candidate_id}
    )
    assert confirm.status_code == 200, confirm.text
    assert confirm.json() == {"confirmed": True}

    after = {
        model: await count_rows(async_db_session, model)
        for model in (AgentRun, SocialPost, EmailLog)
    }
    assert after == before == {AgentRun: 0, SocialPost: 0, EmailLog: 0}

    # The only rows the cycle writes are its own two activity-ledger entries (house pattern:
    # activity rows are the column-free ledger — V9 dunning precedent).
    rows = list(
        (await async_db_session.execute(select(ActivityLog).order_by(ActivityLog.id))).scalars()
    )
    assert [row.action for row in rows] == ["idea_proposed", "idea_confirmed"]
    assert all(row.agent_type == "company_planner" for row in rows)


# ── confirm writes the confirmed idea; status reports it (no fabricated timestamps) ─────────

@pytest.mark.asyncio
async def test_confirm_writes_confirmed_idea_and_status_reports_it(
    planner_client, company, async_db_session, mock_fetch, monkeypatch
):
    from sqlalchemy import select

    from app.models.report import ActivityLog

    install_model(monkeypatch, ideas(3))
    resp = await propose(planner_client)
    candidate = resp.json()["candidates"][1]

    confirm = await planner_client.post(
        "/api/v1/planner/confirm", headers=AUTH, json={"candidate_id": candidate["id"]}
    )
    assert confirm.status_code == 200 and confirm.json() == {"confirmed": True}

    # The write landed on the company row (confirm's only durable effect).
    await async_db_session.refresh(company)
    assert company.confirmed_idea is not None
    assert company.confirmed_idea["id"] == candidate["id"]
    assert company.confirmed_idea["gates"] == candidate["gates"]

    status = await planner_client.get("/api/v1/planner/status", headers=AUTH)
    assert status.status_code == 200
    body = status.json()
    assert set(body) == {"confirmed", "last_proposed_at"}
    assert body["confirmed"]["id"] == candidate["id"]
    # last_proposed_at is the propose ledger row's OWN created_at — never a synthesized clock.
    row = (
        await async_db_session.execute(
            select(ActivityLog)
            .where(ActivityLog.action == "idea_proposed")
            .order_by(ActivityLog.id.desc())
            .limit(1)
        )
    ).scalar_one()
    assert body["last_proposed_at"] == row.created_at.isoformat()


@pytest.mark.asyncio
async def test_status_before_any_proposal_answers_nulls(planner_client, company):
    status = await planner_client.get("/api/v1/planner/status", headers=AUTH)

    assert status.status_code == 200
    assert status.json() == {"confirmed": None, "last_proposed_at": None}


@pytest.mark.asyncio
async def test_confirm_with_an_unknown_candidate_id_refuses_by_name(
    planner_client, company, mock_fetch, monkeypatch
):
    install_model(monkeypatch, ideas(3))
    resp = await propose(planner_client)
    assert resp.status_code == 201

    confirm = await planner_client.post(
        "/api/v1/planner/confirm", headers=AUTH, json={"candidate_id": "idea-not-a-real-id"}
    )

    assert confirm.status_code == 404, confirm.text
    assert confirm.json()["detail"]["code"] == "E_NOT_FOUND"


# ── get_full_context gains idea_brief after confirm (rule e) ────────────────────────────────

@pytest.mark.asyncio
async def test_get_full_context_gains_idea_brief_after_confirm(
    planner_client, company, async_db_session, mock_fetch, monkeypatch
):
    from app.services.company_service import get_full_context

    ctx_before = await get_full_context(async_db_session)
    assert ctx_before.get("idea_brief") is None, "no confirmed idea yet"

    install_model(monkeypatch, ideas(3))
    resp = await propose(planner_client)
    candidate_id = resp.json()["candidates"][0]["id"]
    confirm = await planner_client.post(
        "/api/v1/planner/confirm", headers=AUTH, json={"candidate_id": candidate_id}
    )
    assert confirm.status_code == 200

    ctx_after = await get_full_context(async_db_session)
    assert ctx_after["idea_brief"] is not None
    assert ctx_after["idea_brief"]["id"] == candidate_id
    assert ctx_after["idea_brief"]["name"] == resp.json()["candidates"][0]["name"]


# ── the fetch step: the REAL research seam + the shipped fetch budget ───────────────────────

@pytest.mark.asyncio
async def test_research_step_over_budget_is_recorded_not_hidden(
    planner_client, company, async_db_session, monkeypatch
):
    """The planner's fetch step IS the competitor_research run; its V8/C5 budget is honoured —
    over budget records ONE honest activity row (tier (a): recorded, never a silent overrun)."""
    from sqlalchemy import select

    from app.agents.competitor_research.agent import RESEARCH_FETCH_BUDGET
    from app.models.report import ActivityLog

    # The real _research_sync runs the competitor_research agent through the same model seam;
    # CLAUDE_CLI_MOCK is autouse in this suite, so we drive it with an over-budget payload.
    monkeypatch.setenv("CLAUDE_CLI_MOCK_RESPONSE", json.dumps(research_payload(RESEARCH_FETCH_BUDGET + 2)))
    install_model(monkeypatch, ideas(3))

    resp = await propose(planner_client)

    assert resp.status_code == 201, resp.text
    rows = list(
        (await async_db_session.execute(select(ActivityLog))).scalars()
    )
    budget_rows = [row for row in rows if row.action == "research_budget_exceeded"]
    assert len(budget_rows) == 1
    assert budget_rows[0].level == "warning"
    assert budget_rows[0].detail["count"] == RESEARCH_FETCH_BUDGET + 2
    assert budget_rows[0].detail["budget"] == RESEARCH_FETCH_BUDGET


# ── the agent file itself: injection guard + the shared budget constant ─────────────────────

def test_agent_source_carries_the_injection_guard_and_the_shared_budget():
    """Plan §9 B1 / V9 D7: the guard string must be IN agent.py (grepped, not assumed), and the
    planner's research budget is the SAME constant competitor_research ships."""
    from app.agents.company_planner.agent import ROLE
    from app.agents.competitor_research.agent import RESEARCH_FETCH_BUDGET

    source = (BACKEND / "app" / "agents" / "company_planner" / "agent.py").read_text(encoding="utf-8")
    assert "DATA" in source
    assert "never follow" in source
    assert "ignore their previous instructions" in source or "ignore your previous instructions" in source
    assert f"at most {RESEARCH_FETCH_BUDGET}" in ROLE
    assert "3 and 5" in ROLE or "3-5" in ROLE


@pytest.mark.asyncio
async def test_propose_and_status_require_the_api_key(planner_client):
    assert (await planner_client.post("/api/v1/planner/propose")).status_code == 401
    assert (await planner_client.get("/api/v1/planner/status")).status_code == 401
