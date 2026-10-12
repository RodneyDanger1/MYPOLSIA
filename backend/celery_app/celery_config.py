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

broker_url = settings.celery_broker_url
result_backend = settings.celery_result_backend

task_serializer = "json"
result_serializer = "json"
accept_content = ["json"]
timezone = "UTC"
enable_utc = True

# ── task registry ────────────────────────────────────────────────────────────
# The seven shipped tasks live in exactly these three modules (SPEC-AGENTS §7).
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

# Ceilings the upload never set (lane 04 §0: "no time/memory ceilings… a leaked CLI/Node
# child is contained in one process").  These come from settings so a deployment can tune
# them without editing code.
task_time_limit = settings.celery_task_time_limit            # hard kill
task_soft_time_limit = settings.celery_soft_time_limit       # SoftTimeLimitExceeded first
worker_max_tasks_per_child = settings.celery_max_tasks_per_child

# Routing (unchanged — these three routes did load in the upload)
task_routes = {
    "celery_app.tasks.daily_cycle.*": {"queue": "scheduler"},
    "celery_app.tasks.agent_tasks.*": {"queue": "agents"},
    "celery_app.tasks.maintenance.*": {"queue": "maintenance"},
}

# Result backend settings
result_expires = 86400  # 24h
