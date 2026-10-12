"""Beat schedule — the 6 periodic entries, loaded by `celery_config.beat_schedule`.

The uploaded repo declared these entries in this file but never installed them
(measured: declared 5 / effective 0, FINDINGS §10), and `cleanup_old_activity` had no
entry at all (D-15), so its retention delete could never run.

`expires` (seconds) on every entry is deliberate: Beat publishes on a durable queue and
a backlog (worker down, queue depth, a restart after an outage) would otherwise deliver
hours-old ticks.  A sweep delivered after its own period is not the sweep that was
scheduled — the dedupe key would not even recognise it, because the bucket has moved on.
Rule used here:

* daily cycles: **1 h** — a "morning plan" delivered at noon plans the wrong day's work;
* periodic sweeps: **one full period** (2 h / 3 h / 6 h) — a tick never survives into the
  next tick of the same job, so a backlog collapses instead of replaying;
* the 03:00 retention cleanup: **3 h** — a pure cleanup may run late, but it must not
  drift into the 06:00 morning cycle.

Expired messages are dropped by the broker before dispatch; `run_agent_task` still claims
atomically, so even a re-delivery cannot double-run a row.
"""
from celery.schedules import crontab

from app.config import settings

beat_schedule = {
    # Morning orchestration cycle
    "morning-cycle": {
        "task": "celery_app.tasks.daily_cycle.run_morning_cycle",
        "schedule": crontab(hour=settings.morning_cycle_hour, minute=0),
        "options": {"queue": "scheduler", "expires": 3600},
    },
    # Evening reporting cycle
    "evening-cycle": {
        "task": "celery_app.tasks.daily_cycle.run_evening_cycle",
        "schedule": crontab(hour=settings.evening_cycle_hour, minute=0),
        "options": {"queue": "scheduler", "expires": 3600},
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
}
