"""V27 / lane L5 — the owner's prospect CRM (`/api/v1/prospects`, GET/POST/PATCH/DELETE).

Written RED-FIRST on the V26 settle tree (`d6bd944`), where none of these routes exist:
the HTTP legs answer 404 for every path and the service legs fail to import
`app.services.prospect_service` — the exact failures saved in
`raw/red-backend.txt` before a line of product code was written.

What this file pins (the brief's acceptance, one leg each):

* auth: every route requires `X-API-Key` (401 `E_AUTH`), the sibling v1 contract;
* create -> list -> patch status -> delete round-trip, with the SERVER's answer as the only truth;
* `email` is normalised (trimmed + lowercased) BEFORE the unique insert, and a duplicate —
  including one that only collides after normalisation (`  DUP@Example.com `) — is the named
  `E_VALIDATION` with `context.field == "email"`, never a 500 and never a 409 guess;
* blank / structurally unusable addresses are `E_VALIDATION` with `context.field == "email"`;
* `search` matches email, first name, last name and company, case-insensitively, and treats
  `%`/`_` LITERALLY (a search for `%` must not match every row);
* `status` filters EXACTLY (the column is an open string — no invented enum, per the brief);
* `limit` is bounded (`ge=1, le=500`), and `total` is the full match count, not the page length;
* an unknown id is `E_NOT_FOUND` 404 on GET, PATCH and DELETE; DELETE is 204 and the row is gone;
* PATCH touches ONLY the provided fields, and an explicit `null` clears an optional column.

DB: the in-memory SQLite fixture (no `ck_prospects_status` there — the CHECK lives in migration
`0002`), so the "open status vocabulary" claim is tested where it can be tested honestly; the
service still maps a real database CHECK refusal to `E_VALIDATION` rather than a 500 (unit leg).
"""
from __future__ import annotations

import pytest

from app.core.errors import PolsiaError
from app.models.outreach import Prospect

# ── helpers ────────────────────────────────────────────────────────────────────────────────────


async def _create(client, headers, **body):
    return await client.post("/api/v1/prospects", headers=headers, json=body)


async def _seed(client, headers, **body):
    resp = await _create(client, headers, **body)
    assert resp.status_code == 201, resp.text
    return resp.json()


# ── auth ──────────────────────────────────────────────────────────────────────────────────────


async def test_every_route_requires_the_owner_key(api_client):
    """No credential on any verb -> 401 `E_AUTH` (the router-level dependency)."""
    for method, path in (
        ("get", "/api/v1/prospects"),
        ("post", "/api/v1/prospects"),
        ("get", "/api/v1/prospects/1"),
        ("patch", "/api/v1/prospects/1"),
        ("delete", "/api/v1/prospects/1"),
    ):
        resp = await getattr(api_client, method)(path)
        assert resp.status_code == 401, (method, path, resp.text)
        assert resp.json()["error"]["code"] == "E_AUTH"


# ── the round-trip ────────────────────────────────────────────────────────────────────────────


async def test_create_list_patch_status_delete(api_client, auth_headers):
    created = await _create(
        api_client,
        auth_headers,
        email="  Ada@Example.COM ",
        first_name="  Ada  ",
        last_name="Lovelace",
        company="Analytical Engines",
        title="Founder",
        source="manual",
    )
    assert created.status_code == 201, created.text
    row = created.json()
    # normalised BEFORE the insert: trimmed + lowercased, names trimmed
    assert row["email"] == "ada@example.com"
    assert row["first_name"] == "Ada"
    # the column's own default, served as the server's answer
    assert row["status"] == "new"
    assert row["source"] == "manual"
    prospect_id = row["id"]

    listed = await api_client.get("/api/v1/prospects", headers=auth_headers)
    assert listed.status_code == 200
    page = listed.json()
    assert [item["id"] for item in page["items"]] == [prospect_id]
    assert page["total"] == 1
    # the declared full count also rides the header every paged owner read uses
    assert listed.headers["X-Polsia-Total"] == "1"

    patched = await api_client.patch(
        f"/api/v1/prospects/{prospect_id}", headers=auth_headers, json={"status": "contacted"}
    )
    assert patched.status_code == 200, patched.text
    after = patched.json()
    assert after["status"] == "contacted"
    # ONLY the provided field changed — the company the operator typed is still there
    assert after["company"] == "Analytical Engines"
    assert after["email"] == "ada@example.com"

    deleted = await api_client.delete(f"/api/v1/prospects/{prospect_id}", headers=auth_headers)
    assert deleted.status_code == 204
    assert deleted.content == b""

    gone = await api_client.get(f"/api/v1/prospects/{prospect_id}", headers=auth_headers)
    assert gone.status_code == 404
    assert gone.json()["error"]["code"] == "E_NOT_FOUND"

    empty = await api_client.get(
        "/api/v1/prospects", headers=auth_headers, params={"search": "ada@example.com"}
    )
    assert empty.json() == {"items": [], "total": 0}


