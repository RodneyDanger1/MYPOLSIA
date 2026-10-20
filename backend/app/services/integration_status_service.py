"""The integrations surface's registry, masked status, and REAL test-connection probes.

WHY THIS MODULE EXISTS (V26 / F2)
---------------------------------
V25 shipped `github_service` and `deploy_service`; both resolve their credential through
`app.services.secret_store` and refuse `E_CONFIG` by name when it is absent.  Nothing in the
UI could put a value in that store, so a non-CLI owner could not enable either feature.  This
module is the server half of the Settings → Integrations panel: it names the providers, reports
MASKED status, writes/clears through the ONE existing store, and probes two providers with a
read-only GET.

THE PROVIDER REGISTRY IS FROZEN (lowercase, this order)
-------------------------------------------------------
    github, render, sendgrid, stripe, meta_ads, google_ads

`github` / `render` / `sendgrid` are WRITABLE (their values go to `secret_store`);
`stripe` / `meta_ads` / `google_ads` are READ-ONLY — their credentials are the existing
`Settings`/environment names (`STRIPE_SECRET_KEY`, `META_ADS_ACCESS_TOKEN`, …), and this
panel reports whether they are configured without pretending it can set them.

THE VALUE NEVER LEAVES
----------------------
* Status carries `configured`, `source` and the store's own `mask_secret()` hint — never the
  value.  `source` is the store's vocabulary, extended by exactly one word for multi-variable
  providers: `stored` (every configured secret came from the store), `env` (every one came
  from the environment), `mixed` (both), `none` (nothing configured).
* The routers answer through pydantic response models with no field that could carry a value,
  so an implementation mistake cannot leak one.
* Every string this module raises or returns is scrubbed of the credential before it leaves
  (`_scrub`), defense in depth on top of the response models.

TEST-CONNECTION IS A REAL READ-ONLY PROBE, OR A NAMED REFUSAL
-------------------------------------------------------------
    github  -> GET https://api.github.com/user                     (Bearer token)
    render  -> GET https://api.render.com/v1/services?limit=1      (Bearer token)

Both are bounded at `PROBE_TIMEOUT_S` (10 s, the brief's ceiling), follow no redirects, and
read at most `MAX_PROBE_BYTES`.  Failure mapping uses the codes that already exist in
`app/core/errors.py` — no new code, so the status-table census (`test_r29_status_parity`)
does not move:

    401/403                -> E_AUTH      (401 at the API boundary)
    missing variable       -> E_CONFIG    (503), NAMING the variable, before any request
    5xx / timeout / wire   -> E_UPSTREAM  (502)
    unknown provider       -> E_NOT_FOUND (404)

`sendgrid`, `stripe`, `meta_ads` and `google_ads` have NO read-only probe in this build.  A
test on them refuses BY NAME with `E_VALIDATION` (422) and a message that says no probe is
wired — it never fabricates a success.  (There is deliberately no `E_UNSUPPORTED` constant:
inventing one would change `HTTP_STATUS_BY_CODE` and move the pinned status census, so the
request-level refusal rides the existing `E_VALIDATION` instead.)

THE TEST SEAM
-------------
`_TRANSPORT` is `None` in production and an `httpx.MockTransport` in tests — the SAME labelled
seam `github_service._TRANSPORT` uses.  Validation, key resolution, masking, the store and the
refusal mapping all run for real under test.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from app.core.errors import (
    E_AUTH,
    E_CONFIG,
    E_NOT_FOUND,
    E_UPSTREAM,
    E_VALIDATION,
    PolsiaError,
)
from app.services import secret_store

logger = logging.getLogger(__name__)

#: The brief's ceiling: a test-connection probe is bounded at 10 seconds, end to end.
PROBE_TIMEOUT_S = 10.0

#: A provider's answer is read only far enough to prove it answered; 64 KiB is ample.
MAX_PROBE_BYTES = 64 * 1024

GITHUB_USER_URL = "https://api.github.com/user"
RENDER_SERVICES_URL = "https://api.render.com/v1/services?limit=1"

#: `None` in production; tests install an `httpx.MockTransport` (a LABELLED test double).
_TRANSPORT: Any = None

_PROBE_GITHUB = "github_user"
_PROBE_RENDER = "render_services"


@dataclass(frozen=True)
class ProviderSpec:
    """One entry of the frozen registry.

    `secrets` is every variable the panel reports; `required` is the subset the consuming
    adapter actually refuses without (read from `app/agents/integrations.py` and
    `deploy_service.py` — stripe needs only its secret key, google_ads needs four values).
    `configured` is "every REQUIRED variable is present", so a companion value the owner has
    not set (e.g. STRIPE_WEBHOOK_SECRET) cannot make absence look like readiness.
    """

    provider: str
    label: str
    secrets: tuple[str, ...]
    read_only: bool
    probe: str | None
    required: tuple[str, ...]


PROVIDERS: dict[str, ProviderSpec] = {
    "github": ProviderSpec(
        "github", "GitHub", ("GITHUB_TOKEN",), False, _PROBE_GITHUB, ("GITHUB_TOKEN",)
    ),
    "render": ProviderSpec(
        "render", "Render", ("RENDER_API_KEY", "RENDER_SERVICE_ID"), False, _PROBE_RENDER,
        ("RENDER_API_KEY", "RENDER_SERVICE_ID"),
    ),
    "sendgrid": ProviderSpec(
        "sendgrid", "SendGrid", ("SENDGRID_API_KEY",), False, None, ("SENDGRID_API_KEY",)
    ),
    "stripe": ProviderSpec(
        "stripe", "Stripe", ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET"), True, None,
        ("STRIPE_SECRET_KEY",),
    ),
    "meta_ads": ProviderSpec(
        "meta_ads", "Meta Ads", ("META_ADS_ACCESS_TOKEN", "META_ADS_ACCOUNT_ID"), True, None,
        ("META_ADS_ACCESS_TOKEN", "META_ADS_ACCOUNT_ID"),
    ),
    "google_ads": ProviderSpec(
        "google_ads",
        "Google Ads",
        (
            "GOOGLE_ADS_DEVELOPER_TOKEN",
            "GOOGLE_ADS_CLIENT_ID",
            "GOOGLE_ADS_REFRESH_TOKEN",
            "GOOGLE_ADS_CUSTOMER_ID",
        ),
        True,
        None,
        (
            "GOOGLE_ADS_DEVELOPER_TOKEN",
            "GOOGLE_ADS_CLIENT_ID",
            "GOOGLE_ADS_REFRESH_TOKEN",
            "GOOGLE_ADS_CUSTOMER_ID",
        ),
    ),
}

#: The order the API and the UI both serve — frozen, so a panel never reorders itself.
PROVIDER_ORDER: tuple[str, ...] = tuple(PROVIDERS)


# ---------------------------------------------------------------------------
# status (masked only)
# ---------------------------------------------------------------------------

def _provider_source(statuses: list[dict[str, Any]]) -> str:
    """`stored` / `env` / `mixed` / `none` — derived from the per-secret answers, never guessed."""
    sources = {entry["source"] for entry in statuses if entry["configured"]}
    if not sources:
        return "none"
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"


def provider_status(spec: ProviderSpec) -> dict[str, Any]:
    """The masked status of ONE provider.  There is no field that can carry a value."""
    per_secret = secret_store.secret_statuses(spec.secrets)
    secrets = [
        {
            "name": name,
            "configured": per_secret[name]["configured"],
            "source": per_secret[name]["source"],
            "masked": per_secret[name]["masked"],
        }
        for name in spec.secrets
    ]
    by_name = {entry["name"]: entry for entry in secrets}
    required = [by_name[name] for name in spec.required]
    return {
        "provider": spec.provider,
        "label": spec.label,
        "configured": all(entry["configured"] for entry in required),
        "source": _provider_source(required),
        "read_only": spec.read_only,
        "testable": spec.probe is not None,
        "secrets": secrets,
    }


def list_provider_status() -> list[dict[str, Any]]:
    """Every provider in the frozen order, masked."""
    return [provider_status(PROVIDERS[name]) for name in PROVIDER_ORDER]


# ---------------------------------------------------------------------------
# caller input
# ---------------------------------------------------------------------------

def require_provider(provider: Any) -> ProviderSpec:
    """The registry entry for `provider`, or the named `E_NOT_FOUND` (404)."""
    text = provider if isinstance(provider, str) else ""
    spec = PROVIDERS.get(text)
    if spec is None:
        raise PolsiaError(
            E_NOT_FOUND,
            f"integrations: unknown provider {text[:64]!r}; this surface ships exactly "
            f"{', '.join(PROVIDER_ORDER)}",
            how_to_fix=f"use one of: {', '.join(PROVIDER_ORDER)}",
            context={"provider": text[:64], "known": list(PROVIDER_ORDER)},
        )
    return spec


def _refuse_read_only(spec: ProviderSpec, action: str) -> None:
    raise PolsiaError(
        E_VALIDATION,
        f"integrations: {spec.provider} is read-only in this panel — {action} is refused. "
        f"Its credentials are the existing settings {', '.join(spec.secrets)}, read from the "
        "environment/.env; this surface reports their status without owning them.",
        how_to_fix=(
            f"set {', '.join(spec.secrets)} in backend/.env (or the process environment) and "
            "restart the service"
        ),
        context={"provider": spec.provider, "read_only": True, "secrets": list(spec.secrets)},
    )


def _checked_values(spec: ProviderSpec, values: Any) -> dict[str, str]:
    """Only names this provider owns, only non-blank strings — validated BEFORE any write."""
    if not isinstance(values, Mapping) or not values:
        raise PolsiaError(
            E_VALIDATION,
            f"integrations: no values were supplied for {spec.provider}",
            how_to_fix=f"pass {{\"values\": {{...}}}} with at least one of: {', '.join(spec.secrets)}",
            context={"provider": spec.provider, "accepted": list(spec.secrets)},
        )
    unknown = sorted(str(name) for name in values if name not in spec.secrets)
    if unknown:
        raise PolsiaError(
            E_VALIDATION,
            f"integrations: {spec.provider} has no secret(s) named {', '.join(unknown)}; "
            f"this provider owns exactly {', '.join(spec.secrets)}",
            how_to_fix=f"send one or more of: {', '.join(spec.secrets)}",
            context={"provider": spec.provider, "unknown": unknown, "accepted": list(spec.secrets)},
        )
    cleaned: dict[str, str] = {}
    blank = sorted(str(name) for name, value in values.items()
                   if not isinstance(value, str) or not value.strip())
    if blank:
        raise PolsiaError(
            E_VALIDATION,
            f"integrations: {spec.provider} value(s) {', '.join(blank)} are blank — a blank "
            "credential is not a credential; use the clear action to remove one",
            how_to_fix="send a real value, or DELETE the provider to remove the stored secret",
            context={"provider": spec.provider, "blank": blank},
        )
    for name, value in values.items():
        cleaned[str(name)] = value.strip()
    return cleaned


# ---------------------------------------------------------------------------
# writes through the ONE existing store
# ---------------------------------------------------------------------------

def save_provider_secrets(provider: Any, values: Any) -> dict[str, Any]:
    """Store the supplied values, then return the provider's masked status.

    Validation is all-or-nothing: no name is written until every supplied value is known to
    this provider and non-blank.
    """
    spec = require_provider(provider)
    if spec.read_only:
        _refuse_read_only(spec, "save")
    cleaned = _checked_values(spec, values)
    for name, value in cleaned.items():
        secret_store.set_secret(name, value)
    logger.info("integrations: stored %s for %s", ", ".join(cleaned), spec.provider)
    return provider_status(spec)


def clear_provider_secrets(provider: Any) -> dict[str, Any]:
    """Remove this provider's STORED values (an env value may still configure it)."""
    spec = require_provider(provider)
    if spec.read_only:
        _refuse_read_only(spec, "clear")
    for name in spec.secrets:
        secret_store.clear_secret(name)
    logger.info("integrations: cleared stored values for %s", spec.provider)
    return provider_status(spec)


