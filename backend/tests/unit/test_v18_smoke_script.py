"""Wave V18 / lane L5 — `scripts/smoke.sh`: the one-command, GET-only acceptance that did not exist.

RED-first pins, written before the script.  Three classes of pin:

* STATIC — the script must carry the safety structure: POSIX sh, `set -u`, no write verbs, no `jq`
  hard dependency, no docker, no traps (read-only), a FAIL counter, and an explicit exit code.
* DRY-RUN — `--dry-run` prints the whole plan and makes ZERO network calls.  The seam is a PATH
  shim named `curl` that records every invocation (the same process-boundary seam
  `test_v17_restore_script.py` uses for docker); a dry run that touched the network would append
  a line to that log.
* BEHAVIOUR — against the same shim: a valid key turns the protected checks green and the run exits
  0; a missing key names `FAIL auth-200` and exits 1; a failing check is named and exits 1; the
  backups check is WARN-only and cannot fail a run.

The REAL run against the live stack is `test_live_run_reports_named_checks_when_the_stack_is_up`,
which SKIPS with a reason when nothing answers on `SMOKE_BASE` (default http://127.0.0.1) or when
no `API_KEY` is exported — the parent runs it at settle.  No test here writes to the repo, posts
anything, or starts/stops a service.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "smoke.sh"

#: The named checks the script must print, in order.  Names are the contract: an operator reads
#: `FAIL <name>: <detail>`, so a renamed check is a doc/impl drift the pins catch.
CHECK_NAMES = (
    "health",
    "headers",
    "auth-401",
    "auth-200",
    "capabilities",
    "phone-summary",
    "phone-media",
    "docs",
    "server-header",
    "backups",
)

# A curl shim: records every call, then answers from the frozen response table.  No sockets, no
# stack, no state.  `CURL_DOCS_CODE` lets a test choose what /docs answers; the `X-API-Key` header
# decides whether the protected route answers 200 or 401.
CURL_SHIM = """#!/bin/sh
echo "$*" >> "$CURL_CALL_LOG"
out=""; hdr=""; url=""; key=""
docs_code=${CURL_DOCS_CODE:-404}
while [ $# -gt 0 ]; do
  case "$1" in
    -o) out=$2; shift 2 ;;
    -D) hdr=$2; shift 2 ;;
    -H) case "$2" in X-API-Key:*) key=1 ;; esac; shift 2 ;;
    --max-time) shift 2 ;;
    -w) shift 2 ;;
    *) url=$1; shift ;;
  esac
done
write_body() { [ -n "$out" ] && printf '%s' "$1" > "$out"; return 0; }
write_hdr() {
  [ -n "$hdr" ] || return 0
  {
    printf 'HTTP/1.1 200 OK\\r\\n'
    printf "Content-Security-Policy: default-src 'self'\\r\\n"
    printf 'X-Frame-Options: SAMEORIGIN\\r\\n'
    printf 'X-Content-Type-Options: nosniff\\r\\n'
    printf 'Server: webserver\\r\\n'
    printf 'X-Powered-By: %s\\r\\n' "${CURL_POWERED_BY:-nginx}"
  } > "$hdr"
}
case "$url" in
  */api/v1/health)
    write_hdr; write_body '{"status":"ok"}'; echo 200 ;;
  */api/v1/notifications)
    if [ -n "$key" ]; then
      write_body '{"items":[]}'; echo 200
    else
      write_body '{"ok":false,"error":{"code":"E_AUTH","message":"missing"}}'; echo 401
    fi ;;
  */api/v1/ops/capabilities)
    write_body '{"agents":[{"agent_type":"orchestrator","tools":[],"action_classes":[],"channels":[]}],"registry_tools":[]}'
    echo 200 ;;
  */api/v1/phone/summary)
    write_body '{"ok":true,"data":{"store_status":"ok","unread_total":0,"threads":[]}}'
    echo 200 ;;
  */docs)
    write_body '{"detail":"Not Found"}'; echo "$docs_code" ;;
  *)
    write_hdr; write_body ''; echo 200 ;;
