from __future__ import annotations

import math
from datetime import datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import ORMModel

#: The FREE-FORM JSON columns of `company_config`.  They carry caller/model data (an idea's
#: price band, goals, KPIs, checklist state) and their inner numbers are NOT typed by pydantic,
#: so the finiteness rule S4-F1 established for the typed money fields must be applied by hand.
JSON_FIELDS: tuple[str, ...] = (
    "pricing_model",
    "goals",
    "kpis",
    "confirmed_idea",
    "checklist_state",
)


def non_finite_paths(value: Any, prefix: str = "") -> list[str]:
    """Dotted/indexed path of every non-finite number inside a JSON-ish value ([] when clean).

    `NaN` / `±Infinity` are the S4-F1 class (wave-v13 s4-errors): JSON's non-standard tokens are
    accepted by Python's `json`, so a NaN can reach a JSON column and persist.  Every consumer then
    inherits a non-finite number — and the WIRE laundered it into `null` (pydantic serializes
    non-finite floats as null), which reads as "unmeasured" without a reason.  Shared by the
    write-side model validator and the read-side route guard so both name the SAME path.
    """
    if isinstance(value, float):
        return [prefix] if not math.isfinite(value) else []
    if isinstance(value, Decimal):
        return [prefix] if not value.is_finite() else []
    if isinstance(value, dict):
        paths: list[str] = []
        for key, item in value.items():
            paths.extend(non_finite_paths(item, f"{prefix}.{key}" if prefix else str(key)))
        return paths
    if isinstance(value, (list, tuple)):
        paths = []
        for index, item in enumerate(value):
            paths.extend(non_finite_paths(item, f"{prefix}[{index}]"))
        return paths
    return []


def first_non_finite_field(row: Any) -> tuple[str, list[str]] | None:
    """`(column, paths)` for the first JSON column of a company row carrying a non-finite number."""
    for name in JSON_FIELDS:
        paths = non_finite_paths(getattr(row, name, None), name)
        if paths:
            return name, paths
    return None


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
    # V8/0008 (C2): owner profile + sender identity + readiness.  The NOT NULL columns carry
    # materialised server defaults ('' / false / [] / {}) so every row serves a real value;
    # `owner_hours_per_week` / `owner_budget_usd` / `confirmed_idea` are nullable — None means
    # "no value" and is served as null, never fabricated.
    cycle_report_email: bool = False
    sender_legal_name: str = ""
    sender_postal_address: str = ""
    owner_skills: str = ""
    owner_hours_per_week: int | None = None
    owner_budget_usd: float | None = None
    owner_goal: str = ""
    risk_tolerance: str = ""
    channels_ready: list[str] = []
    market_locale: str = ""
    confirmed_idea: dict[str, Any] | None = None
    checklist_state: dict[str, Any] = {}
    dispatch_paused: bool = False
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

    `extra="forbid"` (S4-F4 / decision #9, wave V15): pydantic's default IGNORED an unknown key,
    so `{"namee": "x"}` answered 200 "saved" while changing nothing — the caller believed the
    company was renamed.  Strict now: the 422 names the offending key (`extra_forbidden` on its
    `loc`), and the whole PUT is all-or-nothing.  Every real writer sends only known keys (the
    settings page PUTs a subset of THIS model's fields), so the flip refuses typos, not clients.
    """

    model_config = ConfigDict(extra="forbid")

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
    # V8/0008: the V8 fields.  The NOT NULL columns at rest carry canonical "unset" values
    # ('' / false / [] / {}), so a JSON `null` for them is NORMALISED to that canonical value by
    # the validators below instead of flushing a NULL into a NOT NULL column (the same
    # single-representation rule `notification_email`/`timezone` already keep).  `owner_budget_usd`
    # is accepted as a wire number and stored as an exact `Decimal` (money is exact at rest,
    # A-T17).  Present-but-null on the nullable fields clears them.
    cycle_report_email: bool | None = None
    sender_legal_name: str | None = None
    sender_postal_address: str | None = None
    owner_skills: str | None = None
    owner_hours_per_week: int | None = None
    owner_budget_usd: Decimal | None = None
    owner_goal: str | None = None
    risk_tolerance: str | None = None
    channels_ready: list[str] | None = None
    market_locale: str | None = None
    confirmed_idea: dict[str, Any] | None = None
    checklist_state: dict[str, Any] | None = None
    dispatch_paused: bool | None = None

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

    @field_validator(
        "sender_legal_name",
        "sender_postal_address",
        "owner_skills",
        "owner_goal",
        "risk_tolerance",
        "market_locale",
        mode="before",
    )
    @classmethod
    def _none_is_the_empty_string(cls, value: Any) -> Any:
        """`null` for a NOT NULL text field stores the canonical '' ("not configured")."""
        return "" if value is None else value

    @field_validator("cycle_report_email", "dispatch_paused", mode="before")
    @classmethod
    def _none_is_false(cls, value: Any) -> Any:
        """`null` for a NOT NULL boolean stores FALSE (the column's own default — one
        representation of "off", never a NULL a later reader has to special-case)."""
        return False if value is None else value

    @field_validator("channels_ready", mode="before")
    @classmethod
    def _none_is_the_empty_list(cls, value: Any) -> Any:
        """`null` for the NOT NULL compatibility list stores [] (the column's server default)."""
        return [] if value is None else value

    @field_validator("checklist_state", mode="before")
    @classmethod
    def _none_is_the_empty_object(cls, value: Any) -> Any:
        """`null` for the NOT NULL checklist stores {} (the column's server default)."""
        return {} if value is None else value

    @field_validator("owner_budget_usd", mode="before")
    @classmethod
    def _budget_wire_number_is_exact(cls, value: Any) -> Any:
        """Money stays exact at rest (A-T17): a wire float is stored via `Decimal(str(v))`."""
        if value is None or isinstance(value, Decimal):
            return value
        return Decimal(str(value))

    @model_validator(mode="after")
    def _no_non_finite_numbers_in_json_fields(self) -> CompanyConfigUpdate:
        """Refuse NaN/±Infinity anywhere inside the FREE-FORM JSON columns (L3-02, V17).

        WHY a model validator: `pricing_model`/`goals`/`kpis`/`confirmed_idea`/`checklist_state`
        are `dict[str, Any]`, so their inner numbers never pass through a typed field — pydantic's
        float checks cannot see them.  Before this check a `PUT /config` whose `confirmed_idea`
        carried `[NaN, 100]` answered 200 and STORED the non-standard token; reads then served
        `null` in its place (pydantic serializes non-finite as null), a fabricated "unmeasured"
        with no reason, and every consumer inherited a non-finite number.  The S4-F1 rule — a
        non-finite number is not a measurement — is enforced HERE for the same reason it exists on
        `invoices.py`'s money fields: the write boundary is cheaper than any repair.
        """
        for field in JSON_FIELDS:
            paths = non_finite_paths(getattr(self, field, None), field)
            if paths:
                raise ValueError(
                    f"{', '.join(paths)} carries NaN/Infinity — not valid JSON and not a "
                    "measurement; send finite numbers (or null for unmeasured)"
                )
        return self
