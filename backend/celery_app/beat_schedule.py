"""Beat schedule — the 13 periodic entries, loaded by `celery_config.beat_schedule`.

The uploaded repo declared these entries in this file but never installed them
(measured: declared 5 / effective 0, FINDINGS §10), and `cleanup_old_activity` had no
entry at all (D-15), so its retention delete could never run.  T1 adds the seventh: the
task-lease sweep, so `requeue_stale_claims` — tested, and never called by anything — runs.

`expires` (seconds) on every entry is deliberate: Beat publishes on a durable queue and
a backlog (worker down, queue depth, a restart after an outage) would otherwise deliver
hours-old ticks.  A sweep delivered after its own period is not the sweep that was
scheduled — the dedupe key would not even recognise it, because the bucket has moved on.
Rule used here:

* the morning cycle: **hourly, expires 1 h** — since T2 it fires every hour and self-gates inside
  the task on the company-local hour (`company_config.daily_cycle_hour`, `settings.morning_cycle_hour`
  as the fallback); a tick older than its own hour is superseded by the next check;
* the evening cycle: **hourly, expires 1 h** — since L4-11 it fires every hour and self-gates on
  the company-local hour (`settings.evening_cycle_hour`); a tick older than its own hour is
  superseded by the next check;
* periodic sweeps: **one full period** (2 h / 3 h / 6 h) — a tick never survives into the
  next tick of the same job, so a backlog collapses instead of replaying;
* the 03:00 retention cleanup: **3 h** — a pure cleanup may run late, but it must not
  drift into the 06:00 morning cycle;
* the task-lease sweep: **every 5 min, expires 4 min (below the interval)** — its period
  is set against the 900 s claim lease rather than the wall clock (a dead worker's claim
  must not stay unreachable for a whole cycle), so a tick older than its own slot
  collapses into the next one;
* the publish-health tick: **every 15 min, expires 15 min** — a health snapshot delivered
  after its own slot describes a moment that has passed; the next tick re-takes it;
* the V8 task cadence: **every 15 min, expires 15 min** — a cadence tick delivered after its own
  slot describes a moment that has passed; the due check + the per-child dedupe key (one child per
  due instant) make a late tick harmless, and the next tick re-takes whatever is still due;
* the V9 outreach follow-ups: **every 3 h, expires 3 h (one full period)** — the due check
  (`follow_up_due <= now`, unanswered, `status='sent'`) plus the per-log dedupe key
  (`followup:<log id>`) make a late tick harmless and a re-delivered tick a no-op, and the
  next tick re-takes whatever is still due.
* the V9 invoice dunning: **daily, expires 6 h** — the ladder is date-keyed and each step fires AT
  MOST ONCE (the activity-row ledger), so a late tick cannot double-send and the next day re-takes
  whatever is still due; the 6 h bound keeps a backlog from replaying stale steps into a new day's
  schedule.

Expired messages are dropped by the broker before dispatch; `run_agent_task` still claims
atomically, so even a re-delivery cannot double-run a row.
"""
from celery.schedules import crontab

