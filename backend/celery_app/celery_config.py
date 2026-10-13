"""Celery configuration — and the two registries the upload never loaded.

Both registries were broken in the uploaded repo (measured, lane 03 D-13/D-14):

* **Task registry.** `worker.py` called `autodiscover_tasks(["celery_app.tasks"])`,
  which searches for a module named `celery_app.tasks.tasks` (the default
  `related_name="tasks"`); `celery_app/tasks/__init__.py` is empty, so the worker
  registered **0 of its 7 tasks** and every `delay()`/beat dispatch arrived as an
  unregistered task.  Fixed by `imports`/`include` below.
* **Beat registry.** The app never imported `celery_app.beat_schedule`, so
  `app.conf.beat_schedule == {}` — **declared 5, effective 0**.  No periodic job
  (morning cycle, evening cycle, any sweep) ever fired.  Fixed by the
  `beat_schedule` assignment below.  Lane 03 D-14: "`celery_config.py` must set
  `beat_schedule = beat_schedule`".
"""
from app.config import settings
from celery_app.beat_schedule import beat_schedule as _beat_schedule

# ── transport settings carried from app.config (R22 pins this in tests/unit/celery) ─────────
# WHY via settings rather than literals: pydantic-settings' field names ARE the lower-cased env
# names (`CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`, … — app/config.py:12-15, case-insensitive),
# so a compose/`.env` override lands in `settings` first and `config_from_object` carries the
# OVERRIDDEN value.  Verified: CELERY_BROKER_URL=redis://env-probe:6379/9 reaches `app.conf`
# (build/R22-limits-obs/raw/probe_env_override.txt).  A literal here would silently defeat that.
broker_url = settings.celery_broker_url
result_backend = settings.celery_result_backend

task_serializer = "json"
result_serializer = "json"
accept_content = ["json"]
timezone = "UTC"
enable_utc = True

# ── task registry ────────────────────────────────────────────────────────────
# The seven shipped tasks live in exactly these three modules (SPEC-AGENTS §7); the task-lease
# sweep (T1) and the publish-health tick (R22) were added to the same three modules.
# `imports` is read by `app.loader.import_default_modules()` in every worker/beat
# process, which executes the `@app.task` decorators; `include` is the same list in
# the form the Celery CLI documents.  Both are set deliberately — the failure mode we
# are fixing is "declared but never loaded".
TASK_MODULES = (
    "celery_app.tasks.agent_tasks",
    "celery_app.tasks.daily_cycle",
    "celery_app.tasks.maintenance",
)
imports = TASK_MODULES
include = TASK_MODULES

# ── beat registry ────────────────────────────────────────────────────────────
# `celery beat` reads `app.conf.beat_schedule`; before this line it read `{}`.
beat_schedule = _beat_schedule

# ── delivery / worker settings ───────────────────────────────────────────────
worker_prefetch_multiplier = 1  # long tasks: one message in flight per worker slot
task_acks_late = True
task_reject_on_worker_lost = True

# WHY late acks (and the re-delivery they allow) are now safe.  `task_acks_late=True`
# means a crash between delivery and ack re-delivers the message; in the upload that was
# unsafe because nothing prevented a second execution of the same row and no side effect
# was idempotent (lane 04 D2: "duplicate tweets/emails").  Three things changed:
#   1. `run_agent_task` CLAIMS the task row atomically before doing anything; a second
#      delivery finds a live claim and exits with E_TASK_CLAIMED / E_TASK_NOT_RUNNABLE
#      and no side effect (celery_app/tasks/agent_tasks.py, "claim" step).
#   2. Every repeating sweep creates its task through `create_task_deduped` with a
#      `<agent_type>:<utc-day>:<bucket>` key, so a re-delivered tick cannot insert a
#      second row (unique index `ix_tasks_dedupe_key`, migration 0002).
#   3. Agent side effects are idempotent by natural key — `stripe_events.stripe_event_id`,
#      `daily_reports.report_date`, `memory_entries.chroma_id`,
#      `social_engagements.mention_id` exist already; migration 0002 adds
#      `ad_metrics (campaign_id, date)`, which was the one missing.
broker_connection_retry_on_startup = True  # a worker started before Redis must not exit

# ── Bounded broker/result transports (measured defect) ────────────────────────────────────────────
# With Redis absent, a single POST /agents/<type>/trigger produced twenty
# `celery.backends.redis: Connection to Redis lost: Retry (n/20) in 1.00 second` lines — a ~20 s stall
# per call, which is exactly the 20.0 s timeout the repo's own e2e run measured on GET /api/v1/config
# while the API was otherwise idle. The caller already handles a broker outage honestly (records the
# terminal failure and refuses with E_REDIS — see app/api/v1/agents.py); what was missing was a bound,
# so the attempt is capped at ~1 s per socket operation with at most one retry instead of a 20-step ladder. Mirrors app/core/redis_client.py.
broker_transport_options = {
    "socket_connect_timeout": 1.0,
    "socket_timeout": 1.0,
    "retry_policy": {"max_retries": 1, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.2},
}
result_backend_transport_options = {
    "socket_connect_timeout": 1.0,
    "socket_timeout": 1.0,
    "retry_policy": {"max_retries": 1, "interval_start": 0, "interval_step": 0.2, "interval_max": 0.2},
}

# Ceilings the upload never set (lane 04 §0: "no time/memory ceilings… a leaked CLI/Node
# child is contained in one process").  These come from settings so a deployment can tune
# them without editing code — and the tuning route is env-first (same reason as broker_url
# above): CELERY_TASK_TIME_LIMIT / CELERY_SOFT_TIME_LIMIT / CELERY_MAX_TASKS_PER_CHILD.
task_time_limit = settings.celery_task_time_limit            # hard kill
task_soft_time_limit = settings.celery_soft_time_limit       # SoftTimeLimitExceeded first
worker_max_tasks_per_child = settings.celery_max_tasks_per_child

# NOT carried here, on purpose: `worker_concurrency`.  Concurrency belongs to the launch command —
# docker-compose.yml:95 runs `celery -A celery_app.worker worker --loglevel=info --concurrency=4`
# and docker-compose.ci.yml overrides env/volumes only, so CI inherits that command.  A CLI
# `--concurrency` overrides `app.conf` even if this were wired, so the wiring would be inert;
# `settings.celery_worker_concurrency` (app/config.py:45-50) is the same "advertised, launch
# command owns it" story.  An operator changes concurrency in the compose command, not here.
worker_concurrency = None  # explicit so app.conf cannot inherit a celery default (R22 pin)

# Routing (unchanged — these three routes did load in the upload)
task_routes = {
    "celery_app.tasks.daily_cycle.*": {"queue": "scheduler"},
    "celery_app.tasks.agent_tasks.*": {"queue": "agents"},
    "celery_app.tasks.maintenance.*": {"queue": "maintenance"},
}

# Result backend settings
result_expires = 86400  # 24h