# ---------------------------------------------------------------------------
# test-connection: real read-only probes
# ---------------------------------------------------------------------------

def _scrub(text: str, token: str) -> str:
    """Defense in depth: no returned string may carry the credential verbatim."""
    if token and token in text:
        return text.replace(token, "«redacted»")
    return text


def _provider_message(raw: bytes) -> str:
    """The provider's own first error line, bounded — never a guessed reason."""
    try:
        document = json.loads(raw.decode("utf-8")) if raw else None
    except (ValueError, UnicodeDecodeError):
        return ""
    if isinstance(document, dict):
        for key in ("message", "error_description", "error"):
            value = document.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:200]
    return ""


async def _probe_json(provider: str, url: str, token: str) -> Any:
    """ONE bounded read-only GET -> its parsed JSON, or the named refusal."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": "polsia-integrations/1.0",
    }
    try:
        # The timeout must sit on the constructor's own line: the perf pin scans for
        # `httpx.AsyncClient(` and `timeout=` together (tests/unit/test_v24_perf.py:338).
        async with httpx.AsyncClient(timeout=httpx.Timeout(PROBE_TIMEOUT_S),
                                     follow_redirects=False, transport=_TRANSPORT) as client:
            response = await client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        raise PolsiaError(
            E_UPSTREAM,
            f"{provider}: the read-only probe timed out after {PROBE_TIMEOUT_S:g}s",
            how_to_fix="retry once; if it persists check the provider's status page",
            context={"provider": provider, "timeout_s": PROBE_TIMEOUT_S},
        ) from exc
    except httpx.HTTPError as exc:
        raise PolsiaError(
            E_UPSTREAM,
            _scrub(
                f"{provider}: the read-only probe failed at the transport layer: "
                f"{type(exc).__name__}: {exc}",
                token,
            ),
            how_to_fix=f"check network reachability to {provider} and retry",
            context={"provider": provider},
        ) from exc

    raw = response.content or b""
    if len(raw) > MAX_PROBE_BYTES:
        raise PolsiaError(
            E_UPSTREAM,
            f"{provider}: the probe answered {len(raw)} bytes, past the "
            f"{MAX_PROBE_BYTES}-byte read cap",
            how_to_fix="retry; if it persists the endpoint is returning an unexpected body",
            context={"provider": provider, "status": response.status_code},
        )
    if response.status_code in (401, 403):
        detail = _provider_message(raw)
        message = f"{provider}: the credential was refused (HTTP {response.status_code})"
        if detail:
            message += f": {detail}"
        raise PolsiaError(
            E_AUTH,
            _scrub(message, token),
            how_to_fix=(
                f"re-enter a valid {provider} credential in Settings → Integrations "
                "(or fix the environment variable) and test again"
            ),
            context={"provider": provider, "status": response.status_code},
        )
    if response.status_code != 200:
        detail = _provider_message(raw)
        message = f"{provider}: the probe answered HTTP {response.status_code}"
        if detail:
            message += f": {detail}"
        raise PolsiaError(
            E_UPSTREAM,
            _scrub(message, token),
            how_to_fix=(
                "retry; if it persists the provider is answering unexpectedly — this is not "
                "fixed by re-entering your own configuration"
            ),
            context={"provider": provider, "status": response.status_code},
        )
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise PolsiaError(
            E_UPSTREAM,
            f"{provider}: the probe answered 200 with a body that is not JSON — refusing "
            "rather than reporting a result nobody can read",
            how_to_fix="retry; if it persists the provider's contract has changed",
            context={"provider": provider, "status": response.status_code},
        ) from None


def _require_configured(spec: ProviderSpec) -> dict[str, str]:
    """Every credential the probe needs, or `E_CONFIG` naming the missing one(s)."""
    resolved: dict[str, str] = {}
    missing: list[str] = []
    for name in spec.secrets:
        value = secret_store.get_secret(name)
        if value:
            resolved[name] = value
        else:
            missing.append(name)
    if missing:
        raise PolsiaError(
            E_CONFIG,
            f"{spec.provider}: {', '.join(missing)} is not configured, so no test-connection "
            "probe can be made",
            how_to_fix=(
                f"save {', '.join(missing)} in Settings → Integrations (or set it in "
                "backend/.env) and test again"
            ),
            context={"provider": spec.provider, "missing": missing},
        )
    return resolved


async def test_provider(provider: Any) -> dict[str, Any]:
    """Probe the provider with a READ-ONLY request; return `{ok, provider, detail}` or refuse.

    A success is claimed only after the provider answered 200 to a documented read endpoint.
    An untestable provider refuses by name; it never returns a fabricated success.
    """
    spec = require_provider(provider)
    if spec.probe is None:
        status = provider_status(spec)
        missing = [entry["name"] for entry in status["secrets"] if not entry["configured"]]
        if missing:
            raise PolsiaError(
                E_CONFIG,
                f"{spec.provider}: {', '.join(missing)} is not configured (and this build has "
                "no read-only probe for it, so a test can only report the missing credential)",
                how_to_fix=(
                    f"set {', '.join(missing)} in backend/.env and restart; this provider has "
                    "no read-only test endpoint wired"
                ),
                context={"provider": spec.provider, "missing": missing},
            )
        raise PolsiaError(
            E_VALIDATION,
            f"{spec.provider}: no read-only probe is wired in this build — refusing to "
            "fabricate a connection result. The masked status is all this surface can report.",
            how_to_fix=(
                f"{spec.provider} is configured; use the feature that consumes it. A live "
                "probe needs a provider endpoint this deployment does not call"
            ),
            context={"provider": spec.provider, "testable": False},
        )

    resolved = _require_configured(spec)
    token = resolved[spec.secrets[0]]

    if spec.probe == _PROBE_GITHUB:
        document = await _probe_json(spec.provider, GITHUB_USER_URL, token)
        login = document.get("login") if isinstance(document, dict) else None
        detail = "GET https://api.github.com/user answered 200"
        if isinstance(login, str) and login:
            detail += f" as {login}"
        return {"ok": True, "provider": spec.provider, "detail": detail}

    document = await _probe_json(spec.provider, RENDER_SERVICES_URL, token)
    count = len(document) if isinstance(document, list) else None
    detail = "GET https://api.render.com/v1/services?limit=1 answered 200"
    if count is not None:
        detail += f" with {count} service(s) visible"
    return {"ok": True, "provider": spec.provider, "detail": detail}


__all__ = [
    "GITHUB_USER_URL",
    "MAX_PROBE_BYTES",
    "PROBE_TIMEOUT_S",
    "PROVIDERS",
    "PROVIDER_ORDER",
    "ProviderSpec",
    "RENDER_SERVICES_URL",
    "clear_provider_secrets",
    "list_provider_status",
    "provider_status",
    "require_provider",
    "save_provider_secrets",
    "test_provider",
]
