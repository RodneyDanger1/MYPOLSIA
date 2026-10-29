"""Meridian agent runtime — the Claude CLI invocation contract, implemented once.

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

import atexit
import contextlib
import os
import random
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.core.errors import (
    E_CLI_JSON,
    E_CLI_MISSING,
    E_CLI_TIMEOUT,
    E_POLICY_DENIED,
    E_PROMPT_TOO_LARGE,
    E_STORAGE,
    ERROR_HINTS,
    MeridianError,
)

# ── platform transport limits (CLI-CONTRACT.md §7) ───────────────────────────────────────────
WIN_CMDLINE_LIMIT = 32_767          # CreateProcessW lpCommandLine max, including the NUL
LINUX_ARG_LIMIT = 128 * 1024        # MAX_ARG_STRLEN = 32 pages x 4 KB
STDIN_LIMIT = 10 * 1024 * 1024      # documented cap for piped stdin

SIGTERM_EXIT_CODE = 143             # our own timeout kill (docs: no result recorded)

SAFE_PERMISSION_MODES = ("default", "acceptEdits", "plan", "auto", "dontAsk", "manual")
BANNED_FLAGS = ("--dangerously-skip-permissions",)
STDIN_POINTER = "Read the context from stdin and execute the task described there."


# ── spill-file containment (D6; audit L3-03) ─────────────────────────────────────────────────
# Every system-prompt spill file this process creates, so a normal crash (unhandled exception,
# interpreter exit) still removes them. A hard force-kill (TerminateProcess / SIGKILL of the
# parent) cannot run atexit handlers — that residual is named in the receipt, not hidden.
_SPILL_TMP_FILES: set[str] = set()


@atexit.register
def _cleanup_spill_tmp_files() -> None:
    for path in list(_SPILL_TMP_FILES):
        with contextlib.suppress(OSError):
            os.unlink(path)

SAFE_ENV_PASSTHROUGH = (
    "PATH", "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "TEMP", "TMP",
    "LANG", "LC_ALL", "SystemRoot", "COMSPEC", "PATHEXT", "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_PROMPT_CACHE_TTL",
)

SPILL_PLACEHOLDER = "@@MERIDIAN_SYSTEM_PROMPT_FILE@@"


# ── run-workspace + child-environment containment (AC-02) ───────────────────────────────────
# WHAT WAS WRONG (BUGS.json AC-02, P0): `build_invocation` set no `cwd`, so `subprocess.Popen`
# inherited the worker's working directory — the BACKEND directory, which holds `.env`,
# `data/secrets.json` and `data/deepseek_api_key.json` — and the allowlist granted bare
# `Read`/`Edit`/`Write` (no path specifier).  On the documented no-DeepSeek-key fallback that made
# every provider key the model could name reachable and writable.
#
# WHAT THIS SECTION CHANGES, and what it can PROVE:
#   1. every spawned CLI process gets a NON-None cwd: a per-invocation directory under a root
#      that is NOT inside the backend's credential tree.  The directory is created at spawn time
#      (`ensure_run_workspace`) and a root it cannot create/verify is a NAMED `E_STORAGE` /
#      `E_POLICY_DENIED` refusal — never a silent fallback to the inherited cwd;
#   2. the child environment is built from the `SAFE_ENV_PASSTHROUGH` whitelist only, and an
#      override that names a credential-bearing variable is refused BY NAME (a caller cannot
#      quietly hand the CLI `DEEPSEEK_API_KEY`/`ANTHROPIC_API_KEY`/`SENDGRID_API_KEY`);
#   3. the file-tool ALLOW rules are path-scoped (`Read(./**)` etc., policy_firewall.py) so the
#      documented permission-rule language cannot mean "any path on this machine".
#
# THE UNVERIFIED LEG, named rather than implied: the `claude` binary is NOT installed on this
# host, so the CLI's own resolution of `Read(./**)` against its cwd cannot be executed here.
# Everything this module can assert locally (cwd, env, argv, secret-store placement) IS asserted
# in tests/unit/test_w1_cli_containment.py.  The absolute-path residual — a model typing
# `Read(C:\...\backend\.env)` — remains the CLI's own file-access policy; the cwd is the boundary
# this process can enforce, and it no longer starts inside the secrets.

#: Operator override for the run-workspace ROOT.  A relative value is anchored at the backend
#: directory (the `deepseek_key_file` anchoring rule), never at the process cwd.
CLI_WORKSPACE_ROOT_ENV = "MERIDIAN_CLI_WORKSPACE_ROOT"

#: Layout under the root: `<root>/runs/<name>` — the same `runs/` shape tool_registry uses for
#: workspace-bound registry tools, so an operator has one place to look.
CLI_RUNS_SUBDIR = "runs"
CLI_RUN_PREFIX = "cli"
_RUN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: Environment variable NAMES that carry a credential.  The whitelist above already keeps them
#: out of the inherited environment; this list closes the explicit-override hole (`env_overrides`
#: / `extra_env`), because "no secret in the child" must not depend on the caller remembering it.
#:
#: URL-STYLE CREDENTIALS (SENTINEL, 2026-10-28): a connection string carries its password in the
#: VALUE (`postgresql://user:pw@host/db`, `redis://:pw@host/0`, `https://key@host/…`,
#: `amqp://user:pw@host//`) while its NAME carries none of the markers above, so `DATABASE_URL`,
#: `REDIS_URL`, `*_DSN` and `*CONNECTION_STRING*` were forwarded verbatim — measured red leg:
#: tests/unit/test_sentinel_agent_boundary.py (4 failed, "DID NOT RAISE", before this line).
#: The postgres password IS a live secret in this deployment and the CLI child is the one process
#: this module promises never sees it.  Re-measured after the fix: 5/5 green.
_CREDENTIAL_ENV_MARKERS = (
    "API_KEY", "APIKEY", "API-KEY", "ACCESS_KEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD",
    "PRIVATE_KEY", "CREDENTIAL",
    # URL/connection-string shapes: the secret rides in the value, not the name.
    "DSN", "DATABASE_URL", "REDIS_URL", "CONNECTION_STRING", "BROKER_URL",
)


def backend_root() -> Path:
    """The backend directory (`app/agents/claude_cli.py` -> `backend/`), never the process cwd."""
    return Path(__file__).resolve().parents[2]


def _absolute(raw: str | Path) -> Path:
    """`raw` as an absolute path: relative values are anchored at the BACKEND, not the cwd."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = backend_root() / path
    try:
        return path.resolve()
    except (OSError, ValueError):  # a path the OS cannot resolve is still a path to refuse on
        return path


