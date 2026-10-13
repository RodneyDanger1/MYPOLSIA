"""FS1 contract-hardening pins (fixer seat 1): L1-01 · L1-02 · L1-10 · L1-11.

Every leg here was RED before its fix (the applied-fix RED legs are recorded in
`polsia-work/build/full-sweep/fix-1/RECEIPT.md`):

* L1-01 — `PUT /api/v1/config {"timezone": "Not/AZone"}` and `{"daily_cycle_hour": 99}` answered
  **200 with the value echoed back**, and the runtime refused the stored value later by name
  (`E_CONFIG` in `company_tz` / `_resolve_morning_hour`): the write succeeded, the scheduler died.
* L1-02 — the service tags a degraded read (`fallback: "sql_like"` when the vector store is down and
  the read became an ILIKE scan; `"sql_unfiltered"` when `where` was dropped); `MemoryItem` had no
  field for the marker, so pydantic dropped it and a keyword scan looked like a semantic hit.
* L1-10 — `CompanyConfigResponse` required `timezone: str` / `daily_cycle_hour: int` on DDL-nullable
  columns: `PUT {"timezone": null}` and any schema-legal NULL row raised a RAW pydantic
  `ValidationError` inside the route → bare, codeless 500 (`text/plain: Internal Server Error`).
* L1-11 — same class on four more list models; ONE NULL row 500'd the WHOLE list.

The nullable models are the parity-safe minimum: the shipped DDL (`alembic/versions/0001`) declares
these columns with `server_default`s but NO `NOT NULL`, so a NULL row is schema-legal and a
raw-SQL/ops writer is an accepted path — the premise R26/R31 already accepted for `ad_campaigns`
(pinned by `test_r26_ads_edges.py`).  `None` must be served as `null`, never coerced to a fabricated
`0`/"pending".
"""
from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import text

from app.models.company import CompanyConfig
from app.models.memory import MemoryEntry

# ══════════════════════════════════════════════════════════════════════════════════════════════
# L1-01 — the config WRITE side refuses what the runtime refuses (422 before the row is touched)
# ══════════════════════════════════════════════════════════════════════════════════════════════


async def _seed_company(session, **kwargs) -> CompanyConfig:
    company = CompanyConfig(name="Probe Co", timezone="UTC", daily_cycle_hour=6, **kwargs)
    session.add(company)
    await session.commit()
    return company


async def _raw_config_row(session) -> tuple[str | None, int | None, str | None]:
    row = (
        await session.execute(text("SELECT timezone, daily_cycle_hour, name FROM company_config"))
    ).one()
    return (row[0], row[1], row[2])


@pytest.mark.asyncio
async def test_put_unknown_timezone_is_a_422_naming_the_field(
    api_client, auth_headers, async_db_session
):
    """Was RED: 200 + `timezone` stored; now FastAPI refuses with the field named, row untouched."""
    await _seed_company(async_db_session)

    resp = await api_client.put(
        "/api/v1/config", headers=auth_headers, json={"timezone": "Not/AZone"}
    )

    assert resp.status_code == 422, resp.text[:300]
    locs = [err["loc"] for err in resp.json()["detail"]]
    assert ["body", "timezone"] in locs, f"the refusal must name the field: {locs}"
    # The row is untouched: the value the runtime would refuse never lands.
    assert (await _raw_config_row(async_db_session))[0] == "UTC"


@pytest.mark.asyncio
async def test_put_out_of_range_daily_cycle_hour_is_a_422(api_client, auth_headers, async_db_session):
    """Was RED for both: `99` (the morning cycle would never run again) and `-1`."""
    await _seed_company(async_db_session)

    for bad in (99, -1):
        resp = await api_client.put(
            "/api/v1/config", headers=auth_headers, json={"daily_cycle_hour": bad}
        )
        assert resp.status_code == 422, f"{bad} must be refused: {resp.text[:200]}"
        locs = [err["loc"] for err in resp.json()["detail"]]
        assert ["body", "daily_cycle_hour"] in locs

    assert (await _raw_config_row(async_db_session))[1] == 6

    # The legal boundary values still pass, and only the sent field changes.
    for good in (0, 23):
        resp = await api_client.put(
            "/api/v1/config", headers=auth_headers, json={"daily_cycle_hour": good}
        )
        assert resp.status_code == 200, resp.text[:200]
        assert resp.json()["daily_cycle_hour"] == good
        assert resp.json()["timezone"] == "UTC"


