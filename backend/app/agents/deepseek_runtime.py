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
  * `finish_reason == "length"` is truncation and is reported as a FAILURE, never as success;
  * `thinking` / `reasoning_effort`: canonicalized (minimal->low, medium/xhigh->high); effort
    `none` disables thinking (the fleet's deterministic JSON runs want no hidden reasoning pass).

Honesty rules (repo-wide): every failure is a NAMED refusal, a truncated response is never a
success, and no envelope key is fabricated — absent measurements stay absent (NULL-vs-zero rule
in `accounting._int_or_none`); no cost is invented because DeepSeek returns tokens, not money.
"""
from __future__ import annotations

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

import httpx

from app.agents import claude_cli
from app.config import settings
from app.core.errors import (
    E_CLI_JSON,
    E_CLI_TIMEOUT,
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
CHAT_COMPLETIONS_PATH = "/chat/completions"

#: reasoning_effort canonical values + the documented aliases (docs: minimal->low, medium/xhigh->high).
REASONING_EFFORT_CANONICAL = ("none", "low", "high", "max")
REASONING_EFFORT_ALIASES = {"minimal": "low", "medium": "high", "xhigh": "high"}

#: Bounded in-process session store: DeepSeek's API is STATELESS, so replaying `--resume`ed
#: conversations is what lets the repair ladder's R1/R2 re-prompts reference the previous answer
#: (the claude CLI gets that from its own on-disk sessions). In-process only; a lost entry is a
#: warned fresh call, never a silent mis-attribution.
SESSION_STORE_LIMIT = 32
_SESSIONS: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
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


def _store_exchange(
    session_id: str | None, messages: list[dict[str, Any]], assistant_text: str
) -> str:
    """Persist (system,user,assistant) under `session_id` (new id when absent); bounded LRU."""
    sid = session_id or f"deepseek-{uuid.uuid4().hex[:16]}"
    history = [dict(message) for message in messages] + [
        {"role": "assistant", "content": assistant_text}
    ]
    with _SESSION_LOCK:
        _SESSIONS[sid] = history
        _SESSIONS.move_to_end(sid)
        while len(_SESSIONS) > SESSION_STORE_LIMIT:
            _SESSIONS.popitem(last=False)
    return sid


def _reset_session_store() -> None:
    """Test hook: drop every stored exchange (the store is process-local by design)."""
    with _SESSION_LOCK:
        _SESSIONS.clear()


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


def _base_url() -> str:
    value = str(_setting("deepseek_base_url", DEEPSEEK_DEFAULT_BASE_URL) or "").strip().rstrip("/")
    return value or DEEPSEEK_DEFAULT_BASE_URL


def _request_headers(api_key: str) -> dict[str, str]:
    """Auth + content type for the call. The key value is never logged, echoed or stored."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _messages_for(plan: InvocationPlan, replay: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """The chat messages: a replayed exchange + the new turn, or a fresh system+user pair.

    The JSON instruction is added ONLY on a fresh exchange (the replayed system message already
    carries it — the repair ladder keeps one conversation per logical run).
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
    messages.append({"role": "user", "content": plan.prompt})
    return messages


def _build_body(plan: InvocationPlan, *, model: str, messages: list[dict[str, Any]]) -> dict[str, Any]:
    """The exact request body; `messages` is resolved ONCE by the caller (replay included), so
    the exchange stored after success is byte-identical to the one actually sent."""
    effort = _reasoning_effort()
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": False,
    }
    if effort == "none":
        # Deterministic agent JSON runs want no hidden reasoning pass; `none` IS the switch.
        body["thinking"] = {"type": "disabled"}
    else:
        body["thinking"] = {"type": "enabled"}
        body["reasoning_effort"] = effort
    if plan.json_schema is not None:
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
) -> claude_cli.InvokeResult:
    """A failed run returned NORMALLY (claude CLI D2: the envelope on stdout carries the failure).

    base_agent._classify_invoke reads `is_error`/`subtype`, `classify_error_envelope` reads
    `api_error_status`, and `_retryable` feeds `subtype`+`api_error_status`+`exit_code` to
    `claude_cli.is_retryable_failure` — all of it works unchanged off this shape.
    """
    elapsed = time.monotonic() - started
    envelope = {
        "type": "result",
        "subtype": subtype,
        "is_error": True,
        "result": detail,
        "api_error_status": api_status,
        "num_turns": 1,
        "duration_ms": int(elapsed * 1000),
        "model": model,
        "usage": {},
        "provider": "deepseek",
    }
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


# ── invoke ───────────────────────────────────────────────────────────────────────────────────

