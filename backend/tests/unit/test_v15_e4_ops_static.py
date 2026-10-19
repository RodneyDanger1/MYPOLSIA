"""Wave V15 / lane E4 — static pins for the surfaces that cannot run on this host (no docker).

2R-01 (rate limit), 2R-03 (`access_log off` on /ws/) and 2R-08 (CSP + X-Frame-Options + nosniff)
live in `nginx/nginx.conf`; 5-04 (scheduled daily backups, the compose-sidecar route) lives in
`docker-compose.yml` + `scripts/backup.sh`.  Nothing here runs nginx or docker — the parent runs
the live drills (listed in the lane receipt) — so these pins are the RUNNABLE form of the static
review: a small brace parser extracts each named location block and asserts WHERE a directive
sits, so a future edit that drops or misplaces one of them fails here, not in production.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
NGINX_CONF = (REPO_ROOT / "nginx" / "nginx.conf").read_text(encoding="utf-8")
COMPOSE = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")

PUBLIC_LOCATIONS = (
    "location = /api/v1/health {",
    "location = /api/v1/outreach/unsubscribe {",
    "location = /api/v1/finance/stripe/webhook {",
)


def _block(text: str, header: str) -> str:
    """The body of the first `header { ... }` block, braces balanced."""
    start = text.index(header)
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    raise AssertionError(f"unbalanced braces after {header!r}")


def _yaml_service(name: str) -> str:
    """The `name:` block of `docker-compose.yml` (up to the next 2-space service key)."""
    match = re.search(
        rf"^  {re.escape(name)}:\n(.*?)(?=^  [a-z_]+:\n|^volumes:|^networks:|\Z)",
        COMPOSE,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, f"compose service {name!r} not found"
    return match.group(0)


def _backup_script() -> str:
    return (REPO_ROOT / "scripts" / "backup.sh").read_text(encoding="utf-8")


# ── 2R-01 / U2-06: rate limiting on the public routes ───────────────────────────────────────────


def test_2r01_zones_are_declared_at_http_scope() -> None:
    http_block = _block(NGINX_CONF, "http {")
    assert re.search(
        r"limit_req_zone\s+\$binary_remote_addr\s+zone=public_burst:10m\s+rate=\d+r/m;",
        http_block,
    )
    assert re.search(
        r"limit_req_zone\s+\$binary_remote_addr\s+zone=webhook_burst:10m\s+rate=\d+r/m;",
        http_block,
    )
    assert "limit_req_status 429;" in http_block, "429, never the 503 default"


def test_2r01_limiter_applies_to_exactly_the_public_locations() -> None:
    for header in PUBLIC_LOCATIONS:
        block = _block(NGINX_CONF, header)
        assert re.search(r"limit_req zone=(public_burst|webhook_burst) burst=\d+ nodelay;", block), header
    assert len(re.findall(r"^\s*limit_req\s+zone=", NGINX_CONF, flags=re.MULTILINE)) == len(
        PUBLIC_LOCATIONS
    ), "limit_req must sit on exactly the public locations and nowhere else (checked once per public location + inside them); the directive may not wander onto keyed /api/ or /ws/"


def test_2r01_429_answers_in_the_named_refusal_envelope() -> None:
    assert re.search(r"error_page\s+429\s+=\s+@rate_limited;", NGINX_CONF)
    body = _block(NGINX_CONF, "location @rate_limited {")
    assert '"code":"E_RATE_LIMITED"' in body, "the code errors.py already maps to HTTP 429"


def test_2r01_ws_and_keyed_api_routes_stay_out_of_the_burst_class() -> None:
    assert "limit_req" not in _block(NGINX_CONF, "location /ws/ {")
    assert "limit_req" not in _block(NGINX_CONF, "location /api/ {")


# ── 2R-03 / W1: the WS key no longer lands in nginx logs ────────────────────────────────────────


def test_2r03_ws_access_log_off_only_in_the_ws_block() -> None:
    ws_block = _block(NGINX_CONF, "location /ws/ {")
    assert re.search(r"^\s*access_log off;", ws_block, flags=re.MULTILINE)
    assert len(re.findall(r"^\s*access_log\s", NGINX_CONF, flags=re.MULTILINE)) == 1, (
        "the directive is present on the /ws/ block ONLY (every other location keeps logging)"
    )


# ── 2R-08 / U2-04 handoff: CSP + X-Frame-Options + nosniff ──────────────────────────────────────


def test_2r08_security_headers_at_server_scope() -> None:
    server = _block(NGINX_CONF, "server {")
    assert re.search(r'add_header X-Frame-Options "SAMEORIGIN" always;', server)
    assert re.search(r'add_header X-Content-Type-Options "nosniff" always;', server)
    for header in (*PUBLIC_LOCATIONS, "location /ws/ {", "location /api/ {", "location / {"):
        assert "add_header" not in _block(NGINX_CONF, header), (
            f"add_header inside {header} would silently drop the server-scope inheritance"
        )
    # V21/L1: `location = /apidocs` is the ONE deliberate exception.  Swagger UI loads its JS/CSS
    # from cdn.jsdelivr.net, which the server-scope `default-src 'self'` blocks — the page would
    # answer 200 and render blank.  A location-level add_header REPLACES the inherited set, so that
    # block re-declares all three headers (verified below); every other location still carries
    # none (the loop above).  The total is therefore 3 server-scope + 3 on /apidocs.
    docs = _block(NGINX_CONF, "location = /apidocs {")
    for header in ("Content-Security-Policy", "X-Frame-Options", "X-Content-Type-Options"):
        assert f"add_header {header}" in docs, f"/apidocs must re-declare {header}"
    assert len(re.findall(r"^\s*add_header\s", NGINX_CONF, flags=re.MULTILINE)) == 6


def test_2r08_csp_is_next_compatible_and_never_grants_unsafe_eval() -> None:
    match = re.search(r'add_header Content-Security-Policy "([^"]+)" always;', NGINX_CONF)
    assert match is not None
    csp = match.group(1)
    for source in (
        "default-src 'self'",
        "script-src 'self' 'unsafe-inline'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data:",
        "font-src 'self' data:",
        "connect-src 'self' ws: wss:",
        "frame-ancestors 'self'",
        "base-uri 'self'",
        "form-action 'self'",
        "object-src 'none'",
    ):
        assert source in csp, source
    assert "unsafe-eval" not in csp


# ── 5-04 / #8: the scheduled daily backup sidecar ───────────────────────────────────────────────


def test_5_04_backup_service_is_a_real_sidecar() -> None:
    block = _yaml_service("backup")
    assert "image: postgres:16-alpine" in block
    assert "entrypoint" in block and "/usr/local/bin/backup.sh" in block
    assert "./scripts/backup.sh:/usr/local/bin/backup.sh:ro" in block
    assert "./backups:/backups" in block
    for volume in ("backend_data", "chroma_data", "redis_data", "celery_beat_data"):
        assert f"- {volume}:/volumes/{volume}:ro" in block, volume
    assert "BACKUP_INTERVAL_SECONDS" in block
    assert "BACKUP_RETENTION_DAYS" in block
    assert "condition: service_healthy" in block, "the sidecar gates on a healthy postgres"
    assert "restart: unless-stopped" in block


def test_5_04_backup_script_has_real_commands() -> None:
    script = _backup_script()
    assert script.startswith("#!/bin/sh\n")
    assert "pg_dump" in script
    assert "tar czf" in script
    assert "-mtime" in script and "rm -f" in script, "retention prune is a real find+rm"
    assert not re.search(r"^\s*(docker|sudo)\s", script, flags=re.MULTILINE), (
        "no docker CLI/socket dependency — the volume mounts do the work"
    )


def test_5_04_make_backup_untouched() -> None:
    assert "docker compose exec -T postgres pg_dump" in MAKEFILE
    assert "BACKUP_VOLUMES = backend_data chroma_data celery_beat_data redis_data" in MAKEFILE
