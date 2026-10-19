"""R27 — the two read routes: auth, boundaries, wire shape, refusal translation, direct-call legs.

Lane R27 (owned: `app/api/v1/social.py`, `app/api/v1/outreach.py`, the two services, this file).
The routes are consumed by the frontend (`/social`, `/outreach` pages), so the pinned contract is:
`X-API-Key` required, `limit` bounded `ge=1 le=500`, `status`/`platform` filters exact,
newest-first order, and — the honesty rule the whole service layer is built on — a FAILED read
must surface as the named `E_DB` refusal (503 + `X-Polsia-Error-Code`), never as a `200 []` that
reads like "no posts".

WHY every route body is ALSO called directly (the M1 measurement, raw/probe_asgi_vs_direct.txt):
under this repo's toolchain the LAST statement of an async endpoint is not credited to coverage
when the request travels through the ASGI stack, while a direct call of the same function credits
it (measured: `pytest tests/unit/api/test_social.py --cov=app.api.v1.social` reports `23` missing
even though the route demonstrably returns; adding one direct call reports 100%). The same pattern
shows on `app/api/v1/finance.py` (passing tests, return lines 75/88/99 reported missing). The
direct-call legs keep the coverage numbers honest under this toolchain — they call REAL production
functions with a REAL session; no double is involved.

M2 (out of lane, reported as REQUIRED-FOLLOW-UP): the same `E_DB` reaches the wire as 503 through
`refusal_guard` (route body) but as 500 through the app-level handler when raised before the route
(e.g. `get_db` failing) — measured in raw/probe_edb_paths.txt.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException, Response
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1.outreach import outreach_prospects
from app.api.v1.social import social_posts
from app.models.outreach import Prospect
from app.models.social import SocialPost

NOW = datetime(2026, 10, 12, 12, 0, 0, tzinfo=UTC)


class _Raiser:
    """A callable that raises `exc` — the dead-driver simulation for the refusal legs."""

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc

    async def __call__(self, *args, **kwargs):
        raise self.exc


def _break_execute(monkeypatch: pytest.MonkeyPatch, db) -> None:
    monkeypatch.setattr(db, "execute", _Raiser(SQLAlchemyError("simulated driver failure")))


def _seed_posts(db, rows: list[tuple[str, str, str, datetime]]) -> None:
    for platform, content, status, created_at in rows:
        db.add(SocialPost(platform=platform, content=content, status=status,
                          created_at=created_at))


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# /api/v1/social/posts — through HTTP
# ═══════════════════════════════════════════════════════════════════════════════════════════════

async def test_social_posts_requires_a_key(api_client):
    """No credential -> 401 named E_AUTH; a WRONG credential is also rejected (not an open door)."""
    resp = await api_client.get("/api/v1/social/posts")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"

    wrong = await api_client.get("/api/v1/social/posts", headers={"X-API-Key": "not-the-key"})
    assert wrong.status_code == 401
    assert wrong.json()["error"]["code"] == "E_AUTH"


async def test_social_posts_empty_state_is_a_200_empty_list(api_client, auth_headers):
    resp = await api_client.get("/api/v1/social/posts", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_social_posts_filters_through_query_params(api_client, auth_headers,
                                                          async_db_session):
    _seed_posts(async_db_session, [
        ("twitter", "t-draft", "draft", NOW),
        ("twitter", "t-pub", "published", NOW - timedelta(minutes=1)),
        ("linkedin", "l-pub", "published", NOW - timedelta(minutes=2)),
    ])
    await async_db_session.flush()

    published = await api_client.get("/api/v1/social/posts?status=published", headers=auth_headers)
    assert {row["content"] for row in published.json()} == {"t-pub", "l-pub"}

    twitter = await api_client.get("/api/v1/social/posts?platform=twitter", headers=auth_headers)
    assert {row["content"] for row in twitter.json()} == {"t-draft", "t-pub"}

    both = await api_client.get("/api/v1/social/posts?status=published&platform=linkedin",
                                headers=auth_headers)
    assert [row["content"] for row in both.json()] == ["l-pub"]

    missing = await api_client.get("/api/v1/social/posts?status=archived", headers=auth_headers)
    # DELIBERATE RE-PIN (V20/L1, card L2-08): `?status=` now validates against
    # `ck_social_posts_status`'s vocabulary, so an unknown word is the named 422 every sibling
    # status read answers instead of a `200 []` that reads like "no posts".
    assert missing.status_code == 422, missing.text[:300]
    assert missing.headers.get("X-Polsia-Error-Code") == "E_VALIDATION"
    assert missing.json()["error"]["context"]["status"] == "archived"


async def test_social_posts_wire_shape_and_order(api_client, auth_headers, async_db_session):
    """Newest-first with the id tie-break, and exactly the response-model fields on the wire."""
    _seed_posts(async_db_session, [
        ("twitter", "old", "draft", NOW - timedelta(days=1)),
        ("twitter", "old-newer-id", "draft", NOW - timedelta(days=1)),
        ("twitter", "newest", "draft", NOW),
    ])
    await async_db_session.flush()

    resp = await api_client.get("/api/v1/social/posts", headers=auth_headers)
    assert resp.status_code == 200
    payload = resp.json()
    assert [row["content"] for row in payload] == ["newest", "old-newer-id", "old"]
    assert set(payload[0]) == {"id", "platform", "content", "status", "tweet_id",
                               "scheduled_for", "published_at", "engagement", "created_at"}
    assert payload[0]["tweet_id"] is None and payload[0]["engagement"] is None


async def test_social_posts_limit_boundaries(api_client, auth_headers, async_db_session):
    _seed_posts(async_db_session, [
        ("twitter", f"p{i}", "draft", NOW - timedelta(minutes=i)) for i in range(3)
    ])
    await async_db_session.flush()

    one = await api_client.get("/api/v1/social/posts?limit=1", headers=auth_headers)
    assert one.status_code == 200 and [r["content"] for r in one.json()] == ["p0"]

    maxed = await api_client.get("/api/v1/social/posts?limit=500", headers=auth_headers)
    assert maxed.status_code == 200 and len(maxed.json()) == 3

    for bad in ("limit=0", "limit=501", "limit=abc"):
        resp = await api_client.get(f"/api/v1/social/posts?{bad}", headers=auth_headers)
        assert resp.status_code == 422, bad


async def test_social_posts_read_failure_is_a_named_refusal_not_an_empty_list(
    api_client, auth_headers, async_db_session, monkeypatch,
):
    """A dead read must NOT be served as `200 []` — the silent-empty failure this layer exists for."""
    _break_execute(monkeypatch, async_db_session)
    resp = await api_client.get("/api/v1/social/posts", headers=auth_headers)
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "E_DB"
    assert "could not read social posts" in body["error"]["message"]
    assert resp.headers["X-Polsia-Error-Code"] == "E_DB"


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# /api/v1/outreach/prospects — through HTTP
# ═══════════════════════════════════════════════════════════════════════════════════════════════

async def test_outreach_prospects_requires_a_key(api_client):
    resp = await api_client.get("/api/v1/outreach/prospects")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"


async def test_outreach_prospects_empty_state_is_a_200_empty_list(api_client, auth_headers):
    resp = await api_client.get("/api/v1/outreach/prospects", headers=auth_headers)
    assert resp.status_code == 200
    assert resp.json() == []


async def test_outreach_prospects_payload_shape_filters_and_order(api_client, auth_headers,
                                                                  async_db_session):
    async_db_session.add_all([
        Prospect(email="new0@example.com", status="new", created_at=NOW),
        Prospect(email="new1@example.com", status="new", created_at=NOW - timedelta(minutes=1)),
        Prospect(email="contacted@example.com", status="contacted",
                 created_at=NOW - timedelta(minutes=2)),
    ])
    await async_db_session.flush()

    resp = await api_client.get("/api/v1/outreach/prospects", headers=auth_headers)
    assert resp.status_code == 200
    payload = resp.json()
    assert [row["email"] for row in payload] == [
        "new0@example.com", "new1@example.com", "contacted@example.com"]
    assert set(payload[0]) == {"id", "email", "first_name", "last_name", "company", "title",
                               "source", "status", "created_at"}

    filtered = await api_client.get("/api/v1/outreach/prospects?status=new", headers=auth_headers)
    assert {row["email"] for row in filtered.json()} == {
        "new0@example.com", "new1@example.com"}


async def test_outreach_prospects_limit_boundaries(api_client, auth_headers, async_db_session):
    async_db_session.add_all([
        Prospect(email=f"lim{i}@example.com", created_at=NOW - timedelta(minutes=i))
        for i in range(3)
    ])
    await async_db_session.flush()

    one = await api_client.get("/api/v1/outreach/prospects?limit=1", headers=auth_headers)
    assert one.status_code == 200 and [r["email"] for r in one.json()] == ["lim0@example.com"]

    maxed = await api_client.get("/api/v1/outreach/prospects?limit=500", headers=auth_headers)
    assert maxed.status_code == 200 and len(maxed.json()) == 3

    for bad in ("limit=0", "limit=501", "limit=abc"):
        resp = await api_client.get(f"/api/v1/outreach/prospects?{bad}", headers=auth_headers)
        assert resp.status_code == 422, bad


async def test_outreach_prospects_read_failure_is_a_named_refusal(
    api_client, auth_headers, async_db_session, monkeypatch,
):
    _break_execute(monkeypatch, async_db_session)
    resp = await api_client.get("/api/v1/outreach/prospects", headers=auth_headers)
    assert resp.status_code == 503
    body = resp.json()
    assert body["ok"] is False and body["error"]["code"] == "E_DB"
    assert "could not read prospects" in body["error"]["message"]
    assert resp.headers["X-Polsia-Error-Code"] == "E_DB"


# ═══════════════════════════════════════════════════════════════════════════════════════════════
# Direct route-body calls (the M1 compensation leg + `refusal_guard` translation pinned as such)
# ═══════════════════════════════════════════════════════════════════════════════════════════════

async def test_social_posts_route_body_direct_call(async_db_session):
    assert await social_posts(status_filter=None, platform=None, limit=50,
                              db=async_db_session) == []

    _seed_posts(async_db_session, [
        ("twitter", "direct-a", "published", NOW),
        ("linkedin", "direct-b", "draft", NOW - timedelta(minutes=1)),
    ])
    await async_db_session.flush()

    rows = await social_posts(status_filter=None, platform=None, limit=50, db=async_db_session)
    assert [r.content for r in rows] == ["direct-a", "direct-b"]
    assert all(type(r).__name__ == "SocialPostResponse" for r in rows)

    filtered = await social_posts(status_filter="published", platform="twitter", limit=1,
                                  db=async_db_session)
    assert [r.content for r in filtered] == ["direct-a"]


async def test_outreach_prospects_route_body_direct_call(async_db_session):
    # V20/L1: the route declares the honest total on `X-Polsia-Total`, so the `Response` FastAPI
    # injects in production is supplied here; the return contract (validated models) is unchanged.
    assert await outreach_prospects(response=Response(), status_filter=None, limit=100,
                                    db=async_db_session) == []

    async_db_session.add_all([
        Prospect(email="direct0@example.com", status="new", created_at=NOW),
        Prospect(email="direct1@example.com", status="contacted",
                 created_at=NOW - timedelta(minutes=1)),
    ])
    await async_db_session.flush()

    rows = await outreach_prospects(response=Response(), status_filter=None, limit=100,
                                    db=async_db_session)
    assert [r.email for r in rows] == ["direct0@example.com", "direct1@example.com"]
    assert all(type(r).__name__ == "ProspectResponse" for r in rows)

    filtered = await outreach_prospects(response=Response(), status_filter="contacted", limit=100,
                                        db=async_db_session)
    assert [r.email for r in filtered] == ["direct1@example.com"]


async def test_route_body_direct_call_translates_E_DB_through_refusal_guard(
    async_db_session, monkeypatch,
):
    """`refusal_guard` is what turns the service's named refusal into the coded HTTP answer.

    Called directly the route RAISES the `HTTPException` (there is no ASGI layer to serialize it);
    the shape asserted here is exactly what `coded_detail_envelope` then puts on the wire.
    """
    _break_execute(monkeypatch, async_db_session)

    for route, kwargs in (
        (social_posts, {"status_filter": None, "platform": None, "limit": 50}),
        (outreach_prospects, {"response": Response(), "status_filter": None, "limit": 100}),
    ):
        with pytest.raises(HTTPException) as excinfo:
            await route(db=async_db_session, **kwargs)
        exc = excinfo.value
        assert exc.status_code == 503
        assert exc.detail["code"] == "E_DB"
        assert exc.headers["X-Polsia-Error-Code"] == "E_DB"
        assert exc.detail["how_to_fix"]                        # a refusal says how to fix it
