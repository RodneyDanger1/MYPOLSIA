"""Test GET/PUT /api/v1/config endpoints, and the Settings fields they rely on."""
import pytest
from pydantic import ValidationError

from app.config import Settings
from app.models.company import CompanyConfig


@pytest.mark.asyncio
async def test_get_config_not_found(api_client, auth_headers):
    resp = await api_client.get("/api/v1/config", headers=auth_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_config_returns_company(api_client, auth_headers, async_db_session):
    company = CompanyConfig(name="Test Corp", timezone="UTC", daily_cycle_hour=6)
    async_db_session.add(company)
    await async_db_session.commit()

    resp = await api_client.get("/api/v1/config", headers=auth_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "Test Corp"


@pytest.mark.asyncio
async def test_update_config(api_client, auth_headers, async_db_session):
    company = CompanyConfig(name="Old Name", timezone="UTC", daily_cycle_hour=6)
    async_db_session.add(company)
    await async_db_session.commit()

    resp = await api_client.put(
        "/api/v1/config",
        headers=auth_headers,
        json={"name": "New Name", "mission": "Updated mission"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["name"] == "New Name"
    assert data["mission"] == "Updated mission"


@pytest.mark.asyncio
async def test_requires_api_key(api_client):
    resp = await api_client.get("/api/v1/config")
    assert resp.status_code == 401


# --- Settings: the activity publish breaker's cooldown (what `events` reads) ---


def test_activity_publish_cooldown_defaults_to_30_seconds():
    """Default 30.0s window (config.py -> events._PUBLISH_COOLDOWN_S)."""
    assert Settings().activity_publish_cooldown_seconds == 30.0


def test_activity_publish_cooldown_rejects_non_positive_values():
    """0 or negative would silently disable the publish breaker, so Settings refuses it."""
    for bad in (0, -1, 0.0, -0.5):
        with pytest.raises(ValidationError) as excinfo:
            Settings(activity_publish_cooldown_seconds=bad)
        assert "activity_publish_cooldown_seconds must be > 0" in str(excinfo.value)
