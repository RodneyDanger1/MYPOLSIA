"""Polsia agent runtime — the Claude CLI invocation contract, implemented once.

Lifted from lane 05 (`research/05-runtime-100x/code/runtime/claude_cli.py`, 39/39 tests green
there) and wired to this application's frozen surfaces:

  * `app.config.settings` — `claude_cli_path`, `claude_max_argv_chars`, `claude_prompt_via_stdin`,
    `claude_max_concurrency`, `claude_timeout_seconds`;
  * `app.core.errors` — every spawn-level failure is a NAMED refusal
    (`E_CLI_MISSING` / `E_CLI_TIMEOUT` / `E_CLI_EXIT` / `E_CLI_JSON` / `E_PROMPT_TOO_LARGE`)
    carrying a `how_to_fix` hint.

Verified facts this module encodes (sources quoted in CLI-CONTRACT.md, D1-D9):

  * `claude -p "<prompt>" --output-format json` prints ONE JSON envelope on stdout; `result`
    holds the text, `structured_output` the schema-validated payload.
  * Exit code is NOT a success signal: "When a failure happens inside the run ... Claude Code
    prints the failure as the result on stdout." The envelope's `is_error`/`subtype` is the
    authority, so stdout is parsed even on a non-zero exit.
  * Long prompts: piped stdin is capped at 10 MB; Windows' whole command line is capped at
    32,767 chars (CreateProcessW); Linux caps a single argument at 128 KB. Anything that does
    not fit the configured argv budget goes over stdin when `CLAUDE_PROMPT_VIA_STDIN` is on.
  * `143` is SIGTERM (our own wall-clock kill): the turn is unfinished and no result exists.
  * Safety: `--permission-mode dontAsk` + `--permission-prompts none` for unattended runs;
    `--bare` so a cloned repo cannot inject hooks; `bypassPermissions` is banned outright.

This is the ONLY module that knows the CLI's flags.
"""
from __future__ import annotations

import contextlib
import os
import random
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.config import settings
from app.core.errors import (
    E_CLI_JSON,
    E_CLI_MISSING,
    E_CLI_TIMEOUT,
    E_PROMPT_TOO_LARGE,
    ERROR_HINTS,
    PolsiaError,
)

# ── platform transport limits (CLI-CONTRACT.md §7) ───────────────────────────────────────────
WIN_CMDLINE_LIMIT = 32_767          # CreateProcessW lpCommandLine max, including the NUL
LINUX_ARG_LIMIT = 128 * 1024        # MAX_ARG_STRLEN = 32 pages x 4 KB
STDIN_LIMIT = 10 * 1024 * 1024      # documented cap for piped stdin

SIGTERM_EXIT_CODE = 143             # our own timeout kill (docs: no result recorded)

SAFE_PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "manual")
BANNED_FLAGS = ("--dangerously-skip-permissions",)
STDIN_POINTER = "Read the context from stdin and execute the task described there."

SAFE_ENV_PASSTHROUGH = (
    "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
    "LANG", "LC_ALL", "SystemRoot", "COMSPEC", "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_PROMPT_CACHE_TTL",
)

SPILL_PLACEHOLDER = "@@POLSIA_SYSTEM_PROMPT_FILE@@"


class ClaudeCLIError(PolsiaError):
    """Historical name from the agent-layer contract (SPEC-AGENTS.md §1.8).

    It IS a `PolsiaError`, so callers that catch the named-refusal type keep working; the
    `code` attribute always carries one of the `E_CLI_*` constants.
    """


# ── invocation ───────────────────────────────────────────────────────────────────────────────

@dataclass
class Invocation:
    argv: list[str]
    stdin_text: str | None = None
    cwd: str | None = None
    env_overrides: dict[str, str] = field(default_factory=dict)
    timeout_s: int = 600
    transport: str = "argv"                 # argv | stdin | file-ref
    prompt_file: str | None = None       # written by the caller when transport == file-ref
    system_prompt_file_content: str | None = None
    schema_name: str = ""                   # bookkeeping: which agent schema rides in the argv

    def estimated_command_line_chars(self) -> int:
        return sum(len(a) + 1 for a in self.argv)

    def redacted_preview(self) -> str:
        """Shell-quote the argv for logs; secrets never ride on argv (auth lives in ~/.claude)."""
        return " ".join(shlex.quote(a) for a in self.argv)


