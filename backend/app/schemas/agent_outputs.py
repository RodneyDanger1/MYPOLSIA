"""The nine agents' output contracts (Pydantic v2) + the `--json-schema` bridge.

Lifted from lane 05's `SCHEMAS.py` (draft-07 bridge, local validator, samples) and aligned with
lane 03's per-agent contracts (`SPEC-AGENTS.md` §3), which are keyed off the DB columns each
agent feeds: `competitors.*`, `social_posts.*`, `prospects.*` / `email_logs.*`, `ad_campaigns.*`,
`revenue_snapshots.*`, `expense_records.*`.

Two things make the schema the contract rather than decoration:

  * the schema is sent to the CLI via `--json-schema` AND re-validated locally with the same
    model, because the CLI's own validation is documented behaviour this host cannot execute
    while the local validator is executable everywhere;
  * a failure to satisfy it is a NAMED code (POLSIA-E110 / E_AGENT_BAD_OUTPUT), never a
    silently-accepted half-object.

`to_cli_schema()` down-converts Pydantic's draft 2020-12 to the draft-07 the CLI documents
($defs -> definitions, $ref rewrite, `$schema`/root `title` removed); `format` is annotation-only
for the CLI, so the formats we need (emails, dates) are enforced with patterns locally.

The failure taxonomy (`FailureCode` + `REMEDIATION` + the `E_*` mapping) is re-exported here so
one import gives a caller the schemas *and* the vocabulary for why an output was rejected.
"""
from __future__ import annotations

from typing import Any, Final, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

# The failure taxonomy lives with the resolution ladder; re-exported for convenience.
from app.agents.structured_output import (  # noqa: F401
    FAILURE_TO_E_CODE,
    REMEDIATION,
    FailureCode,
    e_code_for,
)

AGENT_TYPES = ("orchestrator", "business_planning", "competitor_research", "social_media",
               "email_outreach", "customer_support", "ads_management", "code_generation",
               "finance")
# `deployment` is documented (CLAUDE.md:5, docs/ACCEPTANCE_TESTING.md:110,140-149) but has no
# registry entry, model, table or route; adding it to AGENT_MAP would break
# `GET /agents/status == 9` (contradiction row X-01). TaskSpec therefore accepts it as a task
# type, and its schema exists, while the dispatcher does not ship an agent for it.
ALL_AGENT_TYPES = AGENT_TYPES + ("deployment",)

SCHEMA_VERSION: Final[Literal["1"]] = "1"


# ---------------------------------------------------------------------------
# common envelope
# ---------------------------------------------------------------------------

class AgentEnvelope(BaseModel):
    """Every agent output carries these.

    `status` lets the model REFUSE cleanly (`refused`) or report an honest partial instead of
    emitting a malformed object; the conductor treats `refused` as a policy outcome, never as
    success. `sandbox` / `drafted_not_sent` are set by the runtime (not the model) when a real
    side effect was refused: they make "this did not happen" machine-checkable.
    """

    schema_version: Literal["1"] = SCHEMA_VERSION
    summary: str = Field(min_length=1, max_length=4000)
    status: Literal["ok", "partial", "refused"] = "ok"
    notes: list[str] = Field(default_factory=list, max_length=20)
    sandbox: bool = False
    drafted_not_sent: bool = False


# ---------------------------------------------------------------------------
# 1. orchestrator — the morning plan (+ the evening summary)
# ---------------------------------------------------------------------------

class TaskSpec(BaseModel):
    """One planned task. `depends_on` references other tasks by TITLE (a model cannot know
    content hashes). NOTE: the DAG planner that would resolve titles to node ids is currently
    unwired — `app/orchestration/planner.py` is labelled reference-only (wave U3, auditor
    H-04) — and today the daily cycle dispatches plan entries flat (no DAG resolution)."""

    title: str = Field(min_length=3, max_length=512)
    agent_type: Literal[ALL_AGENT_TYPES]  # type: ignore[valid-type]
    priority: int = Field(ge=1, le=5, default=3)
    estimate_usd: float = Field(ge=0.0, le=25.0, default=0.10)
    estimate_seconds: int = Field(ge=5, le=7200, default=120)
    depends_on: list[str] = Field(default_factory=list, max_length=8)
    side_effect: Literal["read", "internal_write", "external_reversible",
                         "external_irreversible"] = "internal_write"
    description: str = Field(default="", max_length=1000)
    acceptance: str = Field(default="", max_length=500)


