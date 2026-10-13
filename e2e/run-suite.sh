#!/usr/bin/env bash
# run-suite.sh — one labeled Playwright suite run against a chosen frontend, with the raw output AND
# the machine-load evidence banked in a single log file.  (Moved into e2e/ from the G-e2e campaign
# lane, A-H11; the G receipts and RESUME-A3 §8 already quoted this tool as the run recipe.)
#
# Why it exists: the campaign's verdicts quote each run's wall-clock and the host's load (a 4-core box
# shared with other agents makes load a hypothesis before it is a bug), and a bare `npx playwright
# test` loses both.  Run the suite through this so a receipt's numbers are reproducible from its log.
#
# Usage: bash run-suite.sh <label> <base-url> [extra playwright args...]
#   <label>     short run label; the log lands as <log-dir>/run-<label>.txt
#   <base-url>  frontend under test (becomes BASE_URL for playwright.config.ts)
#   extras      passed through to `npx playwright test` (e.g. --list, a spec path, --grep …)
#   --dry-run   parse args/env, run the R-W4-2 restart guard, print the exact plan — launch nothing
#   --help -h   this text, then exit 0
# Env: E2E_RUN_LOG_DIR  where the log goes (default: the caller's current directory)
#      E2E_DRY_RUN=1    same as --dry-run (for callers that cannot pass flags through)
#      E2E_BACKEND_RESTARTED=<anything>  attest that the backend was restarted after the sweep wrote
#                       its restart marker (R-W4-2) — clears the marker; _restart-guard.sh carries
#                       the canonical policy text.  Attestation only: this script has never managed
#                       any server and does not restart the backend for you.
#      E2E_RESTART_MARKER=<path>  override the restart-marker path (default:
#                       <chroma store>/.e2e-backend-restart-required, where the sweep writes it).
#
# Exit codes: 0 = suite green (or: a dry run says the real run would proceed); 2 = usage; 3 = the
#             R-W4-2 restart guard refused (a stale backend would serve this run — its message names
#             the fix); otherwise the suite's own exit code.
#
# R-W4-2 restart guard, in one paragraph: the suite-end sweep deletes chroma documents directly in
# the store; a backend that was LIVE when that happened keeps a stale in-memory collection
# (measured: memory-search 500s — W4 §3.9 / endgame V2 run 3).  The sweep therefore writes a marker
# into the store when it deletes docs while a backend answers /health, and THIS script refuses to
# start a run while that marker is present and a backend still answers — restart the backend, then
# re-run with E2E_BACKEND_RESTARTED=1.  playwright.config.ts's globalSetup (_guard.ts) enforces the
# same policy for bare `npx playwright test` invocations.
#
# The suite always runs from THIS script's own directory — the e2e/ tree that owns
# playwright.config.ts and node_modules — so the tool can be launched from anywhere and bakes in no
# machine paths.  Exit code = the suite's exit code (0 = green).
set -u

HERE="$(cd "$(dirname "$0")" && pwd)"

# Own flags are consumed here; every other argument is a positional (label, base-url) or passes
# through to playwright untouched (--list, --grep=…, spec paths).
DRY="${E2E_DRY_RUN:-}"
HELP=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --help|-h) HELP=1 ;;
    --dry-run) DRY=1 ;;
    *) ARGS+=("$a") ;;
  esac
done
if [ "${#ARGS[@]}" -gt 0 ]; then set -- "${ARGS[@]}"; else set --; fi

if [ "$HELP" = 1 ]; then
  # The header comment above IS the help text (single source — no second copy to drift).
  sed -n '2,/^set -u$/p' "$0" | sed -e 's/^# \{0,1\}//' -e '/^set -u$/d'
  exit 0
fi