def cli_path() -> str:
    """`settings.claude_cli_path`, with the `CLAUDE_CLI_PATH` env override (README:77)."""
    return (os.getenv("CLAUDE_CLI_PATH") or settings.claude_cli_path or "claude").strip()


def argv_budget(max_argv_chars: int | None = None) -> int:
    """The whole-command-line budget: configured cap intersected with the platform limit."""
    configured = settings.claude_max_argv_chars if max_argv_chars is None else max_argv_chars
    platform = WIN_CMDLINE_LIMIT if sys.platform.startswith("win") else LINUX_ARG_LIMIT
    return max(1_024, min(int(configured or platform), platform))


def build_invocation(
    *,
    prompt: str,
    json_schema: dict | None = None,
    system_prompt_static: str = "",
    system_prompt_dynamic: str = "",
    resume_session: str | None = None,
    continue_session: bool = False,
    fork_session: bool = False,
    new_session_id: str | None = None,
    allowed_tools: tuple[str, ...] = (),
    disallowed_tools: tuple[str, ...] = (),
    tools_available: str = "default",
    permission_mode: str = "dontAsk",
    add_dirs: tuple[str, ...] = (),
    mcp_config_path: str | None = None,
    strict_mcp: bool = True,
    model: str | None = None,
    fallback_models: tuple[str, ...] | None = None,
    max_turns: int | None = None,
    max_budget_usd: float | None = None,
    bare: bool = True,
    no_session_persistence: bool = False,
    use_sandbox: bool = False,
    system_prompt_snapshot_off: bool = False,
    prompt_file: str | None = None,
    max_argv_chars: int | None = None,
    prompt_via_stdin: bool | None = None,
    timeout_s: int | None = None,
    schema_name: str = "",
) -> Invocation:
    """Build the exact argv for one headless call.

    Safe defaults for an unattended business agent: `bare=True` (no ambient hooks/plugins),
    `permission_mode='dontAsk'` (never bypass), an explicit allowlist, `--permission-prompts
    none` (never wait on a prompt nobody can answer), and `--output-format json` always.

    Transport rule: the prompt rides on argv while the whole command line fits the budget;
    otherwise (or whenever `CLAUDE_PROMPT_VIA_STDIN` is on) it is piped over stdin, and a
    prompt larger than the documented 10 MB stdin cap is a named `E_PROMPT_TOO_LARGE`
    refusal — the caller must spill it to a file and reference the path.
    """
    if permission_mode not in SAFE_PERMISSION_MODES:
        raise ValueError(
            f"refusing to build a Polsia invocation with permission_mode={permission_mode!r} "
            f"(safe modes: {', '.join(SAFE_PERMISSION_MODES)}; bypassPermissions is banned)"
        )
    budget = argv_budget(max_argv_chars)
    via_stdin_default = bool(settings.claude_prompt_via_stdin)
    want_stdin = via_stdin_default if prompt_via_stdin is None else bool(prompt_via_stdin)

    if len(prompt.encode("utf-8")) > STDIN_LIMIT:
        raise PolsiaError(
            E_PROMPT_TOO_LARGE,
            f"prompt is {len(prompt.encode('utf-8'))} bytes, over the {STDIN_LIMIT}-byte stdin cap",
            how_to_fix="write the context to a spill file and pass the path (transport='file-ref')",
            context={"chars": len(prompt), "stdin_limit": STDIN_LIMIT},
        )

    def _assemble(prompt_arg: str, transport: str, stdin_text: str | None) -> Invocation:
        argv: list[str] = ["claude", "-p", prompt_arg, "--output-format", "json"]
        if json_schema is not None:
            argv += ["--json-schema", _compact_json(json_schema)]

        sys_prompt_file: str | None = None
        if system_prompt_static:
            boundary = "\n__SYSTEM_PROMPT_DYNAMIC_BOUNDARY__\n" if system_prompt_dynamic else ""
            sys_prompt_file = system_prompt_static + boundary + system_prompt_dynamic
            argv += ["--append-system-prompt-file", SPILL_PLACEHOLDER]
        elif system_prompt_dynamic:
            argv += ["--append-system-prompt", system_prompt_dynamic]

        if system_prompt_snapshot_off:
            argv += ["--system-prompt-snapshot", "off"]

        if resume_session:
            argv += ["--resume", resume_session]
            if fork_session:
                argv.append("--fork-session")
        elif continue_session:
            argv.append("--continue")
        if new_session_id:
            argv += ["--session-id", new_session_id]

        if allowed_tools:
            argv += ["--allowedTools", ",".join(allowed_tools)]
        if disallowed_tools:
            argv += ["--disallowedTools", ",".join(disallowed_tools)]
        if tools_available != "default":
            argv += ["--tools", tools_available]
        argv += ["--permission-mode", permission_mode]
        argv += ["--permission-prompts", "none"]

        for d in add_dirs:
            argv += ["--add-dir", d]
        if mcp_config_path:
            argv += ["--mcp-config", mcp_config_path]
            if strict_mcp:
                argv.append("--strict-mcp-config")

        if model:
            argv += ["--model", model]
        if fallback_models:
            argv += ["--fallback-model", ",".join(fallback_models)]
        if max_turns is not None:
            argv += ["--max-turns", str(int(max_turns))]
        if max_budget_usd is not None:
            argv += ["--max-budget-usd", f"{max(0.0, float(max_budget_usd)):.4f}"]
        if bare:
            argv.append("--bare")
        if no_session_persistence:
            argv.append("--no-session-persistence")
        if use_sandbox:
            argv.append("--sandbox")

        for banned in BANNED_FLAGS:
            if banned in argv:
                raise ValueError(f"refusing to build a Polsia invocation containing {banned}")

        return Invocation(
            argv=argv,
            stdin_text=stdin_text,
            transport=transport,
            prompt_file=prompt_file,
            system_prompt_file_content=sys_prompt_file,
            timeout_s=int(timeout_s or settings.claude_timeout_seconds),
            schema_name=schema_name,
        )

    inv = _assemble(prompt, "argv", None)
    if inv.estimated_command_line_chars() > budget and not want_stdin:
        raise PolsiaError(
            E_PROMPT_TOO_LARGE,
            f"command line would be {inv.estimated_command_line_chars()} chars "
            f"(budget {budget}) and CLAUDE_PROMPT_VIA_STDIN is off; "
            f"prompt is {len(prompt)} chars",
            how_to_fix=ERROR_HINTS[E_PROMPT_TOO_LARGE],
            context={"budget_chars": budget, "prompt_chars": len(prompt), "stdin_allowed": False},
        )
    if want_stdin:
        inv = _assemble(STDIN_POINTER, "stdin", prompt)
    if inv.estimated_command_line_chars() > budget:
        raise PolsiaError(
            E_PROMPT_TOO_LARGE,
            f"even without the prompt the command line is "
            f"{inv.estimated_command_line_chars()} chars (budget {budget})",
            how_to_fix="raise CLAUDE_MAX_ARGV_CHARS or shrink the JSON schema",
            context={"budget_chars": budget, "transport": inv.transport},
        )
    return inv