class OrchestratorPlan(AgentEnvelope):
    key_focus: str = Field(min_length=1, max_length=300)
    tasks: list[TaskSpec] = Field(default_factory=list, max_length=12)
    insights: list[str] = Field(default_factory=list, max_length=20)   # evening summary consumers
    mode: Literal["morning", "evening"] = "morning"

    @field_validator("tasks")
    @classmethod
    def dep_titles_must_be_unique(cls, v: list[TaskSpec]) -> list[TaskSpec]:
        titles = [t.title for t in v]
        if len(titles) != len(set(titles)):
            raise ValueError("task titles must be unique (dependencies reference titles)")
        return v


# ---------------------------------------------------------------------------
# 2. business planning
# ---------------------------------------------------------------------------

class Recommendation(BaseModel):
    """Lane 05 shape (title/rationale/impact/effort) + lane 03's area/action/expected_impact
    keys, so both consumers find their fields."""

    title: str = Field(min_length=3, max_length=300)
    rationale: str = Field(min_length=10, max_length=2000)
    impact: Literal["low", "medium", "high"] = "medium"
    effort: Literal["low", "medium", "high"] = "medium"
    area: str | None = Field(default=None, max_length=100)
    action: str | None = Field(default=None, max_length=1000)
    expected_impact: str | None = Field(default=None, max_length=1000)


class BusinessPlan(AgentEnvelope):
    recommendations: list[Recommendation] = Field(default_factory=list, max_length=10)
    kpi_updates: dict[str, float] = Field(default_factory=dict)
    focus_areas: list[str] = Field(default_factory=list, max_length=10)


# ---------------------------------------------------------------------------
# 3. competitor research  (-> competitors table)
# ---------------------------------------------------------------------------

class Competitor(BaseModel):
    name: str = Field(min_length=1, max_length=255)
    website: str | None = Field(default=None, max_length=512)
    positioning: str | None = Field(default=None, max_length=2000)
    strengths: list[str] = Field(default_factory=list, max_length=10)
    weaknesses: list[str] = Field(default_factory=list, max_length=10)
    pricing_info: dict[str, Any] | None = None
    source_urls: list[str] = Field(default_factory=list, max_length=5)   # provenance
    sources: list[str] = Field(default_factory=list, max_length=5)       # lane 03 name for the same thing

    @model_validator(mode="after")
    def _mirror_provenance(self) -> Competitor:
        if not self.source_urls and self.sources:
            self.source_urls = list(self.sources)
        if not self.sources and self.source_urls:
            self.sources = list(self.source_urls)
        return self


class CompetitorReport(AgentEnvelope):
    competitors: list[Competitor] = Field(default_factory=list, max_length=10)
    findings: list[str] = Field(default_factory=list, max_length=15)
    search_queries: list[str] = Field(default_factory=list, max_length=10)


# ---------------------------------------------------------------------------
# 4. social media  (-> social_posts / social_engagements)
# ---------------------------------------------------------------------------

class SocialPostDraft(BaseModel):
    platform: Literal[
        "twitter", "linkedin", "facebook", "instagram", "tiktok", "youtube"
    ] = "twitter"
    content: str = Field(min_length=1, max_length=1000)
    hashtags: list[str] = Field(default_factory=list, max_length=5)
    scheduled_for: str | None = None  # ISO-8601 or null = "as soon as approved"
    reply_to_mention: str | None = None
    # U5/P1: instagram's Content Publishing API has no text-only post, so an image post — and ONLY
    # an image post — carries this. None = the text path exactly as before; the agent forwards the
    # field only when it is present. V8/T4: tiktok (Content Posting video init) and youtube
    # (videos.insert) joined the Literal WITH their adapters, and both take their media from this
    # field too — each refuses by name without it (no text-only shape exists at either provider).
    image_url: str | None = Field(default=None, max_length=2000)


class PostedItem(BaseModel):
    """A post that really went out. Unreachable while SANDBOX_MODE=true: the agent records a
    draft and leaves `posted` empty."""

    content: str = Field(min_length=1, max_length=1000)
    platform: Literal[
        "twitter", "linkedin", "facebook", "instagram", "tiktok", "youtube"
    ] = "twitter"
    tweet_id: str | None = None
    post_id: str | None = None


