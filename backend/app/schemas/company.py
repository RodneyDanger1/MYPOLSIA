from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import ORMModel


class CompanyConfigResponse(ORMModel):
    """Wire shape of the singleton company row.

    `timezone` / `daily_cycle_hour` are NULLABLE in the frozen schema (`models/company.py`;
    `0001_initial_schema.py`'s `CREATE TABLE company_config` carries `server_default`s but no
    `NOT NULL`), and `company_service.get_daily_cycle_hour` documents the state explicitly:
    a stored NULL means "no value", NOT 6.  Declaring them required turned (a) a legal
    `PUT {"timezone": null}` and (b) ONE schema-legal NULL row into a bare, codeless 500 on
    `model_validate` — permanently, for BOTH config routes (L1-10).  `None` here means "not
    configured" and is served as null; the response must never fabricate `"UTC"`/`6` over it,
    because `get_full_context` already falls back (`company.timezone or "UTC"`) and inventing a
    value would hide the state the operator is trying to fix.
    """

    id: int
    name: str
    mission: str | None = None
    vision: str | None = None
    description: str | None = None
    target_market: str | None = None
    value_prop: str | None = None
    pricing_model: dict[str, Any] | None = None
    goals: dict[str, Any] | None = None
    kpis: dict[str, Any] | None = None
    website_url: str | None = None
    github_repo: str | None = None
    product_type: str | None = None
    industry: str | None = None
    timezone: str | None = None
    daily_cycle_hour: int | None = None
    # U5 (P3/P5 frozen names): the night-task surface + owner-notification settings.  The bool
    # columns are NOT NULL at rest (server default FALSE), so the response serves a real value for
    # every row; `night_task_hour`/`notification_email` are nullable — None means "unset".
    night_task_enabled: bool = False
    night_task_limit: int = 3
    night_task_hour: int | None = None
    notification_email: str | None = None
    notify_on_completed: bool = False
    notify_on_failed: bool = False
    notify_on_awaiting_approval: bool = False
    notify_on_run_requeued: bool = False
    created_at: datetime | None = None
    updated_at: datetime | None = None


class CompanyConfigUpdate(BaseModel):
    """The write side of the singleton company row.

    Validation lives HERE because this model is the only writer of the row (`api/v1/config.py`'s
    `update_config` copies every provided field onto it) and the runtime refuses a malformed stored
    value by name later (`company_tz` → `E_CONFIG`; `_resolve_morning_hour` → `E_CONFIG`).  Without
    it, `{"timezone": "Not/AZone"}` and `{"daily_cycle_hour": 99}` answered 200, "Settings saved
    successfully", and then killed every dispatch / the morning cycle (L1-01).  FastAPI now answers
    **422 naming the field**, before the row is touched; the runtime `E_CONFIG` refusals stay as the
    belt for values that reached the row by other means (env, raw SQL, pre-fix writes).
    """

    name: str | None = None
    mission: str | None = None
    vision: str | None = None
    description: str | None = None
    target_market: str | None = None
    value_prop: str | None = None
    pricing_model: dict[str, Any] | None = None
    goals: dict[str, Any] | None = None
    kpis: dict[str, Any] | None = None
    website_url: str | None = None
    github_repo: str | None = None
    product_type: str | None = None
    industry: str | None = None
    timezone: str | None = None
    # Both fields stay optional-and-nullable: a bare `int = Field(ge=0, le=23)` would 422 an absent
    # key, and the settings page PUTs the whole object.
    daily_cycle_hour: int | None = Field(default=None, ge=0, le=23)
    # U5 (P3): the night-task surface.  `night_task_limit` carries the 1..5 bound the plan freezes;
    # `night_task_hour` the same 0..23 bound as the morning hour (company-local, NULL = unset →
    # the tick resolves the documented 23:00 default).  Present-but-null clears the value.
    night_task_enabled: bool | None = None
    night_task_limit: int | None = Field(default=None, ge=1, le=5)
    night_task_hour: int | None = Field(default=None, ge=0, le=23)
    # U5 (P5's frozen name): the owner-notification address + the four terminal-outcome toggles.
    # No format policing here — a malformed address is refused BY THE PROVIDER (recorded, never
    # raising into a run); blank is normalised to None so "cleared in Settings" stores the
    # documented "not configured" state (same rule as the timezone validator below).
    notification_email: str | None = None
    notify_on_completed: bool | None = None
    notify_on_failed: bool | None = None
    notify_on_awaiting_approval: bool | None = None
    notify_on_run_requeued: bool | None = None

    @field_validator("timezone")
    @classmethod
    def _known_zone(cls, value: str | None) -> str | None:
        """`None`/blank = "not configured"; anything else must be a real IANA zone.

        Blank is normalised to `None` so the column matches the documented "no value" state
        (`company_service.get_daily_cycle_hour`: a stored NULL means "no value", NOT 6) — the
        settings page sends `""` when the operator clears the box (it PUTs the whole object).
        """
        if value is None or not value.strip():
            return None
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"{value!r} is not a known IANA timezone") from exc
        return value

    @field_validator("notification_email")
    @classmethod
    def _blank_email_is_unset(cls, value: str | None) -> str | None:
        """`None`/blank = "not configured" — the same rule as `timezone` above (U5/P5).

        The settings page PUTs the whole object and sends `""` when the operator clears the box;
        storing `""` would make the column disagree with its own documented NULL-as-unset state
        (and P5's consumer reads falsy as "unconfigured" anyway — one representation, not two).
        """
        if value is None or not value.strip():
            return None
        return value.strip()