def _compact_json(value: dict) -> str:
    import json

    return json.dumps(value, separators=(",", ":"))


# ── concurrency bound ────────────────────────────────────────────────────────────────────────

_gate_lock = threading.Lock()
_gate: threading.BoundedSemaphore | None = None
_gate_size = 0


def concurrency_gate() -> threading.BoundedSemaphore:
    """One process-wide slot pool sized by `settings.claude_max_concurrency`.

    The brief's rule: bound how many CLI processes this worker may have in flight. A
    `worker_prefetch_multiplier=1`, N-thread Celery worker would otherwise spawn N agents at
    once against a CLI that also self-limits; the semaphore makes the bound explicit and
    measurable. Recreated only when the configured size changes.
    """
    global _gate, _gate_size
    want = max(1, int(settings.claude_max_concurrency or 1))
    with _gate_lock:
        if _gate is None or _gate_size != want:
            _gate = threading.BoundedSemaphore(want)
            _gate_size = want
        return _gate


# ── invoke ───────────────────────────────────────────────────────────────────────────────────

@dataclass
class InvokeResult:
    stdout: str
    stderr: str
    exit_code: int
    wall_seconds: float
    transport: str = "argv"


def invoke(
    inv: Invocation,
    *,
    executable: str | None = None,
    extra_env: dict[str, str] | None = None,
    spawn: Callable[[list[str], str | None, dict, int], tuple[str, int]] | None = None,
) -> InvokeResult:
    """Run one invocation. Raises a named refusal for spawn-level problems; a *failed run*
    returns normally (the envelope on stdout carries the failure — D2).

    `spawn(argv, stdin_text, env, timeout)` is injectable so tests can exercise the transport
    without a CLI binary on the host.
    """
    argv = list(inv.argv)
    argv[0] = executable or cli_path()
    env = {k: os.environ[k] for k in SAFE_ENV_PASSTHROUGH if k in os.environ}
    env.update(inv.env_overrides)
    env.update(extra_env or {})

    tmp_files: list[str] = []
    if inv.system_prompt_file_content is not None:
        fd, path = tempfile.mkstemp(prefix="polsia-sysprompt-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(inv.system_prompt_file_content)
        tmp_files.append(path)
        argv = [path if a == SPILL_PLACEHOLDER else a for a in argv]

    try:
        gate = concurrency_gate()
        with gate:
            if spawn is not None:
                stdout, code = spawn(argv, inv.stdin_text, env, inv.timeout_s)
                return InvokeResult(stdout, "", int(code), 0.0, inv.transport)
            started = time.monotonic()
            proc = subprocess.Popen(  # noqa: S603 - argv list, no shell
                argv,
                stdin=subprocess.PIPE if inv.stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                cwd=inv.cwd,
                env=env,
            )
            try:
                stdout, stderr = proc.communicate(input=inv.stdin_text, timeout=inv.timeout_s)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                raise PolsiaError(
                    E_CLI_TIMEOUT,
                    f"claude CLI exceeded {inv.timeout_s}s (killed; the in-flight turn is unfinished)",
                    how_to_fix=ERROR_HINTS[E_CLI_TIMEOUT],
                    context={"timeout_s": inv.timeout_s, "transport": inv.transport},
                ) from None
            return InvokeResult(
                stdout or "", stderr or "", int(proc.returncode), time.monotonic() - started,
                inv.transport,
            )
    except FileNotFoundError as exc:
        raise PolsiaError(
            E_CLI_MISSING,
            f"claude CLI not found at {argv[0]!r}",
            how_to_fix=ERROR_HINTS[E_CLI_MISSING],
            context={"cli_path": argv[0], "error": str(exc)},
        ) from exc
    finally:
        for path in tmp_files:
            with contextlib.suppress(OSError):
                os.unlink(path)


def _kill_process_tree(proc: subprocess.Popen[str]) -> None:
    """Best-effort: the CLI may have spawned children; kill the group, then the child."""
    if proc.poll() is not None:
        return
    if sys.platform.startswith("win"):
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                check=False,
                timeout=10,
            )
    with contextlib.suppress(OSError):
        proc.kill()
    with contextlib.suppress(subprocess.TimeoutExpired, OSError, ValueError):
        proc.communicate(timeout=10)


