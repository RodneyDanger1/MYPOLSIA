"""Wire models for the `/ops` read surfaces (V20/L1 — closes V19/L2-06 for `api/v1/ops.py`).

WHY: these five routes declared `-> dict[str, Any]`, so OpenAPI published
`additionalProperties: true` and the shape existed only in docstrings (V19/L2-06).  Nothing about
the served bytes changes: every field below is declared in the SAME order the dict was built, so
the typed path serializes identically (`tests/unit/test_v20_api_typing.py` proves it byte for
byte), and every null stays null — a capability/channel list is empty only when the firewall
really carries none (`capability_manifest.agent_manifest_entry`'s fail-closed shape).

PROVENANCE of each field:
  * `EventPublishStatsResponse` <- `app.core.events.publish_stats()` (module counters, per process).
  * `SandboxStateResponse` <- `settings.sandbox_mode`.
  * `AgentQualityRow` <- `ops._agent_quality_rows` over `agent_runs`; `success_rate` and
    `cost_per_completed_task` are null when their denominator/measurement is absent — never 0.
  * `CapabilityManifestResponse` <- `capability_manifest.capability_manifest()`, derived at request
    time from `policy_firewall.AGENT_CAPABILITIES` + `tool_registry` (H-C1, frozen).
"""
from __future__ import annotations

from pydantic import BaseModel

from app.schemas.common import UtcDatetime

__all__ = [
    "AgentCapabilityEntry",
    "AgentQualityResponse",
    "AgentQualityRow",
    "CapabilityManifestResponse",
    "CapabilityTool",
    "EventPublishStatsResponse",
    "SandboxStateResponse",
]


class EventPublishStatsResponse(BaseModel):
    """`GET/POST /ops/events[/stats|/reset]` — the activity-publish breaker's counters."""

    failures: int
    skipped_while_open: int
    breaker_open: bool
    #: The configured cooldown (`settings.activity_publish_cooldown_seconds`, a float).
    cooldown_seconds: float
    last_error: str | None


class SandboxStateResponse(BaseModel):
    """`GET /ops/sandbox` — the read-only `SANDBOX_MODE` flag the UI banner renders."""

    sandbox_mode: bool


class AgentQualityRow(BaseModel):
    """One registry agent's 7-day ledger window (null-honest, deterministic)."""

    agent_type: str
    runs_7d: int
    success_rate: float | None
    #: An ISO-8601 UTC instant, already formatted by `iso_utc` in the service read.
    last_failure_at: UtcDatetime | None
    cost_per_completed_task: float | None


class AgentQualityResponse(BaseModel):
    agents: list[AgentQualityRow]


class CapabilityTool(BaseModel):
    """One registry tool: `name` + the registry's OWN description (never re-written)."""

    name: str
    description: str


class AgentCapabilityEntry(BaseModel):
    """One agent's enforced capability surface (H-C1).

    An agent with no firewall entry answers EMPTY lists — the fail-closed shape of
    `policy_firewall.decide()` — never the dataclass defaults, which would fabricate `read`.
    """

    agent_type: str
    tools: list[CapabilityTool]
    action_classes: list[str]
    channels: list[str]


class CapabilityManifestResponse(BaseModel):
    """`GET /ops/capabilities` — the H-C1 payload, derived at request time."""

    agents: list[AgentCapabilityEntry]
    registry_tools: list[CapabilityTool]