class ReplyItem(BaseModel):
    mention_id: str = Field(min_length=1, max_length=255)
    reply: str = Field(min_length=1, max_length=1000)
    reply_id: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "Provider id of the reply the integration really created; None while sandboxed (a "
            "refused send records no id), and None when the reply was only drafted — \"not sent\" "
            "and \"sent\" must stay distinguishable, so an id is never invented"
        ),
    )


class SocialMediaOutput(AgentEnvelope):
    """One social run's contract.

    `action` is what the run ATTEMPTED, and it is the key the worker's persistence path reads:
    `draft` records drafts only (the sandboxed default), `post` says items in `posted[]` really
    went out (each carrying the provider `tweet_id`/`post_id` that proves it), `reply` says the
    same for `replies[]`, and `noop` is the honest "nothing to do this cycle".  `posted` stays
    empty whenever no provider id was returned — a refused or sandboxed send is a draft, never a
    post — and `replies[].reply_id` follows the same rule.
    """

    action: Literal["draft", "post", "reply", "noop"] = "draft"
    posts: list[SocialPostDraft] = Field(default_factory=list, max_length=5)
    tweets: list[str] = Field(default_factory=list, max_length=10)      # specced key (mock parity)
    drafts: list[str] = Field(default_factory=list, max_length=10)
    posted: list[PostedItem] = Field(default_factory=list, max_length=5)
    replies: list[ReplyItem] = Field(default_factory=list, max_length=10)
    tweet: str | None = Field(default=None, max_length=1000)

    @field_validator("tweets", "drafts", mode="before")
    @classmethod
    def _flatten_to_strings(cls, v: Any) -> Any:
        """Models sometimes hand back `[{"content": "..."}]`; keep the specced key a list of
        strings without inventing content."""
        if not isinstance(v, list):
            return v
        out: list[Any] = []
        for item in v:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict):
                value = item.get("content") or item.get("text") or item.get("tweet")
                if isinstance(value, str):
                    out.append(value)
        return out

    @field_validator("posts")
    @classmethod
    def twitter_length(cls, v: list[SocialPostDraft]) -> list[SocialPostDraft]:
        for p in v:
            if p.platform == "twitter" and len(p.content) > 280:
                raise ValueError("twitter content must be <= 280 chars")
        return v


# ---------------------------------------------------------------------------
# 5. email outreach  (-> prospects / email_campaigns / email_logs)
# ---------------------------------------------------------------------------

class ProspectDraft(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=255)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    company: str | None = Field(default=None, max_length=255)
    title: str | None = Field(default=None, max_length=255)
    source: str = Field(default="agent", max_length=100)


class EmailDraft(BaseModel):
    prospect_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=255)
    subject: str = Field(min_length=1, max_length=512)
    body: str = Field(min_length=1, max_length=8000)
    sequence_step: int = Field(ge=1, le=10, default=1)
    campaign: str | None = Field(default=None, max_length=255)        # lane 03 key
    status: Literal["draft", "sent", "queued", "failed"] = "draft"       # sandbox => "draft"
    sendgrid_id: str | None = Field(default=None, max_length=255)     # None while sandboxed


class FollowUpDraft(BaseModel):
    """ONE planned follow-up touch (R4 OUTREACH-2/3, V9/B3).

    Additive: `EmailOutreachOutput.follow_ups` defaults to `[]`, so every pre-V9 payload
    validates unchanged.  When present, each entry names the prospect, the touch number
    (`step` — a follow-up is never the first touch, so 2..10), how many days AFTER THE SEND
    it is due (`due_in_days` — the chain's cadence: 2-4, then ~4, then ~5 per Appendix A6)
    and the NEW angle it opens with (never "just checking in").  `step`/`due_in_days` are
    the numbers the chain's plumbing reads: `outreach_service.record_sent_emails` persists
    the earliest `due_in_days` per prospect as `email_logs.follow_up_due`, which the
    every-3h follow-up sweep fires.
    """

    prospect_email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=255)
    step: int = Field(default=2, ge=2, le=10)
    due_in_days: int = Field(ge=1, le=90)
    angle: str = Field(min_length=1, max_length=300)


