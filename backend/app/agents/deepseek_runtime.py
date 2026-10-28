"""Polsia agent runtime — the DeepSeek (OpenAI-compatible) chat-completions client.

RD8: the owner's DeepSeek key can serve the agent fleet instead of the Claude CLI. This module
presents the SAME contract as `claude_cli` so every downstream consumer — base_agent's single
dispatch site, the retry classifier, the structured-output repair ladder, accounting — works
untouched:

  * accepts the same `claude_cli.Invocation`; the prompt / system prompt / JSON schema / resume id
    are recovered from it (`plan_from_invocation`, pinned by tests against the argv the ONLY argv
    builder, `claude_cli.build_invocation`, actually emits);
  * returns the same `claude_cli.InvokeResult`; `stdout` is ONE JSON envelope carrying the keys
    base_agent actually parses (`result`, `structured_output`, `is_error`, `subtype`,
    `api_error_status`, plus the accounting keys `usage`, `model`, `num_turns`, `duration_ms`,
    `session_id`);
  * raises the same named refusals: `E_CLI_TIMEOUT` for the wall clock (claude_cli.invoke raises
    it on subprocess.TimeoutExpired and `claude_cli.run_with_retries` retries it), `E_CLI_JSON`
    when the provider's body is not the documented shape, and `E_DEEPSEEK_KEY` when no key is
    configured anywhere (`how_to_fix` points at the in-app Settings);
  * maps HTTP failures into the marker taxonomy `claude_cli.is_retryable_failure` already
    understands: `subtype: error_rate_limit` + `api_error_status: 429` (retryable through BOTH
    the marker list and the status list), `error_server_error`/`error_overloaded` for 5xx,
    `error_during_execution` + 401/403 so the EXISTING CLI_AUTH_FAILED classification fires (and
    the ladder refuses to retry a bad key), `error_api_error` for transport faults
    (connect/read — transient network, retried via the api_error marker).

Provider contract encoded here (official docs, fetched 2026-10-13,
https://api-docs.deepseek.com/api/create-chat-completion):

  * `POST {base}/chat/completions`, `Authorization: Bearer <key>`, `Content-Type: application/json`;
  * models: `deepseek-flash` (DeepSeek-V4.1-Flash — the owner's model) / `deepseek-v4-pro`;
  * JSON output: `response_format={"type": "json_object"}`; the docs WARN the model must ALSO be
    instructed to emit JSON via a system or user message (else it can emit unbounded whitespace
    until the token cap) — every schema request therefore carries that instruction and the schema;
  * `finish_reason == "length"` is truncation and is reported as a FAILURE, never as success; the
    adjacent documented shapes are equally never a success (RD13): `content_filter` ("content was
    omitted due to a flag from our content filters"), `insufficient_system_resource` ("the request
    is interrupted due to insufficient resource of the inference system") and `aborted` ("the
    generation was interrupted") each map to their own named failure subtype
    (`error_max_tokens` / `error_content_filter` / `error_overloaded` / `error_aborted`);
  * `thinking` / `reasoning_effort`: canonicalized (minimal->low, medium/xhigh->high); effort
    `none` disables thinking (the fleet's deterministic JSON runs want no hidden reasoning pass).

Honesty rules (repo-wide): every failure is a NAMED refusal, a truncated response is never a
success, and no envelope key is fabricated — absent measurements stay absent (NULL-vs-zero rule
in `accounting._int_or_none`). The provider returns tokens, not money: the cost key this runtime
emits (`cost_estimate_usd`, L3-04) is computed from the measured token counts through the PINNED
price table in `app.agents.pricing` (`pricing_version` rides the envelope), never presented as a
provider bill — `total_cost_usd` is deliberately never fabricated (that key means the CLI's own
money measurement).

RD13 hardening (all inside the same frozen contract): unknown HTTP statuses classify by family
(>=500 -> `error_server_error`, retried; everything else -> `error_invalid_request`, NOT retried)
instead of borrowing the transport marker; `usage` values that are not real integers stay
UNMEASURED (None) so a provider type slip cannot crash the accounting consumer; a misconfigured
`DEEPSEEK_BASE_URL` is a named `E_VALIDATION` refusal, never a raw httpx `ValueError` escaping the
runtime; and every response snippet is scrubbed of the key (an endpoint that echoes the
Authorization header must not smuggle it into a logged failure envelope).

TOOL CALLING (V12 W-A): the runtime can now CALL TOOLS, against the FROZEN registry seam
(`app.agents.tool_registry` — `TOOL_SPECS` + sync `execute_tool(name, arguments, *, agent_type)`,
owned by W-B, imported LAZILY and never edited here). The activation signal is the CALLING AGENT's
tool list, exactly as `claude_cli.build_invocation` renders it into the argv (`--allowedTools`
non-empty, `--disallowedTools` without `*`), so an agent with NO tools gets a request and a reply
contract that are byte-identical to the pre-loop runtime. With tools on: the registry's full
`TOOL_SPECS` ride the request, every `finish_reason == "tool_calls"` turn is executed and answered
with a `role:"tool"` message carrying the call's own `tool_call_id`, and the loop is bounded at
`TOOL_LOOP_MAX_ITERATIONS` provider calls — a run still asking for tools at the bound refuses BY
NAME (`E_TOOL_LOOP`), which is also the rule for malformed arguments (`E_TOOL_ARGS`), a throwing
executor (`E_TOOL_EXEC`) and a non-`str` return (`E_TOOL_CONTRACT`). A tool result is never
reported as the run's answer, the registry's own refusal payloads pass back verbatim, `usage` is
SUMMED over every provider call (measured-or-absent across the whole run), and the run record
carries tool NAMES only.

VISION INPUT (V25/L1): `invoke(images=[...])` sends a task's attached images as OpenAI-compatible
content parts on the user turn — `{"type":"text"}` plus one `{"type":"image_url"}` data URI per
image.  The caps (PNG/JPEG/WebP, <= 5 MiB each, <= 8 MiB per request, raw bytes) are enforced
BEFORE the request; a task with no images sends the exact same plain-string message as before
(pinned byte-for-byte).  `invoke(task_title=...)` publishes the run's title for
`registry.execute_tool(task_title=...)`, which `web_search` uses as the provider's `ori_question`.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.agents import claude_cli, pricing
from app.config import settings
from app.core.errors import (
    E_ACTION_REPLAY_UNAVAILABLE,
    E_CLI_JSON,
    E_CLI_TIMEOUT,
    E_CONFIG,
    E_DEEPSEEK_KEY,
    E_VALIDATION,
    ERROR_HINTS,
    PolsiaError,
)

logger = logging.getLogger(__name__)

#: Documented defaults; Settings fields land with a parallel seat (RD8 seat 3) — see `_setting`.
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_DEFAULT_MODEL = "deepseek-flash"
DEEPSEEK_DEFAULT_REASONING_EFFORT = "none"
DEEPSEEK_DEFAULT_TIMEOUT_SECONDS = 300
#: The output bound (L3-04): the HTTP analogue of the CLI's `--max-budget-usd` — the run is
#: bounded at the CALL, not only at the dispatch gate. 8192 output tokens is ~6k words, far
#: above any of this product's JSON payloads; the worst-case output charge at flash peak rates
#: is 8192/1e6 * $1.20 ~= $0.0098. A response cut at the bound is reported as `error_max_tokens`
#: (never a success). Overridable via `deepseek_max_tokens` once that Settings field lands.
DEEPSEEK_DEFAULT_MAX_TOKENS = 8192
CHAT_COMPLETIONS_PATH = "/chat/completions"

#: reasoning_effort canonical values + the documented aliases (docs: minimal->low, medium/xhigh->high).
REASONING_EFFORT_CANONICAL = ("none", "low", "high", "max")
REASONING_EFFORT_ALIASES = {"minimal": "low", "medium": "high", "xhigh": "high"}

#: Bounded in-process session store: DeepSeek's API is STATELESS, so replaying `--resume`ed
#: conversations is what lets the repair ladder's R1/R2 re-prompts reference the previous answer
#: (the claude CLI gets that from its own on-disk sessions). In-process only; a lost entry is a
#: warned fresh call, never a silent mis-attribution.
SESSION_STORE_LIMIT = 32
#: V25/L1: a session carrying image content parts can be megabytes, so the store also has a
#: BYTE budget.  Oldest sessions are evicted until the total fits — the count bound alone would
#: let 32 × 8 MiB of images (plus base64 inflation) sit in one worker process.
SESSION_STORE_MAX_BYTES = 64 * 1024 * 1024
_SESSIONS: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
_SESSION_BYTES: dict[str, int] = {}
_SESSION_LOCK = threading.Lock()

#: HTTP status -> error-envelope subtype, chosen so `claude_cli.is_retryable_failure` classifies
#: correctly with NO changes to the ladder (marker words + api_error_status are the carriers).
_STATUS_SUBTYPES: dict[int, str] = {
    400: "error_invalid_request",
    401: "error_during_execution",   # 401/403 -> CLI_AUTH_FAILED (existing classifier), no retry
    402: "error_insufficient_balance",
    403: "error_during_execution",
    408: "error_timeout",
    422: "error_invalid_request",
    429: "error_rate_limit",
    500: "error_server_error",
    502: "error_server_error",
    503: "error_overloaded",
    504: "error_timeout",
}


def _subtype_for_status(status_code: int) -> str:
    """Subtype for any non-200 response: exact table first, then the documented families.

    RD13: `error_api_error` is the TRANSPORT-fault marker (`claude_cli.TRANSIENT_SUBTYPE_MARKERS`
    reads it as retryable), so it must not stand in for unknown statuses — a 404 (wrong base URL),
    a 3xx or a 418 would be retried without a request ever reaching a model. Unknown >=500 servers
    ARE transient and keep the same classification 500/502 already get; every other unknown status
    is a client-side condition (our request or the URL is wrong) and must NOT be retried.
    (409/425 stay retryable through `claude_cli.TRANSIENT_HTTP_STATUS`, which reads the status.)
    """
    if status_code in _STATUS_SUBTYPES:
        return _STATUS_SUBTYPES[status_code]
    return "error_server_error" if status_code >= 500 else "error_invalid_request"


#: `finish_reason` values that mean the completion is NOT the model's finished answer: the named
#: failure subtype each maps to, plus why. Docs enum: stop | length | content_filter | tool_calls |
#: insufficient_system_resource | aborted. `error_overloaded` carries the `overloaded` marker, so
#: the EXISTING retry classifier treats resource pressure as transient; the others carry no
#: transient marker (the same prompt/context should not be re-run blindly). `stop`/`tool_calls`
#: are absent on purpose: those replies carry text that can legitimately be the answer. A
#: `tool_calls` reply with NULL content is refused as E_CLI_JSON when this run has NO tools to
#: call (the pre-loop contract); on a run WITH tools it is the loop's normal hand-off — see
#: `TOOL_LOOP_MAX_ITERATIONS` and `_execute_tool_calls` below.
_INCOMPLETE_FINISH_REASONS: dict[str, tuple[str, str]] = {
    "length": (
        "error_max_tokens",
        "truncated at the output-token cap — raise the output budget or shrink the requested payload",
    ),
    "content_filter": (
        "error_content_filter",
        "output omitted by the provider's content filter — rewrite the prompt; the same prompt "
        "will be filtered again",
    ),
    "insufficient_system_resource": (
        "error_overloaded",
        "interrupted by insufficient inference resources on the provider side — a retry may succeed",
    ),
    "aborted": (
        "error_aborted",
        "the generation was interrupted before completion — re-run the task",
    ),
}

_JSON_INSTRUCTION = (
    "Return ONLY a single JSON object that validates against this JSON Schema: {schema} "
    "No prose, no markdown fences, no commentary."
)


def _setting(name: str, default: Any) -> Any:
    """Read a Settings field defensively.

    getattr-with-default: the `deepseek_*` Settings fields arrive with a parallel seat (RD8
    seat 3). Until then the documented defaults apply and nothing breaks; once they land, the
    same call reads the real configured value (this is the forward-safe pattern, not a stub).
    """
    return getattr(settings, name, default)


def _resolve_api_key() -> str | None:
    """Stored key -> env DEEPSEEK_API_KEY -> None.

    Frozen interface (RD8 seat 2): `app.services.secret_store.get_deepseek_api_key()`. The import
    is FUNCTION-LOCAL because that module may land concurrently; tests patch THIS function, so
    they pass whether or not the store has arrived. The key value is never logged.
    """
    try:
        from app.services.secret_store import get_deepseek_api_key
    except ImportError:
        return (os.getenv("DEEPSEEK_API_KEY") or "").strip() or None
    return (get_deepseek_api_key() or "").strip() or None


def available() -> bool:
    """The base_agent seam predicate: a DeepSeek key exists, so route agent runs to this runtime."""
    return _resolve_api_key() is not None


# ── tool calling (V12 W-A: the loop against the frozen tool-registry seam) ───────────────────

#: The loop's DEFAULT bound: how many provider calls ONE logical run may make. The model gets its
#: tool turns here; a reply that STILL asks for tools at the bound refuses the run BY NAME
#: (`E_TOOL_LOOP`) instead of looping forever or letting a half-finished conversation pass as an
#: answer.
#:
#: F-05: the bound is a SETTING (`deepseek_tool_loop_max_iterations`), not a constant — a run that
#: honestly needs five tool turns was truncated by a number nobody could raise. This value stays the
#: default AND the value used when the setting is absent (a deployment that has not configured one
#: keeps today's behaviour byte for byte). Every read goes through `_tool_loop_bound()`, which clamps
#: to the documented range below: one provider call is the floor (a bound of 0 would mean "refuse
#: every run that needs a tool"), and `TOOL_LOOP_MAX_ITERATIONS_CAP` is the ceiling — each extra
#: call is another round trip, and an unbounded loop is the hang this bound exists to prevent.
TOOL_LOOP_MAX_ITERATIONS = 4
TOOL_LOOP_MIN_ITERATIONS = 1
TOOL_LOOP_MAX_ITERATIONS_CAP = 12
#: The run record carries tool NAMES only — never arguments and never result bodies, which carry
#: untrusted (fetched) content. Truncated per name and capped per run so a provider cannot
#: inflate the envelope or a log line with one absurd name.
TOOL_NAME_MAX_CHARS = 80
TOOL_NAMES_MAX = 32
#: Bound on the runtime's OWN refusal payloads; a registry payload passes through as it is.
TOOL_RESULT_TEXT_LIMIT = 1000


def _tool_loop_bound() -> int:
    """F-05: the tool loop's provider-call bound, from configuration, clamped.

    `deepseek_tool_loop_max_iterations` is the setting; absent (or a value that is not an int) falls
    back to `TOOL_LOOP_MAX_ITERATIONS`, the shipped default. The result is clamped to
    [`TOOL_LOOP_MIN_ITERATIONS`, `TOOL_LOOP_MAX_ITERATIONS_CAP`]: a configured 0 would refuse every
    run that needs a tool at all, and a configured 10_000 is an unbounded loop wearing a number.
    A clamp is NEVER silent about being one — the caller logs the effective value on the refusal
    path, and `_tool_loop_bound` is the single reader, so the pin in the tests is the whole story.
    """
    raw = _setting("deepseek_tool_loop_max_iterations", TOOL_LOOP_MAX_ITERATIONS)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "deepseek runtime: deepseek_tool_loop_max_iterations=%r is not an integer; using the "
            "default %d", raw, TOOL_LOOP_MAX_ITERATIONS,
        )
        value = TOOL_LOOP_MAX_ITERATIONS
    return max(TOOL_LOOP_MIN_ITERATIONS, min(TOOL_LOOP_MAX_ITERATIONS_CAP, value))


# ── vision input (V25/L1) ────────────────────────────────────────────────────────────────────
# A task may carry image attachments; `invoke(images=[...])` turns the user turn into OpenAI-
# compatible CONTENT PARTS (text + `image_url` data URIs).  The contract, deliberately narrow:
#
#   * the model is `deepseek-flash` (the vision-capable model this deployment runs); an image on
#     any other model is still sent — this runtime does not pretend to know a provider's model
#     matrix, and a model that cannot read images answers with the provider's own named error;
#   * only PNG/JPEG/WebP data URIs are accepted.  The bytes were already sniffed at upload, but
#     this boundary re-checks the prefix and decodes the base64 so a caller cannot smuggle a
#     `data:text/html` or a malformed payload into a request;
#   * per image <= 5 MiB and per request <= 8 MiB of RAW image bytes (the same numbers the
#     upload route enforces, so an accepted task is always sendable).  Base64 inflates by ~4/3;
#     the cap is on the decoded bytes, which is what the provider actually charges for;
#   * NO images => the user message is the SAME plain string it has always been.  That
#     byte-identical rule is pinned by tests: a task with no attachments must not change one
#     byte of the request, so the pre-V25 fleet path cannot regress.
VISION_ALLOWED_MIME = ("image/png", "image/jpeg", "image/webp")
VISION_MAX_IMAGE_BYTES = 5 * 1024 * 1024
VISION_MAX_TOTAL_BYTES = 8 * 1024 * 1024


def _image_parts(images: Any) -> list[dict[str, Any]]:
    """Validate `invoke(images=...)` and build the request's `image_url` content parts.

    Returns `[]` for None/empty — the caller then leaves the user message as a plain string.
    Every refusal is `E_VALIDATION` and happens BEFORE any HTTP request: a payload over the
    budget must not be sent, and a malformed one must never be guessed at.
    """
    if not images:
        return []
    if not isinstance(images, (list, tuple)):
        raise PolsiaError(
            E_VALIDATION,
            f"deepseek runtime: images must be a list, got {type(images).__name__}",
            how_to_fix="pass the task's image payloads from task_attachment_service.image_parts()",
            context={"runtime": "deepseek"},
        )
    parts: list[dict[str, Any]] = []
    total = 0
    for index, image in enumerate(images):
        if not isinstance(image, dict):
            raise PolsiaError(
                E_VALIDATION,
                f"deepseek runtime: image #{index} is {type(image).__name__}, expected an object "
                "with mime and data_uri",
                how_to_fix="rebuild the payload with task_attachment_service.image_parts()",
                context={"runtime": "deepseek", "image_index": index},
            )
        mime = str(image.get("mime") or "").strip().lower()
        data_uri = image.get("data_uri")
        if mime not in VISION_ALLOWED_MIME:
            raise PolsiaError(
                E_VALIDATION,
                f"deepseek runtime: image #{index} has mime {mime!r}; only "
                f"{', '.join(VISION_ALLOWED_MIME)} are accepted",
                how_to_fix="upload a PNG, JPEG or WebP image (the upload route sniffs the bytes)",
                context={"runtime": "deepseek", "image_index": index, "mime": mime},
            )
        prefix = f"data:{mime};base64,"
        if not isinstance(data_uri, str) or not data_uri.startswith(prefix):
            raise PolsiaError(
                E_VALIDATION,
                f"deepseek runtime: image #{index} is not a {mime} base64 data URI",
                how_to_fix="rebuild the payload with task_attachment_service.image_parts()",
                context={"runtime": "deepseek", "image_index": index},
            )
        encoded = data_uri[len(prefix):]
        if not encoded:
            raise PolsiaError(
                E_VALIDATION,
                f"deepseek runtime: image #{index} carries no base64 payload",
                how_to_fix="re-upload the image; the stored file is empty",
                context={"runtime": "deepseek", "image_index": index},
            )
        try:
            # validate=True rejects non-base64 characters; the decoded LENGTH is the exact
            # raw-byte count the cap is stated in.
            raw = base64.b64decode(encoded, validate=True)
        except Exception as exc:  # noqa: BLE001 - any decode failure is malformed input, named
            raise PolsiaError(
                E_VALIDATION,
                f"deepseek runtime: image #{index} is not valid base64 ({type(exc).__name__})",
                how_to_fix="rebuild the payload with task_attachment_service.image_parts()",
                context={"runtime": "deepseek", "image_index": index},
            ) from exc
        if len(raw) > VISION_MAX_IMAGE_BYTES:
            raise PolsiaError(
                E_VALIDATION,
                f"deepseek runtime: image #{index} is {len(raw)} bytes, over the "
                f"{VISION_MAX_IMAGE_BYTES}-byte per-image limit",
                how_to_fix="upload images of at most 5 MB",
                context={"runtime": "deepseek", "image_index": index,
                         "limit_bytes": VISION_MAX_IMAGE_BYTES},
            )
        total += len(raw)
        if total > VISION_MAX_TOTAL_BYTES:
            raise PolsiaError(
                E_VALIDATION,
                f"deepseek runtime: image attachments total {total} bytes, over the "
                f"{VISION_MAX_TOTAL_BYTES}-byte per-request limit",
                how_to_fix="remove an image from the task or upload smaller images",
                context={"runtime": "deepseek", "total_bytes": total,
                         "limit_bytes": VISION_MAX_TOTAL_BYTES},
            )
        parts.append({"type": "image_url", "image_url": {"url": data_uri}})
    return parts


def _tool_registry():
    """The FROZEN registry seam (`app.agents.tool_registry`, V12 W-B), imported LAZILY.

    The import is function-local for the same reason `_resolve_api_key`'s is: the module lands
    concurrently with this one, and a tool-LESS run must not require it at all (pinned by test —
    the pre-loop behavior cannot depend on a module that did not exist before V12). A run that
    DOES have tools and cannot find the module is a named `E_CONFIG` refusal: the deployment is
    missing a file, which no retry can fix — and the runtime will never invent tool behavior.
    """
    try:
        from app.agents import tool_registry
    except ImportError as exc:
        raise PolsiaError(
            E_CONFIG,
            "deepseek runtime: this run has tools enabled but app.agents.tool_registry is not "
            f"importable ({type(exc).__name__}: {exc})",
            how_to_fix=(
                "deploy backend/app/agents/tool_registry.py (V12 W-B), or run the agent with no "
                "tools; the runtime never invents tool behavior"
            ),
            context={"runtime": "deepseek"},
        ) from exc
    return tool_registry


def _allowed_tools_from_argv(argv: list[str]) -> tuple[str, ...]:
    """The CALLING AGENT's REGISTRY tool list, exactly as `claude_cli.build_invocation` renders it.

    F-03: `--allowedTools a,b` is the ONE activation signal this runtime reads.  A DeepSeek-bound
    invocation fills it from `tool_registry.tools_for(agent_type)`; an empty/absent flag therefore
    means the run is tool-less (the CLI path, or an agent with no grant) and the request stays
    byte-identical to the pre-loop runtime.  This runtime never adds a tool of its own — the
    REGISTRY enforces the allowlist.

    `--disallowedTools` is CLI permission-rule language (`Bash`, `mcp__*`) and does NOT speak for
    the registry namespace: `AgentCapability.denied_tools=("*",)` means "no CLI tool", not "no
    registry tool" — the two live in deliberately separate namespaces (see
    `policy_firewall.AgentCapability`'s own comment).  Reading the CLI deny list here made a bare
    `*` disable the five registry tools of the ONLY agent that carries one (the orchestrator),
    while `tool_registry.execute_tool` -> `decide()` allowed every one of them.  The rule was also
    redundant: a tool-less run already has no `--allowedTools`, so it could only ever misfire.
    """
    raw = _flag_arg(argv, "--allowedTools") or ""
    return tuple(part.strip() for part in raw.split(",") if part.strip())


def _tool_specs_for(inv: claude_cli.Invocation, tools: list[dict] | None) -> list[dict]:
    """The request's `tools` value; `[]` when this run must behave exactly as it did before.

    The registry's FULL `TOOL_SPECS` are sent, never a runtime-side subset: `execute_tool` is the
    single allowlist enforcement point, so filtering here would create a SECOND copy of W-B's
    agent->tool mapping that could silently drift from it. A denied tool comes back as the
    registry's own named refusal payload, which the model reads on the next request.
    The list is copied: the registry keeps ownership of its own objects.
    """
    if tools is not None:
        return [spec for spec in tools]
    if not _allowed_tools_from_argv(inv.argv):
        return []
    specs = [spec for spec in (getattr(_tool_registry(), "TOOL_SPECS", None) or [])]
    if not specs:
        raise PolsiaError(
            E_CONFIG,
            "deepseek runtime: this run has tools enabled but tool_registry.TOOL_SPECS is empty "
            "or missing",
            how_to_fix=(
                "check that backend/app/agents/tool_registry.py (V12 W-B) exposes TOOL_SPECS, or "
                "run the agent with no tools"
            ),
            context={"runtime": "deepseek"},
        )
    return specs


def _tool_budget_api():
    """The REAL registry module's per-run budget verbs (`begin_run_tool_budget`/`end_run_...`).

    Imported lazily, for the same reason `_tool_registry` is, but DELIBERATELY not through it:
    `_tool_registry()` is the EXECUTOR seam, and the tree's tests legitimately install an executor
    double that speaks only the frozen contract (`TOOL_SPECS` + `execute_tool`,
    tests/unit/test_a3_writepath.py).  Requiring a new attribute on that double would break every
    one of those tests to test something the double does not implement; the budget's contextvars
    are process state owned HERE, so the runtime opens them on the real module and a double that
    never reads them simply never charges — exactly the pre-F-33 behaviour.
    """
    from app.agents import tool_registry

    return tool_registry


def _tool_refusal(code: str, detail: str) -> str:
    """A NAMED refusal payload for a call THE RUNTIME refused to run (E_* first, bounded).

    Only conditions the registry never sees land here: arguments that are not a JSON object, a
    throwing `execute_tool`, a non-`str` return. A refusal the REGISTRY produced is passed back
    VERBATIM — it owns that vocabulary — and is never re-wrapped through this formatter.
    """
    text = f"{code}: {detail}".strip()
    return text if len(text) <= TOOL_RESULT_TEXT_LIMIT else text[:TOOL_RESULT_TEXT_LIMIT] + "…"


def _tool_arguments(raw: Any) -> tuple[dict[str, Any] | None, str | None]:
    """(parsed arguments object, None) — or (None, why the shape is malformed).

    Absent/empty arguments mean the ordinary no-argument call. Anything else must BE a JSON
    object: a string that does not parse, JSON that is not an object, or a non-string type is
    malformed — and a malformed call is NEVER executed with a fabricated shape (substituting `{}`
    would invent arguments the model never sent).
    """
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if not isinstance(raw, str):
        return None, f"arguments were {type(raw).__name__}, expected a JSON object string"
    try:
        parsed = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        return None, f"arguments were not valid JSON ({type(exc).__name__})"
    if not isinstance(parsed, dict):
        return None, f"arguments were a JSON {type(parsed).__name__}, expected an object"
    return parsed, None


def _record_tool_name(names: list[str], name: str) -> None:
    """Remember one tool name for the run record: NAMES only, bounded twice.

    Order-preserving, duplicates collapsed, each name truncated and the list capped — so a
    provider that asks for a megabyte-long tool name cannot inflate the envelope or the log.
    """
    short = name[:TOOL_NAME_MAX_CHARS] if len(name) > TOOL_NAME_MAX_CHARS else name
    if short not in names and len(names) < TOOL_NAMES_MAX:
        names.append(short)


def _requested_tool_names(calls: Any) -> list[str]:
    """The names in a tool_calls turn, read DEFENSIVELY (the shape is provider-controlled)."""
    names: list[str] = []
    if not isinstance(calls, list):
        return names
    for call in calls:
        if not isinstance(call, dict):
            continue
        function = call.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if isinstance(name, str) and name.strip():
            _record_tool_name(names, name.strip())
    return names


# ── the run's action list (§4.13 / F-13) ─────────────────────────────────────────────────────
#: The envelope key carrying THIS run's action records.  The worker persists them under the same
#: name in `tasks.metadata` (`celery_app.tasks._common.ACTION_KEY`); the two literals are pinned
#: equal by `tests/unit/test_a3_writepath.py`.
ACTION_KEY = "actions"
#: §4.13's per-entry bound: 4 KB of serialized record.  The identity fields (`call_key`, `tool`,
#: `args`) are never truncated — they ARE the replay key — so the tool RESULT absorbs the bound.
MAX_ACTION_ENTRY_BYTES = 4096
#: The first cut before the byte fit (a tool answer is re-sent to the model on replay, not archived).
MAX_ACTION_RESULT_CHARS = 4000
#: §4.13's refusal now lives in `app/core/errors.py` (one code, one home): re-sending an
#: effect that may already exist is the defect, so this refuses instead.


def _canonical_arguments(arguments: Any) -> str:
    """The canonical JSON an attempt-independent call identity is hashed over."""
    return json.dumps(
        arguments, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def call_key(tool: str, arguments: Any) -> str:
    """Attempt-independent identity of ONE tool call: `<tool>|<sha256 of canonical args>`.

    §4.13's key.  It is derived from the model's own tool name and arguments, never from the
    attempt, the call id or the run: that is what lets a LATER attempt recognise "this exact call
    already happened" instead of performing it a second time.
    """
    digest = hashlib.sha256(_canonical_arguments(arguments).encode("utf-8")).hexdigest()
    return f"{tool}|{digest}"


def _run_identity(run_id: Any) -> Any:
    """The record's `run_id`: the `agent_runs.id` as the INT the column holds, else the raw value.

    `base_agent` carries the run id as text (it becomes a path segment), so a digit string is the
    numeric id; anything else is recorded verbatim rather than dropped, and an id-less run records
    `null` — never an invented number.
    """
    if isinstance(run_id, bool):
        return None
    if isinstance(run_id, int):
        return run_id
    if isinstance(run_id, str) and run_id.strip().isdigit():
        return int(run_id.strip())
    return run_id if isinstance(run_id, str) and run_id.strip() else None


def _entry_bytes(entry: dict[str, Any]) -> int:
    return len(json.dumps(entry, ensure_ascii=False).encode("utf-8"))


def _action_record(
    *,
    tool: str,
    arguments: Any,
    result: str,
    status: str,
    run_id: Any,
    attempt: Any,
) -> dict[str, Any]:
    """ONE action record, bounded to §4.13's 4 KB entry (`result` absorbs the bound).

    `result` is the tool's answer AS PERSISTED — a replay re-sends exactly this text — and
    `result_digest` binds it, so a corrupted entry is refused on replay instead of being replayed
    as if it were the tool's answer.  An entry whose identity fields alone fill the budget keeps
    its digest and NO `result`: it says what ran, and replay refuses it by name (fail closed)
    rather than inventing an answer or re-sending the call.
    """
    text = result[:MAX_ACTION_RESULT_CHARS]
    entry: dict[str, Any] = {
        "call_key": call_key(tool, arguments),
        "tool": tool,
        "args": arguments if isinstance(arguments, dict) else {},
        "run_id": _run_identity(run_id),
        # §4.13's `attempt`: which execution of this task performed the call.  The worker publishes
        # the claim's own counter; a direct caller that published none is running the first one.
        "attempt": (attempt if isinstance(attempt, int) and not isinstance(attempt, bool)
                    and attempt > 0 else 1),
        "status": status,
        "result": text,
        "result_digest": hashlib.sha256(text.encode("utf-8")).hexdigest(),
    }
    for _ in range(8):
        size = _entry_bytes(entry)
        if size <= MAX_ACTION_ENTRY_BYTES:
            return entry
        # JSON escaping can cost six bytes per character, so trim by the MEASURED overshoot and
        # re-measure instead of assuming one byte per character.
        keep = len(text) - max(1, size - MAX_ACTION_ENTRY_BYTES)
        if keep <= 0:
            break
        text = text[:keep]
        entry["result"] = text
        entry["result_digest"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    entry.pop("result", None)
    return entry


def replay_index(records: Any) -> dict[str, dict[str, Any] | None]:
    """`call_key -> stored record` for every call an earlier attempt TOUCHED.

    `None` means the key is present but its outcome is UNKNOWN — a `started` record, written when a
    process died between recording the intent and recording the answer.  §4.13's replay rule is
    fail-closed, so such a key is kept as a refusal marker: re-executing it could repeat an effect
    that already happened, which is the exact defect this list exists to prevent.
    """
    index: dict[str, dict[str, Any] | None] = {}
    if not isinstance(records, list):
        return index
    for record in records:
        if not isinstance(record, dict):
            continue
        key = record.get("call_key")
        if not isinstance(key, str) or not key:
            continue
        status = record.get("status")
        if status in ("executed", "replayed"):
            index[key] = record          # a later, terminal record for a key wins
        elif status == "started":
            index.setdefault(key, None)  # touched, unknown outcome: never re-executed
    return index


def _replay_refusal(detail: str, *, key: str, tool: str, url: str) -> PolsiaError:
    """The named refusal §4.13 requires where a recorded call cannot be replayed."""
    return PolsiaError(
        E_ACTION_REPLAY_UNAVAILABLE,
        f"{tool} ({key}): {detail}",
        how_to_fix=(
            "an earlier attempt of this task recorded the call but not a replayable answer, and "
            "re-sending it could repeat a real side effect. Nothing was sent. Inspect or clear "
            "tasks.metadata[\"actions\"] for this task to authorise a fresh run of it."
        ),
        context={"call_key": key, "tool": tool, "url": url, "runtime": "deepseek"},
    )


def _replayed_result(stored: dict[str, Any] | None, *, key: str, tool: str, url: str) -> str:
    """The recorded result text for `key`, or a NAMED refusal — never a silent re-send (§4.13)."""
    if not isinstance(stored, dict):
        raise _replay_refusal(
            "an earlier attempt started this call and never recorded its outcome; the effect may "
            "already exist, so it must not be executed again",
            key=key, tool=tool, url=url,
        )
    text = stored.get("result")
    digest = stored.get("result_digest")
    if not isinstance(text, str) or not isinstance(digest, str) or not digest:
        raise _replay_refusal(
            "the recorded entry carries no replayable result (its answer did not fit the entry "
            "bound, or was never persisted)",
            key=key, tool=tool, url=url,
        )
    if hashlib.sha256(text.encode("utf-8")).hexdigest() != digest:
        raise _replay_refusal(
            "the recorded entry's text does not match its own digest; the record was altered or "
            "corrupted after it was written",
            key=key, tool=tool, url=url,
        )
    return text


def _loop_evidence(
    tool_specs: list[dict], tools_used: list[str], journal: list[dict[str, Any]]
) -> dict[str, Any]:
    """A FAILURE envelope's tool evidence (`{}` for a tool-less run: byte-identical, no new keys).

    A2A4-01: the two failures the retry ladder actually retries — a transport fault and a non-200
    status — dropped `tools_used`, so at the retry seam every tool run looked tool-less and the
    cheapest correct mitigation ("do not retry a run whose envelope reported tools") was
    unimplementable against the envelopes the product really produces.

    F-13 rides the same envelope for the same reason: a FAILED run stores no
    `agent_runs.output`, so its action list exists only here — and this envelope is what the next
    delivery replays instead of repeating the run.
    """
    if not tool_specs:
        return {}
    return {"tools_used": list(tools_used), ACTION_KEY: list(journal)}


def _turn_usage(usage_raw: dict[str, Any]) -> dict[str, int | None]:
    """One provider call's four measurements (None = UNMEASURED, the NULL-vs-zero rule)."""
    return {
        "prompt_tokens": _usage_int(usage_raw, "prompt_tokens"),
        "completion_tokens": _usage_int(usage_raw, "completion_tokens"),
        "cache_hit_tokens": _usage_int(usage_raw, "prompt_cache_hit_tokens"),
        "cache_miss_tokens": _usage_int(usage_raw, "prompt_cache_miss_tokens"),
    }


