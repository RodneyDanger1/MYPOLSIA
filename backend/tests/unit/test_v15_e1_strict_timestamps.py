"""V15 / E1 — 5-06 (S4-F5, decision #10 LOCKED): strict timestamps — floats refuse BY NAME.

RED-first: at b17d7b0 the query params are bare `datetime`/`date` types, and pydantic's default
parser reads NUMERIC strings as epoch seconds — `GET /runs?from=1.5` silently became
`1970-01-01T00:00:01.5Z` and answered 200 (measured: TypeAdapter(datetime).validate_python("1.5")
-> 1970-01-01 00:00:01.500000+00:00).  Locked rule: admit ISO-8601 or an INTEGER epoch only;
refuse floats/numeric junk before the driver, naming the rule and the input.

Legs (the task-list acceptance verbatim):
  * `?from=1.5` -> 422, the offending param named in `detail[].loc`;
  * `?from=1700000000` (integer epoch) -> 200, unchanged;
  * `?from=abc` -> 422, unchanged (already named);
  * real ISO flows unchanged (`?from=2026-10-01T00:00:00` -> 200);
  * the export twin (`/runs/export`) refuses the same way;
  * dashboard `report_date=1.5` -> 422; ISO `report_date` -> 200 (null when no report).
"""
from __future__ import annotations

import pytest


def _names_param(detail: list[dict], param: str) -> bool:
    return any(param in [str(part) for part in error.get("loc", [])] for error in detail)


@pytest.mark.asyncio
async def test_float_epoch_from_is_refused_by_name(api_client, auth_headers):
    resp = await api_client.get("/api/v1/runs?from=1.5", headers=auth_headers)

    assert resp.status_code == 422, (
        f"5-06 not fixed: ?from=1.5 answered {resp.status_code} — a non-integer timestamp was "
        "silently coerced to an epoch instant"
    )
    detail = resp.json()["detail"]
    assert _names_param(detail, "from"), detail
    assert "integer" in resp.text.lower(), f"the refusal must name the rule: {resp.text[:300]}"


@pytest.mark.asyncio
async def test_float_epoch_to_is_refused_by_name(api_client, auth_headers):
    resp = await api_client.get("/api/v1/runs?to=2.5", headers=auth_headers)
    assert resp.status_code == 422, resp.text
    assert _names_param(resp.json()["detail"], "to"), resp.json()["detail"]


@pytest.mark.asyncio
async def test_exponent_numeric_is_refused_by_name(api_client, auth_headers):
    resp = await api_client.get("/api/v1/runs?from=1e3", headers=auth_headers)
    assert resp.status_code == 422, resp.text
    assert _names_param(resp.json()["detail"], "from"), resp.json()["detail"]


@pytest.mark.asyncio
async def test_integer_epoch_and_iso_are_still_accepted(api_client, auth_headers):
    epochs = await api_client.get("/api/v1/runs?from=1700000000", headers=auth_headers)
    assert epochs.status_code == 200, epochs.text

    iso = await api_client.get(
        "/api/v1/runs?from=2026-10-01T00:00:00", headers=auth_headers
    )
    assert iso.status_code == 200, iso.text


@pytest.mark.asyncio
async def test_junk_stays_a_named_422(api_client, auth_headers):
    resp = await api_client.get("/api/v1/runs?from=abc", headers=auth_headers)
    assert resp.status_code == 422, resp.text
    assert _names_param(resp.json()["detail"], "from"), resp.json()["detail"]


@pytest.mark.asyncio
async def test_export_twin_refuses_the_same_float_epoch(api_client, auth_headers):
    refused = await api_client.get("/api/v1/runs/export?from=1.5", headers=auth_headers)
    assert refused.status_code == 422, refused.text
    assert _names_param(refused.json()["detail"], "from"), refused.json()["detail"]

    ok = await api_client.get("/api/v1/runs/export?from=1700000000", headers=auth_headers)
    assert ok.status_code == 200, ok.text


@pytest.mark.asyncio
async def test_dashboard_report_date_refuses_numeric_forms(api_client, auth_headers):
    for raw in ("1.5", "19700", "abc"):
        resp = await api_client.get(
            f"/api/v1/dashboard/reports/daily?report_date={raw}", headers=auth_headers
        )
        assert resp.status_code == 422, f"report_date={raw} answered {resp.status_code}"

    iso = await api_client.get(
        "/api/v1/dashboard/reports/daily?report_date=2026-10-14", headers=auth_headers
    )
    assert iso.status_code == 200, iso.text