esac
"""


def _sh() -> str:
    found = shutil.which("sh")
    assert found, "this suite drives scripts/smoke.sh through a real POSIX sh"
    return found


def _make_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8", newline="\n")
    os.chmod(path, 0o755)


def _run(
    tmp_path: Path,
    *args: str,
    env_extra: dict[str, str] | None = None,
    with_shim: bool = True,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    """Run the script with a `curl` shim first on PATH; return (proc, call_log)."""
    call_log = tmp_path / "curl-calls.log"
    call_log.write_text("", encoding="utf-8")
    path = os.environ["PATH"]
    if with_shim:
        shim = tmp_path / "shim"
        shim.mkdir(exist_ok=True)
        _make_executable(shim / "curl", CURL_SHIM)
        path = shim.as_posix() + os.pathsep + path
    env = {
        **os.environ,
        "PATH": path,
        "CURL_CALL_LOG": call_log.as_posix(),
        **(env_extra or {}),
    }
    proc = subprocess.run(
        [_sh(), SCRIPT.as_posix(), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    return proc, call_log


def _root_with_env(tmp_path: Path, sandbox_mode: str) -> Path:
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    (root / ".env").write_text(
        f"API_KEY=from-dotenv-never-read\nSANDBOX_MODE={sandbox_mode}\n", encoding="utf-8"
    )
    return root


# ── static pins ──────────────────────────────────────────────────────────────────────────────


def test_script_exists_and_parses_under_posix_sh() -> None:
    assert SCRIPT.is_file(), f"missing {SCRIPT}"
    proc = subprocess.run([_sh(), "-n", SCRIPT.as_posix()], capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, proc.stderr


def test_script_is_executable_and_has_a_sh_shebang() -> None:
    first = SCRIPT.read_text(encoding="utf-8").splitlines()[0]
    assert first.startswith("#!") and "sh" in first, first
    assert os.access(SCRIPT, os.X_OK), "smoke.sh must be executable (git mode 100755)"


def test_static_pins_the_read_only_structure() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    assert "set -u" in body
    assert "docker" not in body.lower(), "the smoke must not talk to docker at all"
    assert "trap " not in body, "the smoke is read-only; it must not install teardown traps"
    assert "mktemp" in body, "temporary bodies belong in a mktemp dir"
    # No write verb may appear on any curl line.
    for line in body.splitlines():
        if "curl" not in line:
            continue
        for verb in ("-X", "--request", "--data", "--form", "-F ", " -T "):
            assert verb not in line, f"write verb {verb!r} on a curl line: {line.strip()}"


def test_static_pins_no_jq_dependency_and_a_named_python_or_grep_fallback() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    assert "| jq" not in body and "command -v jq" not in body, "jq must never be required"
    assert "python3" in body and "python" in body, "JSON parsing prefers python3"
    assert "grep" in body, "a grep-based fallback (named) must exist when no python is present"


def test_static_pins_the_exit_contract() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"FAILS=\$\(\(FAILS \+ 1\)\)", body), "a FAIL counter must exist"
    assert "RESULT: PASS" in body and "RESULT: FAIL" in body
    assert re.search(r"^\s*exit 1$", body, re.MULTILINE), "a failing run must exit 1"
    assert re.search(r"^\s*exit 0$", body, re.MULTILINE), "a passing run must exit 0"


def test_static_pins_every_check_name() -> None:
    body = SCRIPT.read_text(encoding="utf-8")
    for name in CHECK_NAMES:
        assert name in body, f"the script never names the {name} check"


# ── dry run (executed for real) ──────────────────────────────────────────────────────────────


def test_dry_run_prints_the_plan_and_makes_no_network_calls(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "--dry-run")

    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert "DRY RUN" in out.upper()
    for name in CHECK_NAMES:
        assert name in out, f"the plan must name the {name} check"
    assert "no network" in out.lower() or "0 requests" in out.lower()
    assert calls.read_text(encoding="utf-8") == "", "a dry run must not invoke curl at all"


def test_dry_run_prints_the_base_and_key_presence_but_never_the_key(tmp_path: Path) -> None:
    sentinel = "sk-live-SENTINEL-must-never-be-printed"
    proc, _ = _run(
        tmp_path,
        "--dry-run",
        "--base",
        "http://127.0.0.1:9999",
        "--key-from-env",
        "SMOKE_TEST_KEY",
        env_extra={"SMOKE_TEST_KEY": sentinel},
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout + proc.stderr
    assert sentinel not in out, "the key must never be printed"
    assert "127.0.0.1:9999" in out
    assert f"key: ${'SMOKE_TEST_KEY'} is present" in out, "the plan must state key presence"


# ── behaviour against the shim ──────────────────────────────────────────────────────────────


def test_a_valid_key_passes_the_protected_checks_and_exits_zero(tmp_path: Path) -> None:
    root = _root_with_env(tmp_path, "false")
    proc, calls = _run(
        tmp_path,
        "--base",
        "http://127.0.0.1:80",
        "--root",
        root.as_posix(),
        env_extra={"API_KEY": "a-real-key-value"},
    )

    out = proc.stdout
    assert proc.returncode == 0, out + proc.stderr
    assert "PASS health:" in out
    assert "PASS auth-401:" in out
    assert "PASS auth-200:" in out
    assert "PASS headers:" in out
    assert "PASS capabilities:" in out
    assert "PASS phone-summary:" in out
    assert "PASS server-header:" in out
    assert "PASS docs:" in out, "SANDBOX_MODE=false with /docs 404 is the honest pair"
    assert "SKIP phone-media:" in out, "no thread in the summary -> named skip, never a FAIL"
    assert "WARN backups:" in out, "a clone without backups warns and still passes"
    assert "RESULT: PASS" in out
    log = calls.read_text(encoding="utf-8")
    assert "/api/v1/notifications" in log and "/api/v1/ops/capabilities" in log


def test_no_key_fails_the_protected_check_by_name_and_exits_one(tmp_path: Path) -> None:
    root = _root_with_env(tmp_path, "false")
    proc, _ = _run(
        tmp_path,
        "--base",
        "http://127.0.0.1:80",
        "--root",
        root.as_posix(),
        env_extra={"API_KEY": ""},
    )

    out = proc.stdout
    assert proc.returncode == 1, out + proc.stderr
    assert "FAIL auth-200:" in out
    assert "API_KEY" in out, "the failure must name the environment variable it wanted"
    assert "RESULT: FAIL" in out


def test_a_failing_check_is_named_and_a_raw_500_is_never_green(tmp_path: Path) -> None:
    root = _root_with_env(tmp_path, "false")
    broken = CURL_SHIM.replace(
        'write_body \'{"status":"ok"}\'; echo 200',
        'write_body \'{"detail":"boom"}\'; echo 500',
    )
    shim = tmp_path / "shim"
    shim.mkdir(exist_ok=True)
    _make_executable(shim / "curl", broken)
    env = {
        **os.environ,
        "PATH": shim.as_posix() + os.pathsep + os.environ["PATH"],
        "CURL_CALL_LOG": (tmp_path / "curl-calls.log").as_posix(),
        "API_KEY": "a-real-key-value",
    }
    proc = subprocess.run(
        [_sh(), SCRIPT.as_posix(), "--base", "http://127.0.0.1:80", "--root", root.as_posix()],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )

    out = proc.stdout
    assert proc.returncode == 1, out + proc.stderr
    assert "FAIL health:" in out
    assert "500" in out
    assert "RESULT: FAIL" in out


def test_usage_refusals_exit_two(tmp_path: Path) -> None:
    proc, calls = _run(tmp_path, "--nonsense")
    assert proc.returncode == 2
    assert "REFUSED" in proc.stdout + proc.stderr
    assert calls.read_text(encoding="utf-8") == ""

    proc, _ = _run(tmp_path, "--base")
    assert proc.returncode == 2
    assert "--base" in proc.stdout + proc.stderr


def test_dry_run_does_not_read_the_dotenv_key(tmp_path: Path) -> None:
    """The key comes from the ENVIRONMENT only; a `.env` value must never be picked up silently."""
    root = _root_with_env(tmp_path, "true")
    proc, _ = _run(tmp_path, "--dry-run", "--root", root.as_posix(), env_extra={"API_KEY": ""})

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "from-dotenv-never-read" not in proc.stdout + proc.stderr
    assert "absent" in proc.stdout.lower()


# ── the real run (skips with a reason when nothing is up) ───────────────────────────────────


def _health_ok(base: str) -> bool:
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/api/v1/health", timeout=3) as resp:
            body = json.loads(resp.read().decode("utf-8"))
            return resp.status == 200 and body == {"status": "ok"}
    except (urllib.error.URLError, OSError, ValueError):
        return False


def test_live_run_reports_named_checks_when_the_stack_is_up() -> None:
    base = os.environ.get("SMOKE_BASE", "http://127.0.0.1")
    if not _health_ok(base):
        pytest.skip(f"live stack not reachable at {base} — the parent runs smoke.sh at settle")
    key = os.environ.get("API_KEY")
    if not key:
        pytest.skip("API_KEY is not exported — the live run cannot exercise the protected checks")

    proc = subprocess.run(
        [_sh(), SCRIPT.as_posix(), "--base", base],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "API_KEY": key},
    )
    out = proc.stdout
    assert "RESULT:" in out, out + proc.stderr
    assert "PASS health:" in out, out
    assert "PASS headers:" in out, out
    assert "PASS auth-401:" in out, out
    assert "PASS auth-200:" in out, out
    assert proc.returncode in (0, 1), "only the documented 0/1 verdicts are allowed"
    if proc.returncode == 1:
        assert "FAIL " in out, "a nonzero live run must name what failed"
