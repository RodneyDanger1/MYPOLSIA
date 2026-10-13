"""R22 — the celery app's conf must CARRY the declared settings; a pin, not a comment.

WHY this file exists: `app/config.py` declares the broker URL, the result backend and the three
task ceilings, and `celery_config.py` reads them — but nothing asserted the VALUE reaches the real
app object (`app.conf`) that a worker/beat process runs with.  Without this pin, deleting a wiring
line is a silent regression: Celery falls back to its own defaults (`amqp://…` broker, no time
limit) and every shipped test stays green — the "declared but never loaded" defect class this repo
already measured twice (0 of 7 tasks registered, 0 of 5 beat entries effective).

RED leg (measured; raw/red_leg_a.txt): delete `broker_url = settings.celery_broker_url` from
`celery_app/celery_config.py` and `test_every_declared_celery_setting_is_carried_by_the_app_conf`
fails on the broker_url mismatch (`app.conf.broker_url` falls back to Celery's `amqp://…`).
"""
from __future__ import annotations

from app.config import settings
from celery_app import celery_config
from celery_app.worker import app

# conf key -> (settings field, the DEFAULT declared in app/config.py:39-44).  The default is read
# back off the Settings model itself, so the pin survives env overrides (see the test) while still
# failing if a declared default is changed without updating this table.
DECLARED: dict[str, tuple[str, object]] = {
    "broker_url": ("celery_broker_url", "redis://localhost:6379/0"),
    "result_backend": ("celery_result_backend", "redis://localhost:6379/1"),
    "task_time_limit": ("celery_task_time_limit", 900),
    "task_soft_time_limit": ("celery_soft_time_limit", 840),
    "worker_max_tasks_per_child": ("celery_max_tasks_per_child", 50),
}


def test_every_declared_celery_setting_is_carried_by_the_app_conf() -> None:
    """settings -> celery_config module -> the real app.conf; all three levels must agree.

    Env-independent by construction: a CELERY_* override (compose does exactly this) must flow
    through all three levels, so the assertions compare them against each other — while the
    DECLARED default is pinned separately off the model, where an override cannot hide it.
    """
    for conf_key, (setting_name, declared_default) in DECLARED.items():
        model_default = type(settings).model_fields[setting_name].default
        assert model_default == declared_default, (
            f"app/config.py declares {setting_name}={model_default!r}, the pin expects "
            f"{declared_default!r} — update this table in the same commit as the default"
        )
        effective = getattr(settings, setting_name)
        assert getattr(celery_config, conf_key) == effective, (
            f"celery_config.{conf_key} must READ settings.{setting_name} (got "
            f"{getattr(celery_config, conf_key)!r}, setting is {effective!r})"
        )
        assert getattr(app.conf, conf_key) == effective, (
            f"config_from_object did not carry {conf_key} to the app: "
            f"app.conf.{conf_key}={getattr(app.conf, conf_key)!r}, settings={effective!r}"
        )


def test_worker_concurrency_is_owned_by_the_launch_command_not_the_app_conf() -> None:
    """The concurrency story in one place: docker-compose.yml:95 owns it (`--concurrency=4`).

    A CLI `--concurrency` overrides `app.conf` even if wired, so wiring it here would be inert —
    `celery_config.py` carries the cross-reference comment instead.  This pin fires if someone
    wires it anyway, forcing them to read that comment (and the compose command) first.
    """
    assert app.conf.worker_concurrency is None, (
        "worker_concurrency must stay launch-command-owned (docker-compose.yml:95 "
        "`--concurrency=4`); see the cross-reference comment in celery_config.py"
    )
