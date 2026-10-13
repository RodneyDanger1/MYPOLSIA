#!/usr/bin/env bash
# _restart-guard.sh — the R-W4-2 restart guard.  CANONICAL POLICY TEXT for this protocol; the other
# implementations point here: e2e/run-suite.sh sources this file (pre-flight), e2e/_guard.ts is the
# same policy as a playwright globalSetup for bare `npx playwright test` runs, and
# e2e/_sweep-fixtures.py writes/clears the marker this guard reads.
#
# WHY: the suite-end sweep (`_sweep-fixtures.py`) deletes chroma documents directly in the persist
# store.  A backend process that is LIVE at that moment keeps an in-memory chroma collection that no
# longer matches the store — the measured failure is memory-search 500s on the next run
# (W4-ship/RECEIPT.md §3.9: phantom hits with content=None, 32 tracebacks; endgame V2 run 3
# reproduced it live).  The acceptance protocol is therefore: restart the backend between
# sweep-bearing runs.  Without mechanization that requirement is a silent trap, so this guard makes
# it loud:
#
#     marker absent                                -> proceed (silent; the normal case)
#     marker present, no backend answers /health   -> proceed; marker cleared (a backend that starts
#                                                     from here opens the store fresh)
#     marker present, backend answers, ACK set     -> proceed; marker cleared (the operator attests
#                                                     the restart; E2E_BACKEND_RESTARTED is logged,
#                                                     not proven — /health exposes no start time)
#     marker present, backend answers, no ACK      -> REFUSE (exit 3) with the exact fix
#
# The marker lives at `<chroma store>/.e2e-backend-restart-required` (override: E2E_RESTART_MARKER)
# and is written by the sweep when it deletes documents while a backend answers.  Liveness = any 2xx
# from `<base>/api/v1/health`, probed at E2E_BACKEND_URL (default http://localhost:8000) then
# E2E_API_BASE (default http://localhost) — the same no-auth endpoint and the same contract in all
# three implementations (curl -fsS here, urllib in the sweep, fetch in _guard.ts).
#
# Exit codes (check): 0 = proceed; 3 = refused.
#
# Standalone (used by this lane's negative tests, and useful for debugging):
#     bash _restart-guard.sh marker-path          # print the resolved marker path
#     bash _restart-guard.sh probe                # print live/dead per probe URL
#     bash _restart-guard.sh check [--report-only]
set -u