def _sum_measured(values: list[int | None]) -> int | None:
    """Sum a metric across calls — or None when ANY call left it unmeasured.

    Measured-or-absent, never a mixture: a partial sum of a partly-unmeasured run is
    fabrication-precision (exactly the rule `accounting.tokens_used` and
    `pricing.estimate_cost_usd` apply to one call), so a single unmeasured turn makes the whole
    aggregate unmeasured. A fully-measured zero stays a zero.
    """
    if not values:
        return None
    total = 0
    for value in values:
        if value is None:
            return None
        total += value
    return total


def _accumulated_usage(turns: list[dict[str, int | None]]) -> dict[str, int | None]:
    """The envelope's `usage` for a (possibly multi-call) run; `{}` when nothing was measured."""
    if not turns:
        return {}
    return {
        "input_tokens": _sum_measured([turn["prompt_tokens"] for turn in turns]),
        "output_tokens": _sum_measured([turn["completion_tokens"] for turn in turns]),
        "cache_read_input_tokens": _sum_measured([turn["cache_hit_tokens"] for turn in turns]),
    }


def _price_split(
    turns: list[dict[str, int | None]],
) -> tuple[int | None, int | None, int | None]:
    """(cache-hit, cache-miss, output) tokens for `pricing.estimate_cost_usd`, summed per turn.

    Each turn gets the same conservative completion the single-call path used: an unreported hit
    side is billed at the MISS rate (always the expensive side), a missing miss side is derived
    only when `prompt == hit + miss` is consistent, and anything still unknown stays None — the
    estimate is never fabricated, and (through `_sum_measured`) one unmeasured turn makes it
    unknown. The rule is applied PER TURN because a run-level split could not stay conservative
    about an individual turn whose own cache split was unreported.
    """
    hits: list[int | None] = []
    misses: list[int | None] = []
    for turn in turns:
        prompt = turn["prompt_tokens"]
        hit, miss = turn["cache_hit_tokens"], turn["cache_miss_tokens"]
        if prompt is not None and hit is None:
            hit, miss = 0, prompt
        elif miss is None and hit is not None and prompt is not None:
            derived = prompt - hit
            miss = derived if derived >= 0 else None
        hits.append(hit)
        misses.append(miss)
    return (
        _sum_measured(hits),
        _sum_measured(misses),
        _sum_measured([turn["completion_tokens"] for turn in turns]),
    )


