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

A LIVE DEPLOY GOES THROUGH THE SHIPPED POLICY GATE (AC-03 / AC-04)
------------------------------------------------------------------
Live mode is an EXTERNAL_IRREVERSIBLE action, so it runs the same `policy_firewall` decision path
an agent's external effect runs — the owner-console slot `owner_render_deploy`:
kill switch first (`freeze`/`throttle` -> named `E_POLICY_DENIED`), then the capability/channel
check, then `SANDBOX_MODE` (a labelled `E_SANDBOX_BLOCKED` refusal; nothing is sent), then the
render day cap against a MEASURED count, then a single-use approval token bound to
`deploy_payload(slug, service_id)`.  There is no caller-controlled switch that skips any of it.

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

import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors import (
    E_CONFIG,
    E_DB,
    E_NOT_FOUND,
    E_POLICY_DENIED,
    E_UPSTREAM,
    E_VALIDATION,
    ERROR_HINTS,
    PolsiaError,
)
from app.core.timeutil import company_tz, day_window, now_utc
from app.models.report import ActivityLog
from app.services import channel_service, site_service
from app.services.query_helpers import fetch_scalar
from app.services.secret_store import mask_secret

logger = logging.getLogger(__name__)

#: Render's REST base.  A module attribute (not an inlined literal) so a test can point the adapter
#: at a scratch base; production never changes it.
RENDER_API_BASE = "https://api.render.com/v1"

#: The owner's credential + the Render service a generated site deploys to.
RENDER_API_KEY_ENV = "RENDER_API_KEY"
RENDER_SERVICE_ID_ENV = "RENDER_SERVICE_ID"