beat_schedule = {
    # Morning orchestration cycle — hourly since T2: the task gates itself on the COMPANY-LOCAL
    # hour (company_config.daily_cycle_hour, settings.morning_cycle_hour as the fallback), so the
    # entry no longer pins a UTC hour and a change in Settings takes effect without a beat restart;
    # the 23 non-matching ticks self-gate out in milliseconds.
    "morning-cycle": {
        "task": "celery_app.tasks.daily_cycle.run_morning_cycle",
        "schedule": crontab(minute=0),
        "options": {"queue": "scheduler", "expires": 3600},
    },
    # Evening reporting cycle — hourly since L4-11: the task self-gates on the COMPANY-LOCAL
    # hour (settings.evening_cycle_hour), so the entry no longer pins a UTC crontab hour while
    # the report is filed for the company-local day; the 23 non-matching ticks self-gate out in
    # milliseconds and a change in Settings takes effect without a beat restart (the T2 pattern).
    "evening-cycle": {
        "task": "celery_app.tasks.daily_cycle.run_evening_cycle",
        "schedule": crontab(minute=0),
        "options": {"queue": "scheduler", "expires": 3600},
    },
    # Night task (U5/P3): hourly, expires 1 h — the SAME self-gate pattern as the cycles.  The
    # task reads `company_config.night_task_hour` (company-local; NULL -> the documented 23:00
    # default) and, when `night_task_enabled` is on, hands the dispatcher its next <= limit rows
    # plus ONE receipt; DISABLED it returns {"status": "off"} and writes nothing.  A tick older
    # than its own hour is superseded by the next check (the T2 rationale).
    "night-task": {
        "task": "celery_app.tasks.night_task.run_night_task",
        "schedule": crontab(minute=0),
        "options": {"queue": "scheduler", "expires": 3600},
    },
    # Task cadence (V8/C2): every 15 min — reads every ENABLED `task_schedules` row whose
    # `next_run_at` is due, creates ONE task per due schedule (dedupe key
    # `schedule:<id>:<iso-date-hour>` of the due instant in the company tz) and recomputes
    # `next_run_at` from the COMPANY-local wall clock (never +24h — DST-safe; a disabled row is
    # skipped and an empty due set writes nothing).
    "task-cadence": {
        "task": "celery_app.tasks.cadence.run_due_schedules",
        "schedule": crontab(minute="*/15"),
        "options": {"queue": "scheduler", "expires": 900},
    },
    # Every 2h: check social mentions
    "social-mentions-sweep": {
        "task": "celery_app.tasks.agent_tasks.run_social_sweep",
        "schedule": crontab(minute=0, hour="*/2"),
        "options": {"queue": "agents", "expires": 7200},
    },
    # Every 3h: check email inbox
    "email-inbox-sweep": {
        "task": "celery_app.tasks.agent_tasks.run_email_sweep",
        "schedule": crontab(minute=30, hour="*/3"),
        "options": {"queue": "agents", "expires": 10800},
    },
    # V9/B3: the outreach follow-up sweep — every 3 h, expires one full period.  It reads the
    # overdue `email_logs` rows (`follow_up_due <= now`, `replied_at IS NULL`, `status='sent'`)
    # and creates ONE deduped follow-up task per prospect (key `followup:<log id>`, one task
    # per prospect per tick); nothing due writes nothing.  Queue `agents` — its children are
    # email_outreach agent runs.  Registry note: the module's TASK_MODULES line is settle
    # wiring (see `celery_app/tasks/followups.py`'s docstring).
    "email-followups": {
        "task": "celery_app.tasks.followups.run_follow_up_sweep",
        "schedule": crontab(minute=0, hour="*/3"),
        "options": {"queue": "agents", "expires": 10800},
    },
    # Every 6h: sync ad metrics + Stripe failed payments
    "ads-stripe-sync": {
        "task": "celery_app.tasks.agent_tasks.run_ads_stripe_sync",
        "schedule": crontab(minute=0, hour="*/6"),
        "options": {"queue": "agents", "expires": 21600},
    },
    # 03:00 UTC: delete activity_log rows older than settings.activity_retention_days.
    # NEW entry — the task existed but nothing ever called it (D-15).
    "activity-retention-cleanup": {
        "task": "celery_app.tasks.maintenance.cleanup_old_activity",
        "schedule": crontab(minute=0, hour=3),
        "options": {"queue": "maintenance", "expires": 10800},
    },
    # Every 5 min: recover tasks whose claiming worker died (claim lease expiry). The interval
    # is strictly SHORTER than the 900 s lease (settings.task_claim_lease_seconds), so a claim
    # cannot stay unreachable for a whole cycle; `expires` (4 min) sits below the interval so a
    # backlog of missed ticks collapses into the next one. NEW entry — T1; `requeue_stale_claims`
    # was tested and had no caller until this line existed.
    "task-lease-sweep": {
        "task": "celery_app.tasks.maintenance.requeue_stale_task_claims",
        "schedule": 300.0,
        "options": {"queue": "maintenance", "expires": 240},
    },
    # Every 5 min: re-dispatch stranded `pending` scheduler rows — a crash between a scheduler's
    # commit and its broker hand-off otherwise orphans the row forever (V11/W2, audit F1). PARENT
    # settle: appended per the stanza in `celery_app.tasks.maintenance`'s module docstring, with
    # `options` mirrored from the lease sweep above (`expires` is required by the parity pin).
    "reconcile-stranded-pending": {
        "task": "celery_app.tasks.maintenance.reconcile_stranded_pending_tasks",
        "schedule": crontab(minute="*/5"),
        "options": {"queue": "maintenance", "expires": 240},
    },
    # Every 15 min: is the activity publish breaker clean?  NEW entry — R22.  It exists because
    # `events.publish_stats()` had zero production readers: a Redis outage that silently skipped
    # activity publishes was counted but never SHOWN anywhere.  The tick writes the full stats
    # dict to the worker's log (WARNING while anything is non-clean) — see
    # `celery_app.tasks.maintenance.check_activity_publish_health` for the process-boundary note
    # (breaker counters are per process; the API process's are served by GET /api/v1/ops/events/stats).
    "activity-publish-healthcheck": {
        "task": "celery_app.tasks.maintenance.check_activity_publish_health",
        "schedule": crontab(minute="*/15"),
        "options": {"queue": "maintenance", "expires": 900},
    },
    # Hourly since V12/W-C (audit F12): the ladder SELF-GATES on the company-local hour
    # (`dunning.DUNNING_LOCAL_HOUR`, the cycles' T2 pattern) instead of pinning 13:00 UTC — the
    # old entry claimed "08:00 company-CT" but moved with the zone for any other COMPANY_TIMEZONE;
    # the 23 non-matching ticks return {"status": "off"} in milliseconds. The ladder's
    # once-per-step ledger makes a late or re-delivered tick harmless.
    "invoice-dunning": {
        "task": "celery_app.tasks.dunning.run_dunning_ladder",
        "schedule": crontab(minute=0),
        "options": {"queue": "agents", "expires": 3600},
    },
}