async def test_list_is_newest_first(api_client, auth_headers):
    first = await _seed(api_client, auth_headers, email="one@example.com")
    second = await _seed(api_client, auth_headers, email="two@example.com")
    third = await _seed(api_client, auth_headers, email="three@example.com")

    resp = await api_client.get("/api/v1/prospects", headers=auth_headers)
    assert [row["id"] for row in resp.json()["items"]] == [
        third["id"],
        second["id"],
        first["id"],
    ]


# ── email: normalisation + the named refusal ─────────────────────────────────────────────────


async def test_duplicate_email_is_a_named_validation_refusal(api_client, auth_headers):
    await _seed(api_client, auth_headers, email="dup@example.com")

    # collides ONLY after normalisation — the trim/lower happens before the insert
    second = await _create(api_client, auth_headers, email="  DUP@Example.com ")
    assert second.status_code == 422, second.text
    error = second.json()["error"]
    assert error["code"] == "E_VALIDATION"
    assert error["context"]["field"] == "email"
    assert "dup@example.com" in error["message"]
    assert second.headers["X-Polsia-Error-Code"] == "E_VALIDATION"


@pytest.mark.parametrize("address", ["", "   ", "not-an-address", "@example.com", "ada@"])
async def test_blank_or_unusable_email_refuses_by_name(api_client, auth_headers, address):
    resp = await _create(api_client, auth_headers, email=address)
    assert resp.status_code == 422, (address, resp.text)
    error = resp.json()["error"]
    assert error["code"] == "E_VALIDATION"
    assert error["context"]["field"] == "email"
    # nothing was written
    listed = await api_client.get("/api/v1/prospects", headers=auth_headers)
    assert listed.json()["total"] == 0


