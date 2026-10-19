"""The Render deploy adapter (wave V25, lane L3): take a generated site live, honestly.

WHAT THIS IS
------------
A generated site lives on this box (`services/site_service` -> `data/sites/<slug>/`).  This module
submits a **deploy of the configured Render service** for it:

    POST https://api.render.com/v1/services/<service_id>/deploys   {"clearCache": "do_not_clear"}

Render's deploy API is the whole contract: the credential and the service id are the owner's
(`RENDER_API_KEY` / `RENDER_SERVICE_ID`, environment first — docker-compose `env_file:` exports the
whole file into the container env — then the `.env`-backed `Settings` fields).  No key or no service
id is the NAMED `E_CONFIG`; there is no degraded path that pretends to deploy.

DRY RUN IS THE DEFAULT — AND IT CHANGES NOTHING
-----------------------------------------------
`dry_run=True` (the default, on the route and here) resolves the credentials, validates the slug,
and returns the EXACT call it would make — method, URL, body, header names — without opening a
connection and without touching `meta.json`.  The credential is present only as a masked display
form (`Bearer rnd…leak`); the raw key is never echoed in a response, a log line or a persisted
record.  A dry run still refuses `E_CONFIG` when the deploy could never run: previewing a call whose
credential is absent would be a false impression of readiness.

LIVE IS NEVER "SUCCESS" UNTIL RENDER SAYS SO
--------------------------------------------
A live deploy opens one bounded `httpx` call (`DEPLOY_TIMEOUT_SECONDS`).  Success requires a 2xx
whose JSON carries a non-empty deploy `id`; a 2xx without one is the named `E_UPSTREAM` — a
plausible body is not proof.  On success the record is persisted with Render's own deploy status
(`created` at submit time, so the normal state is `submitted`, NOT `deployed` — Render has accepted
the build, not finished it) and the service's public URL read best-effort (`GET /services/<id>`,
`null` when it cannot be read — never a guessed `*.onrender.com` host).

Failure mapping, named:
  * `E_CONFIG`  — 401/403 (key rejected) or 404 (no such service): the owner's configuration.
                  Nothing is persisted: no deploy was attempted.
  * `E_UPSTREAM` — timeout, transport error, 5xx, another 4xx, or a malformed 2xx.  The failure IS
                  persisted (`state: "failed"` + reason) so the UI can show why, and a timeout's
                  reason says plainly that Render may have accepted the deploy.
  * `E_VALIDATION` — the site slug does not exist (the deploy REQUEST names bad input; the deploy
                  STATE read in `deploy_state` names a missing resource with `E_NOT_FOUND`).

WHERE THE STATE LIVES
---------------------
`meta.json` beside the generated site, under `last_deploy`:

    {"at": ISO-8601, "state": "submitted"|"deployed"|"failed", "status": Render's word|null,
     "deploy_id": str|null, "url": str|null, "error": {"code", "message"}|null}

Reads/writes go through `site_service` (`read_deploy_state` / `record_deploy`) so the "a site exists
iff meta.json is readable" rule has ONE implementation, and the write is atomic (temp + replace).
The file sits inside the PUBLIC site directory: only non-secret deploy facts are stored there.
"""
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
from app.core.errors import E_CONFIG, E_NOT_FOUND, E_UPSTREAM, E_VALIDATION, PolsiaError
from app.services import site_service
from app.services.secret_store import mask_secret

#: Render's REST base.  A module attribute (not an inlined literal) so a test can point the adapter
#: at a scratch base; production never changes it.
RENDER_API_BASE = "https://api.render.com/v1"

#: The owner's credential + the Render service a generated site deploys to.
RENDER_API_KEY_ENV = "RENDER_API_KEY"
RENDER_SERVICE_ID_ENV = "RENDER_SERVICE_ID"

#: ONE bounded attempt.  A deploy submit is a small POST; 20 s is generous for Render's API and
#: short enough that a stuck request cannot wedge the API worker.
DEPLOY_TIMEOUT_SECONDS = 20.0

#: Render's documented "do not wipe the build cache" value for a plain site deploy.
CLEAR_CACHE = "do_not_clear"

#: Render statuses that mean the deploy is actually serving.
_LIVE_STATUSES = frozenset({"live"})


# ── credential resolution ───────────────────────────────────────────────────────────────────
def _setting(name: str) -> str:
    value = getattr(settings, name, "")
    return value.strip() if isinstance(value, str) else ""


