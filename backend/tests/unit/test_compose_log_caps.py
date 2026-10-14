"""V7-T08 + V7-T09 — compose hardening: loopback publish + container log caps.

V7-T08 (D1 default): the stack must publish port 80 on 127.0.0.1 only — today it is
`"80:80"`, open on every host interface.  V7-T09: EVERY service must cap its container
logs (json-file driver, max-size 10m, max-file 3) — today no service has a `logging:`
block at all.  `yaml.safe_load` parsing the file IS the validity proof for both edits:
a broken compose file fails this test at parse time.

Scope: the ROOT docker-compose.yml only (docker-compose.ci.yml is a separate,
untouched artifact).  Raw RED evidence: raw/v7-red-tests.txt in the lane dir.
"""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]  # backend/tests/unit/x.py -> repo root
COMPOSE = ROOT / "docker-compose.yml"

SERVICES = (
    "postgres",
    "redis",
    "backend",
    "celery_worker",
    "celery_beat",
    "frontend",
    "nginx",
)

LOG_CAPS = {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_compose_yaml_parses_and_carries_the_seven_services() -> None:
    services = _compose()["services"]
    missing = sorted(set(SERVICES) - set(services))
    assert not missing, f"docker-compose.yml lost services: {missing}"


def test_nginx_publishes_on_loopback_only() -> None:
    """V7-T08: exactly ["127.0.0.1:80:80"] — not "80:80" (the whole LAN could reach it)."""
    services = _compose()["services"]
    assert services["nginx"]["ports"] == ["127.0.0.1:80:80"], (
        "nginx must be published on 127.0.0.1:80 only (D1 default)"
    )


def test_every_service_caps_its_container_logs() -> None:
    """V7-T09: all seven services carry the exact json-file logging block."""
    services = _compose()["services"]
    extras = sorted(set(services) - set(SERVICES))
    assert not extras, f"new services must also carry the logging block: {extras}"
    for name in SERVICES:
        logging_block = services[name].get("logging")
        assert logging_block == LOG_CAPS, (
            f"service '{name}' must cap its container logs (json-file, max-size 10m, "
            f"max-file 3); found {logging_block!r}"
        )