if [ $# -lt 2 ]; then
  echo "usage: bash run-suite.sh <label> <base-url> [extra playwright args...]" >&2
  echo "       flags: --dry-run | --help — see --help for the env contract and the R-W4-2 guard" >&2
  exit 2
fi
LABEL="$1"; BASE="$2"; shift 2

# The R-W4-2 restart guard (canonical policy text in _restart-guard.sh).  Refuses (exit 3) while the
# sweep's marker is present and a backend still answers — a run on that backend's stale chroma cache
# fails mid-suite (measured).  A missing guard file fails LOUD: never silently skip the guard.
if [ ! -f "$HERE/_restart-guard.sh" ]; then
  echo "run-suite.sh: e2e/_restart-guard.sh is missing — refusing to run without the R-W4-2 guard." >&2
  exit 3
fi
# shellcheck source=./_restart-guard.sh
. "$HERE/_restart-guard.sh"

# The teardown sweep (playwright.config.ts -> _helpers.ts -> _sweep-fixtures.py) inherits THIS
# environment, and it refuses (by design) when it cannot confirm the tagged memory rows' chroma
# documents — deleting the sqlite half alone would strand ghost search hits. Measured during the
# V2 endgame: with CHROMA_PERSIST_DIR unset, a fully green 20/20 suite still exited 1 at teardown.
# Point it at the same store the backend serves from — and restart the backend between runs: the
# backend caches its chroma collection per process (the W4 receipt's R-W4-2 discloses why).  The
# restart used to be an operator ritual; the guard right below now enforces it.
if [ -z "${CHROMA_PERSIST_DIR:-}" ]; then
  echo "WARNING: CHROMA_PERSIST_DIR is unset — the sweep, the guard, and the backend all fall back to" >&2
  echo "         the D7-resolved store (<repo>/backend/data/chroma on this host); set it explicitly" >&2
  echo "         to pin one store for every half of the run." >&2
fi

GUARD_MODE=""
if [ -n "$DRY" ]; then GUARD_MODE="--report-only"; fi
restart_guard_check "$GUARD_MODE"
RG=$?
if [ "$RG" = 3 ]; then
  echo "run-suite.sh: refusing to start (R-W4-2 restart guard, exit 3) — the guard's message above names the fix." >&2
  exit 3
fi
if [ "$RG" != 0 ]; then
  echo "run-suite.sh: restart guard exited $RG unexpectedly — refusing to run." >&2
  exit "$RG"
fi
MARKER_PATH="$(restart_guard_marker_path)"
if [ -e "$MARKER_PATH" ]; then MARKER_STATE=present; else MARKER_STATE=absent; fi

LOG_DIR="${E2E_RUN_LOG_DIR:-$PWD}"
OUT="$LOG_DIR/run-$LABEL.txt"

if [ -n "$DRY" ]; then
  echo "DRY RUN — nothing launched; the plan a real run would execute:"
  echo "  label:         $LABEL"
  echo "  base-url:      $BASE"
  echo "  extras:        $*"
  echo "  run log:       $OUT (written by a real run only)"
  echo "  restart guard: marker=$MARKER_PATH ($MARKER_STATE) ack=${E2E_BACKEND_RESTARTED:-unset}"
  echo "  command:       (cd \"$HERE\" && BASE_URL=\"$BASE\" npx playwright test --reporter=list $*)"
  exit 0
fi

mkdir -p "$LOG_DIR"

# Machine-load evidence via the host's own tools; degrade to n/a off Windows instead of failing.
load_pct() {
  if command -v powershell >/dev/null 2>&1; then
    powershell -NoProfile -Command '(Get-CimInstance Win32_Processor | Measure-Object -Property LoadPercentage -Average).Average' 2>/dev/null || echo n/a
  else
    echo "n/a (no powershell)"
  fi
}
proc_counts() {
  if command -v tasklist >/dev/null 2>&1; then
    echo "node=$(tasklist 2>/dev/null | grep -ci node.exe) python=$(tasklist 2>/dev/null | grep -ci python.exe) chrome=$(tasklist 2>/dev/null | grep -ci chrome.exe)"
  else
    echo "n/a (no tasklist)"
  fi
}

{
  echo "=== suite run: $LABEL"
  echo "started: $(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "BASE_URL (frontend under test): $BASE"
  echo "extra args: $*"
  echo "restart guard (R-W4-2): marker=$MARKER_PATH ($MARKER_STATE) ack=${E2E_BACKEND_RESTARTED:-unset}"
  echo "cpu load% before: $(load_pct)"
  echo "process counts before: $(proc_counts)"
} > "$OUT"

S=$(date +%s)
(cd "$HERE" && BASE_URL="$BASE" npx playwright test --reporter=list "$@") >> "$OUT" 2>&1
RC=$?
E=$(date +%s)
{
  echo "=== finished: $(date '+%Y-%m-%d %H:%M:%S %z')"
  echo "=== wall-clock: $((E-S))s  exit=$RC"
  echo "cpu load% after: $(load_pct)"
} >> "$OUT"
echo "DONE label=$LABEL rc=$RC elapsed=$((E-S))s log=$OUT"
exit $RC
