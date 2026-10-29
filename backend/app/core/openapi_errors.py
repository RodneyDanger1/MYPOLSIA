"""The app's error-response declarations, applied to EVERY operation (SV-W4-01, wave 5).

WHY THIS FILE EXISTS
--------------------
Measured on the rebuilt wave-4 app (HEAD `7b81f56`, live `GET /openapi.json`): **114 operations,
0 of which declared a 401**, and **113 declared no error response beyond FastAPI's automatic
422** (the single exception was `GET /attachments/{id}`, `404,502`).  Yet the same build really
answers `401 E_AUTH`, `404 E_NOT_FOUND`, `503 E_CONFIG/E_STORAGE/E_DB` and `500 E_INTERNAL` — a
client generator, a typed SDK or an operator reading `/docs` could not see any of it.  The audit
that found this is `polsia-work/build/swarm-2026-10-28/systems-validator/VALIDATION-MATRIX.md`
§3.1; this module is the fix, and `app.main` calls it once while the app is assembled (before the
schema is first generated).

WHAT IS DECLARED, AND WHY EACH CLASS IS TRUE
--------------------------------------------
The declarations are derived from the app's own wiring, not copied by hand:

  * **500 `E_INTERNAL`** — EVERY operation.  `app.main.ErrorBoundary` turns any uncaught exception
    into this envelope (`main.py`, class `ErrorBoundary`), so no operation can honestly omit it.
  * **503 `E_CONFIG` / `E_STORAGE` / `E_DB` / `E_REDIS` / `E_PHONE_UNAVAILABLE` / `E_CLI_MISSING`**
    — every operation except the dependency-free liveness probe `GET /api/v1/health`
    (`app/api/v1/health.py` says it "never consults a dependency").  A readiness/DB session, the
    secret store, the artifact volume, the phone store, the broker and the provider seam all map
    to 503 in `app/core/errors.HTTP_STATUS_BY_CODE`; the only operation that touches none of them
    is liveness.  This is a declared SUPERSET of the class: one read-only probe (e.g. `/signals`,
    whose feed failures are reported per-source inside a 200) may never take this path in
    practice — declaring the service-unavailable class on a dependency-touching operation is the
    conservative direction (a missing class is the defect this module exists to remove).
  * **401 `E_AUTH`** — exactly the operations whose dependency tree contains `verify_api_key`
    (that is the router/route guard the app really uses; the header is `X-API-Key`).  The five
    public operations are therefore NOT given a 401 by accident:
    `/health`, `/ready`, `/finance/stripe/webhook`, `/support/inbox/inbound` (its own provider
    secret; a wrong secret is a real 401, declared below), `/outreach/unsubscribe` (a public
    opt-out link).
  * **404 `E_NOT_FOUND`** — every resource-addressed operation (its path template carries a
    `{param}`): that is the class that can name an absent run/task/row.  A collection read cannot
    return it and does not declare it.
  * **operation-specific extras** — `OPERATION_EXTRA_STATUSES` below.  Each entry was traced to
    the code that raises it; no status is declared on a route that cannot produce it.

DOCUMENTED EXCEPTIONS (pinned by `tests/unit/test_w5_openapi_error_responses.py`)
--------------------------------------------------------------------------------
  * `GET /api/v1/health` — `200 | 500` only (liveness: no dependency, no credential).
  * `POST /api/v1/finance/stripe/webhook` — unauthenticated by design; a bad signature is a
    400 with the framework's plain `{"detail": <string>}` body (pinned by
    `tests/unit/test_refusal_unify.py`), not the refusal envelope — declared that way.
  * `POST /api/v1/support/inbox/inbound` — the provider secret, never `X-API-Key`: a missing or
    wrong secret is `401 E_AUTH`, declared explicitly (it is not a `verify_api_key` dependency).
  * `DELETE /api/v1/data` — 403 `E_POLICY_DENIED` (the approval-token gate in `data.py`).
  * `POST /api/v1/tasks/{task_id}/run`, `PATCH /api/v1/tasks/{task_id}/status` — 409
    (`E_TASK_NOT_RUNNABLE`, `tasks.py`).
  * The 422 validation body keeps the framework's `{"detail": [...]}` shape (auto-declared by
    FastAPI; `main.py` deliberately preserves it) — it is not the refusal envelope, so it is not
    re-declared here.  A CODED 422 (E_VALIDATION through `refusal_guard`) is a DIFFERENT body and
    is named as a residual finding (SV-W6-02): the schema's 422 `$ref` is the framework model only.

DOMAIN STATUSES (SV-W5-01, wave 6) — DECLARED FROM RAISE SITES, NOT GUESSED
-------------------------------------------------------------------------
Wave 5 declared the four universal classes (401/404/500/503) and named the operation-specific
domain refusals as a residual.  This wave traced every domain code to the `raise`/decision site in
the product (census: `raw/w6/domain-raises.txt`, call-graph: `raw/w6/domain-reach.txt`) and moved
the REACHABLE ones into `OPERATION_EXTRA_STATUSES`.  The rule is unchanged: a status is declared on
an operation only when a raise site is reachable from that operation's handler.

  * 403 — only the four EXTERNAL-EFFECT operations (data wipe, invoice send, site deploy, phone
    send).  `E_EGRESS_BLOCKED` is 403 too, but it is raised only by
    `trust_service.resolve_egress`/`pinned_http_get`, which serve the `web_fetch` TOOL (never an
    HTTP operation), so no operation declares a second 403 for it.
  * 404 — every resource-addressed operation (path template) AND the pathless operations whose
    request names a row/plan: `POST /approvals`, `POST /planner/propose`,
    `POST /planner/confirm`, `POST /vision/describe`, `GET /site/package/download`.  Wave 5's
    pin said "a collection read cannot 404"; that heuristic was too strong (SV-W6-03): these five
    are addressed by body/query, not by path.
  * 409 — the state conflicts: the two task routes, the chat confirm token
    (`E_TASK_NOT_RUNNABLE`), the concurrent-planner lock (`E_TASK_CLAIMED`), and the phone
    driver's `E_PHONE_SEND_UNPAIRED` (`phone_send_service.SEND_STATUS_BY_CODE`).
  * 413 / 504 — `POST /planner/propose` ONLY.  `E_PROMPT_TOO_LARGE` is raised by
    `claude_cli.build_invocation` and `E_CLI_TIMEOUT` by the bounded `_run_planner_step` + the
    runtime; propose is the one operation that runs an agent step inside the request.  Task
    dispatch runs it in the worker, where the refusal is stored on the run — not returned.
  * 429 — `POST /planner/propose` (the day-cap `accounting.authorize` gate),
    `POST /invoices/{invoice_id}/send` and `POST /sites/{slug}/deploy` (the firewall's send caps,
    `POLSIA-E117`), and `POST /ws/ticket` (the ticket mint's own bound).  `POST /tasks`,
    `POST /chat/confirm` and `POST /auto-mode` enqueue work and do NOT spend in the request: the
    budget gate runs in the worker, so a 429 there would be a lie (the wave-5 census listed them;
    corrected here).
  * 502 — the upstream faults the API really relays: `GET /trust/link` (DNS/DoH transport),
    `GET /economics/convert` (FX provider), `POST /integrations/{provider}/test` (a live probe),
    `POST /vision/describe` (the model call, translated to `E_UPSTREAM`), the two firewall-gated
    sends (provider refusal), the deploy (Render) and `GET /attachments/{id}` (a vanished blob).
    `E_BALANCE` is NOT a 402 anywhere: it is an unlisted tool-level code (500), raised only inside
    the research-fetch/deepseek tool seam, so no operation declares it.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.utils import create_model_field
from pydantic import BaseModel, Field

from app.api.deps import get_db
from app.core.security import verify_api_key

logger = logging.getLogger(__name__)


class PolsiaErrorDetail(BaseModel):
    """The `error` object of the product refusal envelope (`app/core/errors.to_http_detail`)."""

    code: str = Field(description="the named E_* code, e.g. E_AUTH / E_NOT_FOUND / E_INTERNAL")
    message: str = Field(description="one sentence naming what failed")
    how_to_fix: str = Field(default="", description="what the caller/operator can do next")
    context: dict[str, Any] = Field(
        default_factory=dict, description="structured detail (ids, counts, request id)"
    )


class PolsiaErrorEnvelope(BaseModel):
    """The refusal envelope every named failure uses on the wire: `{"ok": false, "error": {...}}`."""

    ok: Literal[False] = Field(description="always false — the request failed")
    error: PolsiaErrorDetail


def _envelope(description: str) -> dict[str, Any]:
    """One `responses=` entry carrying the envelope model (FastAPI turns it into a $ref)."""
    return {"model": PolsiaErrorEnvelope, "description": description}


#: Bodies that are NOT the envelope, declared without a model on purpose (a generic JSON object).
_JSON_OBJECT: dict[str, Any] = {
    "content": {"application/json": {"schema": {"type": "object"}}},
}

#: The shared classes.  405 is deliberately absent: the app's 405 is raised by the framework and
#: named below (`E_METHOD_NOT_ALLOWED`), but it is not an operation-level response — a route is
#: only reached when the verb matches.
#
#: EVERY status this module can declare MUST have an entry here: `declarations_for` looks the entry
#: up by status, and a missing key raised KeyError inside `declare_error_responses` (whose
#: boot-safety `except` then dropped the WHOLE operation's declarations, including 500/503/401 —
#: measured while landing SV-W5-01: adding 413/504 to the extras table without a shared entry
#: silently removed 7 operations' error declarations).  `tests/unit/test_w6_domain_statuses.py`
#: pins the coverage instead of relying on the boot guard.
SHARED_ERROR_RESPONSES: dict[int, dict[str, Any]] = {
    401: _envelope(
        "E_AUTH — the X-API-Key header is missing or wrong (WWW-Authenticate: X-API-Key, "
        "X-Polsia-Error-Code: E_AUTH)"
    ),
    404: _envelope("E_NOT_FOUND — the run/task/row named by the path does not exist"),
    403: _envelope(
        "E_POLICY_DENIED / E_SANDBOX_BLOCKED — an external effect was refused by policy, the "
        "sandbox, an approval gate or an effect cap; the refusal names the gate that refused it"
    ),
    409: _envelope(
        "E_TASK_NOT_RUNNABLE / E_RUN_CANCELLED / E_TASK_CLAIMED / E_PHONE_SEND_UNPAIRED — the "
        "row or token is not in a state that accepts this operation (the current state is in "
        "error.context)"
    ),
    413: _envelope(
        "E_PROMPT_TOO_LARGE — the prompt (or the argv that would carry it) exceeds the runtime's "
        "budget; nothing was dispatched"
    ),
    429: _envelope(
        "E_RATE_LIMITED / E_BUDGET_EXCEEDED — a rate or spend cap refused this request; back off "
        "until the window resets, or raise the configured cap"
    ),
    500: _envelope(
        "E_INTERNAL — the request failed unexpectedly; the body's context.request_id matches the "
        "X-Request-ID response header and the server log"
    ),
    502: _envelope(
        "an upstream dependency answered wrongly — E_INTEGRATION / E_UPSTREAM / E_CLI_EXIT / "
        "E_CLI_JSON / E_AGENT_BAD_OUTPUT (the request was valid; the provider or runtime failed)"
    ),
    503: _envelope(
        "a dependency this operation needs is unavailable — E_CONFIG / E_STORAGE / E_DB / "
        "E_REDIS / E_PHONE_UNAVAILABLE / E_CLI_MISSING (`retryable`: the service, not the "
        "request, is the problem)"
    ),
    504: _envelope(
        "E_CLI_TIMEOUT — the model/runtime step exceeded its deadline; the attempt may still be "
        "unwinding server-side, so a retry costs the timeout again"
    ),
}

#: Operation-specific refusals, each traced to the code that raises it (wave-6 census:
#: `polsia-work/build/swarm-2026-10-28/systems-validator/raw/w6/domain-reach.txt`; each comment
#: names the raising seam).  The value is the set of DOMAIN statuses the operation can really emit,
#: not a wish-list: every entry below was derived from a reachable `raise`/decision site.
OPERATION_EXTRA_STATUSES: dict[tuple[str, str], tuple[int, ...]] = {
    # ── external effects: the policy firewall's decision object ─────────────────────────────────
    # data.py `delete_data_route` -> `_verify_wipe_token` raises E_POLICY_DENIED (403).
    ("DELETE", "/api/v1/data"): (403,),
    # deploy_service.deploy_site: `require_external_effects_enabled` -> E_POLICY_DENIED (403);
    # `_authorise_live_deploy` -> policy_firewall.enforce (sandbox 403 / kill switch 403 / render
    # day cap POLSIA-E117 -> 429); Render failures -> E_UPSTREAM (502).
    ("POST", "/api/v1/sites/{slug}/deploy"): (403, 429, 502),
    # invoice_service.deliver_invoice_email: request_side_effect (sandbox -> E_SANDBOX_BLOCKED 403,
    # email cap -> E_RATE_LIMITED 429) and the provider refusal -> E_INTEGRATION (502).
    ("POST", "/api/v1/invoices/{invoice_id}/send"): (403, 429, 502),
    # phone_send_service.send: kill switch -> E_POLICY_DENIED (403), sandbox -> E_SANDBOX_BLOCKED
    # (403), and SEND_STATUS_BY_CODE maps the driver's E_PHONE_SEND_UNPAIRED to 409.
    ("POST", "/api/v1/phone/send"): (403, 409),
    # planner_service._propose_once: _gate_planner_budget -> accounting.authorize -> BudgetStop
    # (E_BUDGET_EXCEEDED 429; kill switch E_POLICY_DENIED 403); the propose lock -> E_TASK_CLAIMED
    # (409); a missing company -> E_NOT_FOUND (404, pathless: SV-W6-03); _run_planner_step ->
    # E_CLI_TIMEOUT (504); the inline agent step -> the runtime's E_CLI_*/E_AGENT_BAD_OUTPUT (502)
    # and E_PROMPT_TOO_LARGE (413, claude_cli.build_invocation).
    ("POST", "/api/v1/planner/propose"): (403, 404, 409, 413, 429, 502, 504),
    # ── upstream faults the API really relays ───────────────────────────────────────────────────
    # tasks.py `get_attachment_endpoint` already declared 502 with a description; the raise site
    # is task_attachment_service._read_bytes -> E_UPSTREAM (502) when the row's blob vanished.  It
    # is listed here so the shared ENVELOPE body is attached to that declaration (the route's own
    # entry had no `content`, so a generated client had nothing to parse).
    ("GET", "/api/v1/attachments/{attachment_id}"): (502,),
    # trust_service.check_link -> _resolve_public / _fetch_headers raise E_UPSTREAM (502); the
    # Wayback leg is caught and reported as a field (not a refusal).
    ("GET", "/api/v1/trust/link"): (502,),
    # fx_service.convert -> _fetch raises E_UPSTREAM (502) for a transport/parse failure.
    ("GET", "/api/v1/economics/convert"): (502,),
    # integration_status_service.test_provider -> _probe_json raises E_UPSTREAM (502).
    ("POST", "/api/v1/integrations/{provider}/test"): (502,),
    # vision_service._call_model: a provider refusal/timeout/off-contract answer -> E_UPSTREAM
    # (502, `_upstream_from_runtime` translates E_CLI_TIMEOUT/E_CLI_JSON); an unknown attachment
    # or task -> E_NOT_FOUND (404, pathless: the id arrives in the body).
    ("POST", "/api/v1/vision/describe"): (404, 502),
    # ── pathless operations addressed by body/query (SV-W6-03) ─────────────────────────────────
    # site_package_service.resolve_package_dir raises E_NOT_FOUND for an unbuilt package.
    ("GET", "/api/v1/site/package/download"): (404,),
    # approvals.py `issue_approval_endpoint`: a missing attach target -> E_NOT_FOUND (404).
    ("POST", "/api/v1/approvals"): (404,),
    # planner_service.confirm: no company row / no proposal on record / an unknown candidate id
    # -> E_NOT_FOUND (404).
    ("POST", "/api/v1/planner/confirm"): (404,),
    # ── state conflicts ─────────────────────────────────────────────────────────────────────────
    # tasks.py `run_task_endpoint` / `update_task_status_endpoint` raise E_TASK_NOT_RUNNABLE (409).
    ("POST", "/api/v1/tasks/{task_id}/run"): (409,),
    ("PATCH", "/api/v1/tasks/{task_id}/status"): (409,),
    # chat_service._refuse_confirm: every non-consumable confirm token is E_TASK_NOT_RUNNABLE
    # (409) — a pathless operation whose conflict is named by a body token.
    ("POST", "/api/v1/chat/confirm"): (409,),
    # ── the framework/format exceptions that are not the envelope ───────────────────────────────
    # finance.py `stripe_webhook`: bad signature / missing header -> 400 (plain detail or a coded
    # E_VALIDATION envelope); a missing signing secret -> E_CONFIG 503 (already declared).
    ("POST", "/api/v1/finance/stripe/webhook"): (400,),
    # support.py `require_inbound_secret`: missing/wrong provider secret -> E_AUTH 401.
    ("POST", "/api/v1/support/inbox/inbound"): (401,),
    # ws.py `_authorise_mint` (sentinel's F-03b, landed after this declaration was designed): a
    # mint at the per-operator socket cap or over the rolling mint window is E_RATE_LIMITED (429).
    ("POST", "/api/v1/ws/ticket"): (429,),
}

#: Domain statuses that are NOT declared on any operation, with the reason and the evidence — the
#: "documented exception set" SV-W5-01 asked for.  Pinned by `tests/unit/test_w6_domain_statuses.py`
#: (each key must be absent from the operations the value names).
DOMAIN_STATUS_EXCEPTIONS: dict[int, str] = {
    402: (
        "no operation emits 402.  E_BALANCE is a TOOL-level code with no row in "
        "errors.HTTP_STATUS_BY_CODE (so it would be a 500); its raise sites are "
        "research_fetch_service.fetch and tool_registry, which serve agent tools, not HTTP routes."
    ),
    403: (
        "declared on the four EXTERNAL-EFFECT operations only (data wipe, invoice send, site "
        "deploy, phone send).  E_EGRESS_BLOCKED (also 403) is raised by "
        "trust_service.resolve_egress/pinned_http_get, which serve the web_fetch TOOL, so no "
        "operation declares it."
    ),
    413: (
        "declared on POST /planner/propose only: claude_cli.build_invocation is the only raiser, "
        "and propose is the only operation that builds an invocation inside the request.  Task "
        "dispatch builds it in the worker, where the refusal is stored on the run, not returned."
    ),
    429: (
        "declared on the four spend/effect operations (planner propose, invoice send, site "
        "deploy, ws ticket).  POST /tasks, POST /chat/confirm and POST /auto-mode ENQUEUE and do "
        "not spend in the request: accounting.authorize has exactly two production call sites "
        "(planner_service:421 and the unwired orchestration planner), so an operation-level 429 "
        "on them would be false."
    ),
    504: (
        "declared on POST /planner/propose only (the bounded _run_planner_step + the runtime).  "
        "POST /vision/describe translates a runtime E_CLI_TIMEOUT to E_UPSTREAM 502 "
        "(vision_service._upstream_from_runtime), so it must not declare 504."
    ),
}

#: Operations that are public on purpose (no `verify_api_key` in the dependency tree) and the
#: operations whose declared error set is narrower than the shared one.  The test pins both sets.
UNAUTHENTICATED_OPERATIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("GET", "/api/v1/health"),
        ("GET", "/api/v1/ready"),
        ("GET", "/api/v1/outreach/unsubscribe"),
        ("POST", "/api/v1/finance/stripe/webhook"),
        ("POST", "/api/v1/support/inbox/inbound"),
    }
)

#: The only operation with no dependency at all: it declares `500` and nothing else.
NO_UNAVAILABLE_OPERATION: frozenset[tuple[str, str]] = frozenset({("GET", "/api/v1/health")})


def _dependant_calls(route: APIRoute) -> list[Any]:
    """Every callable in the route's dependency tree (route + router + parameter deps)."""
    calls: list[Any] = []
    stack = [route.dependant]
    seen: set[int] = set()
    while stack:
        dependant = stack.pop()
        if id(dependant) in seen:
            continue
        seen.add(id(dependant))
        if dependant.call is not None:
            calls.append(dependant.call)
        stack.extend(dependant.dependencies)
    return calls