def _resolved(name: str) -> str | None:
    """`name` from the process env first, then the `.env`-backed Settings field; blank -> None.

    A whitespace-only env value is treated as ABSENT (it cannot be a credential) and falls through
    to the Settings field, rather than shadowing a configured `.env` value with nothing.
    """
    env_value = os.environ.get(name)
    if env_value is not None and env_value.strip():
        return env_value.strip()
    return _setting(name.lower()) or None


def credentials() -> tuple[str | None, str | None, list[str]]:
    """`(api_key, service_id, missing)` — missing is the NAMED env vars, in a stable order."""
    api_key = _resolved(RENDER_API_KEY_ENV)
    service_id = _resolved(RENDER_SERVICE_ID_ENV)
    missing = [
        name
        for name, value in ((RENDER_API_KEY_ENV, api_key), (RENDER_SERVICE_ID_ENV, service_id))
        if value is None
    ]
    return api_key, service_id, missing


def _require_credentials(api_key: str | None, service_id: str | None) -> tuple[str, str]:
    """E_CONFIG (naming exactly what is absent) or the pair, stripped.  Never a fallback."""
    missing = [
        name
        for name, value in ((RENDER_API_KEY_ENV, api_key), (RENDER_SERVICE_ID_ENV, service_id))
        if not value
    ]
    if missing:
        raise PolsiaError(
            E_CONFIG,
            "Render deploy is not configured: missing " + ", ".join(missing),
            how_to_fix=(
                "set RENDER_API_KEY (Render dashboard -> Account Settings -> API Keys) and "
                "RENDER_SERVICE_ID (the srv-… id of the service to deploy) in the environment "
                "or backend/.env, then retry"
            ),
            context={"missing": missing},
        )
    return str(api_key), str(service_id)


# ── the plan ────────────────────────────────────────────────────────────────────────────────
def planned_call(
    service_id: str, *, api_key: str, base_url: str = RENDER_API_BASE
) -> dict[str, Any]:
    """The exact HTTP call a deploy makes — credential REDACTED, body verbatim.

    Header NAMES are the call's; the authorization VALUE is a masked display form, because a plan
    is for a human to inspect and a secret in a response body (or a browser's devtools) is a leak.
    """
    return {
        "method": "POST",
        "url": f"{base_url.rstrip('/')}/services/{service_id}/deploys",
        "headers": {
            "authorization": f"Bearer {mask_secret(api_key)}",
            "accept": "application/json",
            "content-type": "application/json",
        },
        "body": {"clearCache": CLEAR_CACHE},
    }


# ── small helpers ───────────────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _require_site(slug: str, root: Path | None) -> None:
    """The deploy REQUEST's existence check: unknown slug -> `E_VALIDATION` (the brief's word).

    `read_site_meta` is the one disk reader; its `E_NOT_FOUND` is retold here because a deploy of a
    slug the caller invented is bad input, while `deploy_state` (a resource READ) keeps
    `E_NOT_FOUND`.  An invalid slug shape already refuses `E_VALIDATION` from `validate_slug`.
    """
    try:
        site_service.read_site_meta(slug, root=root)
    except PolsiaError as exc:
        if exc.code != E_NOT_FOUND:
            raise
        raise PolsiaError(
            E_VALIDATION,
            f"no site exists for slug {slug!r}, so there is nothing to deploy",
            how_to_fix="list the sites (GET /api/v1/sites) and deploy one it returns, or create one",
            context={"slug": slug, "reason": "unknown_site"},
        ) from exc


def _error_message(response: httpx.Response) -> str:
    """Render's own error line, bounded — used in honest `detail` strings."""
    try:
        document = response.json()
    except ValueError:
        document = None
    if isinstance(document, dict):
        for key in ("message", "error"):
            value = document.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip().replace("\n", " ")[:200]
        if isinstance(document.get("error"), dict):
            inner = document["error"].get("message")
            if isinstance(inner, str) and inner.strip():
                return inner.strip().replace("\n", " ")[:200]
    return (response.text or "").strip().replace("\n", " ")[:200]