@pytest.mark.asyncio
async def test_put_valid_zone_is_accepted_and_echoed(api_client, auth_headers, async_db_session):
    await _seed_company(async_db_session)

    resp = await api_client.put(
        "/api/v1/config", headers=auth_headers, json={"timezone": "America/Chicago"}
    )

    assert resp.status_code == 200, resp.text[:200]
    assert resp.json()["timezone"] == "America/Chicago"
    assert (await _raw_config_row(async_db_session))[0] == "America/Chicago"


@pytest.mark.asyncio
async def test_put_cleared_timezone_is_stored_as_null(api_client, auth_headers, async_db_session):
    """The settings page PUTs the whole object and sends `""` for a cleared box.

    `""` is normalised to `None` — the state `company_service.get_daily_cycle_hour` documents
    ("a stored NULL means 'no value'"), NOT a zone the runtime later refuses.  Was RED twice over:
    the empty string was stored verbatim, and the response model then could not serve it.
    """
    await _seed_company(async_db_session)

    resp = await api_client.put("/api/v1/config", headers=auth_headers, json={"timezone": ""})

    assert resp.status_code == 200, resp.text[:200]
    assert resp.json()["timezone"] is None
    assert (await _raw_config_row(async_db_session))[0] is None


def test_update_model_normalises_blank_timezone_and_bounds_the_hour():
    """Model-level pin (no DB): the exact contract the route relies on."""
    from pydantic import ValidationError

    from app.schemas.company import CompanyConfigUpdate

    assert CompanyConfigUpdate(timezone="").timezone is None
    assert CompanyConfigUpdate(timezone="   ").timezone is None
    assert CompanyConfigUpdate(timezone=None).timezone is None
    assert CompanyConfigUpdate(timezone="UTC").timezone == "UTC"
    assert CompanyConfigUpdate(daily_cycle_hour=0).daily_cycle_hour == 0
    assert CompanyConfigUpdate(daily_cycle_hour=23).daily_cycle_hour == 23
    # An absent key stays absent — `int = Field(ge=0, le=23)` would have 422'd the whole object.
    assert CompanyConfigUpdate(name="x").daily_cycle_hour is None
    for bad in (24, -1, 99):
        with pytest.raises(ValidationError):
            CompanyConfigUpdate(daily_cycle_hour=bad)
    with pytest.raises(ValidationError):
        CompanyConfigUpdate(timezone="Not/AZone")


# ══════════════════════════════════════════════════════════════════════════════════════════════
# L1-02 — the memory service's degradation markers reach the wire (and the page can see them)
# ══════════════════════════════════════════════════════════════════════════════════════════════


def _boom() -> None:
    raise RuntimeError("chroma unreachable (test)")


class _WhereRejectingCollection:
    """A collection whose client rejects `where` — the `sql_unfiltered` trigger."""

    def query(self, **kwargs):
        if "where" in kwargs:
            raise TypeError("This collection does not accept `where`")
        return {
            "documents": [["alpha content"]],
            "metadatas": [[{"category": "strategy", "title": "alpha plan", "source": "test"}]],
            "ids": [["chroma-1"]],
            "distances": [[0.11]],
        }


class _SemanticCollection:
    """The healthy vector store — hits must carry NO marker."""

    def query(self, **kwargs):
        return {
            "documents": [["alpha content"]],
            "metadatas": [[{"category": "strategy", "title": "alpha plan", "source": "test"}]],
            "ids": [["chroma-1"]],
            "distances": [[0.11]],
        }