class EmailOutreachOutput(AgentEnvelope):
    action: Literal["find", "draft", "send", "noop"] = "draft"
    prospects: list[ProspectDraft] = Field(default_factory=list, max_length=50)
    emails: list[EmailDraft] = Field(default_factory=list, max_length=25)
    follow_ups_scheduled: int = Field(default=0, ge=0, le=1000)
    # V9/B3: the chain, additive — each entry is one planned follow-up touch (FollowUpDraft).
    # Default `[]`: an output without the key keeps its exact old meaning, and the worker's
    # persist path writes `follow_up_due` from these entries only when they are stated.
    follow_ups: list[FollowUpDraft] = Field(default_factory=list, max_length=25)


# ---------------------------------------------------------------------------
# 6. customer support
# ---------------------------------------------------------------------------

class SupportReply(BaseModel):
    model_config = {"populate_by_name": True}

    # IMAP UID / thread id
    message_ref: str = Field(
        min_length=1,
        max_length=255,
        description="provider message id from the inbox — absent means the reply cannot be acted on",
    )
    intent: Literal["question", "complaint", "refund_request", "bug", "praise", "other"]
    sentiment: Literal["positive", "neutral", "negative"]
    reply_body: str = Field(min_length=1, max_length=8000)
    needs_human: bool = False
    proposed_action: Literal["none", "send_reply", "refund", "escalate"] = "send_reply"
    # lane 03's column-aligned keys
    message_id: str | None = Field(default=None, max_length=255)
    from_address: str | None = Field(default=None, alias="from", max_length=320)
    subject: str | None = Field(default=None, max_length=512)
    body: str | None = Field(default=None, max_length=8000)
    status: Literal["draft", "sent", "failed"] = "draft"


class CustomerSupportOutput(AgentEnvelope):
    replies: list[SupportReply] = Field(default_factory=list, max_length=50)


# ---------------------------------------------------------------------------
# 7. ads management  (-> ad_campaigns / ad_metrics)
# ---------------------------------------------------------------------------

class AdsRecommendation(BaseModel):
    platform: Literal["google_ads", "meta_ads"]
    campaign_ref: str = Field(
        min_length=1,
        max_length=255,
        description="provider campaign id from the ad platform — absent means the recommendation cannot be applied",
    )
    action: Literal["pause", "resume", "scale_up", "scale_down", "new_campaign", "noop"]
    rationale: str = Field(min_length=10, max_length=2000)
    budget_change_usd: float = Field(ge=-10000.0, le=10000.0, default=0.0)
    campaign_id: int | None = None                     # lane 03 key
    applied: bool = False                                 # real mutation only when unsandboxed


class AdCampaignRow(BaseModel):
    platform: Literal["google_ads", "meta_ads"]
    external_id: str | None = Field(default=None, max_length=255)
    name: str = Field(min_length=1, max_length=255)
    status: str = Field(default="active", max_length=50)
    daily_budget_usd: float = Field(default=0.0, ge=0.0)
    total_spent_usd: float = Field(default=0.0, ge=0.0)


class AdMetricRow(BaseModel):
    campaign_external_id: str = Field(min_length=1, max_length=255)
    date: str = Field(min_length=8, max_length=32)
    impressions: int = Field(default=0, ge=0)
    clicks: int = Field(default=0, ge=0)
    conversions: int = Field(default=0, ge=0)
    spend_usd: float = Field(default=0.0, ge=0.0)
    # D17/R33: `ad_metrics.revenue_usd` is the real money column both engines hold (`Numeric(14,4)
    # DEFAULT '0' NOT NULL`, alembic 0002:203-207 / 0003:71) and the input `roas` is GENERATED
    # from (`models/ads.py:75-87`).  The persist path already maps it
    # (`ads_service.record_ads_run`, the `("spend_usd", "revenue_usd")` money loop), but the field
    # itself was missing HERE — and this model is the contract: the run payload is validated with
    # it and re-dumped before the worker sees it, so an undeclared key was dropped at validation
    # and the column could only ever hold its default.  `ge=0.0` mirrors `ck_ad_metrics_nonneg`
    # (PostgreSQL-only; the writer asserts it for both engines).  Not-reported stays absent from
    # the row — the column default is the writer's, never a fabricated measurement.
    revenue_usd: float = Field(default=0.0, ge=0.0)
    ctr: float = Field(default=0.0, ge=0.0)
    cpc: float = Field(default=0.0, ge=0.0)
    roas: float = Field(default=0.0, ge=0.0)