def _assistant_tool_echo(message: dict[str, Any]) -> dict[str, Any]:
    """The assistant turn to send BACK on the next request: the provider's own calls, verbatim.

    `content` is normalized to "" when the provider sent null (the documented shape of a
    tool-calling turn) and `reasoning_content`, when the provider sent any, rides back IN FULL —
    with `tools` in play the API rejects a later request whose tool conversation dropped it, so
    that echo is a protocol requirement rather than a nicety.
    """
    content = message.get("content")
    echo: dict[str, Any] = {
        "role": "assistant",
        "content": content if isinstance(content, str) else "",
        "tool_calls": [call for call in (message.get("tool_calls") or [])],
    }
    reasoning = message.get("reasoning_content")
    if isinstance(reasoning, str) and reasoning:
        echo["reasoning_content"] = reasoning
    return echo


def _execute_tool_calls(
    calls: Any, *, agent_type: str, url: str, task_title: str | None = None,
    run_id: str | None = None, replay: dict[str, dict[str, Any] | None] | None = None,
    attempt: int | None = None, journal: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Run every call of ONE tool_calls turn -> (`role:"tool"` messages, names that reached it).

    The registry is asked to execute and is trusted to REFUSE BY NAME rather than throw, so its
    payload becomes the tool result VERBATIM. Three conditions it never sees are named here — and
    none of them may crash the run or fabricate a result:

      * malformed arguments -> `E_TOOL_ARGS`, and the tool is NOT executed;
      * a throwing `execute_tool` (a seam violation) -> `E_TOOL_EXEC` with the exception type;
      * a non-`str` return -> `E_TOOL_CONTRACT` (the value is dropped, never coerced).

    A STRUCTURALLY broken call — one that cannot be answered with a `tool_call_id` — is
    off-contract, so it raises `E_CLI_JSON` instead of being guessed at.

    F-13 (§4.13): `replay` is the `call_key -> recorded action` index of what an EARLIER attempt of
    this task already did.  A call whose key is in it is NOT executed: the recorded result is
    handed back to the model (so the conversation continues correctly) and journalled as
    `replayed`.  A stored record that cannot be replayed RAISES
    `E_ACTION_REPLAY_UNAVAILABLE` — re-sending an effect that may already exist is the defect,
    so the only safe answer is a named refusal.

    `journal` is the SINK the records are appended to (one per call this turn executed or
    replayed), which is what the worker persists.  It is an out-parameter rather than a third
    return value on purpose: this function's two-value return is pinned by every pre-F-13 caller
    and executor double in the tree (`tests/unit/test_v25_vision.py`,
    `test_v26_run_context.py::test_the_run_id_reaches_execute_tool_through_the_runtime_seam`),
    and THE TREE WINS over the packet's three-value sketch.  A caller with no sink pays nothing:
    the records are built into a local list and dropped.

    V26/L3: `run_id` is the calling RUN's id (an `agent_runs.id` as text, or any caller-supplied
    id). It rides the SAME frozen signature as a keyword-only, defaulted addition and is
    forwarded to `execute_tool` ONLY when the run HAS one — a run without an id (and every
    pre-V26 executor double) sees the pre-V25 signature, and the registry keeps the shared
    workspace root byte-identical. The id is validated by the registry before it becomes a path
    segment; the runtime never builds a path from it.
    """
    tool_messages: list[dict[str, Any]] = []
    reached: list[str] = []
    records: list[dict[str, Any]] = journal if journal is not None else []
    registry = _tool_registry()
    for call in calls:
        if not isinstance(call, dict):
            raise PolsiaError(
                E_CLI_JSON,
                "deepseek response message.tool_calls carries a non-object entry",
                how_to_fix="check the requested model accepts chat completions; retry once, then report",
                context={"url": url, "runtime": "deepseek"},
            )
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            raise PolsiaError(
                E_CLI_JSON,
                "deepseek response tool call carries no usable id (its result could not be "
                "attributed to the call)",
                how_to_fix="check for a proxy/portal in front of api.deepseek.com; retry once, then report",
                context={"url": url, "runtime": "deepseek"},
            )
        function = call.get("function")
        if not isinstance(function, dict):
            raise PolsiaError(
                E_CLI_JSON,
                "deepseek response tool call carries no function object",
                how_to_fix="check the requested model accepts chat completions; retry once, then report",
                context={"url": url, "runtime": "deepseek"},
            )
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            raise PolsiaError(
                E_CLI_JSON,
                "deepseek response tool call carries no function name",
                how_to_fix="check the requested model accepts chat completions; retry once, then report",
                context={"url": url, "runtime": "deepseek"},
            )
        name = name.strip()
        arguments, malformed = _tool_arguments(function.get("arguments"))
        key = call_key(name, arguments) if malformed is None else ""
        if key and replay is not None and key in replay:
            # §4.13: this exact call was already performed by an earlier attempt of this task.
            # Answering from the record is the point: the effect exists once, the model still gets
            # the result it asked for, and the retry cannot open a second PR.
            text = _replayed_result(replay[key], key=key, tool=name, url=url)
            # The call reached the JOURNAL, and its effect exists in the world, so the name belongs
            # in `tools_used`: a retry-decision that read "no tool ran" here would be exactly the
            # blind seam A2A4-01 is about.
            reached.append(name)
            records.append(
                _action_record(tool=name, arguments=arguments, result=text, status="replayed",
                               run_id=run_id, attempt=attempt)
            )
            logger.info("deepseek runtime: %s replayed from an earlier attempt's record", name)
        elif malformed is not None:
            # Garbage arguments are the MODEL's mistake: it is told, by name, and gets its
            # bounded turns to fix it. Nothing is executed from a shape we cannot read.
            text = _tool_refusal("E_TOOL_ARGS", f"{name}: {malformed}; the tool was NOT executed")
        else:
            # It REACHED the executor — which is what the run record's "ran" means — whether the
            # registry then refused it or a seam violation blew up in our face.
            reached.append(name)
            # V25/L1: the run's task title rides the SAME frozen signature as a keyword-only
            # addition, and only when the run HAS one — a title-less run calls `execute_tool`
            # with the pre-V25 signature, so an older executor double (or a registry from a
            # build that predates `task_title`) keeps working unchanged.
            tool_kwargs: dict[str, Any] = {"agent_type": agent_type}
            if isinstance(task_title, str) and task_title.strip():
                tool_kwargs["task_title"] = task_title.strip()
            # V26/L3: the run's id rides the same additive rule — forwarded only when present,
            # so an id-less run keeps the pre-V25 executor signature exactly.  A non-string id
            # is forwarded on purpose: `execute_tool` owns the E_VALIDATION refusal for it, and
            # silently dropping it would run the tool at the wrong (shared) root.
            if run_id is not None and (not isinstance(run_id, str) or run_id.strip()):
                tool_kwargs["run_id"] = run_id
            try:
                text = registry.execute_tool(name, arguments, **tool_kwargs)
            except Exception as exc:  # noqa: BLE001 - the seam says "never throws"; a violation is NAMED, never fatal
                logger.warning(
                    "deepseek runtime: execute_tool(%s) raised %s", name, type(exc).__name__
                )
                text = _tool_refusal(
                    "E_TOOL_EXEC", f"{name}: the tool executor raised {type(exc).__name__}: {exc}"
                )
            else:
                if not isinstance(text, str):
                    text = _tool_refusal(
                        "E_TOOL_CONTRACT",
                        f"{name}: execute_tool returned {type(text).__name__}, expected str "
                        "(the value was dropped, not coerced)",
                    )
            logger.info("deepseek runtime: tool %s -> %d chars of result", name, len(text))
            records.append(
                _action_record(tool=name, arguments=arguments, result=text, status="executed",
                               run_id=run_id, attempt=attempt)
            )
        tool_messages.append({"role": "tool", "tool_call_id": call_id, "content": text})
    return tool_messages, reached


def _post_completion(
    *,
    http: httpx.Client,
    url: str,
    headers: dict[str, str],
    body: dict[str, Any],
    timeout: httpx.Timeout,
    timeout_s: int,
    key: str,
    model_id: str,
    started: float,
    num_turns: int,
    usage: dict[str, Any] | None,
    extra: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, claude_cli.InvokeResult | None]:
    """ONE chat-completions exchange: (`payload`, None) on a documented 200, else (None, failure).

    Raises the same named refusals the single-call runtime raised (`E_CLI_TIMEOUT` for the wall
    clock, `E_VALIDATION` for a request that cannot be sent at all, `E_CLI_JSON` for a 200 body
    that is not the documented completion shape). A transport fault or a non-200 status comes
    back as a FAILURE ENVELOPE exactly as before — carrying the loop's call count and the usage
    measured so far, so a mid-loop failure cannot lose the tokens already spent.

    A2A4-01: `extra` carries the loop's tool evidence (`tools_used` + the action list) on those
    two failure envelopes.  They are exactly the failures the retry ladder retries, and until this
    argument existed a tool run that ended in a 429 or a transport fault was indistinguishable
    from a tool-less one at the retry seam.
    """
    try:
        # Same process-wide bound as the CLI path (claude_cli.concurrency_gate): the knob that
        # matters is how many agent runs THIS worker may have in flight. DeepSeek's own limit
        # (2500) is orders of magnitude above anything a Polsia worker fleet sends, so reusing
        # the existing gate keeps one bound instead of inventing a second, dead setting.
        with claude_cli.concurrency_gate():
            response = http.post(url, json=body, headers=headers, timeout=timeout)
    except httpx.TimeoutException:
        # Mirror claude_cli.invoke's TimeoutExpired branch exactly: a raised E_CLI_TIMEOUT is
        # what claude_cli.run_with_retries retries on the next attempt.
        raise PolsiaError(
            E_CLI_TIMEOUT,
            f"deepseek API exceeded {timeout_s}s (request aborted; the turn is unfinished)",
            # F-18: `timeout_s` is the SMALLER of this run's own deadline and the provider-wide
            # setting, so the remedy names both instead of promising that the setting alone helps.
            how_to_fix=(
                f"this call was bounded at {timeout_s}s — the smaller of this run's own deadline "
                "(Invocation.timeout_s, set by the agent's timeout) and deepseek_timeout_seconds "
                "in Settings; raise deepseek_timeout_seconds, or shrink the task"
            ),
            context={"timeout_s": timeout_s, "url": url, "runtime": "deepseek"},
        ) from None
    except httpx.TransportError as exc:
        # Connect/read/write faults never reached a model: transient network is retryable, and
        # the `api_error` marker in the subtype is what tells the existing ladder so.
        return None, _failure_result(
            subtype="error_api_error",
            detail=f"deepseek transport error: {type(exc).__name__}: {exc}",
            started=started,
            model=model_id,
            api_status=None,
            num_turns=num_turns,
            usage=usage,
            extra=extra,
        )
    except (ValueError, httpx.InvalidURL) as exc:
        # httpx raises a plain ValueError / InvalidURL for URL shapes it cannot build a request
        # from at all (`http://` with no host, a non-numeric port). That is a configuration fault
        # (RD13): name it instead of letting a raw httpx exception escape the runtime.
        raise PolsiaError(
            E_VALIDATION,
            f"deepseek request could not be sent: {type(exc).__name__}: {exc}",
            how_to_fix="check DEEPSEEK_BASE_URL (documented value: https://api.deepseek.com)",
            context={"url": url, "runtime": "deepseek"},
        ) from None

    if response.status_code != 200:
        return None, _failure_result(
            subtype=_subtype_for_status(response.status_code),
            detail=_redact(
                f"deepseek HTTP {response.status_code}: {_body_snippet(response) or 'no body'}",
                key,
            ),
            started=started,
            model=model_id,
            api_status=response.status_code,
            num_turns=num_turns,
            usage=usage,
            extra=extra,
        )

    try:
        payload = response.json()
    except ValueError:
        raise PolsiaError(
            E_CLI_JSON,
            _redact(
                f"deepseek API returned HTTP 200 with a non-JSON body: "
                f"{_body_snippet(response, 200)!r}",
                key,
            ),
            how_to_fix="check for a proxy/portal in front of api.deepseek.com; retry once, then report",
            context={"url": url, "runtime": "deepseek"},
        ) from None
    if not isinstance(payload, dict):
        raise PolsiaError(
            E_CLI_JSON,
            f"deepseek API returned HTTP 200 with a JSON {type(payload).__name__}, expected an object",
            how_to_fix="check for a proxy/portal in front of api.deepseek.com; retry once, then report",
            context={"url": url, "runtime": "deepseek"},
        )
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        raise PolsiaError(
            E_CLI_JSON,
            "deepseek response carries no choices[0] object (not the documented completion shape)",
            how_to_fix="check for a proxy/portal in front of api.deepseek.com; retry once, then report",
            context={"url": url, "runtime": "deepseek"},
        )
    return payload, None


# ── invocation -> request plan ───────────────────────────────────────────────────────────────

@dataclass
class InvocationPlan:
    """What one agent run means to the chat-completions API, recovered from an `Invocation`."""

    prompt: str
    system_prompt: str = ""
    json_schema: dict | None = None
    resume_session_id: str | None = None


def _flag_arg(argv: list[str], flag: str) -> str | None:
    """The argument after `flag` in the CLI argv (first occurrence), or None."""
    try:
        idx = argv.index(flag)
    except ValueError:
        return None
    return argv[idx + 1] if idx + 1 < len(argv) else None


def plan_from_invocation(inv: claude_cli.Invocation) -> InvocationPlan:
    """Recover (prompt, system prompt, JSON schema, resume id) from the SAME `Invocation`
    `claude_cli.build_invocation` produces.

    The RD8 contract is that this runtime accepts the unchanged Invocation, so the only argv
    shapes parsed here are the ones that builder emits: `-p <prompt>`, `--json-schema <compact>`,
    `--append-system-prompt[-file]` (static rides `system_prompt_file_content`), `--resume <id>`.
    """
    if inv.transport == "stdin" and inv.stdin_text is not None:
        prompt = inv.stdin_text
    elif inv.transport == "file-ref" and inv.prompt_file:
        try:
            prompt = Path(inv.prompt_file).read_text(encoding="utf-8")
        except OSError as exc:
            raise PolsiaError(
                E_VALIDATION,
                f"deepseek runtime: prompt file {inv.prompt_file!r} is unreadable: {exc}",
                how_to_fix="write the spill file the invocation references, or use transport='argv'",
                context={"prompt_file": inv.prompt_file, "transport": inv.transport},
            ) from exc
    else:
        prompt = _flag_arg(inv.argv, "-p") or ""
    if not prompt or not prompt.strip():
        raise PolsiaError(
            E_VALIDATION,
            "deepseek runtime: could not recover the prompt from the invocation "
            "(-p <prompt> / stdin / file-ref all empty)",
            how_to_fix=ERROR_HINTS[E_VALIDATION],
            context={"transport": inv.transport},
        )

    schema: dict | None = None
    raw_schema = _flag_arg(inv.argv, "--json-schema")
    if raw_schema:
        try:
            parsed = json.loads(raw_schema)
        except ValueError as exc:
            raise PolsiaError(
                E_VALIDATION,
                f"deepseek runtime: --json-schema payload is not JSON: {exc}",
                how_to_fix=ERROR_HINTS[E_VALIDATION],
            ) from exc
        if not isinstance(parsed, dict):
            raise PolsiaError(
                E_VALIDATION,
                "deepseek runtime: --json-schema payload is not a JSON object",
                how_to_fix=ERROR_HINTS[E_VALIDATION],
            )
        schema = parsed

    system_prompt = (
        inv.system_prompt_file_content
        or _flag_arg(inv.argv, "--append-system-prompt")
        or ""
    )
    return InvocationPlan(
        prompt=prompt,
        system_prompt=system_prompt,
        json_schema=schema,
        resume_session_id=_flag_arg(inv.argv, "--resume"),
    )


# ── session replay (bounded, in-process) ─────────────────────────────────────────────────────

def has_session(session_id: str | None) -> bool:
    """F-23: is `session_id` replayable in THIS process right now?

    The repair ladder's R1 re-prompt is a correction of the previous answer; the runtime serves it
    by replaying the stored exchange. When the id is unknown (worker restart, eviction under the
    byte budget, a session minted by another provider) `_session_messages` logs and runs FRESH — so
    the provider would receive the corrective text as the WHOLE task and answer from nothing. This
    predicate is how a caller asks BEFORE paying for that call. It never mutates the store or the
    LRU ordering: a question must not change the answer.
    """
    if not session_id:
        return False
    with _SESSION_LOCK:
        return session_id in _SESSIONS


def _session_messages(session_id: str | None) -> list[dict[str, Any]] | None:
    """The stored exchange for `session_id`, or None (unknown -> honest fresh call, logged)."""
    if not session_id:
        return None
    with _SESSION_LOCK:
        history = _SESSIONS.get(session_id)
        if history is None:
            logger.warning(
                "deepseek runtime: --resume %s is not in this process's session store "
                "(worker restart, or a session from another provider); running fresh",
                session_id,
            )
            return None
        _SESSIONS.move_to_end(session_id)
        return [dict(message) for message in history]


def _session_bytes(history: list[dict[str, Any]]) -> int:
    """The exchange's approximate in-process size: JSON text length (images dominate it)."""
    try:
        return len(json.dumps(history, ensure_ascii=False, default=str))
    except (TypeError, ValueError):  # pragma: no cover - every part is JSON by construction
        return sum(len(str(part)) for part in history)