# ── retry ladder ─────────────────────────────────────────────────────────────────────────────

TRANSIENT_SUBTYPE_MARKERS = ("rate_limit", "overloaded", "server_error", "api_error", "timeout")
TRANSIENT_HTTP_STATUS = (408, 409, 425, 429, 500, 502, 503, 504)


def is_retryable_failure(code: str, *, subtype: str = "", api_status: int | None = None,
                         exit_code: int = 0) -> bool:
    """Which failures are worth a second attempt (settings.agent_max_retries).

    Retryable: a transport-level non-zero exit, and an envelope that names a transient condition
    (`rate_limit`/`overloaded`/`server_error`, or a 408/429/5xx `api_error_status`).

    NOT retryable, deliberately:
      * `E_CLI_MISSING` — a deploy problem; retrying burns the day's budget for nothing;
      * `E_CLI_JSON` — a hook polluted stdout; the fix is `--bare`, not a retry;
      * `E_CLI_TIMEOUT` — our wall clock already expired on this exact prompt; the documented
        remediation is a smaller task or a higher timeout, and a retry would hold the worker slot
        and pay for the same hang again (the hint on the refusal says so).
    """
    if code == E_CLI_TIMEOUT:
        return False
    if api_status in TRANSIENT_HTTP_STATUS:
        return True
    haystack = f"{subtype} {exit_code}".lower()
    return bool(any(marker in haystack for marker in TRANSIENT_SUBTYPE_MARKERS))