#: AC-03/AC-04 — the policy slot and channel this adapter is gated under.  The slot is a
#: `policy_firewall.DIRECT_CALLER_CAPABILITIES` row (owner console, not a model agent), so
#: `decide()` applies the kill switch, sandbox, render day cap and the single-use approval token
#: to a LIVE deploy exactly as it does for an agent's external effects.
OWNER_DEPLOY_SLOT = "owner_render_deploy"
RENDER_CHANNEL = "render"
DEPLOY_ACTION = "deploy_site"

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

    V28/L3: the field is named BY LITERAL, not computed (`name.lower()`).  The computed form made
    the link from RENDER_API_KEY to `Settings.render_api_key` exist only at runtime — a field
    rename would have degraded silently to "not configured", and no audit could see the edge.
    `tests/unit/test_v28_wiring.py::test_every_settings_field_has_a_reader` now pins both reads.
    """
    env_value = os.environ.get(name)
    if env_value is not None and env_value.strip():
        return env_value.strip()
    if name == RENDER_API_KEY_ENV:
        return _setting("render_api_key") or None
    if name == RENDER_SERVICE_ID_ENV:
        return _setting("render_service_id") or None
    return None  # not a credential this adapter knows: absent, never a guessed attribute


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


# ── the policy gate (AC-03 / AC-04) ────────────────────────────────────────────────────────
# WHAT WAS WRONG: `deploy_site` posted to api.render.com with NO sandbox check, NO kill-switch
# read, NO rate cap and NO approval — a live deploy was reachable while SANDBOX_MODE=true, which
# CLAUDE.md:52 documents as the control that prevents real Render calls ("only API-key
# authenticated"), and the render channel's configured 3/day cap was never consulted.
#
# WHAT REPLACES IT: the SHIPPED decision path.  `policy_firewall.enforce` (the function
# `side_effects.request_side_effect` wraps for agent runs) evaluates the owner-console slot
# `owner_render_deploy`: kill switch -> capability/class/channel -> sandbox -> render day cap ->
# approval token.  A live deploy is EXTERNAL_IRREVERSIBLE, so it always needs a single-use token
# bound to the exact (slug, service id) payload; the token is minted for
# `fingerprint("external_irreversible", "render", deploy_payload(slug, service_id))`.
#
# The CAP COUNT is MEASURED, not assumed: every successful live deploy writes one
# `channel_send:render` row through `channel_service.record_channel_send` (the shipped producer
# for a channel with no table of its own), and `render_deploys_today` counts those rows in the
# company-local day.  Before the first deploy that is a measured 0, never an invented one.  A
# caller that cannot supply the session is refused by name — a live deploy is never sent on an
# unknown cap.
#
# RESIDUAL, named: a successful deploy whose ledger write fails is reported as the deploy it is
# (never converted into a failure — the side effect already happened) and the failure is LOGGED;
# the render cap then under-counts that one deploy.  `channel_service` itself still reports
# `render` as "unknown" until its `_ACTIVITY_RECORDED_CHANNELS` tuple absorbs the channel (a
# REQUEST is filed with its owner in the lane report — the file is outside this seat's territory).


def deploy_payload(slug: str, service_id: str) -> dict[str, Any]:
    """The exact payload the operator approves: what the approval fingerprint is bound to.

    Deliberately minimal and stable: the action, the site slug and the Render service id.  The
    API key is NOT part of it (a fingerprint is stored in audit rows; a credential must never be),
    and neither is `dry_run` (only the live action is ever gated).
    """
    return {"action": DEPLOY_ACTION, "slug": slug, "service_id": service_id}


async def render_deploys_today(db: AsyncSession, *, now: datetime | None = None) -> int:
    """MEASURED live Render deploys in the company-local day containing `now`.

    Counts the `channel_send:render` ledger rows `_record_render_deploy` writes.  A COUNT that
    comes back empty is the named `E_DB` — never collapsed into a fabricated zero.
    """
    anchor = now or now_utc()
    start, end = day_window(db, day=anchor.astimezone(company_tz()).date())
    stmt = (
        select(func.count())
        .select_from(ActivityLog)
        .where(
            ActivityLog.action == f"{channel_service.CHANNEL_SEND_ACTION_PREFIX}{RENDER_CHANNEL}",
            ActivityLog.created_at >= start,
            ActivityLog.created_at < end,
        )
    )
    value = await fetch_scalar(db, stmt, what="render deploys today")
    if value is None:
        raise PolsiaError(
            E_DB,
            "count for render deploys today returned no value",
            how_to_fix=ERROR_HINTS[E_DB],
            context={"query": "render deploys today"},
        )
    return int(value)


def _authorise_live_deploy(
    *, slug: str, service_id: str, sends_today: int, approval_token: str
) -> dict[str, Any]:
    """Run the shipped decision path for the live deploy; refuse by name or return the payload.

    Returns the approved payload on ALLOW.  The sandbox draft and the missing/invalid approval
    token both come back as DECISIONS (not exceptions from `enforce`), and both are raised here as
    the named refusal: a live deploy is a single network POST, so there is no useful "draft" to
    persist — the honest answer is the refusal that names the control that stopped it.
    """
    from app.agents.policy_firewall import (
        ActionClass,
        ActionRequest,
        Decision,
        configured_approval_secret,
        enforce,
    )

    payload = deploy_payload(slug, service_id)
    decision = enforce(
        ActionRequest(
            agent_type=OWNER_DEPLOY_SLOT,
            action_class=ActionClass.EXTERNAL_IRREVERSIBLE,
            channel=RENDER_CHANNEL,
            value_usd=0.0,
            payload=payload,
            approval_token=approval_token,
            sends_today=sends_today,
            # RateCaps defines no render HOURLY cap; None keeps "no hour cap configured" distinct
            # from a measured zero (the firewall only consults it when a cap exists).
            sends_this_hour=None,
        ),
        approval_secret=configured_approval_secret(),
        direct_caller=OWNER_DEPLOY_SLOT,
    )
    if decision.decision is Decision.ALLOW:
        return payload

    code = decision.e_code or E_POLICY_DENIED
    fix = decision.how_to_fix or ERROR_HINTS.get(code, "")
    if decision.decision is Decision.REQUIRE_APPROVAL:
        fix = (
            f"mint a single-use approval token for fingerprint "
            f"{decision.audit.get('fingerprint', '')} (POST /api/v1/approvals with "
            f"{{\"action_fingerprint\": \"<the fingerprint above>\"}}, which needs APPROVAL_SECRET "
            "configured) and repeat this deploy with {\"approval_token\": \"<token>\"}; the token "
            "is bound to this site's slug and Render service id, so a token minted for another "
            "site does not verify"
        )
    raise PolsiaError(
        code,
        f"{DEPLOY_ACTION}: the live Render deploy of a site is refused by policy: {decision.reason}",
        how_to_fix=fix,
        context={
            "decision": decision.decision.value,
            "policy_code": decision.code,
            **decision.audit,
        },
    )


async def _record_render_deploy(
    db: AsyncSession, *, slug: str, deploy_id: str, service_id: str, at: str
) -> bool:
    """Record ONE real deploy in the channel ledger the render cap counts.

    Called only AFTER Render accepted the deploy (a 2xx carrying a deploy id) and AFTER the
    `meta.json` record was written, so the ledger never counts an attempt that did not happen.
    A ledger write failure must not rewrite the outcome of a real deploy: it is logged (with the
    deploy id, never the credential) and the cap under-counts that one deploy — the residual is
    named in the section comment above rather than hidden.
    """
    try:
        await channel_service.record_channel_send(
            db,
            RENDER_CHANNEL,
            summary=f"render deploy {deploy_id} submitted for site {slug}",
            detail={"site": slug, "service_id": service_id, "deploy_id": deploy_id, "at": at},
            agent_type=OWNER_DEPLOY_SLOT,
        )
    except (PolsiaError, SQLAlchemyError) as exc:
        logger.warning(
            "render deploy %s for site %s was accepted but the channel ledger could not record "
            "it (%s: %s); the render day cap will under-count this deploy",
            deploy_id,
            slug,
            type(exc).__name__,
            exc,
        )
        return False
    return True


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
    db: AsyncSession | None = None,
    approval_token: str = "",
) -> dict[str, Any]:
    """Submit (or preview) a deploy of `slug` to Render.

    `dry_run` defaults TRUE and changes nothing: no connection, no `meta.json` write.  Live mode
    makes ONE bounded call and claims success only for a 2xx carrying a deploy id.  `api_key` /
    `service_id` / `transport` / `at` / `base_url` are test seams; production passes none of them
    except `root` for the static mount's directory and `db` for the cap's measured count.

    LIVE MODE IS POLICY-GATED (AC-03/AC-04), in this order and before any network work:
      1. the operator kill switch (`require_external_effects_enabled`) — `freeze`/`throttle` is
         the named `E_POLICY_DENIED`, nothing is sent;
      2. the render day cap from a MEASURED count (`render_deploys_today`); `db` is required —
         no session means no measured count, which is the named `E_CONFIG` rather than a deploy
         on an unverifiable cap;
      3. `policy_firewall.enforce` on the owner-console slot: sandbox (`E_SANDBOX_BLOCKED`, the
         labelled refusal, nothing sent), the cap, and the single-use approval token an
         EXTERNAL_IRREVERSIBLE action always needs (`E_POLICY_DENIED` with the fingerprint and
         how to mint the token).
    A successful live deploy also writes one `channel_send:render` ledger row so the cap counts it.
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

    # AC-04 — the kill switch, read at CALL time before any transport work (a dry run above is a
    # read-only preview and is deliberately not blocked: freeze still allows reads).
    from app.agents.policy_firewall import require_external_effects_enabled

    require_external_effects_enabled(f"{DEPLOY_ACTION} of site {slug!r} (live Render deploy)")

    # AC-03 — the cap count must be measured; a caller without the session cannot measure it.
    if db is None:
        raise PolsiaError(
            E_CONFIG,
            f"a LIVE Render deploy of {slug!r} needs the database session this adapter counts "
            f"today's deploys through, and none was supplied — the {RENDER_CHANNEL} day cap "
            "would be unknown",
            how_to_fix=(
                "call deploy_site(..., db=<AsyncSession>); the HTTP route passes its request "
                "session. A live deploy is never sent on a cap that cannot be measured"
            ),
            context={"slug": slug, "channel": RENDER_CHANNEL},
        )
    sends_today = await render_deploys_today(db)
    _authorise_live_deploy(
        slug=slug,
        service_id=service,
        sends_today=sends_today,
        approval_token=approval_token,
    )

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
    # AC-03 — count the REAL deploy the cap is about.  After the site record, never before: a
    # ledger row must not exist for an attempt Render did not accept.
    await _record_render_deploy(
        db,
        slug=slug,
        deploy_id=deploy_id.strip(),
        service_id=service,
        at=stamp,
    )
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
    "DEPLOY_ACTION",
    "DEPLOY_TIMEOUT_SECONDS",
    "OWNER_DEPLOY_SLOT",
    "RENDER_API_BASE",
    "RENDER_API_KEY_ENV",
    "RENDER_CHANNEL",
    "RENDER_SERVICE_ID_ENV",
    "credentials",
    "deploy_payload",
    "deploy_site",
    "deploy_state",
    "planned_call",
    "render_deploys_today",
]