def _store_exchange(
    session_id: str | None, messages: list[dict[str, Any]], assistant_text: str
) -> str:
    """Persist (system,user,assistant) under `session_id` (new id when absent); bounded LRU.

    Bounded TWICE — by count (`SESSION_STORE_LIMIT`) and by bytes (`SESSION_STORE_MAX_BYTES`) —
    because a vision exchange carries the images inline.  At least ONE session always survives:
    evicting the exchange a repair is about to read would turn a bounded store into a silent
    "unknown session" for the very run that just wrote it.
    """
    sid = session_id or f"deepseek-{uuid.uuid4().hex[:16]}"
    history = [dict(message) for message in messages] + [
        {"role": "assistant", "content": assistant_text}
    ]
    size = _session_bytes(history)
    with _SESSION_LOCK:
        _SESSIONS[sid] = history
        _SESSIONS.move_to_end(sid)
        _SESSION_BYTES[sid] = size
        while len(_SESSIONS) > SESSION_STORE_LIMIT:
            old_sid, _ = _SESSIONS.popitem(last=False)
            _SESSION_BYTES.pop(old_sid, None)
        total = sum(_SESSION_BYTES.values())
        while len(_SESSIONS) > 1 and total > SESSION_STORE_MAX_BYTES:
            old_sid, _ = _SESSIONS.popitem(last=False)
            total -= _SESSION_BYTES.pop(old_sid, 0)
    return sid