def secret_store_paths() -> tuple[Path, ...]:
    """Every local credential store this process knows about, resolved defensively at call time.

    The two stores are the ones `services/secret_store.py` really writes (`MERIDIAN_SECRETS_FILE` /
    `DEEPSEEK_KEY_FILE`, else the repo-anchored defaults) plus the backend `.env`, which
    docker-compose exports into the container and which the README documents as the place
    deployment credentials live.  This is a *containment* list, not a reader: nothing here logs,
    opens or returns a value.
    """
    candidates: list[str] = [str(backend_root() / ".env")]
    # SV-W4-05 (wave 5): the two setting names are now REAL Settings fields, read directly.  The
    # old loop read them as `getattr(settings, setting_name, "")` over a variable name, which
    # could never be checked by a census and could never be configured while the field was absent.
    for env_name, value, default in (
        ("DEEPSEEK_KEY_FILE", settings.deepseek_key_file, "data/deepseek_api_key.json"),
        ("MERIDIAN_SECRETS_FILE", settings.secrets_file, "data/secrets.json"),
    ):
        raw = os.environ.get(env_name) or value or default
        candidates.append(str(raw))
    resolved = [str(_absolute(raw)) for raw in candidates]
    return tuple(Path(item) for item in dict.fromkeys(resolved))


def cli_workspace_root() -> Path:
    """The root a CLI run directory may live under: `MERIDIAN_CLI_WORKSPACE_ROOT`, else the OS temp."""
    raw = (os.getenv(CLI_WORKSPACE_ROOT_ENV) or "").strip()
    return _absolute(raw) if raw else _absolute(Path(tempfile.gettempdir()) / "meridian-cli-workspace")


