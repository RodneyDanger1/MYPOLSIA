"""R19 — `DELETE /api/v1/memory/{id}` through the HTTP seam.

This route is what `e2e/_sweep-fixtures.py`'s REQUESTS item asked for (RD10-e2eci RECEIPT,
§REQUESTS 1): a backend-mediated delete that removes the sqlite row AND its chroma
document, so a caller with a live backend never has to reach into the store directly.

`StatefulChromaDouble` is imported from the sibling service-level module
(`tests.unit.test_memory_delete`) — one definition serves both seams, and the shipped
`tests/conftest.py::mock_chroma` stays the fixture for the call-shape legs (conftest is
another seat's file this wave and is deliberately not touched).
"""
from __future__ import annotations

import pytest

from tests.unit.test_memory_delete import StatefulChromaDouble


@pytest.fixture
def stateful_chroma(mocker):
    """The store-semantics double, patched into both namespaces like conftest's mock_chroma."""
    collection = StatefulChromaDouble()
    mocker.patch("app.core.chroma_client.get_collection", return_value=collection)
    mocker.patch("app.services.memory_service.get_collection", return_value=collection)
    return collection


async def _create(api_client, auth_headers, *, title="Doomed memory", content="delete me via the API"):
    resp = await api_client.post(
        "/api/v1/memory",
        headers=auth_headers,
        json={"category": "strategy", "title": title, "content": content},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.asyncio
async def test_delete_memory_removes_both_halves(api_client, auth_headers, mock_chroma):
    entry = await _create(api_client, auth_headers)

    resp = await api_client.delete(f"/api/v1/memory/{entry['id']}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == entry["id"]
    assert body["chroma_id"] == entry["chroma_id"]  # the receipt names the document it removed
    mock_chroma.delete.assert_called_once_with(ids=[entry["chroma_id"]])

    listed = await api_client.get("/api/v1/memory", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json() == []


@pytest.mark.asyncio
async def test_delete_missing_memory_is_404_e_not_found(api_client, auth_headers):
    resp = await api_client.delete("/api/v1/memory/999999", headers=auth_headers)

    assert resp.status_code == 404
    error = resp.json()["error"]  # the app-wide refusal envelope (main.py coded handlers)
    assert error["code"] == "E_NOT_FOUND"
    assert "999999" in error["message"]
    assert error["how_to_fix"]


@pytest.mark.asyncio
async def test_delete_twice_second_call_is_404(api_client, auth_headers, mock_chroma):
    entry = await _create(api_client, auth_headers)

    first = await api_client.delete(f"/api/v1/memory/{entry['id']}", headers=auth_headers)
    assert first.status_code == 200

    second = await api_client.delete(f"/api/v1/memory/{entry['id']}", headers=auth_headers)
    assert second.status_code == 404
    assert second.json()["error"]["code"] == "E_NOT_FOUND"


@pytest.mark.asyncio
async def test_search_after_delete_returns_no_ghosts_through_the_api(
    api_client, auth_headers, stateful_chroma
):
    token = "ghosttokenapi"
    entry = await _create(api_client, auth_headers, title="Ghost bait API", content=f"unique {token}")

    before = await api_client.get(f"/api/v1/memory?q={token}", headers=auth_headers)
    assert before.status_code == 200
    assert [hit["id"] for hit in before.json()] == [entry["chroma_id"]]

    gone = await api_client.delete(f"/api/v1/memory/{entry['id']}", headers=auth_headers)
    assert gone.status_code == 200

    # The semantic leg: a surviving chroma document would answer this search.
    after_search = await api_client.get(f"/api/v1/memory?q={token}", headers=auth_headers)
    assert after_search.status_code == 200
    assert after_search.json() == []
    # … and the plain list is empty too.
    after_list = await api_client.get("/api/v1/memory", headers=auth_headers)
    assert after_list.json() == []


@pytest.mark.asyncio
async def test_chroma_delete_failure_is_503_e_storage_and_the_row_survives(
    api_client, auth_headers, mock_chroma
):
    entry = await _create(api_client, auth_headers)
    mock_chroma.delete.side_effect = RuntimeError("the vector store is down")

    resp = await api_client.delete(f"/api/v1/memory/{entry['id']}", headers=auth_headers)

    assert resp.status_code == 503
    error = resp.json()["error"]
    assert error["code"] == "E_STORAGE"
    assert error["context"]["chroma_id"] == entry["chroma_id"]

    # Nothing half-deleted: the row is still listed, so the delete can be retried honestly.
    listed = await api_client.get("/api/v1/memory", headers=auth_headers)
    assert listed.status_code == 200
    assert [row["id"] for row in listed.json()] == [entry["id"]]

    # After the store recovers, the very same call finishes the delete.
    mock_chroma.delete.side_effect = None
    retry = await api_client.delete(f"/api/v1/memory/{entry['id']}", headers=auth_headers)
    assert retry.status_code == 200
    assert (await api_client.get("/api/v1/memory", headers=auth_headers)).json() == []


@pytest.mark.asyncio
async def test_delete_requires_the_api_key(api_client):
    resp = await api_client.delete("/api/v1/memory/1")  # no X-API-Key header

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "E_AUTH"