def _reset_session_store() -> None:
    """Test hook: drop every stored exchange (the store is process-local by design)."""
    with _SESSION_LOCK:
        _SESSIONS.clear()
        _SESSION_BYTES.clear()


# ── request construction ─────────────────────────────────────────────────────────────────────

def _model_for(override: str | None) -> str:
    if override and override.strip():
        return override.strip()
    value = str(_setting("deepseek_model", DEEPSEEK_DEFAULT_MODEL) or "").strip()
    return value or DEEPSEEK_DEFAULT_MODEL


def _reasoning_effort() -> str:
    """Canonical reasoning_effort from Settings; unknown values are a NAMED refusal, not coerced."""
    raw = str(_setting("deepseek_reasoning_effort", DEEPSEEK_DEFAULT_REASONING_EFFORT) or "").strip().lower()
    value = REASONING_EFFORT_ALIASES.get(raw, raw) or DEEPSEEK_DEFAULT_REASONING_EFFORT
    if value not in REASONING_EFFORT_CANONICAL:
        raise PolsiaError(
            E_VALIDATION,
            f"deepseek reasoning_effort {raw!r} is not one of {'|'.join(REASONING_EFFORT_CANONICAL)} "
            "(documented aliases: minimal, medium, xhigh)",
            how_to_fix="set DEEPSEEK_REASONING_EFFORT to none|low|high|max",
            context={"value": raw},
        )
    return value