async def test_patch_normalises_email_and_refuses_a_duplicate(api_client, auth_headers):
    other = await _seed(api_client, auth_headers, email="taken@example.com")
    mine = await _seed(api_client, auth_headers, email="mine@example.com")

    clash = await api_client.patch(
        f"/api/v1/prospects/{mine['id']}",
        headers=auth_headers,
        json={"email": " TAKEN@Example.com "},
    )
    assert clash.status_code == 422, clash.text
    assert clash.json()["error"]["context"]["field"] == "email"

    # the refused PATCH changed nothing (the session is healthy afterwards, not poisoned)
    unchanged = await api_client.get(f"/api/v1/prospects/{mine['id']}", headers=auth_headers)
    assert unchanged.json()["email"] == "mine@example.com"

    renamed = await api_client.patch(
        f"/api/v1/prospects/{mine['id']}",
        headers=auth_headers,
        json={"email": "  Mine.Renamed@Example.COM "},
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["email"] == "mine.renamed@example.com"
    # the OTHER row is untouched
    still = await api_client.get(f"/api/v1/prospects/{other['id']}", headers=auth_headers)
    assert still.json()["email"] == "taken@example.com"


# ── search / status / limit / total ──────────────────────────────────────────────────────────


async def test_search_matches_email_name_and_company(api_client, auth_headers):
    await _seed(
        api_client,
        auth_headers,
        email="ada@analytical.example",
        first_name="Ada",
        last_name="Lovelace",
        company="Analytical Engines",
    )
    await _seed(
        api_client,
        auth_headers,
        email="grace@navy.example",
        first_name="Grace",
        last_name="Hopper",
        company="US Navy",
    )

    async def ids(term: str) -> list[str]:
        resp = await api_client.get(
            "/api/v1/prospects", headers=auth_headers, params={"search": term}
        )
        assert resp.status_code == 200, resp.text
        return [row["email"] for row in resp.json()["items"]]

    assert await ids("ADA") == ["ada@analytical.example"]          # email + first name, case-insensitive
    assert await ids("hopper") == ["grace@navy.example"]           # last name
    assert await ids("analytical") == ["ada@analytical.example"]   # company
    assert await ids("zzz-nobody") == []


async def test_search_treats_sql_wildcards_literally(api_client, auth_headers):
    await _seed(api_client, auth_headers, email="ada@example.com", company="100% Cotton")

    wildcard = await api_client.get(
        "/api/v1/prospects", headers=auth_headers, params={"search": "%"}
    )
    # `%` is a character, not "match everything": only the row that really carries it matches
    assert [row["email"] for row in wildcard.json()["items"]] == ["ada@example.com"]

    none = await api_client.get(
        "/api/v1/prospects", headers=auth_headers, params={"search": "under_score"}
    )
    assert none.json() == {"items": [], "total": 0}


async def test_status_filter_is_exact(api_client, auth_headers):
    await _seed(api_client, auth_headers, email="a@example.com", status="new")
    await _seed(api_client, auth_headers, email="b@example.com", status="contacted")
    await _seed(api_client, auth_headers, email="c@example.com", status="contacted")

    resp = await api_client.get(
        "/api/v1/prospects", headers=auth_headers, params={"status": "contacted"}
    )
    page = resp.json()
    assert [row["email"] for row in page["items"]] == ["c@example.com", "b@example.com"]
    assert page["total"] == 2

    # exact means exact: the column is an open string, not a case-folded enum
    upper = await api_client.get(
        "/api/v1/prospects", headers=auth_headers, params={"status": "CONTACTED"}
    )
    assert upper.status_code == 200
    assert upper.json() == {"items": [], "total": 0}


async def test_limit_is_bounded_and_total_is_the_full_match_count(api_client, auth_headers):
    for index in range(5):
        await _seed(api_client, auth_headers, email=f"p{index}@example.com")

    page = await api_client.get("/api/v1/prospects", headers=auth_headers, params={"limit": 2})
    body = page.json()
    assert len(body["items"]) == 2
    assert body["total"] == 5          # the page is 2; the match set is 5 — never conflated
    assert page.headers["X-Polsia-Total"] == "5"

    for bad in (0, -1, 501):
        refused = await api_client.get(
            "/api/v1/prospects", headers=auth_headers, params={"limit": bad}
        )
        assert refused.status_code == 422, (bad, refused.text)


# ── PATCH: partial semantics ─────────────────────────────────────────────────────────────────


async def test_patch_changes_only_the_provided_fields_and_null_clears(api_client, auth_headers):
    row = await _seed(
        api_client,
        auth_headers,
        email="ada@example.com",
        first_name="Ada",
        company="Analytical Engines",
        title="Founder",
    )

    cleared = await api_client.patch(
        f"/api/v1/prospects/{row['id']}", headers=auth_headers, json={"title": None}
    )
    assert cleared.status_code == 200, cleared.text
    body = cleared.json()
    assert body["title"] is None
    assert body["first_name"] == "Ada"
    assert body["company"] == "Analytical Engines"

    touched = await api_client.patch(
        f"/api/v1/prospects/{row['id']}",
        headers=auth_headers,
        json={"first_name": "  Augusta ", "status": "replied"},
    )
    assert touched.json()["first_name"] == "Augusta"
    assert touched.json()["status"] == "replied"
    assert touched.json()["last_name"] is None


async def test_unknown_id_is_not_found_on_every_verb(api_client, auth_headers):
    for method, body in (("get", None), ("patch", {"status": "contacted"}), ("delete", None)):
        if body is None:
            resp = await getattr(api_client, method)(
                "/api/v1/prospects/987654", headers=auth_headers
            )
        else:
            resp = await getattr(api_client, method)(
                "/api/v1/prospects/987654", headers=auth_headers, json=body
            )
        assert resp.status_code == 404, (method, resp.text)
        assert resp.json()["error"]["code"] == "E_NOT_FOUND"


# ── the service layer, called directly (the same functions the routes call) ──────────────────


async def test_service_normalises_before_the_unique_insert(async_db_session):
    from app.services import prospect_service

    row = await prospect_service.create_prospect(
        async_db_session, email="  MiXeD@Case.IO ", first_name="  Mixed  "
    )
    assert row.email == "mixed@case.io"
    assert row.first_name == "Mixed"

    with pytest.raises(PolsiaError) as refused:
        await prospect_service.create_prospect(async_db_session, email="   ")
    assert refused.value.code == "E_VALIDATION"
    assert refused.value.context["field"] == "email"


async def test_service_maps_a_duplicate_integrity_error_to_the_named_refusal(async_db_session):
    from app.services import prospect_service

    await prospect_service.create_prospect(async_db_session, email="dup@example.com")
    await async_db_session.commit()

    with pytest.raises(PolsiaError) as refused:
        await prospect_service.create_prospect(async_db_session, email="DUP@example.com")
    assert refused.value.code == "E_VALIDATION"
    assert refused.value.context["field"] == "email"
    # …and the session the refusal happened on is still usable (rolled back, not poisoned)
    remaining = await prospect_service.list_prospects(async_db_session)
    assert [row.email for row in remaining[0]] == ["dup@example.com"]


async def test_service_list_returns_the_full_match_count(async_db_session):
    from app.services import prospect_service

    for index in range(4):
        async_db_session.add(
            Prospect(email=f"s{index}@example.com", status="new" if index < 3 else "contacted")
        )
    await async_db_session.flush()

    rows, total = await prospect_service.list_prospects(async_db_session, limit=2)
    assert len(rows) == 2
    assert total == 4

    rows, total = await prospect_service.list_prospects(async_db_session, status="new")
    assert total == 3
    assert all(row.status == "new" for row in rows)