@pytest.mark.asyncio
async def test_keyword_fallback_marker_reaches_the_wire(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """Was RED: the service tagged `fallback: "sql_like"`, the wire JSON did not carry it."""
    import app.services.memory_service as memory_service

    async_db_session.add(
        MemoryEntry(category="strategy", title="alpha plan", content="alpha content", source="test")
    )
    await async_db_session.commit()
    monkeypatch.setattr(memory_service, "get_collection", _boom)

    resp = await api_client.get("/api/v1/memory?q=alpha&limit=5", headers=auth_headers)

    assert resp.status_code == 200, resp.text[:300]
    hits = resp.json()
    assert hits, "the keyword scan matched the seeded row"
    assert all(hit["fallback"] == "sql_like" for hit in hits), (
        "a keyword scan must never be served as an unmarked similarity hit"
    )


@pytest.mark.asyncio
async def test_where_drop_marker_reaches_the_wire(
    api_client, auth_headers, async_db_session, monkeypatch
):
    """The `sql_unfiltered` leg: the category filter was dropped, so the marker must travel."""
    import app.services.memory_service as memory_service

    monkeypatch.setattr(memory_service, "get_collection", lambda: _WhereRejectingCollection())

    resp = await api_client.get(
        "/api/v1/memory?q=alpha&limit=5&category=strategy", headers=auth_headers
    )

    assert resp.status_code == 200, resp.text[:300]
    assert [hit["fallback"] for hit in resp.json()] == ["sql_unfiltered"]


@pytest.mark.asyncio
async def test_semantic_hits_carry_no_marker(api_client, auth_headers, monkeypatch):
    """The healthy path must NOT gain a marker — `fallback: null` means "real similarity hit"."""
    import app.services.memory_service as memory_service

    monkeypatch.setattr(memory_service, "get_collection", lambda: _SemanticCollection())

    resp = await api_client.get("/api/v1/memory?q=alpha&limit=5", headers=auth_headers)

    assert resp.status_code == 200, resp.text[:300]
    hits = resp.json()
    assert hits and hits[0]["fallback"] is None


@pytest.mark.asyncio
async def test_plain_list_rows_carry_a_null_marker(api_client, auth_headers, async_db_session):
    """`list_memories` rows are not searches: the key exists and is `null`, never a fake marker."""
    async_db_session.add(
        MemoryEntry(category="strategy", title="alpha plan", content="alpha content", source="test")
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/memory", headers=auth_headers)

    assert resp.status_code == 200, resp.text[:300]
    rows = resp.json()
    assert rows and all(row["fallback"] is None for row in rows)


def test_memory_item_carries_the_marker_and_ignores_extra_keys():
    from app.schemas.memory import MemoryItem

    served = MemoryItem.model_validate(
        {
            "id": 1,
            "content": "alpha content",
            "category": "strategy",
            "title": "alpha plan",
            "source": "test",
            "memory_id": 1,
            "distance": None,
            "fallback": "sql_like",
        }
    )
    assert served.fallback == "sql_like"
    assert "fallback" in served.model_dump(), "null/marker must be serialised, never omitted"


# ══════════════════════════════════════════════════════════════════════════════════════════════
# L1-10 — CompanyConfigResponse serves the legal NULLs instead of a bare 500
# ══════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_put_null_timezone_is_served_not_a_bare_500(
    api_client, auth_headers, async_db_session
):
    """Was RED: a raw pydantic ValidationError inside the route → bare codeless 500."""
    await _seed_company(async_db_session)

    resp = await api_client.put("/api/v1/config", headers=auth_headers, json={"timezone": None})

    assert resp.status_code == 200, resp.text[:300]
    assert resp.json()["timezone"] is None
    assert (await _raw_config_row(async_db_session))[0] is None


@pytest.mark.asyncio
async def test_null_config_row_is_served_by_both_routes(api_client, auth_headers, async_db_session):
    """A schema-legal NULL row (raw/ops writer) must not freeze the Settings page forever."""
    await async_db_session.execute(text("DELETE FROM company_config"))
    await async_db_session.execute(
        text(
            "INSERT INTO company_config (name, timezone, daily_cycle_hour)"
            " VALUES ('Null Co', NULL, NULL)"
        )
    )
    await async_db_session.commit()

    read = await api_client.get("/api/v1/config", headers=auth_headers)
    assert read.status_code == 200, read.text[:300]
    assert read.json()["timezone"] is None
    assert read.json()["daily_cycle_hour"] is None

    write = await api_client.put("/api/v1/config", headers=auth_headers, json={"name": "Renamed"})
    assert write.status_code == 200, write.text[:300]
    assert write.json()["name"] == "Renamed"
    assert write.json()["timezone"] is None and write.json()["daily_cycle_hour"] is None


def test_config_response_serves_nulls_without_fabricating_values():
    from app.schemas.company import CompanyConfigResponse

    served = CompanyConfigResponse.model_validate(
        {"id": 1, "name": "Null Co", "timezone": None, "daily_cycle_hour": None}
    )
    assert served.timezone is None and served.daily_cycle_hour is None
    dumped = served.model_dump()
    assert dumped["timezone"] is None and dumped["daily_cycle_hour"] is None, (
        "the response must serve null, never a fabricated 'UTC'/6"
    )


# ══════════════════════════════════════════════════════════════════════════════════════════════
# L1-11 — ONE schema-legal NULL row must not 500 the whole list (four endpoints)
# ══════════════════════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tasks_list_serves_a_null_priority_status_row(api_client, auth_headers, async_db_session):
    from app.models.task import Task

    async_db_session.add(Task(title="fine", agent_type="finance", priority=2, status="pending"))
    await async_db_session.execute(
        text(
            "INSERT INTO tasks (title, agent_type, priority, status, created_at)"
            " VALUES ('raw row', 'finance', NULL, NULL, CURRENT_TIMESTAMP)"
        )
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/tasks", headers=auth_headers)

    assert resp.status_code != 500, f"a schema-legal row must not 500 the list: {resp.text[:200]}"
    assert resp.status_code == 200, resp.text[:300]
    rows = {row["title"]: row for row in resp.json()}
    assert (rows["raw row"]["priority"], rows["raw row"]["status"]) == (None, None)
    assert rows["fine"]["status"] == "pending", "recorded values must be undisturbed"


@pytest.mark.asyncio
async def test_revenue_list_serves_a_null_money_row(api_client, auth_headers, async_db_session):
    from app.models.finance import RevenueSnapshot

    async_db_session.add(RevenueSnapshot(snapshot_date=date(2026, 10, 12), mrr_cents=1))
    await async_db_session.execute(
        text(
            "INSERT INTO revenue_snapshots (snapshot_date, mrr_cents, arr_cents,"
            " active_subscribers, created_at)"
            " VALUES ('2026-10-13', NULL, NULL, NULL, CURRENT_TIMESTAMP)"
        )
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/finance/revenue", headers=auth_headers)

    assert resp.status_code != 500, f"a schema-legal row must not 500 the list: {resp.text[:200]}"
    assert resp.status_code == 200, resp.text[:300]
    by_date = {row["snapshot_date"]: row for row in resp.json()}
    null_row = by_date["2026-10-13"]
    assert (
        null_row["mrr_cents"],
        null_row["arr_cents"],
        null_row["active_subscribers"],
    ) == (None, None, None), "null is 'not measured' — never a fabricated 0"


@pytest.mark.asyncio
async def test_social_posts_list_serves_a_null_platform_status_row(
    api_client, auth_headers, async_db_session
):
    from app.models.social import SocialPost

    async_db_session.add(SocialPost(platform="twitter", content="fine", status="draft"))
    await async_db_session.execute(
        text(
            "INSERT INTO social_posts (platform, content, status, created_at)"
            " VALUES (NULL, 'raw row', NULL, CURRENT_TIMESTAMP)"
        )
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/social/posts", headers=auth_headers)

    assert resp.status_code != 500, f"a schema-legal row must not 500 the list: {resp.text[:200]}"
    assert resp.status_code == 200, resp.text[:300]
    rows = {row["content"]: row for row in resp.json()}
    assert (rows["raw row"]["platform"], rows["raw row"]["status"]) == (None, None)
    assert rows["fine"]["status"] == "draft"


@pytest.mark.asyncio
async def test_prospects_list_serves_a_null_status_row(api_client, auth_headers, async_db_session):
    from app.models.outreach import Prospect

    async_db_session.add(Prospect(email="fine@example.com", status="new"))
    await async_db_session.execute(
        text(
            "INSERT INTO prospects (email, status, created_at)"
            " VALUES ('raw@example.com', NULL, CURRENT_TIMESTAMP)"
        )
    )
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/outreach/prospects", headers=auth_headers)

    assert resp.status_code != 500, f"a schema-legal row must not 500 the list: {resp.text[:200]}"
    assert resp.status_code == 200, resp.text[:300]
    rows = {row["email"]: row for row in resp.json()}
    assert rows["raw@example.com"]["status"] is None
    assert rows["fine@example.com"]["status"] == "new"


def test_list_response_models_serve_schema_legal_nulls():
    """Model-level pin for the four nullable declarations (the R31 `AdCampaignResponse` shape)."""
    from app.schemas.finance import RevenueSnapshotResponse
    from app.schemas.social import ProspectResponse, SocialPostResponse
    from app.schemas.task import TaskResponse

    task = TaskResponse.model_validate({"id": 1, "title": "t", "agent_type": "finance",
                                        "priority": None, "status": None})
    assert (task.priority, task.status) == (None, None)

    snapshot = RevenueSnapshotResponse.model_validate(
        {"id": 1, "snapshot_date": "2026-10-13", "mrr_cents": None, "arr_cents": None,
         "active_subscribers": None}
    )
    assert (snapshot.mrr_cents, snapshot.arr_cents, snapshot.active_subscribers) == (None, None, None)

    post = SocialPostResponse.model_validate(
        {"id": 1, "platform": None, "content": "c", "status": None}
    )
    assert (post.platform, post.status) == (None, None)

    prospect = ProspectResponse.model_validate({"id": 1, "email": "a@b.c", "status": None})
    assert prospect.status is None


@pytest.mark.asyncio
async def test_config_null_row_check_for_the_ads_control_stays_green(api_client, auth_headers):
    """Control leg (R31 precedent, unchanged): a NULL `ad_campaigns` row is served 200."""
    resp = await api_client.get("/api/v1/ads/campaigns", headers=auth_headers)
    assert resp.status_code == 200, resp.text[:200]