def _timeout_seconds() -> int:
    value = _setting("deepseek_timeout_seconds", DEEPSEEK_DEFAULT_TIMEOUT_SECONDS)
    try:
        timeout = int(value)
    except (TypeError, ValueError):
        timeout = DEEPSEEK_DEFAULT_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEEPSEEK_DEFAULT_TIMEOUT_SECONDS


def _call_timeout_seconds(inv: claude_cli.Invocation) -> int:
    """The wall clock for ONE provider call: the invocation's own bound, capped by the setting.

    F-18: `base_agent` sets `Invocation.timeout_s` (its `self.timeout`, the CLI's wall clock) and
    this path read only the provider-wide `deepseek_timeout_seconds`, so a caller that asked for
    7 s got 300 s — and a 4-call tool loop got 4 x 300 s inside one Celery task whose soft limit
    is 840 s.  `min` keeps every shipped run byte-identical (invocation 300, setting 300 -> 300)
    while honouring a caller that asked for less; the setting stays the DEFAULT for an invocation
    that carries none.  An unparseable or non-positive value is the setting, never an unbounded
    wait — a zero deadline would mean "no deadline" to httpx, the opposite of a bound.
    """
    configured = _timeout_seconds()
    try:
        requested = int(inv.timeout_s)
    except (TypeError, ValueError):
        return configured
    return max(1, min(configured, requested)) if requested > 0 else configured


def _max_tokens() -> int:
    """The per-call output bound (L3-04): settings override when present, else the default.

    Same forward-safe `_setting` pattern as the other `deepseek_*` knobs: nothing breaks while
    the Settings field is absent, and the same call reads the configured value once it lands.
    A non-positive / non-integer override falls back to the default — a 0 here would mean
    "unbounded" to some providers, the opposite of the bound's purpose.
    """
    value = _setting("deepseek_max_tokens", DEEPSEEK_DEFAULT_MAX_TOKENS)
    try:
        bound = int(value)
    except (TypeError, ValueError):
        bound = DEEPSEEK_DEFAULT_MAX_TOKENS
    return bound if bound > 0 else DEEPSEEK_DEFAULT_MAX_TOKENS


def _base_url() -> str:
    """The configured base URL, validated UP FRONT (RD13).

    A value without a scheme (`api.deepseek.com` — a plausible copy/paste of the docs host) only
    fails deep inside httpx, as a raw `ValueError` that escapes `invoke` and the retry ladder
    (which re-raises anything that is not `PolsiaError`/`E_CLI_TIMEOUT`) into the worker as an
    unhandled exception. A configuration fault must be a NAMED refusal at the boundary that reads
    the configuration, so the scheme and the host are checked here.
    """
    value = str(_setting("deepseek_base_url", DEEPSEEK_DEFAULT_BASE_URL) or "").strip().rstrip("/")
    value = value or DEEPSEEK_DEFAULT_BASE_URL
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise PolsiaError(
            E_VALIDATION,
            f"deepseek base url {value!r} is not a usable http(s) URL "
            f"(scheme {parsed.scheme!r}, host {parsed.netloc!r})",
            how_to_fix="set DEEPSEEK_BASE_URL to the documented https://api.deepseek.com",
            context={"value": value},
        )
    return value


def _request_headers(api_key: str) -> dict[str, str]:
    """Auth + content type for the call. The key value is never logged, echoed or stored."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _messages_for(
    plan: InvocationPlan,
    replay: list[dict[str, Any]] | None,
    image_parts: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The chat messages: a replayed exchange + the new turn, or a fresh system+user pair.

    The JSON instruction is added ONLY on a fresh exchange (the replayed system message already
    carries it — the repair ladder keeps one conversation per logical run).

    VISION (V25/L1): the validated `image_url` parts ride the FRESH user turn.  On a replay they
    are already inside the stored exchange, so the new turn is a plain string — re-sending them
    would double the payload and duplicate the conversation the model is being asked to repair.
    """
    if replay:
        return [*replay, {"role": "user", "content": plan.prompt}]
    system_text = plan.system_prompt or ""
    if plan.json_schema is not None:
        instruction = _JSON_INSTRUCTION.format(
            schema=json.dumps(plan.json_schema, separators=(",", ":"))
        )
        system_text = f"{system_text}\n\n{instruction}".strip() if system_text else instruction
    messages: list[dict[str, Any]] = []
    if system_text:
        messages.append({"role": "system", "content": system_text})
    if image_parts:
        messages.append(
            {
                "role": "user",
                "content": [{"type": "text", "text": plan.prompt}, *image_parts],
            }
        )
    else:
        messages.append({"role": "user", "content": plan.prompt})
    return messages