# --- marker path ----------------------------------------------------------------------------------
restart_guard_marker_path() {
  if [ -n "${E2E_RESTART_MARKER:-}" ]; then
    printf '%s\n' "${E2E_RESTART_MARKER}"
    return 0
  fi
  local dir="${CHROMA_PERSIST_DIR:-}"
  if [ -z "$dir" ]; then
    # Unset default mirrors the backend's D7 resolver (app/core/chroma_client.resolve_persist_dir):
    # on this host that is <repo>/backend/data/chroma — NOT the drive-relative /app form
    # (audit L3-01, RD7 follow-up).  The script lives in e2e/; the repo root is its parent.
    dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/backend/data/chroma"
  fi
  # A leading "/app/..." that an operator DID set explicitly on Windows resolves against the
  # CURRENT DRIVE via python's os.path.abspath (C:/app/...); MSYS bash would resolve "/app"
  # against the Git installation root instead.  Translate only when that divergence is
  # demonstrable: the /app form is not itself a directory and the C:/app form is.
  case "$dir" in
    /app/*)
      if [ ! -d "$dir" ] && [ -d "C:/app/${dir#/app/}" ]; then
        dir="C:/app/${dir#/app/}"
      fi
      ;;
  esac
  printf '%s\n' "$dir/.e2e-backend-restart-required"
}

# --- liveness probes ------------------------------------------------------------------------------
restart_guard_probe_urls() {
  printf '%s\n' "${E2E_BACKEND_URL:-http://localhost:8000}/api/v1/health"
  printf '%s\n' "${E2E_API_BASE:-http://localhost}/api/v1/health"
}

# 0 = some backend answered 2xx (prints the URL that did); 1 = none.
# NOTE (measured on this host): curl's own `-o /dev/null` FAILS the write with rc 23 even on a real
# HTTP 200 (curl 8.18.0 mingw64 + MSYS: '[23] client returned ERROR on write'), which would make a
# live backend look dead — the redirect is done at the SHELL level instead (verified: curl with
# `>/dev/null 2>&1` exits 0 on the same URLs).
restart_guard_backend_live() {
  local url
  while IFS= read -r url; do
    if curl -fsS -m 3 "$url" >/dev/null 2>&1; then
      printf '%s\n' "$url"
      return 0
    fi
  done < <(restart_guard_probe_urls)
  return 1
}

# --- the check ------------------------------------------------------------------------------------
restart_guard_check() {
  local report_only=0
  if [ "${1:-}" = "--report-only" ]; then report_only=1; fi
  local marker
  marker="$(restart_guard_marker_path)"
  if [ ! -e "$marker" ]; then
    return 0
  fi
  echo "[restart-guard] R-W4-2 marker present: $marker"
  if [ -r "$marker" ]; then
    sed 's/^/    /' "$marker"
  fi
  if ! command -v curl >/dev/null 2>&1; then
    echo "[restart-guard] REFUSING (exit 3): curl is not on PATH, so backend liveness cannot be" >&2
    echo "  checked and the stale-backend question is unanswerable.  Restart the backend, then" >&2
    echo "  re-run with E2E_BACKEND_RESTARTED=1 to attest it (or install curl)." >&2
    return 3
  fi
  local live_url
  if live_url="$(restart_guard_backend_live)"; then
    if [ -n "${E2E_BACKEND_RESTARTED:-}" ]; then
      if [ "$report_only" = 1 ]; then
        echo "[restart-guard] report-only: would clear the marker (backend live at $live_url; E2E_BACKEND_RESTARTED is set)."
      else
        rm -f "$marker"
        echo "[restart-guard] marker cleared: backend live at $live_url and the restart is attested (E2E_BACKEND_RESTARTED=${E2E_BACKEND_RESTARTED})."
      fi
      return 0
    fi
    {
      echo ""
      echo "[restart-guard] =============================================================="
      echo "[restart-guard] REFUSING THIS RUN (exit 3) — R-W4-2 restart protocol"
      echo "[restart-guard] =============================================================="
      echo "A backend is answering at $live_url, and the suite-end sweep deleted chroma"
      echo "document(s) while a backend was live — that is why the marker above exists.  The"
      echo "backend caches its chroma collection per process: its view is now stale and the"
      echo "measured failure mode is memory-search 500s mid-run"
      echo "(W4-ship/RECEIPT.md §3.9; endgame V2 run 3 reproduced it live)."
      echo ""
      echo "Fix — restart the backend, then re-run with the restart attested:"
      echo "    E2E_BACKEND_RESTARTED=1 bash run-suite.sh <label> <base-url> ...extras"
      echo ""
      echo "(This script never restarts the backend itself — it has never managed any server;"
      echo " the operator/parent owns the stack.  If the backend is already DOWN, just re-run:"
      echo " no live backend means no stale cache, and the marker is cleared automatically.)"
    } >&2
    return 3
  fi
  # No backend answering: the direct delete cannot have left a live stale cache; a backend that
  # starts from here opens the store fresh.  Clear the marker so the protocol does not wedge.
  if [ "$report_only" = 1 ]; then
    echo "[restart-guard] report-only: no backend answered the /health probes — a real run would clear the marker and proceed."
    return 0
  fi
  rm -f "$marker"
  echo "[restart-guard] marker cleared: no backend answered the /health probes — any backend that starts from here opens the store fresh (was: $marker)."
  return 0
}

# --- standalone driver (sourced use only defines the functions above) ------------------------------
if [ "${BASH_SOURCE[0]:-}" = "$0" ]; then
  case "${1:-check}" in
    marker-path) restart_guard_marker_path ;;
    probe)
      if live="$(restart_guard_backend_live)"; then echo "live: $live"; else echo "dead: no probe URL answered 2xx"; fi
      restart_guard_probe_urls | sed 's/^/  probed: /'
      ;;
    check) restart_guard_check "${2:-}"; exit $? ;;
    *)
      echo "usage: bash _restart-guard.sh {check [--report-only]|probe|marker-path}" >&2
      exit 2
      ;;
  esac
fi