def backoff_delay(attempt: int, *, base_s: float, cap_s: float = 30.0,
                  rng: random.Random | None = None) -> float:
    """Exponential backoff with full jitter: base * 2**attempt * U(0.5, 1.0), capped.

    Jitter matters here because the retry loop runs in every Celery worker at once; without it
    a transient provider hiccup retries in lockstep.
    """
    rand = (rng or random).random
    raw = float(base_s) * (2 ** max(0, attempt))
    return min(cap_s, raw * (0.5 + 0.5 * rand()))


def run_with_retries(
    dispatch: Callable[[], InvokeResult],
    *,
    attempts: int | None = None,
    backoff_s: float | None = None,
    classify: Callable[[InvokeResult], bool] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    rng: random.Random | None = None,
) -> tuple[InvokeResult, int]:
    """Call `dispatch()` up to `attempts` times while `classify(result)` says 'retry'.

    Returns `(last_result, attempts_used)`. A spawn-level refusal (E_CLI_MISSING/TIMEOUT) is
    re-raised by `dispatch`; timeouts and transient exit codes are retried, everything else
    returns immediately so the caller can map it to a named code without paying for the ladder.
    """
    total = 1 + max(0, int(settings.agent_max_retries if attempts is None else attempts))
    backoff = float(settings.agent_retry_backoff_seconds if backoff_s is None else backoff_s)
    last: InvokeResult | None = None
    for attempt in range(total):
        try:
            last = dispatch()
        except PolsiaError as exc:
            if exc.code != E_CLI_TIMEOUT or attempt == total - 1:
                raise
            sleep(backoff_delay(attempt, base_s=backoff, rng=rng))
            continue
        if classify is None or not classify(last) or attempt == total - 1:
            return last, attempt + 1
        sleep(backoff_delay(attempt, base_s=backoff, rng=rng))
    assert last is not None
    return last, total


# ── preflight ────────────────────────────────────────────────────────────────────────────────

def safety_preflight(*, kill_grade: str, auth_ok: bool = True, cli_present: bool = True) -> list[str]:
    """Problems that must stop dispatch BEFORE any spawn (fail closed; empty list = clear)."""
    problems: list[str] = []
    if kill_grade == "freeze":
        problems.append("E_POLICY_DENIED: kill switch is frozen")
    if not cli_present:
        problems.append(f"{E_CLI_MISSING}: {cli_path()!r} is not executable")
    if not auth_ok:
        problems.append(f"{E_CLI_JSON}: `claude auth status` did not report a logged-in account")
    return problems


__all__ = [
    "BANNED_FLAGS",
    "ClaudeCLIError",
    "Invocation",
    "InvokeResult",
    "SAFE_PERMISSION_MODES",
    "SIGTERM_EXIT_CODE",
    "STDIN_LIMIT",
    "STDIN_POINTER",
    "WIN_CMDLINE_LIMIT",
    "argv_budget",
    "backoff_delay",
    "build_invocation",
    "cli_path",
    "concurrency_gate",
    "invoke",
    "is_retryable_failure",
    "run_with_retries",
    "safety_preflight",
]