def _methods(route: APIRoute) -> set[str]:
    return {method.upper() for method in (route.methods or ())}


def declarations_for(route: APIRoute) -> dict[int, dict[str, Any]]:
    """The error responses ONE operation must declare — the single rule this module encodes."""
    methods = _methods(route)
    target = (sorted(methods)[0] if methods else "GET", route.path)
    calls = _dependant_calls(route)
    authenticated = any(call is verify_api_key for call in calls)
    db_backed = any(call is get_db for call in calls)

    statuses: set[int] = {500}
    if target not in NO_UNAVAILABLE_OPERATION:
        statuses.add(503)
    if authenticated:
        statuses.add(401)
    if "{" in route.path:
        statuses.add(404)
    for method in methods:
        statuses.update(OPERATION_EXTRA_STATUSES.get((method, route.path), ()))

    out: dict[int, dict[str, Any]] = {}
    for status in sorted(statuses):
        if status == 400 and target == ("POST", "/api/v1/finance/stripe/webhook"):
            # The webhook's bad-signature body is the framework's plain {"detail": <string>} (and
            # the coded event-id defect is the envelope): declare a JSON object, not a lie.
            out[status] = {
                "description": (
                    "the delivery could not be verified: a plain {\"detail\": <string>} body for "
                    "a bad/missing Stripe signature, or the refusal envelope for a coded event "
                    "defect (E_VALIDATION)"
                ),
                **_JSON_OBJECT,
            }
            continue
        shared = SHARED_ERROR_RESPONSES[status]
        entry: dict[str, Any] = {}
        for key, value in shared.items():
            entry[key] = dict(value) if isinstance(value, dict) else value
        if status == 503 and db_backed:
            entry["description"] = (
                "a dependency this operation needs is unavailable — E_CONFIG / E_STORAGE / "
                "E_DB / E_REDIS / E_PHONE_UNAVAILABLE / E_CLI_MISSING (the request may be "
                "retried once the service is up)"
            )
        out[status] = entry
    return out


