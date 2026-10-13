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
# Env: E2E_RUN_LOG_DIR  where the log goes (default: the caller's current directory)
#
# The suite always runs from THIS script's own directory — the e2e/ tree that owns
# playwright.config.ts and node_modules — so the tool can be launched from anywhere and bakes in no
# machine paths.  Exit code = the suite's exit code (0 = green).
set -u

if [ $# -lt 2 ]; then
  echo "usage: bash run-suite.sh <label> <base-url> [extra playwright args...]" >&2
  exit 2
fi
LABEL="$1"; BASE="$2"; shift 2

HERE="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="${E2E_RUN_LOG_DIR:-$PWD}"
mkdir -p "$LOG_DIR"
OUT="$LOG_DIR/run-$LABEL.txt"

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