def _normalise_url(raw: Any) -> str | None:
    """Render's `serviceDetails.url` as an absolute http(s) URL, or None — never invented."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None
    if not text.lower().startswith(("http://", "https://")):
        text = f"https://{text.lstrip('/')}"
    return text


def _failure_record(
    *, at: str, code: str, message: str, outcome_unknown: bool
) -> dict[str, Any]:
    return {
        "at": at,
        "state": "failed",
        "status": None,
        "deploy_id": None,
        "url": None,
        "error": {"code": code, "message": message, "outcome_unknown": outcome_unknown},
    }


def _store(slug: str, record: dict[str, Any], root: Path | None) -> None:
    site_service.record_deploy(slug, record, root=root)


# ── reads ───────────────────────────────────────────────────────────────────────────────────
def deploy_state(slug: str, *, root: Path | None = None) -> dict[str, Any]:
    """`{slug, configured, missing, service_id, last_deploy}` — a pure local read, no Render call.

    `last_deploy` is `null` when the site has never been deployed (or its record is not a JSON
    object); an unknown slug is the named `E_NOT_FOUND`, because this is a resource read.
    """
    _api_key, service_id, missing = credentials()
    return {
        "slug": site_service.validate_slug(slug),
        "configured": not missing,
        "missing": missing,
        "service_id": service_id,
        "last_deploy": site_service.read_deploy_state(slug, root=root),
        # api_key is deliberately not returned in any form: the UI needs "configured", not the key.
    }


# ── the deploy ──────────────────────────────────────────────────────────────────────────────
async def deploy_site(
    slug: str,
    *,
    dry_run: bool = True,
    root: Path | None = None,
    api_key: str | None = None,
    service_id: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    at: str | None = None,
    base_url: str = RENDER_API_BASE,
) -> dict[str, Any]:
    """Submit (or preview) a deploy of `slug` to Render.

    `dry_run` defaults TRUE and changes nothing: no connection, no `meta.json` write.  Live mode
    makes ONE bounded call and claims success only for a 2xx carrying a deploy id.  `api_key` /
    `service_id` / `transport` / `at` / `base_url` are test seams; production passes none of them
    except `root` for the static mount's directory.
    """
    _require_site(slug, root)
    resolved_key, resolved_service, _missing = credentials()
    key = api_key or resolved_key
    service = service_id or resolved_service
    key, service = _require_credentials(key, service)
    plan = planned_call(service, api_key=key, base_url=base_url)

    if dry_run:
        return {
            "slug": slug,
            "dry_run": True,
            "configured": True,
            "state": "dry_run",
            "status": None,
            "deploy_id": None,
            "url": None,
            "at": None,
            "message": (
                f"dry run: {plan['method']} {plan['url']} was NOT sent; nothing was changed"
            ),
            "plan": plan,
        }

    stamp = at or _now()
    headers = {
        "authorization": f"Bearer {key}",
        "accept": "application/json",
        "content-type": "application/json",
    }
    try:
        async with httpx.AsyncClient(timeout=DEPLOY_TIMEOUT_SECONDS, transport=transport) as client:
            response = await client.post(plan["url"], headers=headers, json=plan["body"])
            if response.status_code in (401, 403):
                raise PolsiaError(
                    E_CONFIG,
                    f"Render rejected the key in {RENDER_API_KEY_ENV} (HTTP {response.status_code}): "
                    f"{_error_message(response)}",
                    how_to_fix=(
                        "replace RENDER_API_KEY with a current Render API key (Account Settings -> "
                        "API Keys) and retry"
                    ),
                    context={"status": response.status_code, "env": RENDER_API_KEY_ENV},
                )
            if response.status_code == 404:
                raise PolsiaError(
                    E_CONFIG,
                    f"Render has no service {service!r} for {RENDER_SERVICE_ID_ENV} (HTTP 404): "
                    f"{_error_message(response)}",
                    how_to_fix=(
                        "set RENDER_SERVICE_ID to the srv-… id of an existing Render service "
                        "(Render dashboard -> the service -> Settings)"
                    ),
                    context={"status": 404, "env": RENDER_SERVICE_ID_ENV, "service_id": service},
                )
            if not 200 <= response.status_code < 300:
                detail = _error_message(response)
                message = f"HTTP {response.status_code} from POST {plan['url']}" + (
                    f": {detail}" if detail else ""
                )
                record = _failure_record(
                    at=stamp, code=E_UPSTREAM, message=message, outcome_unknown=False
                )
                _store(slug, record, root)
                raise PolsiaError(
                    E_UPSTREAM,
                    message,
                    how_to_fix=(
                        "Render is the failing side here; check https://status.render.com and retry. "
                        "The failed attempt is recorded on the site's deploy state"
                    ),
                    context={"status": response.status_code, "response": detail},
                )
            try:
                document = response.json()
            except ValueError:
                document = None
            deploy_id = document.get("id") if isinstance(document, dict) else None
            if not isinstance(deploy_id, str) or not deploy_id.strip():
                message = (
                    f"Render answered HTTP {response.status_code} for POST {plan['url']} without a "
                    "deploy id — no deploy can be claimed from that response"
                )
                record = _failure_record(
                    at=stamp, code=E_UPSTREAM, message=message, outcome_unknown=True
                )
                _store(slug, record, root)
                raise PolsiaError(
                    E_UPSTREAM,
                    message,
                    how_to_fix=(
                        "check the Render dashboard for this service; if a deploy did start, its id "
                        "will be there. Retry only if none did"
                    ),
                    context={"status": response.status_code},
                )
            render_status = document.get("status")
            render_status = (
                render_status.strip()
                if isinstance(render_status, str) and render_status.strip()
                else None
            )
            url = await _read_service_url(client, plan["url"], headers=headers)
    except PolsiaError:
        raise
    except httpx.TimeoutException:
        message = (
            f"timed out after {DEPLOY_TIMEOUT_SECONDS:g}s waiting for POST {plan['url']}; "
            "Render may have accepted the deploy — check the Render dashboard before retrying"
        )
        record = _failure_record(
            at=stamp, code=E_UPSTREAM, message=message, outcome_unknown=True
        )
        _store(slug, record, root)
        raise PolsiaError(
            E_UPSTREAM,
            message,
            how_to_fix=(
                "slow is not failed: open the Render dashboard and look for a deploy of this "
                "service before retrying, so a second build is not queued on top of the first"
            ),
            context={"timeout_seconds": DEPLOY_TIMEOUT_SECONDS},
        ) from None
    except httpx.ConnectError:
        message = f"could not reach {plan['url']}: the connection failed before any request was sent"
        record = _failure_record(
            at=stamp, code=E_UPSTREAM, message=message, outcome_unknown=False
        )
        _store(slug, record, root)
        raise PolsiaError(
            E_UPSTREAM,
            message,
            how_to_fix="check this machine's outbound HTTPS access to api.render.com and retry",
            context={"timeout_seconds": DEPLOY_TIMEOUT_SECONDS},
        ) from None
    except httpx.HTTPError as exc:
        message = (
            f"Render call failed mid-flight ({type(exc).__name__}: {exc}); the deploy may have "
            "been accepted — check the Render dashboard before retrying"
        )
        record = _failure_record(
            at=stamp, code=E_UPSTREAM, message=message, outcome_unknown=True
        )
        _store(slug, record, root)
        raise PolsiaError(
            E_UPSTREAM,
            message,
            how_to_fix=(
                "open the Render dashboard and look for a deploy of this service before retrying"
            ),
            context={"error": type(exc).__name__},
        ) from None

    state = "deployed" if render_status in _LIVE_STATUSES else "submitted"
    record = {
        "at": stamp,
        "state": state,
        "status": render_status,
        "deploy_id": deploy_id.strip(),
        "url": url,
        "error": None,
    }
    _store(slug, record, root)
    return {
        "slug": slug,
        "dry_run": False,
        "configured": True,
        "state": state,
        "status": render_status,
        "deploy_id": deploy_id.strip(),
        "url": url,
        "at": stamp,
        "message": (
            f"Render accepted the deploy: {deploy_id.strip()} "
            f"(status {render_status or 'unknown'})"
        ),
        "plan": plan,
    }


async def _read_service_url(
    client: httpx.AsyncClient, deploys_url: str, *, headers: dict[str, str]
) -> str | None:
    """Render's public service URL, best-effort: None when it cannot be read.

    A deploy id already proved the submit; a failed URL read must NOT turn a real deploy into an
    error, and it must not fabricate `https://<service>.onrender.com` either (that host may not be
    the service's address).  `deploys_url` ends in `/services/<id>/deploys`.
    """
    service_url = deploys_url[: -len("/deploys")]
    try:
        response = await client.get(service_url, headers=headers)
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        document = response.json()
    except ValueError:
        return None
    if not isinstance(document, dict):
        return None
    details = document.get("serviceDetails")
    return _normalise_url(details.get("url")) if isinstance(details, dict) else None


__all__ = [
    "CLEAR_CACHE",
    "DEPLOY_TIMEOUT_SECONDS",
    "RENDER_API_BASE",
    "RENDER_API_KEY_ENV",
    "RENDER_SERVICE_ID_ENV",
    "credentials",
    "deploy_site",
    "deploy_state",
    "planned_call",
]
