/**
 * R-W4-2 restart guard for bare Playwright invocations — the `globalSetup` hook wired in
 * `playwright.config.ts`.  Canonical policy text: `e2e/_restart-guard.sh` (run-suite.sh's pre-flight
 * sources the same check; the sweep writes/clears the marker).  Why a globalSetup hook at all: the
 * suite is run BOTH through run-suite.sh and as a bare `npx playwright test` (the campaign's ×3
 * recipe), and globalSetup is the one place every test-bearing invocation passes through.  A run
 * must not start while the sweep's restart marker is present and a backend that witnessed the
 * sweep is still answering /health — that backend's cached chroma collection is stale, and the
 * measured failure mode is memory-search 500s mid-run (W4-ship/RECEIPT.md §3.9; endgame V2 run 3
 * reproduced it live).
 *
 *   no marker                                          -> return (zero probes on clean trees / CI)
 *   marker + no backend answering /health              -> clear marker, proceed (fresh store next)
 *   marker + backend answering + E2E_BACKEND_RESTARTED -> clear marker, proceed (attested restart)
 *   marker + backend answering + no attestation        -> throw (the run is refused)
 *
 * `--list` / `--help` never run this: checking that tests are collectable must not require stack
 * state, and listing must not touch the marker.
 */
import { existsSync, readFileSync, unlinkSync } from "node:fs";
import path from "node:path";

/** The marker the sweep writes — same resolution in all three implementations: explicit
 * CHROMA_PERSIST_DIR wins; else the backend's D7 default (app/core/chroma_client.resolve_persist_dir)
 * mirrored here — on Windows that is <repo>/backend/data/chroma, NOT the drive-relative
 * C:/app/data/chroma (audit L3-01, RD7 follow-up); POSIX/containers keep /app/data/chroma. */
function defaultChromaDir(): string {
  if (process.platform === "win32") {
    // _guard.ts lives in e2e/; the repo root is its parent.  __dirname is available under
    // playwright's CJS transpile; the typeof guard keeps a bare node type-strip import loadable.
    const here = typeof __dirname !== "undefined" ? __dirname : process.cwd();
    return path.resolve(here, "..", "backend", "data", "chroma");
  }
  return "/app/data/chroma";
}
export function restartMarkerPath(): string {
  const override = process.env.E2E_RESTART_MARKER;
  if (override) return override;
  const dir = path.resolve(process.env.CHROMA_PERSIST_DIR ?? defaultChromaDir());
  return path.join(dir, ".e2e-backend-restart-required");
}

/** Same two candidates as _restart-guard.sh and the sweep: the backend's direct port and the :80
 * lab proxy.  Any 2xx on the no-auth /api/v1/health counts as "a backend is answering". */
function probeUrls(): string[] {
  return [
    `${process.env.E2E_BACKEND_URL ?? "http://localhost:8000"}/api/v1/health`,
    `${process.env.E2E_API_BASE ?? "http://localhost"}/api/v1/health`,
  ];
}

async function backendLive(): Promise<string | null> {
  for (const url of probeUrls()) {
    try {
      const res = await fetch(url, { signal: AbortSignal.timeout(3000) });
      if (res.ok) return url;
    } catch {
      /* dead / refused / timeout — try the next candidate */
    }
  }
  return null;
}

export default async function globalSetup(): Promise<void> {
  if (process.argv.includes("--list") || process.argv.includes("--help")) return;
  const marker = restartMarkerPath();
  if (!existsSync(marker)) return;
  console.log(`[restart-guard] R-W4-2 marker present: ${marker}`);
  try {
    const body = readFileSync(marker, "utf8").trimEnd();
    if (body) console.log(body.split("\n").map((line) => `[restart-guard]   ${line}`).join("\n"));
  } catch {
    /* an unreadable body is not an error — presence is the signal */
  }
  const live = await backendLive();
  if (live && !process.env.E2E_BACKEND_RESTARTED) {
    throw new Error(
      [
        "[restart-guard] REFUSING THIS RUN — R-W4-2 restart protocol.",
        `A backend is answering at ${live}, and the suite-end sweep deleted chroma document(s)`,
        "while a backend was live — that is why the marker above exists.  The backend caches its",
        "chroma collection per process: its view is stale, and the measured failure mode is",
        "memory-search 500s mid-run (W4-ship/RECEIPT.md §3.9; endgame V2 run 3 reproduced it live).",
        "Fix — restart the backend, then re-run:",
        "    E2E_BACKEND_RESTARTED=1 npx playwright test ...",
        "(the attestation clears the marker and is logged; it does not restart anything for you).",
      ].join("\n"),
    );
  }
  try {
    unlinkSync(marker);
  } catch (err) {
    throw new Error(`[restart-guard] marker ${marker} could not be cleared: ${String(err)}`);
  }
  console.log(
    live
      ? `[restart-guard] marker cleared: backend live at ${live} and the restart is attested (E2E_BACKEND_RESTARTED set).`
      : "[restart-guard] marker cleared: no backend answered the /health probes — any backend that starts from here opens the store fresh.",
  );
}