class AdsOutput(AgentEnvelope):
    recommendations: list[AdsRecommendation] = Field(default_factory=list, max_length=10)
    metrics_snapshot: dict[str, float] = Field(default_factory=dict)
    campaigns: list[AdCampaignRow] = Field(default_factory=list, max_length=50)
    metrics: list[AdMetricRow] = Field(default_factory=list, max_length=200)


# ---------------------------------------------------------------------------
# 8. code generation  (-> GitHub PRs via allowlisted git only)
# ---------------------------------------------------------------------------

class CodeChange(BaseModel):
    repo: str = Field(min_length=3, max_length=512)          # owner/name
    branch: str = Field(min_length=1, max_length=255)
    pr_title: str = Field(min_length=3, max_length=512)
    pr_body: str = Field(min_length=1, max_length=20000)
    files_changed: list[str] = Field(default_factory=list, max_length=100)
    risk: Literal["low", "medium", "high"] = "medium"
    tests_run: list[str] = Field(default_factory=list, max_length=20)


class CommitRow(BaseModel):
    sha: str | None = Field(default=None, max_length=64)
    message: str = Field(min_length=1, max_length=2000)


class CodeGenOutput(AgentEnvelope):
    action: Literal["plan", "implement", "open_pr", "noop"] = "plan"
    changes: list[CodeChange] = Field(default_factory=list, max_length=3)
    branch: str | None = Field(default=None, max_length=255)
    commits: list[CommitRow] = Field(default_factory=list, max_length=20)
    pr_url: str | None = Field(default=None, max_length=512)     # None while sandboxed
    pr_number: int | None = None
    files_changed: int = Field(default=0, ge=0)
    diff_summary: str = Field(default="", max_length=4000)


# ---------------------------------------------------------------------------
# 9. finance  (-> revenue_snapshots / expense_records / stripe_events)
# ---------------------------------------------------------------------------

class ExpenseDraft(BaseModel):
    category: str = Field(min_length=1, max_length=100)
    vendor: str = Field(min_length=1, max_length=255)
    amount_cents: int = Field(ge=0)
    currency: str = Field(default="usd", max_length=10)
    date: str | None = None                                # ISO date
    description: str | None = Field(default=None, max_length=2000)
    external_ref: str | None = Field(default=None, max_length=255)


class RevenueSnapshotDraft(BaseModel):
    """The `revenue_snapshots` row for today.  Every money field is nullable on purpose: a field
    the model did not measure validates as `None`, and the finance agent OMITS it from the row
    instead of zero-filling it — "omitted" and "measured 0" are different states and stay so."""

    snapshot_date: str = Field(min_length=8, max_length=32)
    mrr_cents: int | None = Field(default=None, ge=0)
    arr_cents: int | None = Field(default=None, ge=0)
    active_subscribers: int | None = Field(default=None, ge=0)
    churned_today: int | None = Field(default=None, ge=0)
    new_today: int | None = Field(default=None, ge=0)
    total_revenue_month_cents: int | None = Field(default=None, ge=0)
    stripe_balance_cents: int | None = Field(default=None, ge=0)


class FailedPayment(BaseModel):
    event_id: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "Stripe event id — PROVENANCE for the webhook delivery only, never the refund "
            "target: Stripe refunds are created against a PaymentIntent (pi_…), not an event"
        ),
    )
    payment_intent_id: str | None = Field(
        default=None,
        max_length=255,
        description=(
            "Stripe PaymentIntent id (pi_…) — the identifier a refund call needs; absent (or "
            "not pi_-prefixed) means no refund can be requested for this charge, and the "
            "refund adapter refuses it by name"
        ),
    )
    customer_id: str | None = Field(default=None, max_length=255)
    amount_cents: int = Field(default=0, ge=0)
    currency: str = Field(default="usd", max_length=10)
    #: The model's PROPOSAL that this charge should be refunded; the policy firewall decides
    #: whether money actually moves (sandbox refuses it, and outside sandbox it needs approval).
    refund_requested: bool = False