def invoke(
    inv: claude_cli.Invocation,
    *,
    client: httpx.Client | None = None,
    api_key: str | None = None,
    model: str | None = None,
) -> claude_cli.InvokeResult:
    """Run one DeepSeek chat completion for `inv`; same shape as `claude_cli.invoke`.

    Spawn-level problems RAISE a named refusal (`E_DEEPSEEK_KEY`, `E_CLI_TIMEOUT`, `E_CLI_JSON`);
    a failed provider run returns normally with the failure envelope on stdout (mirroring D2).
    `client` is injectable for tests (`httpx.MockTransport`); production builds a per-call client
    (one HTTP call per agent run, so pooling buys nothing against multi-second model latency, and
    per-call lifetime keeps no state between runs).
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
    model_id = _model_for(model)
    timeout_s = _timeout_seconds()
    url = f"{_base_url()}{CHAT_COMPLETIONS_PATH}"
    messages = _messages_for(plan, replay=_session_messages(plan.resume_session_id))
    body = _build_body(plan, model=model_id, messages=messages)
    headers = _request_headers(key)
    timeout = httpx.Timeout(timeout_s)

    started = time.monotonic()
    own_client = client is None
    http = client if client is not None else httpx.Client(timeout=timeout)
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
            how_to_fix="raise deepseek_timeout_seconds in Settings, or shrink the task",
            context={"timeout_s": timeout_s, "url": url, "runtime": "deepseek"},
        ) from None
    except httpx.TransportError as exc:
        # Connect/read/write faults never reached a model: transient network is retryable, and
        # the `api_error` marker in the subtype is what tells the existing ladder so.
        return _failure_result(
            subtype="error_api_error",
            detail=f"deepseek transport error: {type(exc).__name__}: {exc}",
            started=started,
            model=model_id,
            api_status=None,
        )
    finally:
        if own_client:
            http.close()

    if response.status_code != 200:
        return _failure_result(
            subtype=_STATUS_SUBTYPES.get(response.status_code, "error_api_error"),
            detail=f"deepseek HTTP {response.status_code}: {_body_snippet(response) or 'no body'}",
            started=started,
            model=model_id,
            api_status=response.status_code,
        )

    try:
        payload = response.json()
    except ValueError:
        raise PolsiaError(
            E_CLI_JSON,
            f"deepseek API returned HTTP 200 with a non-JSON body: {_body_snippet(response, 200)!r}",
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
    choice = choices[0]
    message_raw = choice.get("message")
    message = message_raw if isinstance(message_raw, dict) else {}
    content_raw = message.get("content")
    finish_reason = str(choice.get("finish_reason") or "")
    if not isinstance(content_raw, str):
        if finish_reason == "length":
            content_raw = ""  # truncated before any text was emitted (e.g. mid-reasoning)
        else:
            raise PolsiaError(
                E_CLI_JSON,
                "deepseek response choices[0].message.content is missing or not a string",
                how_to_fix="check the requested model accepts chat completions; retry once, then report",
                context={"url": url, "runtime": "deepseek"},
            )
    content = content_raw

    usage_scalar = payload.get("usage")
    usage_raw = usage_scalar if isinstance(usage_scalar, dict) else {}
    envelope_usage = {
        "input_tokens": usage_raw.get("prompt_tokens"),
        "output_tokens": usage_raw.get("completion_tokens"),
        "cache_read_input_tokens": usage_raw.get("prompt_cache_hit_tokens"),
    }

    if finish_reason == "length":
        # Truncation is NOT a success: the JSON (or prose) is incomplete. Report it as a named
        # failure envelope (subtype error_max_tokens -> ENVELOPE_IS_ERROR, not retryable: the
        # same prompt would truncate again; the fix is a bigger output budget or a smaller ask).
        return _failure_result(
            subtype="error_max_tokens",
            detail=(
                "deepseek response truncated at the output-token cap "
                f"(finish_reason=length; received {len(content)} chars) — raise the output "
                "budget or shrink the requested payload; any JSON is incomplete"
            ),
            started=started,
            model=model_id,
            api_status=None,
        )

    stored_messages = messages
    session_id = _store_exchange(plan.resume_session_id, stored_messages, content)

    envelope: dict[str, Any] = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "result": content,
        "session_id": session_id,
        "num_turns": 1,
        "duration_ms": int((time.monotonic() - started) * 1000),
        "usage": envelope_usage,
        "model": str(payload.get("model") or model_id),
        "stop_reason": finish_reason or "stop",
        "provider": "deepseek",
    }
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
    "CHAT_COMPLETIONS_PATH",
    "DEEPSEEK_DEFAULT_BASE_URL",
    "DEEPSEEK_DEFAULT_MODEL",
    "DEEPSEEK_DEFAULT_REASONING_EFFORT",
    "DEEPSEEK_DEFAULT_TIMEOUT_SECONDS",
    "InvocationPlan",
    "SESSION_STORE_LIMIT",
    "available",
    "invoke",
    "plan_from_invocation",
]