def _build_body(
    plan: InvocationPlan,
    *,
    model: str,
    messages: list[dict[str, Any]],
    tools: list[dict] | None = None,
) -> dict[str, Any]:
    """The exact request body; `messages` is resolved ONCE by the caller (replay included), so
    the exchange stored after success is byte-identical to the one actually sent.

    F-22 — the body is a function of the CHANNEL. `response_format={"type":"json_object"}` and a
    `tools` array are mutually exclusive BY CONSTRUCTION: a tool-enabled request never carries
    `response_format`, and a structured request never carries `tools`. Sourcing, stated honestly:
    the vendor page (https://api-docs.deepseek.com/api/create-chat-completion, fetched 2026-10-25)
    documents the two fields INDEPENDENTLY and does NOT state that they compose — an explicit
    rejection statement for the pair is UNVERIFIED, so this code does not rely on one. What the
    page DOES require is that JSON output also be instructed in a system/user message; that is why
    the instruction (below) must survive the omission, and it does.

    The JSON contract is not lost on a tool run: `_messages_for` puts the schema instruction in the
    system turn whenever `plan.json_schema` is set, which is the instruction the provider's own docs
    require in every JSON request. `response_format` is the belt, not the braces.
    """
    effort = _reasoning_effort()
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
        # L3-04: the output bound — the request is capped at the call, so one runaway
        # generation cannot spend more than bound x output-rate (the dispatch gate then sees
        # a MEASURED cost instead of a NULL column).
        "max_tokens": _max_tokens(),
    }
    if effort == "none":
        # Deterministic agent JSON runs want no hidden reasoning pass; `none` IS the switch.
        body["thinking"] = {"type": "disabled"}
    else:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = effort
    if tools:
        body["tools"] = list(tools)
    elif plan.json_schema is not None:
        body["response_format"] = {"type": "json_object"}
    return body


# ── result envelopes (the claude_cli contract, mirrored) ─────────────────────────────────────