class FinanceOutput(AgentEnvelope):
    """The finance agent's contract.  The money fields are nullable so that a field the model did
    not measure survives validation as `None` — the agent then omits it from the snapshot row
    instead of writing a fabricated 0.  `snapshot_written` in this payload is always False: the
    worker sets it from what `persist_finance_run` actually wrote."""

    mrr_cents: int | None = Field(default=None, ge=0)
    arr_cents: int | None = Field(default=None, ge=0)
    active_subscribers: int | None = Field(default=None, ge=0)
    churned_today: int | None = Field(default=None, ge=0)
    new_today: int | None = Field(default=None, ge=0)
    stripe_balance_cents: int | None = Field(default=None, ge=0)
    total_revenue_month_cents: int | None = Field(default=None, ge=0)
    snapshot_written: bool = False
    snapshot: RevenueSnapshotDraft | None = None
    expenses: list[ExpenseDraft] = Field(default_factory=list, max_length=50)
    failed_payments: list[FailedPayment] = Field(default_factory=list, max_length=100)
    alerts: list[str] = Field(default_factory=list, max_length=10)
    refunds_performed: int = Field(default=0, ge=0)     # >0 only when the firewall allowed it

    @field_validator("arr_cents")
    @classmethod
    def arr_matches_mrr(cls, v: int | None, info) -> int | None:
        # tolerance: 12x MRR within 2% (partial-month rounding is normal)
        if v is None:
            return v      # not measured: there is no number to check against MRR
        mrr = info.data.get("mrr_cents")
        if mrr and abs(v - 12 * mrr) > max(200, int(0.02 * 12 * mrr)):
            raise ValueError("arr_cents must be approximately 12 * mrr_cents")
        return v


# ---------------------------------------------------------------------------
# 10. deployment — documented, deliberately NOT in the registry (see ALL_AGENT_TYPES note)
# ---------------------------------------------------------------------------

class DeploymentDraft(BaseModel):
    service_ref: str = Field(min_length=1, max_length=255)
    action: Literal["deploy", "rollback", "noop"] = "noop"
    commit_ref: str | None = Field(default=None, max_length=255)
    status: Literal["triggered", "completed", "failed", "noop"] = "noop"
    health_check_passed: bool | None = None
    deploy_id: str | None = Field(default=None, max_length=255)


class DeploymentOutput(AgentEnvelope):
    deployments: list[DeploymentDraft] = Field(default_factory=list, max_length=5)


# ---------------------------------------------------------------------------
# registry + CLI-schema bridge
# ---------------------------------------------------------------------------

AGENT_SCHEMAS: dict[str, type[BaseModel]] = {
    "orchestrator": OrchestratorPlan,
    "business_planning": BusinessPlan,
    "competitor_research": CompetitorReport,
    "social_media": SocialMediaOutput,
    "email_outreach": EmailOutreachOutput,
    "customer_support": CustomerSupportOutput,
    "ads_management": AdsOutput,
    "code_generation": CodeGenOutput,
    "finance": FinanceOutput,
}

#: The schema for a documented agent the registry does not ship (see the ALL_AGENT_TYPES note).
UNREGISTERED_SCHEMAS: dict[str, type[BaseModel]] = {"deployment": DeploymentOutput}


def schema_for(agent_type: str) -> type[BaseModel]:
    model = AGENT_SCHEMAS.get(agent_type) or UNREGISTERED_SCHEMAS.get(agent_type)
    if model is None:
        from app.core.errors import E_AGENT_UNKNOWN, ERROR_HINTS, PolsiaError

        raise PolsiaError(
            E_AGENT_UNKNOWN,
            f"no output schema for agent_type {agent_type!r}",
            how_to_fix=ERROR_HINTS[E_AGENT_UNKNOWN],
            context={"known": sorted(AGENT_SCHEMAS)},
        )
    return model


def to_cli_schema(model: type[BaseModel]) -> dict:
    """JSON Schema the CLI accepts on --json-schema (draft-07 compatible).

    Pydantic v2 emits draft 2020-12 by default: we remove `$schema`, drop the root `title`, and
    rewrite `$defs`/`#/$defs/...` to draft-07's `definitions`/`#/definitions/...`. `format`
    keywords stay (the CLI treats them as annotations).
    """
    schema = model.model_json_schema()
    schema.pop("$schema", None)
    schema.pop("title", None)
    defs = schema.pop("$defs", None)
    if defs:
        schema["definitions"] = defs

        def _rewrite(obj: Any) -> Any:
            if isinstance(obj, dict):
                return {k: (_rewrite(v).replace("#/$defs/", "#/definitions/")
                            if k == "$ref" and isinstance(v, str) else _rewrite(v))
                        for k, v in obj.items()}
            if isinstance(obj, list):
                return [_rewrite(x) for x in obj]
            return obj

        schema = _rewrite(schema)
    return schema


