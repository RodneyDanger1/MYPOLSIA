"""V7-T10 — the five channel credential fields (Option A: Settings fields, no adapter edit).

Before: `app/agents/integrations.py` read `linkedin_access_token` / `linkedin_author_urn` /
`meta_page_access_token` / `meta_facebook_page_id` / `meta_instagram_user_id` through
`_setting`, which fell back to `""` because the FIELDS did not exist on `Settings` — with
pydantic-settings `extra="ignore"`, an env var alone could not activate the channel.  The
five are `Settings` fields now (default `""` = unconfigured); the adapters keep reading them
via `_setting` with NO edit, and refuse BY NAME while a channel is unconfigured.

No value is ever printed by this file (fake probe strings only).

Raw RED evidence: raw/v7-red-tests.txt in the lane dir.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings, settings

FIELDS = (
    "linkedin_access_token",
    "linkedin_author_urn",
    "meta_page_access_token",
    "meta_facebook_page_id",
    "meta_instagram_user_id",
)

ROOT = Path(__file__).resolve().parents[3]  # backend/tests/unit/x.py -> repo root
ENV_EXAMPLE = ROOT / ".env.example"


def test_five_channel_credential_fields_exist_with_empty_defaults() -> None:
    for name in FIELDS:
        assert name in Settings.model_fields, f"Settings is missing the {name} field"
        field = Settings.model_fields[name]
        assert field.default == "", f"{name} must default to the empty string (unconfigured)"


def test_every_field_flows_through_the_adapter_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    """mirrors how integrations.py reads it: `_setting` resolves via getattr on `settings`."""
    from app.agents import integrations

    for name in FIELDS:
        probe = f"v7-t10-probe-{name}"
        monkeypatch.setattr(settings, name, probe)
        assert integrations._setting(name) == probe

    for name in FIELDS:
        monkeypatch.setattr(settings, name, "")
        assert integrations._setting(name) == ""


def test_env_example_documents_the_five_keys_as_commented_placeholders() -> None:
    text = ENV_EXAMPLE.read_text(encoding="utf-8")
    for name in FIELDS:
        key = name.upper()
        assert f"# {key}=" in text, f".env.example must document {key} as a commented placeholder"