def _failure_result(
    *,
    subtype: str,
    detail: str,
    started: float,
    model: str,
    api_status: int | None,
    exit_code: int = 1,
    num_turns: int = 1,
    usage: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> claude_cli.InvokeResult:
    """A failed run returned NORMALLY (claude CLI D2: the envelope on stdout carries the failure).

    base_agent._classify_invoke reads `is_error`/`subtype`, `classify_error_envelope` reads
    `api_error_status`, and `_retryable` feeds `subtype`+`api_error_status`+`exit_code` to
    `claude_cli.is_retryable_failure` — all of it works unchanged off this shape.

    `num_turns`/`usage`/`extra` exist for the TOOL LOOP (V12): a run that fails AFTER executing
    tool turns reports how many provider calls it made and the tokens those calls measured,
    instead of the single-call defaults. Every default here reproduces the pre-loop envelope
    byte for byte, so the tool-less paths are unchanged.
    """
    elapsed = time.monotonic() - started
    envelope = {
        "type": "result",
        "subtype": subtype,
        "is_error": True,
        "result": detail,
        "api_error_status": api_status,
        "num_turns": num_turns,
        "duration_ms": int(elapsed * 1000),
        "model": model,
        "usage": usage if usage is not None else {},
        "provider": "deepseek",
    }
    if extra:
        envelope.update(extra)
    return claude_cli.InvokeResult(
        stdout=json.dumps(envelope, ensure_ascii=False),
        stderr=detail[:500],
        exit_code=exit_code,
        wall_seconds=elapsed,
        transport="http",
    )


def _body_snippet(response: httpx.Response, limit: int = 400) -> str:
    text = (response.text or "").strip()
    return text[:limit] if len(text) > limit else text


def _usage_int(usage_raw: dict[str, Any], key: str) -> int | None:
    """A token measurement from the provider's `usage` object, or None (UNMEASURED).

    The docs type these fields as integers; this function is the boundary against whatever a
    provider/proxy actually sent. A non-integer (string, list, dict, bool) would ride into the
    envelope and crash the consumer — `accounting._int_or_none` runs `int(value)` inside
    `base_agent._finish_call` on an otherwise-SUCCESSFUL run (`int("n/a")` -> ValueError). So a
    non-integer collapses to None (absent measurement, per the NULL-vs-zero rule) — never a
    coerced value and never a fabricated 0.
    """
    value = usage_raw.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _redact(text: str, secret: str) -> str:
    """Defense in depth: no envelope this runtime emits may contain the key verbatim.

    A broken or hostile endpoint can echo the Authorization header back in an error body; that
    snippet becomes `result`/`stderr` on the failure envelope and, via `base_agent._finish_call`,
    the recorded run error. Scrub it exactly like `secret_store.verify_deepseek_key` scrubs its
    details.
    """
    if secret and secret in text:
        return text.replace(secret, "(key hidden)")
    return text


# ── invoke ───────────────────────────────────────────────────────────────────────────────────

def invoke(
    inv: claude_cli.Invocation,
    *,
    client: httpx.Client | None = None,
    api_key: str | None = None,
    model: str | None = None,
    tools: list[dict] | None = None,
    agent_type: str | None = None,
    task_title: str | None = None,
    images: list[dict[str, Any]] | None = None,
    run_id: str | None = None,
    replay: list[dict[str, Any]] | None = None,
    attempt: int | None = None,
) -> claude_cli.InvokeResult:
    """Run one DeepSeek chat completion for `inv`; same shape as `claude_cli.invoke`.

    Spawn-level problems RAISE a named refusal (`E_DEEPSEEK_KEY`, `E_CLI_TIMEOUT`, `E_CLI_JSON`);
    a failed provider run returns normally with the failure envelope on stdout (mirroring D2).
    `client` is injectable for tests (`httpx.MockTransport`); production builds a per-call client
    (one HTTP call per agent run, so pooling buys nothing against multi-second model latency, and
    per-call lifetime keeps no state between runs).

    TOOL CALLING (V12): the CALLING AGENT's tool list decides everything. Empty (today's
    `tool_less` argv, or an explicitly empty `tools=`) -> the request and the reply contract are
    byte-identical to the pre-loop runtime. Non-empty -> `TOOL_SPECS` ride the request, every
    `tool_calls` turn is executed through the FROZEN registry seam and answered with a `role:"tool"`
    message, and the loop is bounded at `TOOL_LOOP_MAX_ITERATIONS` provider calls (a run still
    asking for tools there refuses BY NAME). `tools=` / `agent_type=` override the derivation for
    a caller that does not come through `base_agent`. A tool-enabled run also opens exactly ONE
    `tool_registry.begin_run_tool_budget()` scope for its own metered provider calls and tool-path
    sends, closed in a `finally` (F-33/S2-R2) — a tool-less run never opens one.

    V26/L3: `run_id` is the calling run's identity (`agent_runs.id` as text). It is OPTIONAL,
    keyword-only and defaulted, and is forwarded through the tool loop to
    `execute_tool(run_id=...)` only when present; the three code tools then scope their
    workspace to `<settings.code_workspace_dir>/runs/<id>/`. Absent, the run reads and writes
    the shared workspace root exactly as before (byte-identical request/result shapes).

    F-13 (§4.13): `replay` is the action list a PREVIOUS attempt of this task recorded
    (`tasks.metadata["actions"]`, read by `base_agent`).  A tool call whose `call_key` is in it is
    answered from the record instead of being executed again, and a record that cannot be
    replayed raises `E_ACTION_REPLAY_UNAVAILABLE`.  Omitted/empty, this runtime behaves exactly as
    it did before (the request body never carries the list; only the envelope gains `actions`, on
    the same presence rule as `tools_used`).  `attempt` is the task's execution number, recorded on
    each action record for audit.
    """
    key = (api_key if api_key is not None else _resolve_api_key() or "").strip() or ""
    if not key:
        raise PolsiaError(
            E_DEEPSEEK_KEY,
            "deepseek runtime: no API key is configured (neither stored in Settings nor in "
            "DEEPSEEK_API_KEY)",
            how_to_fix=ERROR_HINTS[E_DEEPSEEK_KEY],
            context={"runtime": "deepseek"},
        )
    plan = plan_from_invocation(inv)
    # Resolved ONCE per logical run: the same specs ride every request of the loop, and `[]`
    # keeps the run on the pre-loop path (no `tools` key, no extra envelope keys, one call).
    tool_specs = _tool_specs_for(inv, tools)
    # The registry needs to know WHO is calling to enforce its allowlist; base_agent already
    # carries the agent's identity in `schema_name`, and a direct caller can be explicit.
    tool_agent_type = (agent_type if agent_type is not None else inv.schema_name or "").strip()
    model_id = _model_for(model)
    # F-18: the invocation's own wall clock bounds each provider call (capped by the setting), so
    # a caller that asked for 7 s is not held for the provider-wide 300 s — in every call of a
    # tool loop, not just the first.
    timeout_s = _call_timeout_seconds(inv)
    url = f"{_base_url()}{CHAT_COMPLETIONS_PATH}"
    messages = _messages_for(
        plan,
        replay=_session_messages(plan.resume_session_id),
        image_parts=_image_parts(images),
    )
    body = _build_body(plan, model=model_id, messages=messages, tools=tool_specs or None)
    headers = _request_headers(key)
    timeout = httpx.Timeout(timeout_s)

    started = time.monotonic()
    own_client = client is None
    http = client if client is not None else httpx.Client(timeout=timeout)
    # Loop state: every provider call's measurements (summed into the envelope), the tool names
    # that reached the executor (metadata ONLY), and the call count (accounting + the bound).
    turns: list[dict[str, int | None]] = []
    tools_used: list[str] = []
    #: F-13: this attempt's action records.  Same presence rule as `tools_used` — absent means the
    #: run had no tools at all, `[]` means tools were offered and none ran.
    journal: list[dict[str, Any]] = []
    stored_actions = replay_index(replay)
    provider_calls = 0
    # F-05: the bound is read ONCE per run (a setting that changes mid-run must not move the
    # finish line under the loop), clamped by `_tool_loop_bound`.
    loop_bound = _tool_loop_bound()
    # F-33 (S2-R2): the per-run tool budget had NO producer — `begin_run_tool_budget()` existed and
    # was called ONLY by tests, so every real run read `None`: nothing was metered, and this run's
    # own tool-path sends were never charged against a channel cap.  ONE budget is opened for this
    # run (the metered provider-call count + the per-channel sends this run really performs) and
    # closed in the `finally` below, so a later run in the same worker context starts from zero.
    # Only a run that can EXECUTE tools opens one: a tool-less run performs no tool call, and its
    # path stays byte-identical to the pre-F-33 runtime.
    budget_api = _tool_budget_api() if tool_specs else None
    tool_budget = budget_api.begin_run_tool_budget() if budget_api is not None else None
    try:
        while True:
            provider_calls += 1
            payload, failure = _post_completion(
                http=http,
                url=url,
                headers=headers,
                body=body,
                timeout=timeout,
                timeout_s=timeout_s,
                key=key,
                model_id=model_id,
                started=started,
                num_turns=provider_calls,
                usage=(_accumulated_usage(turns) if tool_specs else None),
                # A2A4-01 + F-13: what the loop had ALREADY done when this call failed rides the
                # failure envelope, because a failed run stores no output and this envelope is
                # what the next attempt replays.
                extra=_loop_evidence(tool_specs, tools_used, journal) or None,
            )
            if failure is not None:
                return failure
            # `_post_completion` returns exactly ONE of (payload, failure); mypy cannot read that
            # off the tuple type — this can never fire.
            assert payload is not None

            choice = payload["choices"][0]
            message_raw = choice.get("message")
            message = message_raw if isinstance(message_raw, dict) else {}
            content_raw = message.get("content")
            finish_reason = str(choice.get("finish_reason") or "")
            usage_scalar = payload.get("usage")
            turns.append(_turn_usage(usage_scalar if isinstance(usage_scalar, dict) else {}))

            calls = message.get("tool_calls")
            if tool_specs and (finish_reason == "tool_calls" or (isinstance(calls, list) and calls)):
                if not isinstance(calls, list) or not calls:
                    # The reply CLAIMED tool calls and supplied none. There is nothing to execute,
                    # and re-asking the identical request would only repeat the same answer — so
                    # this is off-contract, refused by name (mirroring the tool-LESS null-content
                    # rule) instead of burning the loop's bounded turns.
                    raise PolsiaError(
                        E_CLI_JSON,
                        "deepseek response finished with finish_reason='tool_calls' but carries "
                        "no tool calls",
                        how_to_fix="check for a proxy/portal in front of api.deepseek.com; retry once, then report",
                        context={"url": url, "runtime": "deepseek"},
                    )
                if provider_calls >= loop_bound:
                    # The bound is reached WITH another tool request pending: refuse by name. The
                    # capped turn's calls are NOT executed — nothing ran for them, so nothing may
                    # be reported as if it had.
                    requested = _requested_tool_names(calls)
                    logger.warning(
                        "deepseek runtime: tool loop bound (%d provider calls) reached; refusing "
                        "by name, tools requested in the last turn: %s",
                        loop_bound,
                        ", ".join(requested) or "none named",
                    )
                    return _failure_result(
                        subtype="error_tool_loop",
                        detail=(
                            f"E_TOOL_LOOP: the model was still requesting tools after "
                            f"{provider_calls} provider calls (bound {loop_bound}); "
                            f"the last turn asked for "
                            f"{', '.join(requested) if requested else 'an unnamed tool'}. The run "
                            "is refused by name — no final answer exists, and none may be "
                            "reported as one."
                        ),
                        started=started,
                        model=model_id,
                        api_status=None,
                        num_turns=provider_calls,
                        usage=_accumulated_usage(turns),
                        extra=_loop_evidence(tool_specs, tools_used, journal),
                    )
                tool_messages, reached = _execute_tool_calls(
                    calls, agent_type=tool_agent_type, url=url, task_title=task_title,
                    run_id=run_id, replay=stored_actions, attempt=attempt, journal=journal,
                )
                for name in reached:
                    _record_tool_name(tools_used, name)
                # Protocol: the assistant turn (its own calls, verbatim) followed by ONE
                # `role:"tool"` message per call, each carrying that call's `tool_call_id`.
                messages = [*messages, _assistant_tool_echo(message), *tool_messages]
                body["messages"] = messages
                continue

            if not isinstance(content_raw, str):
                if finish_reason in _INCOMPLETE_FINISH_REASONS:
                    # Stopped before any text was emitted (e.g. mid-reasoning, filtered,
                    # aborted): still an incomplete response, reported as the named failure
                    # below — never E_CLI_JSON, and never a success.
                    content_raw = ""
                else:
                    # Off-contract completion (docs type assistant `content` as string|null): the
                    # content-parts LIST shape is documented for REQUEST messages only, so a
                    # proxy echoing it back is refused by name rather than guessed at. A
                    # `tool_calls` reply with null content lands here whenever this run had NO
                    # tools to call — the pre-loop contract, kept verbatim.
                    raise PolsiaError(
                        E_CLI_JSON,
                        "deepseek response choices[0].message.content is missing or not a string",
                        how_to_fix="check the requested model accepts chat completions; retry once, then report",
                        context={"url": url, "runtime": "deepseek"},
                    )
            content = content_raw
            break
    finally:
        # Close THIS run's tool budget before anything else: the budget is run-scoped state, and a
        # run that leaves it open would meter (and charge) the next run in this context.
        if tool_budget is not None and budget_api is not None:
            budget_api.end_run_tool_budget(tool_budget)
        if own_client:
            http.close()

    # The envelope's `usage` is the SUM over this run's provider calls: a tool turn spends tokens
    # too, so pricing only the last reply would undercount the run. Every field is
    # measured-or-absent across the WHOLE run (`_sum_measured`): one unmeasured turn makes that
    # field None, because a partial sum would be fabrication-precision — the same NULL-vs-zero
    # rule the single-call runtime applied field by field, and the rule
    # `pricing.estimate_cost_usd` applies to the estimate below. A tool-less run has exactly one
    # turn here, so the result is identical to the pre-loop envelope.
    envelope_usage = _accumulated_usage(turns)
    # The PRICING split (L3-04) — separate from the envelope's measured fields. The provider
    # documents `prompt_tokens == prompt_cache_hit_tokens + prompt_cache_miss_tokens`; when a
    # shape is unreported the split is completed the CONSERVATIVE way (the miss rate is always
    # the more expensive side), because a strict NULL here would restore exactly the
    # unmeasured-cost defect this estimate exists to remove:
    #   * hit side unreported -> bill the whole prompt at the miss rate;
    #   * miss side unreported -> miss = prompt - hit (only when consistent).
    # `_price_split` applies that rule PER TURN and then sums (a run-level split could not stay
    # conservative about a turn whose own cache split was unreported).
    price_hit_tokens, price_miss_tokens, price_output_tokens = _price_split(turns)

    incomplete = _INCOMPLETE_FINISH_REASONS.get(finish_reason)
    if incomplete is not None:
        # Truncation — and the adjacent documented shapes (content filter, resource pressure, an
        # aborted generation) — is NOT a success: the JSON (or prose) is incomplete/omitted and
        # nothing may be fabricated from it. `error_max_tokens` (length) stays non-retryable: the
        # same prompt would truncate again; `error_overloaded` (resource pressure) is retryable
        # through the existing marker list.
        subtype, why = incomplete
        return _failure_result(
            subtype=subtype,
            detail=(
                f"deepseek response incomplete: {why} "
                f"(finish_reason={finish_reason}; received {len(content)} chars) — any JSON it "
                "carries is incomplete"
            ),
            started=started,
            model=model_id,
            api_status=None,
            num_turns=provider_calls,
            # A tool run that truncates on its LAST call still spent the earlier turns' tokens:
            # the measurement is carried, and a tool-LESS run keeps the historical empty usage.
            usage=(_accumulated_usage(turns) if tool_specs else None),
            extra=_loop_evidence(tool_specs, tools_used, journal) or None,
        )

    stored_messages = messages
    # `messages` is the RUN'S REAL conversation by now (a tool run's assistant/`tool` turns
    # included), so a later `--resume` replays the exchange that actually happened.
    session_id = _store_exchange(plan.resume_session_id, stored_messages, content)

    model_served = str(payload.get("model") or model_id)
    cost_estimate = pricing.estimate_cost_usd(
        model_served,
        cache_hit_tokens=price_hit_tokens,
        cache_miss_tokens=price_miss_tokens,
        output_tokens=price_output_tokens,
    )

    envelope: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": content,
        "session_id": session_id,
        "num_turns": provider_calls,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "usage": envelope_usage,
        "model": model_served,
        "stop_reason": finish_reason or "stop",
        "provider": "deepseek",
    }
    if tool_specs:
        # The run record's tool evidence: NAMES only, bounded by `_record_tool_name`, so a parent
        # can verify a real tool-calling run without the envelope carrying fetched content or the
        # model's own arguments. `tools_used == []` is a measured zero (tools were available, the
        # model called none) — a key that is merely ABSENT means the run had no tools at all.
        envelope["tools_used"] = list(tools_used)
        # F-13: the run's action list.  Same presence rule as `tools_used`: absent means the run
        # had no tools (byte-identical envelope), `[]` means tools were offered and none ran.
        envelope[ACTION_KEY] = list(journal)
        logger.info(
            "deepseek runtime: tool run finished after %d provider call(s); tools used: %s",
            provider_calls,
            ", ".join(tools_used) or "none",
        )
    if cost_estimate is not None:
        # L3-04: a DISTINCT key — computed from the measured tokens through the pinned table,
        # never `total_cost_usd` (which means the CLI's own money measurement). `accounting`
        # reads it, stamps `cost_source=token_estimate` and the version when persisting.
        envelope["cost_estimate_usd"] = round(cost_estimate, 8)
        envelope["pricing_version"] = pricing.PRICING_VERSION
    if plan.json_schema is not None:
        try:
            candidate = json.loads(content)
        except ValueError:
            candidate = None
        if isinstance(candidate, dict):
            # Primary payload for structured_output.resolve_output; when the content does not
            # parse, `result` still carries the text and the existing ladder repairs it.
            envelope["structured_output"] = candidate

    return claude_cli.InvokeResult(
        stdout=json.dumps(envelope, ensure_ascii=False),
        stderr="",
        exit_code=0,
        wall_seconds=time.monotonic() - started,
        transport="http",
    )


__all__ = [
    "ACTION_KEY",
    "CHAT_COMPLETIONS_PATH",
    "DEEPSEEK_DEFAULT_BASE_URL",
    "DEEPSEEK_DEFAULT_MAX_TOKENS",
    "DEEPSEEK_DEFAULT_MODEL",
    "DEEPSEEK_DEFAULT_REASONING_EFFORT",
    "DEEPSEEK_DEFAULT_TIMEOUT_SECONDS",
    "E_ACTION_REPLAY_UNAVAILABLE",
    "InvocationPlan",
    "MAX_ACTION_ENTRY_BYTES",
    "SESSION_STORE_LIMIT",
    "SESSION_STORE_MAX_BYTES",
    "TOOL_LOOP_MAX_ITERATIONS",
    "TOOL_LOOP_MAX_ITERATIONS_CAP",
    "TOOL_LOOP_MIN_ITERATIONS",
    "VISION_ALLOWED_MIME",
    "VISION_MAX_IMAGE_BYTES",
    "VISION_MAX_TOTAL_BYTES",
    "available",
    "call_key",
    "has_session",
    "invoke",
    "plan_from_invocation",
    "replay_index",
]
