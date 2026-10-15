"""The Celery app object: `celery -A celery_app.worker worker` / `… beat`.

Before this wave the app registered **0 of its 7 tasks** and loaded **0 of its 5 declared
beat entries** (both measured: lane 03 D-13/D-14).  Three mechanisms, all pointing at the
same three modules, make the registries real:

1. `include=TASK_MODULES` — the documented Celery form, used by the worker/beat CLI;
2. `celery_config.imports` — the loader executes these modules in every process;
3. the explicit import below — so that *importing this module alone* (which is what
   probes, tests and `celery -A celery_app.worker` do) already yields the full registry
   instead of depending on when Celery decides to finalize the app.
"""
from celery import Celery

from celery_app import celery_config

app = Celery("polsia", include=list(celery_config.TASK_MODULES))
app.config_from_object("celery_app.celery_config")  # this also installs beat_schedule

# Eager registration of the task modules.  Each of them does
# `from celery_app.worker import app` — safe here because `app` is already bound above —
# and each `@app.task` decorator registers its name on this app.
import celery_app.tasks.agent_tasks  # noqa: E402,F401
import celery_app.tasks.cadence  # noqa: E402,F401  (V8/C2: same registry requirement as above)
import celery_app.tasks.daily_cycle  # noqa: E402,F401
import celery_app.tasks.dunning  # noqa: E402,F401  (V9/B4: same registry requirement as above)
import celery_app.tasks.followups  # noqa: E402,F401  (V9/B3: same registry requirement as above)
import celery_app.tasks.maintenance  # noqa: E402,F401
import celery_app.tasks.night_task  # noqa: E402,F401  (U5/P3: same registry requirement as above)
import celery_app.tasks.phone_activity  # noqa: E402,F401  (V16/E5: same registry requirement as above)