def cli_schema_for(agent_type: str) -> dict:
    return to_cli_schema(schema_for(agent_type))


def required_top_level_fields(agent_type: str) -> tuple[str, ...]:
    return tuple(to_cli_schema(schema_for(agent_type)).get("required") or ())


def validate_agent_output(agent_type: str, payload: dict) -> tuple[bool, BaseModel | list[str]]:
    """Local validation. Returns (True, model) or (False, [error strings]).

    L1-06: the model validated here is the SAME one the CLI bridge sends — `schema_for`, incl.
    the documented-but-unregistered `deployment` schema.  The module docstring's whole point is
    that "the schema is sent to the CLI via --json-schema AND re-validated locally with the same
    model"; before this, `cli_schema_for('deployment')` emitted a schema whose own validator
    answered "unknown agent_type", so the bridge and the validator could disagree about which
    agents exist.  An agent_type with no schema at all still answers the same unknown-agent
    string this function has always returned — `(False, ...)`, never a raise.
    """
    from app.core.errors import PolsiaError

    try:
        model = schema_for(agent_type)
    except PolsiaError:
        return False, [f"unknown agent_type '{agent_type}'"]
    try:
        return True, model.model_validate(payload)
    except ValidationError as exc:
        return False, [f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()]


def placeholder_payload(agent_type: str, summary: str, note: str) -> dict:
    """The schema's own empty shape, with an explicit note and `status='partial'`.

    Used ONLY by the sanctioned `CLAUDE_CLI_MOCK` path when the mock response is not
    schema-shaped (the repo's default mock response is prose — CLAUDE.md:102). It carries no
    invented content: every list is empty, every count is zero, and `notes` says exactly why, so
    a mocked run can never be mistaken for work that happened. Never reachable with a real CLI:
    the real path raises `E_AGENT_BAD_OUTPUT` instead.
    """
    model = schema_for(agent_type)
    payload: dict[str, Any] = {}
    for name, fld in model.model_fields.items():
        if name == "summary":
            payload[name] = (summary or "no summary")[:4000]
        elif name == "notes":
            payload[name] = [note][:20]
        elif name == "status":
            payload[name] = "partial"
        elif name == "schema_version":
            payload[name] = SCHEMA_VERSION
        elif name == "mode":
            payload[name] = "morning"
        else:
            annotation = fld.annotation
            default = fld.get_default(call_default_factory=True)
            if default is not None and default is not ...:
                payload[name] = default
            elif "list" in str(annotation).lower():
                payload[name] = []
            elif "dict" in str(annotation).lower():
                payload[name] = {}
            elif "int" in str(annotation).lower():
                payload[name] = 0
            elif "bool" in str(annotation).lower():
                payload[name] = False
            elif "float" in str(annotation).lower():
                payload[name] = 0.0
            else:
                payload[name] = ""
    return payload


__all__ = [
    "AGENT_SCHEMAS",
    "AGENT_TYPES",
    "ALL_AGENT_TYPES",
    "FAILURE_TO_E_CODE",
    "REMEDIATION",
    "SCHEMA_VERSION",
    "UNREGISTERED_SCHEMAS",
    "AdCampaignRow",
    "AdMetricRow",
    "AdsOutput",
    "AdsRecommendation",
    "AgentEnvelope",
    "BusinessPlan",
    "CodeChange",
    "CodeGenOutput",
    "CommitRow",
    "Competitor",
    "CompetitorReport",
    "CustomerSupportOutput",
    "DeploymentOutput",
    "EmailDraft",
    "EmailOutreachOutput",
    "ExpenseDraft",
    "FailureCode",
    "FailedPayment",
    "FinanceOutput",
    "FollowUpDraft",
    "OrchestratorPlan",
    "PostedItem",
    "ProspectDraft",
    "Recommendation",
    "ReplyItem",
    "RevenueSnapshotDraft",
    "SocialMediaOutput",
    "SocialPostDraft",
    "SupportReply",
    "TaskSpec",
    "cli_schema_for",
    "e_code_for",
    "placeholder_payload",
    "required_top_level_fields",
    "schema_for",
    "to_cli_schema",
    "validate_agent_output",
]