def declare_error_responses(app: FastAPI) -> int:
    """Apply `declarations_for` to every schema operation.  Returns the number of entries added.

    Called from `create_app()` AFTER the routers are included and BEFORE the schema is first
    generated.  An operation that already declared a status keeps its own entry (the precedent
    was `tasks.py`'s `GET /attachments/{id}` -> `404,502`), so a route-specific declaration is
    never overwritten by the shared text.
    """
    added = 0
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.include_in_schema:
            continue
        try:
            declared = declarations_for(route)
        except Exception:  # noqa: BLE001 — a declaration bug must not stop the app booting
            # ...but it must NOT be silent either: before this warning existed, a missing
            # SHARED_ERROR_RESPONSES entry for a status in OPERATION_EXTRA_STATUSES dropped the
            # WHOLE operation's declarations (500/503/401 included) with no trace, which is the
            # exact defect class this module exists to remove.  The coverage is pinned by
            # tests/unit/test_w6_domain_statuses.py; this line makes a future regression visible.
            logger.warning(
                "openapi_errors: no declaration for %s %s (the operation keeps only its own "
                "responses)", ",".join(sorted(_methods(route))), route.path, exc_info=True,
            )
            continue
        for status, entry in declared.items():
            # The key TYPE matters: FastAPI looks `response_fields` up with the SAME key object the
            # route used (an int 404 in tasks.py, a str elsewhere), so the merge must keep it.
            existing_key: str | int | None = None
            if str(status) in route.responses:
                existing_key = str(status)
            elif status in route.responses:
                existing_key = status
            if existing_key is not None:
                existing = route.responses[existing_key]
                # A route that already declared this status KEEPS its own description — but a
                # declaration with NO body is not a wire shape a client can parse.  This was
                # measured on `GET /attachments/{id}` (tasks.py declared `404: {description}` and
                # `502: {description}`): the status was right, the body was empty, and a generated
                # client had nothing to deserialize.  Attach the shared envelope, keep the text.
                if not (isinstance(existing, dict) and existing.get("content")):
                    merged = dict(entry)
                    if isinstance(existing, dict) and existing.get("description"):
                        merged["description"] = existing["description"]
                    route.responses[existing_key] = merged
                    entry = merged
                else:
                    continue
            else:
                route.responses[str(status)] = entry
            # Routes built by FastAPI compile their `responses={...}` models into
            # `route.response_fields` in `APIRoute.__init__`, keyed by the SAME key object the
            # route used.  Our entries are added AFTER that construction, so the schema generator
            # would otherwise find no field for the status and emit the description with NO
            # content (measured: components had no PolsiaErrorEnvelope and every 401 body was
            # empty).  Build the same field FastAPI builds under the SAME key: `get_openapi_path`
            # looks up `route.response_fields.get(additional_status_code)` with the key as it
            # appears in `route.responses` — an int 404 needs an int 404 field, a str "503" a str.
            field_key: Any = existing_key if existing_key is not None else str(status)
            model = entry.get("model")
            if model is not None and field_key not in route.response_fields:
                route.response_fields[field_key] = create_model_field(
                    name=f"Response_{status}_{route.unique_id}",
                    type_=model,
                    mode="serialization",
                )
            added += 1
    return added


__all__ = [
    "DOMAIN_STATUS_EXCEPTIONS",
    "NO_UNAVAILABLE_OPERATION",
    "OPERATION_EXTRA_STATUSES",
    "PolsiaErrorDetail",
    "PolsiaErrorEnvelope",
    "SHARED_ERROR_RESPONSES",
    "UNAUTHENTICATED_OPERATIONS",
    "declare_error_responses",
    "declarations_for",
]
