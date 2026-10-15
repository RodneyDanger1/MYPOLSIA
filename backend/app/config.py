"""Application settings (pydantic-settings).

Field names are the lower-cased env-var names (pydantic-settings default), which is
what every existing call site and test relies on.
"""
from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore", case_sensitive=False
    )

    # --- app ---
    app_name: str = "Polsia"
    environment: str = "development"
    debug: bool = False
    log_level: str = "INFO"
    cors_origins: list[str] = ["http://localhost", "http://localhost:3000"]

    # --- auth ---
    api_key: str = "dev-key"

    # --- database ---
    database_url: str = "postgresql+asyncpg://polsia:polsia_password@localhost:5432/polsia"
    db_echo: bool = False
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_pre_ping: bool = True
    db_pool_recycle_seconds: int = 1800
    db_statement_timeout_ms: int = 30_000

    # --- redis / celery ---
    redis_url: str = "redis://localhost:6379/0"
    celery_broker_url: str = "redis://localhost:6379/0"
    celery_result_backend: str = "redis://localhost:6379/1"
    activity_channel: str = "polsia:activity"
    celery_task_time_limit: int = 900
    celery_soft_time_limit: int = 840
    celery_max_tasks_per_child: int = 50
    # advertised — not implemented in this repository (R20 item 2): declared for `.env.example`
    # parity, but no code path reads it.  Worker concurrency is a launch-command knob, and the
    # launch command owns it: docker-compose.yml:95 runs
    # `celery -A celery_app.worker worker --loglevel=info --concurrency=4`.  Kept, not deleted —
    # the removal-vs-label decision is recorded in polsia-work/build/R20-minors REQUESTS.
    celery_worker_concurrency: int = 4
    # R23 note (celery_worker_concurrency), read-verified: docker-compose.yml:95 is exactly the
    # command quoted above and the beat service (docker-compose.yml:120) passes no `--concurrency`
    # flag at all; a sibling pointer in celery_app/celery_config.py records the same split (R22).

    # --- daily cycles (morning hour: company-local, task-gated; evening hour: UTC, beat crontab) ---
    morning_cycle_hour: int = 6
    evening_cycle_hour: int = 20
    company_timezone: str = "UTC"

    # --- task queue discipline ---
    task_claim_lease_seconds: int = 900
    task_max_attempts: int = 3
    task_retry_backoff_seconds: int = 20
    activity_retention_days: int = 90
    activity_bulk_flush: int = 200

    # --- activity event bus ---
    # The publish breaker's window: after a failed Redis publish, further events are SKIPPED (and
    # counted -- see events.publish_stats()) for this long instead of each one stalling out the
    # redis-py retry ladder.  Must be > 0; 0 would disable the breaker outright.
    activity_publish_cooldown_seconds: float = 30.0

    @field_validator("activity_publish_cooldown_seconds")
    @classmethod
    def _cooldown_must_be_positive(cls, value: float) -> float:
        """A non-positive cooldown disables the breaker, so every event stalls again."""
        if value <= 0:
            raise ValueError(
                f"activity_publish_cooldown_seconds must be > 0, got {value!r} "
                "(0 would disable the publish breaker)"
            )
        return value

    # --- chroma ---
    # POSIX-absolute because it is the CONTAINER path (backend/Dockerfile ENV); on Windows the
    # consumer (app.core.chroma_client.resolve_persist_dir) resolves it to backend/data/chroma
    # unless CHROMA_PERSIST_DIR is set explicitly — the default itself stays the container path
    # (D7; audit L3-01).
    chroma_persist_dir: str = "/app/data/chroma"
    chroma_collection: str = "polsia_memory"

    # --- claude CLI ---
    # `claude` = PATH lookup (dev machines); the container image pins /usr/local/bin/claude via its
    # ENV (backend/Dockerfile:25) and a CLAUDE_CLI_PATH env var wins over this default at runtime
    # (claude_cli.cli_path()) — one story across code, image and `.env.example` (audit L5-08).
    claude_cli_path: str = "claude"
    claude_cli_mock: bool = False
    claude_cli_mock_response: str = '{"result": "Mock Claude response for testing"}'
    claude_timeout_seconds: int = 300
    claude_max_concurrency: int = 4
    claude_max_argv_chars: int = 24_000
    claude_prompt_via_stdin: bool = False
    claude_max_turns: int = 1
    claude_repair_attempts: int = 1
    claude_resume_sessions: bool = False
    sandbox_mode: bool = True

    # --- agent budgets (cost control) ---
    agent_run_cost_cap_usd: float = 2.0
    agent_daily_cost_cap_usd: float = 25.0
    agent_max_retries: int = 2
    agent_retry_backoff_seconds: float = 1.5

    # --- kill switch (fleet dispatch gate; registry D4 — the switch was settings-dead) ---
    # "" = run; "throttle" = agents with external actions pause (reads continue); "freeze" = all
    # dispatch refused.  policy_firewall.kill_grade_from_env() reads POLSIA_KILL_SWITCH from the
    # environment FIRST, then this field; crew_factory.dispatch_preflight() enforces it.  The
    # POLSIA_-prefixed name is the documented switch; a `.env` line reaches pydantic but NOT
    # os.environ outside docker, so it is accepted here by alias (KILL_SWITCH, the plain
    # field-name convention, stays accepted too).
    kill_switch: str = Field(
        default="",
        validation_alias=AliasChoices("POLSIA_KILL_SWITCH", "KILL_SWITCH"),
    )

    # --- approvals (D1 / T02: owner-authed approval tokens; FROZEN interface) ---
    # The HMAC secret behind `policy_firewall.issue_approval` / `verify_approval`: the approvals
    # API issues a TTL-bounded token for a named action fingerprint, and the firewall verifies it
    # with this secret.  The field name and its `str`/`""` shape are FROZEN by D1's interface —
    # the approvals seat (T02) consumes `settings.approval_secret` directly.
    approval_secret: str = ""

    # --- planner ---
    planner_max_nodes: int = 12
    planner_context_token_budget: int = 12_000

    # --- logging ---
    log_json: bool = False

    # --- stripe ---
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # --- integrations (optional) ---
    twitter_api_key: str = ""
    twitter_api_secret: str = ""
    twitter_access_token: str = ""
    twitter_access_secret: str = ""
    sendgrid_api_key: str = ""
    sendgrid_from_email: str = ""
    github_token: str = ""
    google_ads_developer_token: str = ""
    google_ads_client_id: str = ""
    # advertised — not implemented in this repository; RESERVED — no consumer yet (R23 re-verified:
    # nothing reads `google_ads_client_secret`, while the four sibling GOOGLE_ADS_* settings are
    # name-checked by app/agents/integrations.py:182-187 — and that adapter refuses past the check
    # anyway: "needs an account-scoped API client that this deployment does not ship").  Intended
    # as the OAuth client secret of that not-yet-built client (Google's OAuth flow pairs it with
    # the client id).  KEPT by the 2026-10-13 owner decision (D14) — our google_ads channel
    # exists; the eight sibling dead settings were removed the same day.
    google_ads_client_secret: str = ""
    google_ads_refresh_token: str = ""
    google_ads_customer_id: str = ""
    meta_ads_access_token: str = ""
    meta_ads_account_id: str = ""
    # Channel publishing (V7-T10): app/agents/integrations.py reads all five through
    # `_setting`; blank = unconfigured -> the adapter refuses BY NAME with the missing
    # names listed.  Values are never logged and never returned by the API.
    linkedin_access_token: str = ""  # LinkedIn app token (w_member_social/w_organization_social)
    linkedin_author_urn: str = ""  # posting author: urn:li:person:… or urn:li:organization:…
    meta_page_access_token: str = ""  # Meta page token (pages_manage_posts + IG publishing)
    meta_facebook_page_id: str = ""  # the Facebook page the page token posts as
    meta_instagram_user_id: str = ""  # the page's linked Instagram business account id
    # V8/T4: the TikTok/YouTube adapters read these through `_setting` exactly like the V7 five;
    # blank = unconfigured -> the adapter refuses BY NAME with the missing name listed. Values are
    # never logged and never returned by the API.
    tiktok_access_token: str = ""  # TikTok Content Posting API token (video.publish scope)
    youtube_access_token: str = ""  # YouTube Data API OAuth token (youtube.upload scope)

    # V8-D1 handoff (C5's sending-domain concept): the domain outreach email would send FROM this
    # domain once real sends exist — comment-level, kept for that wave. No code path reads it yet;
    # an honest placeholder, not a wired setting.
    outreach_sending_domain: str = ""

    # --- deepseek (the agent runtime's model provider) ---
    # Read through app.services.secret_store: a key entered in the dashboard is stored
    # at `deepseek_key_file` and WINS over this variable; this variable is the
    # environment fallback (docker-compose `env_file:`, an exported shell, or
    # backend/.env).  Neither value is ever logged or returned by the API — the status
    # routes answer with a masked form only.
    deepseek_api_key: str = ""
    deepseek_key_file: str = "data/deepseek_api_key.json"
    # Runtime client knobs — frozen for the DeepSeek runtime seat (RD8; REMAINDERS-NOTE 2026-10-13:
    # "the app must run on the owner's DeepSeek key", V4.1 Flash via the OpenAI-compatible
    # endpoint).  reasoning_effort: "none" = plain answers (the fast default).
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_model: str = "deepseek-flash"
    deepseek_reasoning_effort: str = "none"
    deepseek_timeout_seconds: int = 300

    # --- phone link (wave V16, E5: the owner's phone, read-only) ---
    # The Phone Link store directory (phone.db/calling.db/contacts.db — the Windows Phone Link
    # app's database).  docker-compose bind-mounts it READ-ONLY at `/phone-link` and sets this to
    # that path; empty means "resolve the measured Windows install path" (the GUID directory
    # changes per install, so the default is a glob — `services/phone_link_service`).
    # Reads only: nothing in this repository ever writes to the store.
    phone_link_db_dir: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
