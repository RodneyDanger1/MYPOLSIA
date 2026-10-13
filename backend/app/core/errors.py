"""Named failure codes.

Owner rule this file exists to enforce: **a capability that does not exist returns a
NAMED refusal** — an `E_*` code plus a `howToFix` — and an empty list never stands in
for "could not read". A silent `None`, a swallowed exception or a fake success are the
worst possible outcomes, so every failure path in this application raises one of these
instead of returning something plausible.

Usage:
    raise PolsiaError(E_CLI_TIMEOUT, "claude CLI exceeded 300s", how_to_fix="...")

    try:
        ...
    except SomeThirdPartyError as exc:
        raise PolsiaError(E_INTEGRATION, f"Stripe call failed: {exc}",
                          how_to_fix="check STRIPE_SECRET_KEY / network") from exc
"""
from __future__ import annotations

from typing import Any

# ── codes ────────────────────────────────────────────────────────────────────
# configuration / infrastructure
E_CONFIG = "E_CONFIG"                     # a required setting is missing or unreadable
E_DB = "E_DB"                             # database unavailable / unreadable
E_STORAGE = "E_STORAGE"                   # filesystem or object storage failure
E_REDIS = "E_REDIS"                       # broker / pub-sub unavailable

# auth / validation
E_AUTH = "E_AUTH"                         # missing or wrong credentials
E_VALIDATION = "E_VALIDATION"             # caller-supplied input is malformed
E_NOT_FOUND = "E_NOT_FOUND"               # requested resource does not exist

# agent runtime
E_AGENT_UNKNOWN = "E_AGENT_UNKNOWN"       # agent_type not in AGENT_MAP
E_CLI_MISSING = "E_CLI_MISSING"           # the claude CLI binary is not on PATH
E_CLI_TIMEOUT = "E_CLI_TIMEOUT"           # CLI exceeded its deadline
E_CLI_EXIT = "E_CLI_EXIT"                 # CLI exited non-zero
E_CLI_JSON = "E_CLI_JSON"                 # CLI output was not the documented envelope
E_AGENT_BAD_OUTPUT = "E_AGENT_BAD_OUTPUT"  # agent output failed schema validation
E_PROMPT_TOO_LARGE = "E_PROMPT_TOO_LARGE"  # prompt exceeds the argv limit
E_DEEPSEEK_KEY = "E_DEEPSEEK_KEY"          # no DeepSeek API key configured (RD8 runtime)

# policy / budget
E_SANDBOX_BLOCKED = "E_SANDBOX_BLOCKED"   # real side effect refused while sandboxed
E_POLICY_DENIED = "E_POLICY_DENIED"       # action refused by the policy firewall
E_BUDGET_EXCEEDED = "E_BUDGET_EXCEEDED"   # per-run / per-day spend cap hit
E_RATE_LIMITED = "E_RATE_LIMITED"         # provider or internal rate cap

# schedule / queue
E_TASK_CLAIMED = "E_TASK_CLAIMED"         # another worker holds a live claim
E_TASK_NOT_RUNNABLE = "E_TASK_NOT_RUNNABLE"  # task is not in a runnable state
E_INTEGRATION = "E_INTEGRATION"           # third-party integration failure


class PolsiaError(Exception):
    """A failure that names itself and says how to fix it.

    `code` is always an `E_*` constant; `how_to_fix` is required for anything a human
    can act on, and `context` carries the structured detail for the activity log.
    """

    def __init__(
        self,
        code: str,
        message: str,
        how_to_fix: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.how_to_fix = how_to_fix
        self.context = context or {}
        super().__init__(f"{code}: {message}")

    def as_dict(self) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "how_to_fix": self.how_to_fix,
                "context": self.context,
            }
        }


def refusal(code: str, message: str, **context: Any) -> dict[str, Any]:
    """The refusal shape returned by tools/handlers that decline to act.

    Returns `{"ok": False, "error": {...}}` — never a fake success, never an empty
    list pretending to be an answer.
    """
    return {"ok": False, **PolsiaError(code, message, context=context).as_dict()}