def _new_run_name() -> str:
    return f"{CLI_RUN_PREFIX}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


def planned_run_workspace(
    *, root: str | Path | None = None, run_id: str | None = None
) -> Path:
    """The path the child WILL run in (no filesystem side effect).

    The caller can pin `root` (tests) and `run_id` (a caller that already owns a workspace); the
    name is validated so a run id can never traverse out of `runs/`.
    """
    base = _absolute(root) if root is not None else cli_workspace_root()
    name = run_id or _new_run_name()
    if not _RUN_NAME_RE.fullmatch(name):
        raise MeridianError(
            E_STORAGE,
            f"refusing a CLI run workspace with the invalid name {name!r}",
            how_to_fix="pass a run id of 1-64 letters, digits, '.', '_' or '-' (no separators)",
            context={"run_id": name},
        )
    return base / CLI_RUNS_SUBDIR / name


def workspace_refusal(directory: str | Path) -> MeridianError | None:
    """The named refusal for an unsafe run cwd, or None when the directory is acceptable.

    Unsafe is BOTH directions:
      * the cwd CONTAINS a credential store (`./secrets.json` would name a real secret), and
      * the cwd is INSIDE a directory that directly holds one — `backend/` holds `.env`,
        `backend/data/` holds both stores — because `../` traversal from a subdirectory would
        reach it, and "the CLI inherits the backend cwd" is exactly the AC-02 defect.
    The shipped default (the OS temp dir) satisfies both; an operator override that does not is
    refused by name with the fix, never silently accepted.
    """
    resolved = _absolute(directory)
    for secret in secret_store_paths():
        secret_dir = secret.parent
        try:
            contains = secret == resolved or secret.is_relative_to(resolved)
            inside = resolved == secret_dir or resolved.is_relative_to(secret_dir)
        except (OSError, ValueError):  # pragma: no cover - cross-drive comparison raises ValueError
            continue
        if contains:
            return MeridianError(
                E_POLICY_DENIED,
                f"refusing to run the CLI with cwd {str(resolved)!r}: it contains (or is) the "
                f"credential store {str(secret)!r}, so a bare relative read could open a secret",
                how_to_fix=(
                    f"set {CLI_WORKSPACE_ROOT_ENV} to a directory this process owns and that holds "
                    "no credential store (the default is the system temp dir), then retry"
                ),
                context={"cwd": str(resolved), "credential_store": str(secret)},
            )
        if inside:
            return MeridianError(
                E_POLICY_DENIED,
                f"refusing to run the CLI with cwd {str(resolved)!r}: it is inside "
                f"{str(secret_dir)!r}, the directory that holds the credential store "
                f"{str(secret)!r}, so '../' traversal could reach a secret",
                how_to_fix=(
                    f"set {CLI_WORKSPACE_ROOT_ENV} to a directory outside the backend's credential "
                    "tree (the default is the system temp dir), then retry"
                ),
                context={"cwd": str(resolved), "credential_directory": str(secret_dir)},
            )
    return None


def ensure_run_workspace(directory: str | Path | None = None) -> Path:
    """Create (0700) and return the run cwd the child will use — never None, never unsafe.

    A None argument means "this invocation carries no workspace": a fresh per-run directory is
    chosen.  Creation or verification failure is a NAMED refusal — falling back to the inherited
    cwd is exactly the defect this function exists to prevent.
    """
    candidate = planned_run_workspace() if directory is None else _absolute(directory)
    refusal = workspace_refusal(candidate)
    if refusal is not None:
        raise refusal
    try:
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise MeridianError(
            E_STORAGE,
            f"the CLI run workspace {str(candidate)!r} could not be created "
            f"({type(exc).__name__}: {exc})",
            how_to_fix=(
                f"set {CLI_WORKSPACE_ROOT_ENV} to a writable directory outside the backend's "
                "credential tree and retry; the CLI is not run without its own workspace"
            ),
            context={"cwd": str(candidate)},
        ) from exc
    if not candidate.is_dir():  # pragma: no cover - mkdir("a/file/..") cannot land here
        raise MeridianError(
            E_STORAGE,
            f"the CLI run workspace {str(candidate)!r} is not a directory",
            how_to_fix=f"point {CLI_WORKSPACE_ROOT_ENV} at a directory",
            context={"cwd": str(candidate)},
        )
    return candidate


