"""Polsia agent runtime — structured output extraction, validation and the repair ladder.

Lifted from lane 05 (`research/05-runtime-100x/code/runtime/structured_output.py`) and wired to
`app.core.errors`: every failure keeps its stable `POLSIA-Exxx` code and gains the matching
`E_*` refusal code the API layer reports, so one failure has one name at every layer.

This module is the single choke point between "the Claude CLI printed something" and "the
orchestrator got a schema-valid object". Everything an agent returns passes through
`resolve_output()`.

Documented behaviour encoded here (CLI-CONTRACT.md §2, D2/D3/D4):

  * the envelope is ALWAYS JSON on stdout — a failure inside the run still prints it, so
    `is_error`/`subtype` is the authority, not the exit code;
  * `subtype: "success"` with no `structured_output` is a FAILURE (POLSIA-E106);
  * `error_max_turns` / `error_max_budget_usd` / `error_max_structured_output_retries` are
    distinct codes, never a generic "it failed";
  * text-level repairs (fenced block, trailing comma, smart quotes, scalar coercion) run before
    any re-prompt, and every successful repair is recorded in `used_repair` — never hidden.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Iterable, Optional

from app.core.errors import (
    E_AGENT_BAD_OUTPUT,
    E_BUDGET_EXCEEDED,
    E_CLI_EXIT,
    E_CLI_JSON,
    E_CLI_MISSING,
    E_CLI_TIMEOUT,
    E_POLICY_DENIED,
    E_RATE_LIMITED,
    E_SANDBOX_BLOCKED,
    E_VALIDATION,
    PolsiaError,
)


# ---------------------------------------------------------------------------
# Failure taxonomy. Codes are stable; add new ones, never renumber.
# ---------------------------------------------------------------------------

class FailureCode(str, Enum):
    OK = "POLSIA-E000"
    CLI_BINARY_MISSING = "POLSIA-E100"        # spawn failed: claude not found / not on PATH
    CLI_AUTH_FAILED = "POLSIA-E101"           # envelope error w/ auth signal; api_error_status 401/403
    CLI_TIMEOUT = "POLSIA-E102"               # our wall-clock process cap hit; SIGTERM -> exit 143
    CLI_NONZERO_EXIT = "POLSIA-E103"          # exit != 0 and stdout has no parseable envelope
    CLI_STDOUT_NOT_JSON = "POLSIA-E104"       # envelope parse failed
    ENVELOPE_IS_ERROR = "POLSIA-E105"         # is_error true / subtype error_* (generic)
    STRUCTURED_OUTPUT_MISSING = "POLSIA-E106" # subtype success but structured_output absent
    MAX_TURNS = "POLSIA-E107"                 # subtype error_max_turns
    BUDGET_EXCEEDED = "POLSIA-E108"           # subtype error_max_budget_usd, or our pre-dispatch cap
    STRUCTURED_RETRIES_EXHAUSTED = "POLSIA-E109"   # error_max_structured_output_retries
    SCHEMA_VALIDATION_FAILED = "POLSIA-E110"  # local validator (pydantic) rejected the payload
    REFUSAL = "POLSIA-E111"                   # model refused (explicit marker or refusal text)
    EMPTY_RESULT = "POLSIA-E112"              # result text empty/whitespace, nothing structured
    PARTIAL_RESULT = "POLSIA-E113"            # parseable JSON but required fields missing (lenient path)
    POLICY_BLOCKED = "POLSIA-E114"            # output-action firewall denied a side effect
    INJECTION_FLAGGED = "POLSIA-E115"         # untrusted-content taint caused a review hold
    DUPLICATE_SUPPRESSED = "POLSIA-E116"      # idempotency: work already done this cycle
    RATE_CAPPED = "POLSIA-E117"               # channel cap hit
    KILL_SWITCH = "POLSIA-E118"               # kill switch active
    UNKNOWN = "POLSIA-E119"


REMEDIATION: dict[FailureCode, str] = {
    FailureCode.CLI_BINARY_MISSING: "Deploy: install Claude Code CLI in the worker image and set CLAUDE_CLI_PATH; fail the health check, do not retry.",
    FailureCode.CLI_AUTH_FAILED: "Refresh ~/.claude OAuth on the host (claude login) or switch to ANTHROPIC_API_KEY; do not retry until auth check passes.",
    FailureCode.CLI_TIMEOUT: "Lower the task's scope or raise CLAUDE_TIMEOUT_SECONDS; check for a waiting permission prompt (deny-all mode) before retrying.",
    FailureCode.CLI_NONZERO_EXIT: "Inspect raw stdout/stderr; retry once at most.",
    FailureCode.CLI_STDOUT_NOT_JSON: "A hook/plugin printed to stdout; force --bare in production invocations and re-run.",
    FailureCode.ENVELOPE_IS_ERROR: "Branch on subtype/api_error_status; retryable only for rate_limit/overloaded/server_error.",
    FailureCode.STRUCTURED_OUTPUT_MISSING: "Model completed without emitting the schema. Re-ask with the schema restated; if it recurs, shrink the schema.",
    FailureCode.MAX_TURNS: "Raise --max-turns or split the task; a repeated MAX_TURNS means the task belongs in the DAG as two nodes.",
    FailureCode.BUDGET_EXCEEDED: "Day/agent budget exhausted. Defer the node to tomorrow's plan; surface it in the evening summary as deferred_budget, never as failed.",
    FailureCode.STRUCTURED_RETRIES_EXHAUSTED: "CLI gave up after internal re-prompts. Run the repair ladder (reprompt -> narrow extraction -> coercion); if still failing, queue for human with the error list.",
    FailureCode.SCHEMA_VALIDATION_FAILED: "Local validator disagreed with the CLI. Fix schema drift first (same schema at both ends); only then repair the payload.",
    FailureCode.REFUSAL: "Legitimate refusal: rewrite the task instruction (not the schema), or escalate to a human. Never re-ask the identical prompt.",
    FailureCode.EMPTY_RESULT: "Retry once; if empty again the prompt is over-constrained. Log the raw envelope.",
    FailureCode.PARTIAL_RESULT: "Only allowed for ledger-tolerant agents (competitor_research, business_planning). Otherwise run repair; never invent defaults for missing required fields.",
    FailureCode.POLICY_BLOCKED: "Firewall decision is final for this cycle; the blocked action goes to pending_approvals with the exact reason.",
    FailureCode.INJECTION_FLAGGED: "Content carried instruction-like text. Hold irreversible actions; a human reviews the quarantined content and the proposed action.",
    FailureCode.DUPLICATE_SUPPRESSED: "Idempotency key already satisfied today. No user-visible action needed; recorded for the ledger (skipped_duplicate).",
    FailureCode.RATE_CAPPED: "Channel cap reached. Re-schedule to the next window; do not raise the cap from inside an agent.",
    FailureCode.KILL_SWITCH: "Operator froze the fleet. All side effects denied; reads may continue.",
    FailureCode.UNKNOWN: "Unclassified: capture the full envelope + stderr and open a runtime bug.",
}

# One failure, one name at every layer: the POLSIA-Exxx taxonomy maps onto the frozen
# `app.core.errors` codes the API boundary and the activity log speak.
FAILURE_TO_E_CODE: dict[FailureCode, str] = {
    FailureCode.OK: "",
    FailureCode.CLI_BINARY_MISSING: E_CLI_MISSING,
    FailureCode.CLI_AUTH_FAILED: E_CLI_EXIT,
    FailureCode.CLI_TIMEOUT: E_CLI_TIMEOUT,
    FailureCode.CLI_NONZERO_EXIT: E_CLI_EXIT,
    FailureCode.CLI_STDOUT_NOT_JSON: E_CLI_JSON,
    FailureCode.ENVELOPE_IS_ERROR: E_CLI_EXIT,
    FailureCode.STRUCTURED_OUTPUT_MISSING: E_CLI_JSON,
    FailureCode.MAX_TURNS: E_CLI_EXIT,
    FailureCode.BUDGET_EXCEEDED: E_BUDGET_EXCEEDED,
    FailureCode.STRUCTURED_RETRIES_EXHAUSTED: E_AGENT_BAD_OUTPUT,
    FailureCode.SCHEMA_VALIDATION_FAILED: E_AGENT_BAD_OUTPUT,
    FailureCode.REFUSAL: E_VALIDATION,
    FailureCode.EMPTY_RESULT: E_CLI_JSON,
    FailureCode.PARTIAL_RESULT: E_AGENT_BAD_OUTPUT,
    FailureCode.POLICY_BLOCKED: E_SANDBOX_BLOCKED,
    FailureCode.INJECTION_FLAGGED: E_POLICY_DENIED,
    FailureCode.DUPLICATE_SUPPRESSED: "",
    FailureCode.RATE_CAPPED: E_RATE_LIMITED,
    FailureCode.KILL_SWITCH: E_POLICY_DENIED,
    FailureCode.UNKNOWN: E_CLI_JSON,
}


def e_code_for(code: FailureCode) -> str:
    return FAILURE_TO_E_CODE.get(code) or E_AGENT_BAD_OUTPUT


@dataclass
class OutputResolution:
    """Result of resolving one CLI invocation into a validated payload."""
    ok: bool
    payload: Optional[dict]
    code: FailureCode
    hint: str = ""
    used_repair: Optional[str] = None     # name of the repair step that succeeded, if any
    raw_result: str = ""
    partial: bool = False
    detail: str = ""
    ladder_log: list[str] = field(default_factory=list)

    @property
    def e_code(self) -> str:
        return e_code_for(self.code)

    def to_polsia_error(self, *, agent_type: str = "") -> PolsiaError:
        """The named refusal for a failed resolution (never a success-shaped empty result)."""
        return PolsiaError(
            self.e_code,
            f"{agent_type or 'agent'} failed: {self.code.value} {self.detail}".strip(),
            how_to_fix=self.hint or REMEDIATION.get(self.code, ""),
            context={
                "failure_code": self.code.value,
                "detail": self.detail,
                "used_repair": self.used_repair,
                "ladder": list(self.ladder_log),
                "raw_result": self.raw_result[:500],
            },
        )


# ---------------------------------------------------------------------------
# Step 1 — envelope parsing
# ---------------------------------------------------------------------------

def extract_envelope(stdout: str) -> dict:
    """Parse the CLI's stdout into the result envelope.

    `--output-format json` prints one JSON object. `stream-json` prints one JSON object per
    line and the last line is the `result` message; we accept both. Raises ValueError (the
    caller maps it to E_CLI_JSON / E_CLI_EXIT) when nothing parses.
    """
    text = (stdout or "").strip()
    if not text:
        raise ValueError("empty stdout")
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass
    # stream-json / polluted stdout: take the last parseable JSON object line.
    for line in reversed(text.splitlines()):
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                return obj
        except json.JSONDecodeError:
            continue
    # last resort: last balanced-brace slice
    m = re.search(r"\{.*\}\s*$", text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError("no JSON object found on stdout")


def usage_from_envelope_raw(env: dict) -> dict:
    """The raw accounting sub-objects, kept verbatim for `agent_runs.usage_json`."""
    return {
        "usage": env.get("usage") or {},
        "modelUsage": env.get("modelUsage") or env.get("model_usage") or {},
        "total_cost_usd": env.get("total_cost_usd"),
        "duration_ms": env.get("duration_ms"),
        "num_turns": env.get("num_turns"),
        "session_id": env.get("session_id"),
        "subtype": env.get("subtype"),
        "is_error": env.get("is_error"),
        "terminal_reason": env.get("terminal_reason"),
        "permission_denials": env.get("permission_denials") or [],
    }


# ---------------------------------------------------------------------------
# Step 2 — local text-level repairs (deterministic, cheap)
# ---------------------------------------------------------------------------

_FENCE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
_SMART = str.maketrans({"\u201c": '"', "\u201d": '"', "\u2018": "'", "\u2019": "'"})


def extract_json_from_text(text: str) -> Optional[Any]:
    """Pull a JSON value out of free-form model text: fenced block, brace slice, or whole
    string; then kill trailing commas and smart quotes."""
    if not text:
        return None
    candidates: list[str] = []
    fenced = _FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))
    slice_m = re.search(r"[\[{].*[\]}]\s*$", text, re.DOTALL)
    if slice_m:
        candidates.append(slice_m.group(0))
    candidates.append(text.strip())
    for cand in candidates:
        for variant in (cand, _de_trailing_commas(cand.translate(_SMART))):
            try:
                return json.loads(variant)
            except json.JSONDecodeError:
                continue
    return None


def _de_trailing_commas(s: str) -> str:
    return re.sub(r",\s*([}\]])", r"\1", s)


def coerce_types(payload: Any, field_types: dict[str, type]) -> Any:
    """Coerce scalar types on a dict payload for the listed top-level fields.
    '42' -> 42, 'true' -> True, '3.5' -> 3.5. Never fabricates values."""
    if not isinstance(payload, dict):
        return payload
    out = dict(payload)
    for key, want in field_types.items():
        if key not in out or out[key] is None:
            continue
        val = out[key]
        if isinstance(val, want):
            continue
        try:
            if want is bool and isinstance(val, str):
                out[key] = val.strip().lower() in ("true", "1", "yes")
            elif want in (int, float) and isinstance(val, str):
                out[key] = want(float(val.strip()))
            elif want is str and not isinstance(val, (dict, list)):
                out[key] = str(val)
        except (TypeError, ValueError):
            pass  # leave as-is; the validator will report it
    return out


# ---------------------------------------------------------------------------
# Step 3 — the full resolution ladder
# ---------------------------------------------------------------------------

_REFUSAL_MARKERS = (
    "i can't help with that", "i cannot help with that", "i won't", "i will not",
    "unable to assist", "cannot assist with", "i'm not able to help",
)


def _looks_like_refusal(text: str) -> bool:
    t = (text or "").lower()
    return any(m in t for m in _REFUSAL_MARKERS)


def classify_error_envelope(env: dict) -> tuple[FailureCode, str]:
    """Map an error envelope to (code, detail). Called only when is_error/subtype says the run
    failed, or when the required output is absent."""
    subtype = str(env.get("subtype") or "")
    status = env.get("api_error_status")
    match subtype:
        case "error_max_turns":
            return FailureCode.MAX_TURNS, f"terminal_reason={env.get('terminal_reason')}"
        case "error_max_budget_usd":
            return FailureCode.BUDGET_EXCEEDED, "CLI budget cap reached"
        case "error_max_structured_output_retries":
            return FailureCode.STRUCTURED_RETRIES_EXHAUSTED, "; ".join(env.get("errors") or [])[:400]
        case "error_during_execution":
            if status in (401, 403) or env.get("startup_failure_reason") in (
                "authentication_failed", "oauth_org_not_allowed", "account_on_hold",
            ):
                return FailureCode.CLI_AUTH_FAILED, f"api_error_status={status}"
            return FailureCode.ENVELOPE_IS_ERROR, f"api_error_status={status}"
        case "":
            return FailureCode.ENVELOPE_IS_ERROR, "is_error=true without subtype"
        case _:
            return FailureCode.ENVELOPE_IS_ERROR, f"subtype={subtype}"


def resolve_output(
    *,
    stdout: str,
    exit_code: int = 0,
    schema_name: str = "",
    validator: Optional[Callable[[dict], Any]] = None,
    required_fields: Iterable[str] = (),
    tolerate_partial: bool = False,
    coercions: Optional[dict[str, type]] = None,
) -> OutputResolution:
    """Full ladder: envelope -> structured_output -> local validation -> repairs.

    `validator(payload)` must raise on invalid (e.g. Pydantic `model_validate`).
    `required_fields` enables the PARTIAL_RESULT classification for the lenient path.
    """
    # -- E100/E102/E103: transport-level failures ---------------------------
    if exit_code == 143:
        return OutputResolution(False, None, FailureCode.CLI_TIMEOUT,
                                REMEDIATION[FailureCode.CLI_TIMEOUT], detail="SIGTERM/143")
    try:
        env = extract_envelope(stdout)
    except ValueError as exc:
        code = FailureCode.CLI_NONZERO_EXIT if exit_code != 0 else FailureCode.CLI_STDOUT_NOT_JSON
        return OutputResolution(False, None, code, REMEDIATION[code], detail=str(exc))

    # -- E101/E105/E107/E108/E109: envelope-level failures -------------------
    if env.get("is_error") is True or str(env.get("subtype", "")).startswith("error_"):
        code, detail = classify_error_envelope(env)
        return OutputResolution(False, None, code, REMEDIATION[code], detail=detail)

    # -- primary payload ------------------------------------------------------
    payload = env.get("structured_output")
    raw_result = str(env.get("result") or "")
    used: Optional[str] = None

    if payload is None:
        parsed = extract_json_from_text(raw_result)
        if parsed is not None and not env.get("is_error"):
            payload, used = parsed, "text_extract"
        elif _looks_like_refusal(raw_result):
            return OutputResolution(False, None, FailureCode.REFUSAL,
                                    REMEDIATION[FailureCode.REFUSAL], raw_result=raw_result[:500])
        elif not raw_result.strip():
            return OutputResolution(False, None, FailureCode.EMPTY_RESULT,
                                    REMEDIATION[FailureCode.EMPTY_RESULT])
        else:
            return OutputResolution(False, None, FailureCode.STRUCTURED_OUTPUT_MISSING,
                                    REMEDIATION[FailureCode.STRUCTURED_OUTPUT_MISSING],
                                    raw_result=raw_result[:500])

    if not isinstance(payload, dict):
        return OutputResolution(False, None, FailureCode.SCHEMA_VALIDATION_FAILED,
                                REMEDIATION[FailureCode.SCHEMA_VALIDATION_FAILED],
                                detail=f"structured_output is {type(payload).__name__}, expected object")

    # -- local validation + deterministic repairs ----------------------------
    if validator is not None:
        try:
            validator(payload)
        except Exception as exc:  # validator-specific exception text kept in detail
            coerced = coerce_types(payload, coercions or {})
            if coerced != payload:
                try:
                    validator(coerced)
                    payload, used = coerced, "type_coercion"
                except Exception:
                    pass
            if used != "type_coercion":
                missing = [f for f in required_fields if f not in payload]
                if tolerate_partial and missing and len(missing) < len(list(required_fields)):
                    return OutputResolution(False, payload, FailureCode.PARTIAL_RESULT,
                                            REMEDIATION[FailureCode.PARTIAL_RESULT],
                                            partial=True, detail=f"missing={missing}")
                return OutputResolution(False, payload, FailureCode.SCHEMA_VALIDATION_FAILED,
                                        REMEDIATION[FailureCode.SCHEMA_VALIDATION_FAILED],
                                        detail=str(exc)[:400])

    return OutputResolution(True, payload, FailureCode.OK, used_repair=used, raw_result=raw_result[:200])


# ---------------------------------------------------------------------------
# The retry/repair ladder (bounded). The dispatch callback is supplied by the caller so this
# module stays CLI-agnostic and unit-testable.
# ---------------------------------------------------------------------------

@dataclass
class RepairPolicy:
    max_cli_reprompts: int = 1      # R1: same session, "fix the JSON" (settings.claude_repair_attempts)
    allow_narrow_extraction: bool = True   # R2: fresh single-turn extraction of only the bad fields
    allow_text_repair: bool = True  # R3: deterministic python fixes (already inside resolve_output)
    ladder_log: list[str] = field(default_factory=list)


def policy_from_settings() -> RepairPolicy:
    from app.config import settings

    return RepairPolicy(max_cli_reprompts=max(0, int(settings.claude_repair_attempts or 0)))


def prompt_repair_message(code: FailureCode, detail: str, schema_name: str) -> str:
    """R1 message: precisely tells the model what failed. Short, no re-statement of the task."""
    return (
        "Your previous answer did not satisfy the output contract.\n"
        f"validator_error: {detail or code.value}\n"
        f"schema: {schema_name}\n"
        "Return ONLY a single JSON object that validates against the schema. "
        "Do not add prose, markdown or commentary."
    )


def run_repair_ladder(
    *,
    dispatch: Callable[[Optional[str]], tuple[str, int]],  # (prompt_override|None) -> (stdout, exit_code)
    resolve_kwargs: dict,
    policy: Optional[RepairPolicy] = None,
) -> OutputResolution:
    """Bounded ladder:
       R0 initial call
       R1 up to `max_cli_reprompts` corrective re-prompts (same session)
       R2 one fresh narrow extraction call (only if R1 exhausted and allowed)
       then FAIL with the *original* code + every attempt in the ladder log.
    """
    policy = policy or policy_from_settings()
    history: list[str] = []
    last: Optional[OutputResolution] = None

    for attempt in range(policy.max_cli_reprompts + 1):
        override = None
        if attempt > 0 and last is not None:
            override = prompt_repair_message(last.code, last.detail, resolve_kwargs.get("schema_name", ""))
        stdout, exit_code = dispatch(override)
        last = resolve_output(stdout=stdout, exit_code=exit_code, **resolve_kwargs)
        history.append(f"R{attempt}->{last.code.value}")
        if last.ok:
            last.used_repair = last.used_repair or ("re_prompt" if attempt else None)
            last.ladder_log = list(history)
            policy.ladder_log.extend(history)
            return last

    if policy.allow_narrow_extraction and last is not None and last.code in (
        FailureCode.SCHEMA_VALIDATION_FAILED, FailureCode.STRUCTURED_OUTPUT_MISSING,
    ):
        narrow = (
            "Extract the following fields from the information above and return ONLY this JSON object: "
            f"{resolve_kwargs.get('schema_name', 'see schema')}. If a value is not present, use null. "
            "Return valid JSON only."
        )
        stdout, exit_code = dispatch(narrow)
        last = resolve_output(stdout=stdout, exit_code=exit_code, **resolve_kwargs)
        history.append(f"R2->{last.code.value}")
        if last.ok:
            last.used_repair = "narrow_extraction"
            last.ladder_log = list(history)
            policy.ladder_log.extend(history)
            return last

    policy.ladder_log.extend(history)
    assert last is not None
    last.ladder_log = list(history)
    return last  # carries the final named failure code + hint


__all__ = [
    "FAILURE_TO_E_CODE",
    "FailureCode",
    "OutputResolution",
    "REMEDIATION",
    "RepairPolicy",
    "classify_error_envelope",
    "coerce_types",
    "e_code_for",
    "extract_envelope",
    "extract_json_from_text",
    "policy_from_settings",
    "prompt_repair_message",
    "resolve_output",
    "run_repair_ladder",
    "usage_from_envelope_raw",
]