ERROR_HINTS: dict[str, str] = {
    E_CONFIG: "set the variable in .env (see .env.example) and restart the service",
    E_DB: "check DATABASE_URL and that PostgreSQL/Redis are reachable",
    E_REDIS: "check REDIS_URL and that the broker is running",
    E_AUTH: "send a valid X-API-Key header",
    E_VALIDATION: "fix the request body/parameters named in the message",
    E_NOT_FOUND: "check the id, or seed the row first (make seed)",
    E_AGENT_UNKNOWN: "use an agent_type from crew_factory.AGENT_MAP",
    E_CLI_MISSING: "install the Claude Code CLI and/or set CLAUDE_CLI_PATH",
    E_CLI_TIMEOUT: "raise CLAUDE_TIMEOUT_SECONDS or shrink the prompt",
    E_CLI_EXIT: "run the CLI by hand with the same prompt and read its stderr",
    E_CLI_JSON: "check the CLI version supports --output-format json",
    E_AGENT_BAD_OUTPUT: "the repair ladder will retry; otherwise tighten the agent schema",
    E_PROMPT_TOO_LARGE: "enable CLAUDE_PROMPT_VIA_STDIN or lower the context budget",
    E_DEEPSEEK_KEY: "enter your DeepSeek API key in Settings (or set DEEPSEEK_API_KEY) and retry",
    E_STORAGE: "repair or remove the local secret store file (backend/data/deepseek_api_key.json) and retry",
    E_SANDBOX_BLOCKED: "set SANDBOX_MODE=false once credentials are configured",
    E_POLICY_DENIED: "the action needs an approval or a wider tool allowlist",
    E_BUDGET_EXCEEDED: "raise the agent/day cap or wait for the next window",
    E_RATE_LIMITED: "back off; the limiter will retry after the window resets",
    E_TASK_CLAIMED: "another worker owns it; wait for the lease to expire",
    E_TASK_NOT_RUNNABLE: "check tasks.status — only pending/queued rows can be claimed",
    E_INTEGRATION: "check the provider's credentials, quota and status page",
}

# Which HTTP status a code means when it reaches the API boundary. Anything unlisted is a 500:
# an unmapped failure is by definition an internal one, and guessing 4xx would blame the caller.
#
# THIS TABLE IS THE SINGLE SOURCE OF TRUTH for code -> status.  `deps.STATUS_BY_CODE` is an ALIAS
# of it (it used to be a second literal table; the two disagreed for nine codes — E_DB, E_STORAGE,
# the four E_CLI_*, E_INTEGRATION, E_AGENT_BAD_OUTPUT, E_PROMPT_TOO_LARGE — so the same
# `PolsiaError` answered 502/503/504/413 when raised inside a route and a codeless 500 when raised
# before it; measured in build/R27-social-outreach/raw/status_table_diff.txt + probe_edb_paths.txt,
# consolidated here by lane R29).
HTTP_STATUS_BY_CODE: dict[str, int] = {
    E_AUTH: 401,
    E_VALIDATION: 422,
    E_NOT_FOUND: 404,
    E_AGENT_UNKNOWN: 422,
    E_TASK_NOT_RUNNABLE: 409,
    E_TASK_CLAIMED: 409,
    E_BUDGET_EXCEEDED: 429,
    E_RATE_LIMITED: 429,
    E_SANDBOX_BLOCKED: 403,
    E_POLICY_DENIED: 403,
    # A config outage is the same class as the broker outage below: the service itself cannot
    # take the work right now (a malformed COMPANY_TIMEZONE is reachable at request time — every
    # day window resolves the zone), and it is never the caller's fault — hence 503, not 4xx.
    E_CONFIG: 503,
    # A broker/dependency outage is neither the caller's fault nor an unmapped internal error:
    # 503 says "this service cannot take the work right now", which is exactly what happened.
    E_REDIS: 503,
    # R29: the nine rows below used to live only in `deps.STATUS_BY_CODE`, so the in-route
    # `refusal_guard` path knew them while this canonical table (read by the app-level handler for
    # everything raised before a route) fell through to 500.  The VALUES are exactly what the
    # in-route path has always emitted — this move adds no new status to any shipped response.
    E_DB: 503,            # database unavailable / unreadable — same class as E_REDIS
    E_STORAGE: 503,       # filesystem or secret-store failure — same class
    E_CLI_MISSING: 503,   # the agent runtime cannot take work until the CLI exists
    E_CLI_TIMEOUT: 504,   # CLI exceeded its deadline — the gateway-timeout analogue
    E_CLI_EXIT: 502,      # CLI exited non-zero — an upstream-gateway fault
    E_CLI_JSON: 502,      # CLI output was not the documented envelope — same upstream class
    E_AGENT_BAD_OUTPUT: 502,  # agent output failed schema validation — upstream fault
    E_INTEGRATION: 502,       # third-party integration failure — upstream fault
    E_PROMPT_TOO_LARGE: 413,  # the caller's prompt exceeds the argv limit — caller input
}


def http_status_for(code: str) -> int:
    return HTTP_STATUS_BY_CODE.get(code, 500)


def to_http_detail(exc: PolsiaError) -> dict[str, Any]:
    """The body an API client sees for a named refusal."""
    hint = exc.how_to_fix or ERROR_HINTS.get(exc.code, "")
    return {"ok": False, "error": {
        "code": exc.code,
        "message": exc.message,
        "how_to_fix": hint,
        "context": exc.context,
    }}