def _credential_shaped(name: str) -> bool:
    upper = name.upper()
    return any(marker in upper for marker in _CREDENTIAL_ENV_MARKERS)


def child_environment(
    *,
    env_overrides: dict[str, str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """The CLI child's ENTIRE environment: the whitelist, plus explicit non-credential additions.

    Nothing is inherited implicitly.  An explicit override whose NAME is credential-shaped is a
    named refusal (a caller trying to hand the child a provider key is a defect, not a preference).
    """
    env = {key: os.environ[key] for key in SAFE_ENV_PASSTHROUGH if key in os.environ}
    for label, source in (("env_overrides", env_overrides), ("extra_env", extra_env)):
        for raw_name, raw_value in (source or {}).items():
            name = str(raw_name)
            if _credential_shaped(name):
                raise MeridianError(
                    E_POLICY_DENIED,
                    f"refusing to pass {name!r} ({label}) into the Claude CLI child process: the "
                    "child environment is a whitelist and provider credentials are not part of it",
                    how_to_fix=(
                        "authenticate the CLI itself (claude login, or CLAUDE_CONFIG_DIR pointing at "
                        "an existing login) instead of forwarding provider keys; the backend keeps "
                        "its own credentials to itself"
                    ),
                    context={"variable": name, "source": label},
                )
            env[name] = str(raw_value)
    return env



class ClaudeCLIError(MeridianError):
    """Historical name from the agent-layer contract (SPEC-AGENTS.md §1.8).

    It IS a `MeridianError`, so callers that catch the named-refusal type keep working; the
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
    #: W8-LD-02: the wall clock this WHOLE logical run may take — every provider call of its tool
    #: loop, and (because `run_with_retries` re-dispatches the SAME invocation) every attempt of
    #: the in-run ladder see what is LEFT of it.  `None` = no run-level deadline: a hand-built
    #: `Invocation` keeps the pre-wave-9 contract (`timeout_s` per call, nothing above it).
    run_budget_seconds: float | None = None
    #: The absolute `time.monotonic()` instant that budget expires at.  Set by
    #: `build_invocation` (the ONE builder every agent uses) from the worker's soft time limit —
    #: see `run_wall_clock_budget`.
    deadline_monotonic: float | None = None

    def estimated_command_line_chars(self) -> int:
        return sum(len(a) + 1 for a in self.argv)

    def remaining_seconds(self, now: float | None = None) -> float | None:
        """Seconds left of the run budget; `None` when this invocation has no run deadline."""
        if self.deadline_monotonic is None:
            return None
        return float(self.deadline_monotonic) - (time.monotonic() if now is None else now)

    def redacted_preview(self) -> str:
        """Shell-quote the argv for logs; secrets never ride on argv (auth lives in ~/.claude)."""
        return " ".join(shlex.quote(a) for a in self.argv)


def cli_path() -> str:
    """`settings.claude_cli_path`, with the `CLAUDE_CLI_PATH` env override (README:77)."""
    return (os.getenv("CLAUDE_CLI_PATH") or settings.claude_cli_path or "claude").strip()


# ── the run's wall-clock budget (W8-LD-02) ──────────────────────────────────────────────────
# WHAT WAS WRONG (measured by wave-8's matrix, row B07): one logical run's worst case was
# `attempts x claude_timeout_seconds + backoff` = 3 x 300 s + 4.5 s = **904.5 s**, but Celery's
# SOFT time limit in this deployment is **840 s** (hard kill 900 s).  A run that reached its
# third provider timeout therefore could NOT settle inside the worker that ran it:
# `SoftTimeLimitExceeded` fired first, the task row stayed `in_progress` until the 900 s lease
# sweep and the `agent_runs` row stayed `running` until the 2 h stale-run reaper.  The binding
# promise of this module's ladder ("the run always settles itself") was false in exactly the
# case the ladder exists for.
#
# WHAT THIS SECTION CHANGES.  The ladder's arithmetic is no longer a claim in a docstring: the
# run gets ONE wall-clock budget, DERIVED from the worker's own kill ceiling, and both runtimes
# enforce it (every provider call is clamped to what is left; a spent budget is the named
# `E_CLI_TIMEOUT` refusal instead of a kill):
#
#     budget  = soft_limit - settle_reserve - ladder_backoff_total
#     worst   = budget + ladder_backoff_total            (the sleeps after the budget is spent)
#             = soft_limit - settle_reserve             <= soft_limit          [B07]
#
# At the shipped numbers: soft 840 s, hard 900 s, retries 2, backoff 1.5 s ->
#     settle_reserve   = max(30, 900 - 840)      = 60 s
#     backoff_total    = 1.5 + 3.0               = 4.5 s
#     budget           = 840 - 60 - 4.5          = 775.5 s
#     worst            = 775.5 + 4.5             = 780 s   (840 - 60, 60 s left to settle)
#
# WHY A BUDGET AND NOT A SMALLER PER-CALL TIMEOUT.  The alternative (clamp each call to
# `(soft - reserve) / attempts` = 258 s) also fits the ladder, but it shrinks EVERY call — and it
# still leaves the DeepSeek tool loop (up to `deepseek_tool_loop_max_iterations` = 4 provider
# calls in ONE dispatch) at 4 x 258 = 1032 s > 840 s.  A run-level deadline bounds the ladder AND
# the tool loop with the same number, keeps a single call's full 300 s ceiling, and stays correct
# if an operator retunes `CELERY_SOFT_TIME_LIMIT` (the budget is derived, never a literal).

#: The smallest settle window this module will accept, even when an operator sets the soft limit
#: EQUAL to the hard limit (the platform then gives a run no grace at all after
#: `SoftTimeLimitExceeded`, which is itself the defect this floor keeps visible).
MIN_SETTLE_RESERVE_SECONDS = 30.0

#: The deepest ladder sleep a run may pay, matching `backoff_delay`'s own cap.
MAX_BACKOFF_SECONDS = 30.0


def settle_reserve_seconds() -> float:
    """The wall clock a run needs AFTER its last provider call to settle inside its worker.

    Derived from the platform's own two clocks — the gap between the soft and the hard limit is
    exactly how long Celery lets a task keep running after `SoftTimeLimitExceeded` — floored at
    `MIN_SETTLE_RESERVE_SECONDS` so an `soft == hard` deployment cannot silently get a 0 s
    reserve (a settle that needs the write it cannot finish is the wedge this bounds).
    """
    gap = float(settings.celery_task_time_limit) - float(settings.celery_soft_time_limit)
    return max(MIN_SETTLE_RESERVE_SECONDS, gap)


def ladder_backoff_total_seconds(attempts: int | None = None) -> float:
    """The most a whole ladder can SLEEP between its attempts (`backoff_delay`'s upper bound).

    `attempts` is the ATTEMPT count (1 + retries), the same unit `run_with_retries` measures and
    returns — NOT the retry count, which is one less.  `None` means the configured ladder
    (`1 + settings.agent_max_retries`).
    """
    total_attempts = max(1, 1 + int(settings.agent_max_retries) if attempts is None else int(attempts))
    return float(
        sum(
            min(MAX_BACKOFF_SECONDS, float(settings.agent_retry_backoff_seconds) * 2**attempt)
            for attempt in range(total_attempts - 1)
        )
    )


def run_wall_clock_budget(*, soft_limit_s: float | None = None) -> float:
    """The wall clock ONE logical run (all its attempts and tool-loop calls) may take.

    `soft_limit - settle_reserve - ladder_backoff_total`, floored at 1 s: a run must always be
    allowed at least one provider call, and an operator who configures a soft limit below the
    ladder's own sleep budget gets a bounded run rather than a division by zero or a negative
    timeout.  Per-call bounds are NOT changed by this number (a single call keeps
    `claude_timeout_seconds`); it is the aggregate ceiling the runtimes enforce.
    """
    soft = float(settings.celery_soft_time_limit if soft_limit_s is None else soft_limit_s)
    # W9-LD-02 hardening: an operator may configure soft >= hard (nothing else validates it); the
    # run budget must still finish INSIDE the hard kill, so the effective ceiling is min(soft, hard).
    hard = float(settings.celery_task_time_limit)
    soft = min(soft, hard)
    return max(1.0, soft - settle_reserve_seconds() - ladder_backoff_total_seconds())



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
    workspace_root: str | Path | None = None,
    workspace_run_id: str | None = None,
) -> Invocation:
    """Build the exact argv for one headless call.

    Safe defaults for an unattended business agent: `bare=True` (no ambient hooks/plugins),
    `permission_mode='dontAsk'` (never bypass), an explicit allowlist, `--permission-prompts
    none` (never wait on a prompt nobody can answer), and `--output-format json` always.

    Transport rule: the prompt rides on argv while the whole command line fits the budget;
    otherwise (or whenever `CLAUDE_PROMPT_VIA_STDIN` is on) it is piped over stdin, and a
    prompt larger than the documented 10 MB stdin cap is a named `E_PROMPT_TOO_LARGE`
    refusal — the caller must spill it to a file and reference the path.

    AC-02: the invocation ALWAYS carries a `cwd` — a per-invocation directory under
    `cli_workspace_root()` (`workspace_root`/`workspace_run_id` exist for tests and for a caller
    that already owns a workspace).  A root that contains a credential store is the named
    `E_POLICY_DENIED`; the directory itself is created at spawn time (`invoke()`), so building an
    invocation is side-effect-free.
    """
    if permission_mode not in SAFE_PERMISSION_MODES:
        raise ValueError(
            f"refusing to build a Meridian invocation with permission_mode={permission_mode!r} "
            f"(safe modes: {', '.join(SAFE_PERMISSION_MODES)}; bypassPermissions is banned)"
        )
    budget = argv_budget(max_argv_chars)
    run_budget = run_wall_clock_budget()
    via_stdin_default = bool(settings.claude_prompt_via_stdin)
    want_stdin = via_stdin_default if prompt_via_stdin is None else bool(prompt_via_stdin)

    planned_workspace = planned_run_workspace(root=workspace_root, run_id=workspace_run_id)
    workspace_problem = workspace_refusal(planned_workspace)
    if workspace_problem is not None:
        raise workspace_problem

    if len(prompt.encode("utf-8")) > STDIN_LIMIT:
        raise MeridianError(
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
            # AC-02: `--add-dir` is a RE-GRANT.  Adding a directory that holds a credential store
            # would undo the cwd containment, so it is refused by name here (no current caller
            # passes one; the guard is what keeps a future caller from re-opening the hole).
            added = _absolute(d)
            for secret in secret_store_paths():
                if secret == added or secret.is_relative_to(added):
                    raise MeridianError(
                        E_POLICY_DENIED,
                        f"refusing --add-dir {d!r}: it would re-grant the CLI the credential "
                        f"store {str(secret)!r}",
                        how_to_fix=(
                            "pass only directories that hold the work product; credential stores "
                            "belong to the backend process, never to the model's file tools"
                        ),
                        context={"add_dir": str(added), "credential_store": str(secret)},
                    )
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
                raise ValueError(f"refusing to build a Meridian invocation containing {banned}")

        return Invocation(
            argv=argv,
            stdin_text=stdin_text,
            cwd=str(planned_workspace),
            transport=transport,
            prompt_file=prompt_file,
            system_prompt_file_content=sys_prompt_file,
            timeout_s=int(timeout_s or settings.claude_timeout_seconds),
            schema_name=schema_name,
            # W8-LD-02: ONE wall clock for the whole run, derived from the worker's soft limit.
            # `run_with_retries` re-dispatches this SAME invocation, so a later attempt inherits
            # what the earlier one left — the ladder cannot multiply the budget.
            run_budget_seconds=run_budget,
            deadline_monotonic=time.monotonic() + run_budget,
        )

    inv = _assemble(prompt, "argv", None)
    if inv.estimated_command_line_chars() > budget and not want_stdin:
        raise MeridianError(
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
        raise MeridianError(
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


def _call_timeout(inv: Invocation, *, now: float | None = None) -> int:
    """The wall clock THIS provider call may take: the per-call ceiling, capped by the run budget.

    Raises the named `E_CLI_TIMEOUT` refusal when the run's budget is already spent — a spent
    budget must never become a fresh `timeout_s` (that is the defect: three 300 s calls inside an
    840 s worker).  `timeout_s` itself is untouched, so a single call keeps its full ceiling.
    """
    remaining = inv.remaining_seconds(now)
    if remaining is None:
        return int(inv.timeout_s)
    if remaining <= 0:
        raise MeridianError(
            E_CLI_TIMEOUT,
            (
                f"the run's wall-clock budget ({inv.run_budget_seconds or 0:.1f}s, derived from "
                "the worker's soft time limit) is spent; refusing to start another provider call"
            ),
            how_to_fix=ERROR_HINTS[E_CLI_TIMEOUT],
            context={
                "run_budget_seconds": inv.run_budget_seconds,
                "remaining_seconds": round(remaining, 3),
                "timeout_s": int(inv.timeout_s),
                "transport": inv.transport,
            },
        )
    return max(1, int(min(float(inv.timeout_s), remaining)))


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

    W8-LD-02: this call's wall clock is `min(inv.timeout_s, what is left of the run budget)`.  A
    run whose budget is already spent refuses BY NAME (`E_CLI_TIMEOUT`, "the run's wall-clock
    budget is spent") WITHOUT spawning — the run then settles itself inside its worker instead of
    being killed by Celery's soft/hard limit and left for the reapers.
    """
    argv = list(inv.argv)
    argv[0] = executable or cli_path()
    env = child_environment(env_overrides=inv.env_overrides, extra_env=extra_env)
    # W8-LD-02: refuse a spent budget BEFORE any filesystem side effect or spawn.
    timeout_s = _call_timeout(inv)
    # AC-02 — NEVER spawn with cwd=None (the pre-fix code passed `cwd=inv.cwd`, which was None for
    # every agent, so the child inherited the BACKEND directory and could open `.env`,
    # `data/secrets.json` and `data/deepseek_api_key.json`).  An invocation built by a hand-rolled
    # caller gets a fresh run directory too; one that cannot be created is a named refusal.
    run_workspace_existed = bool(_absolute(inv.cwd).is_dir()) if inv.cwd else False
    run_workspace = ensure_run_workspace(inv.cwd)
    command_cwd = str(run_workspace)

    tmp_files: list[str] = []
    if inv.system_prompt_file_content is not None:
        fd, path = tempfile.mkstemp(prefix="meridian-sysprompt-", suffix=".txt")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(inv.system_prompt_file_content)
        tmp_files.append(path)
        _SPILL_TMP_FILES.add(path)  # atexit net (D6) in case of a crash before the finally
        argv = [path if a == SPILL_PLACEHOLDER else a for a in argv]

    try:
        gate = concurrency_gate()
        with gate:
            if spawn is not None:
                stdout, code = spawn(argv, inv.stdin_text, env, timeout_s)
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
                cwd=command_cwd,
                env=env,
                # POSIX: run in our OWN session so `_kill_process_tree` can kill the whole GROUP
                # (the CLI's grandchildren included); without this the group is never ours (D6).
                start_new_session=(os.name == "posix"),
            )
            try:
                stdout, stderr = proc.communicate(input=inv.stdin_text, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                raise MeridianError(
                    E_CLI_TIMEOUT,
                    f"claude CLI exceeded {timeout_s}s (killed; the in-flight turn is unfinished)",
                    how_to_fix=ERROR_HINTS[E_CLI_TIMEOUT],
                    context={
                        "timeout_s": timeout_s,
                        "ceiling_s": int(inv.timeout_s),
                        "run_budget_seconds": inv.run_budget_seconds,
                        "transport": inv.transport,
                    },
                ) from None
            return InvokeResult(
                stdout or "", stderr or "", int(proc.returncode), time.monotonic() - started,
                inv.transport,
            )
    except FileNotFoundError as exc:
        raise MeridianError(
            E_CLI_MISSING,
            f"claude CLI not found at {argv[0]!r}",
            how_to_fix=ERROR_HINTS[E_CLI_MISSING],
            context={"cli_path": argv[0], "error": str(exc)},
        ) from exc
    finally:
        for path in tmp_files:
            with contextlib.suppress(OSError):
                os.unlink(path)
            _SPILL_TMP_FILES.discard(path)
        if not run_workspace_existed:
            # Only the directory THIS call created is removed, and only when the CLI left it
            # empty: a run that wrote files keeps them (they are the run's deliverable), while
            # the crash-free no-write path leaves no per-invocation clutter behind.
            with contextlib.suppress(OSError):
                run_workspace.rmdir()


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
    else:
        # POSIX: the child runs in its own session (Popen start_new_session) — kill the GROUP so
        # its grandchildren die with it. The docstring's promise, kept (D6; audit L3-02).
        with contextlib.suppress(OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
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
      * `E_CLI_TIMEOUT` — in THIS (envelope) shape, i.e. exit 143: our wall clock already expired
        on this exact prompt; the documented remediation is a smaller task or a higher timeout.

    The raised shape of the same code is a DIFFERENT contract (L3-13): a `MeridianError` with
    `E_CLI_TIMEOUT` thrown by `invoke()` (or the DeepSeek runtime's `httpx.TimeoutException`
    branch) IS retried by `run_with_retries` up to `agent_max_retries` with jittered backoff —
    the hang may be transient provider pressure. Worst case one hang costs
    (1 + `agent_max_retries`) x the configured timeout of a worker slot;
    `ERROR_HINTS[E_CLI_TIMEOUT]` states the same.
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

    Returns `(last_result, attempts_used)`. A spawn-level refusal (E_CLI_MISSING) is re-raised by
    `dispatch`; a RAISED `E_CLI_TIMEOUT` (the CLI wall clock, or the DeepSeek runtime's
    `httpx.TimeoutException` branch — both raise the same code) is RETRIED here with jittered
    backoff up to `settings.agent_max_retries`, because the hang may be transient provider
    pressure; the ENVELOPE shape for the same condition (exit 143 ->
    `is_retryable_failure`) is classified non-retryable instead. Everything else returns
    immediately so the caller can map it to a named code without paying for the ladder.
    """
    total = 1 + max(0, int(settings.agent_max_retries if attempts is None else attempts))
    backoff = float(settings.agent_retry_backoff_seconds if backoff_s is None else backoff_s)
    last: InvokeResult | None = None
    for attempt in range(total):
        try:
            last = dispatch()
        except MeridianError as exc:
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
    "CLI_RUNS_SUBDIR",
    "CLI_WORKSPACE_ROOT_ENV",
    "ClaudeCLIError",
    "Invocation",
    "InvokeResult",
    "MAX_BACKOFF_SECONDS",
    "MIN_SETTLE_RESERVE_SECONDS",
    "SAFE_ENV_PASSTHROUGH",
    "SAFE_PERMISSION_MODES",
    "SIGTERM_EXIT_CODE",
    "STDIN_LIMIT",
    "STDIN_POINTER",
    "WIN_CMDLINE_LIMIT",
    "argv_budget",
    "backend_root",
    "backoff_delay",
    "build_invocation",
    "child_environment",
    "cli_path",
    "cli_workspace_root",
    "concurrency_gate",
    "ensure_run_workspace",
    "invoke",
    "is_retryable_failure",
    "ladder_backoff_total_seconds",
    "planned_run_workspace",
    "run_wall_clock_budget",
    "run_with_retries",
    "safety_preflight",
    "secret_store_paths",
    "settle_reserve_seconds",
    "workspace_refusal",
]
