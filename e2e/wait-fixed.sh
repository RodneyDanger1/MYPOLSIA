#!/usr/bin/env bash
# wait-fixed.sh — poll a (re)built frontend until it serves LIVE data, via a real browser; run it
# before starting the suite after any rebuild/restart window.  (Moved into e2e/ from the G-e2e
# campaign lane, A-H11, where it polled through the owner's rebuild window.)
# See wait-fixed.cjs for the readiness criterion and why a plain 200 on /tasks is NOT one — the
# frozen-prerender defect the G-e2e campaign found served 200 with a baked-in empty state.
#
# Usage: BASE_URL=http://localhost bash wait-fixed.sh
# Env: BASE_URL (default http://localhost), WAIT_PATH, TRIES, INTERVAL_MS — see wait-fixed.cjs
#      E2E_WAIT_LOG  where the poll log goes (default: ./wait-fixed.log in the caller's dir)
#
# node is invoked from bash ON PURPOSE: a direct `node wait-fixed.cjs` in a background session on this
# Windows host aborts with "stdin is not a tty" (it killed a scratch `next build` the same way), while
# node spawned from a bash script with stdin redirected is fine.  Keep the redirection.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
LOG="${E2E_WAIT_LOG:-$PWD/wait-fixed.log}"
cd "$HERE" || exit 90      # then `node wait-fixed.cjs` stays a RELATIVE name: native tools on this host
                           # do not accept MSYS-style /c/... paths as arguments
node wait-fixed.cjs < /dev/null >> "$LOG" 2>&1
RC=$?
echo "WAIT-FIXED EXIT=$RC" >> "$LOG"
tail -1 "$LOG"
exit $RC
