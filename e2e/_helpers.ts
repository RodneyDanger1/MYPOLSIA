/**
 * Shared helpers for the page specs. NOT a spec file (the runner only matches *.spec.ts).
 *
 * Four jobs:
 *  1. the fixture marker every spec prefixes its rows with — obviously-named, so any row a run leaves
 *     behind is identifiable as a test fixture and is swept by the next run of the same spec;
 *  2. seeding/cleanup for the three resources with NO write API (social posts, prospects, ad campaigns):
 *     on this host the Celery broker (redis) is down, so no agent run can create those rows, and the
 *     API exposes reads only.  The specs insert ONE row into the same SQLite file the backend serves
 *     from, assert the page renders it, and delete it again.  Specs still derive every expectation
 *     from the HTTP API — this helper never fabricates API output;
 *  3. an APIRequestContext factory for calls made outside test bodies (`test.beforeAll` cannot use the
 *     test-scoped `request` fixture), carrying the same X-API-Key the browser context sends.
 */
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import path from "node:path";
import { DatabaseSync } from "node:sqlite";
import { request as playwrightRequest } from "@playwright/test";

export const PREFIX = "[e2e-fixture]";

/**
 * The SQLite file the e2e backend serves from — the docker-free local lab's DB.
 *
 * Repo-relative and gitignored (`.e2e-lab.sqlite`; the `*.sqlite` rule covers it) on purpose: the old
 * default was ONE machine's absolute path, so on any other checkout (another user, a CI runner)
 * `new DatabaseSync` threw before a single assertion ran. The dependency is explicit now — set
 * `E2E_SQLITE` (or put the lab DB at this path) and the fixture-seeding specs run; leave it unset and
 * they SKIP BY NAME (`LAB_DB_SKIP_REASON`) instead of erroring, which is what CI needs since its
 * stack is Postgres and a native SQLite fixture write would be invisible to the backend under test.
 */
export const SQLITE_PATH =
  process.env.E2E_SQLITE ?? path.resolve(__dirname, "..", ".e2e-lab.sqlite");

/** The named reason the fixture-seeding specs skip when the SQLite lab is not there. */
export const LAB_DB_SKIP_REASON =
  `fixtures need the SQLite lab (docker-free local stack): E2E_SQLITE is unset and ${SQLITE_PATH} ` +
  "does not exist — the CI stack is Postgres (.github/workflows/integration.yml), where native " +
  "SQLite fixture writes would never be served";

/** True when a lab DB is actually there to write to; specs gate their SQLite seeding on it. */
export function labDbAvailable(): boolean {
  return existsSync(SQLITE_PATH);
}

/**
 * Fixture WRITES go straight to the backend.  POST /agents/{type}/trigger can take ~14s when the
 * broker is down (the handler awaits the bounded enqueue ladder; measured), and the local stand-in
 * proxy in front of :80 has a 30s cap — the browser path under test still goes through :80.
 */
export const BACKEND_URL = process.env.E2E_BACKEND_URL ?? "http://localhost:8000";

/**
 * Where API READS go: the same host the pages themselves call (the reverse-proxy stand-in on :80,
 * which serves /api/* to the backend).  Deliberately independent of BASE_URL — the browser may be
 * pointed at a different frontend, but the API it consumes, and the API this suite asserts against,
 * is still this one.  (Learned the hard way: with BASE_URL=http://localhost:3002, a relative
 * request.get("/api/v1/...") resolves against the FRONTEND and 404s.)
 */
export const API_BASE = process.env.E2E_API_BASE ?? "http://localhost";

function openDb() {
  // Measured (node:sqlite, Node 26): `new DatabaseSync(p)` CREATES an empty DB whenever the directory
  // exists, so a missing lab DB would silently accept fixture writes nobody serves; it throws only when
  // a parent directory is missing. Check first and fail with the reason instead of a raw sqlite error.
  if (!existsSync(SQLITE_PATH)) {
    throw new Error(
      `e2e fixtures need the SQLite lab DB at ${SQLITE_PATH} — set E2E_SQLITE to the DB the backend ` +
        "serves from. The CI stack is Postgres (.github/workflows/integration.yml), where a native " +
        "SQLite fixture write would never be served; the seeding specs skip there by name.",
    );
  }
  const db = new DatabaseSync(SQLITE_PATH);
  // The backend may hold the file while a request is in flight; wait instead of failing on busy.
  db.exec("PRAGMA busy_timeout = 5000");
  return db;
}

/** Run one statement; returns normalized numeric counters. */
export function sqliteRun(
  sql: string,
  params: (string | number | null)[] = [],
): { changes: number; lastInsertRowid: number } {
  const db = openDb();
  try {
    const res = db.prepare(sql).run(...params);
    return { changes: Number(res.changes), lastInsertRowid: Number(res.lastInsertRowid) };
  } finally {
    db.close();
  }
}

/** Read rows (used to bank fixture evidence; assertions read the API, not the DB). */
export function sqliteAll<T = Record<string, unknown>>(
  sql: string,
  params: (string | number | null)[] = [],
): T[] {
  const db = openDb();
  try {
    return db.prepare(sql).all(...params) as T[];
  } finally {
    db.close();
  }
}

/** API context for beforeAll/afterAll calls, same auth the suite's browser context carries. */
export function backendRequest() {
  return playwrightRequest.newContext({
    baseURL: process.env.BASE_URL ?? "http://localhost",
    extraHTTPHeaders: { "X-API-Key": process.env.API_KEY ?? "dev-key" },
  });
}

/**
 * Suite-end sweep (T12) — the belt to the per-spec `finally` braces.  The config points Playwright's
 * `globalTeardown` at THIS module (`globalTeardown: "./_helpers"`: the option accepts a module path,
 * not a function, so the default export at the bottom of this file is the hook).  It runs after the
 * last test, including after failed or timed-out specs whose own cleanup never ran — the case a
 * sweep-as-last-spec would miss whenever the run is filtered or a spec never reaches its end.
 *
 * The work lives in `_sweep-fixtures.py` and runs under the venv python on purpose: the memory
 * fixtures need BOTH stores cleaned — sqlite rows AND their ChromaDB documents — and chromadb is
 * python-only (the same store the G-e2e campaign's close-out sweep used).  The script deletes
 * tagged documents FIRST and tagged sqlite rows second, refuses to delete rows whose documents it
 * cannot prove/remove, deletes ONLY by the `[e2e-fixture]` tag, and is idempotent (second run: 0).
 *
 * A failure here THROWS: a run whose residue was not swept must not report success.  (Nothing is
 * wiped that a human created; the untagged rows present before this suite — e.g. "Manual
 * ads_management run" — cannot match a tag predicate.)
 */
export function sweepFixtures(): void {
  const script = path.join(__dirname, "_sweep-fixtures.py");
  if (!existsSync(script)) {
    throw new Error(`suite-end sweep script missing: ${script}`);
  }
  const res = spawnSync(pythonInterpreter(), [script], { stdio: "inherit" });
  if (res.error) {
    throw new Error(`suite-end sweep could not start: ${res.error.message}`);
  }
  if (res.status !== 0) {
    throw new Error(
      `suite-end sweep exited ${res.status} — [e2e-fixture] residue was NOT fully removed ` +
        "(its own output above names what it kept and why)",
    );
  }
}

/**
 * The python the suite-end sweep runs under; override with E2E_PYTHON.
 *
 * The repo venv is preferred (chromadb lives there), but its absence is NOT fatal: on a CI checkout
 * python comes from `actions/setup-python`, i.e. from PATH, and the old throw here failed a run in
 * which every test had already passed. A python that cannot be started at all still fails LOUD — in
 * `sweepFixtures`, where `spawnSync` reports it and the teardown refuses to claim success.
 */
function pythonInterpreter(): string {
  const override = process.env.E2E_PYTHON;
  if (override) return override;
  const root = path.resolve(__dirname, "..");
  const candidates = [
    path.join(root, ".venv", "Scripts", "python.exe"), // Windows
    path.join(root, ".venv", "bin", "python"), // POSIX
  ];
  return candidates.find((candidate) => existsSync(candidate)) ?? "python";
}

/** Playwright's globalTeardown hook (see `sweepFixtures` above for why it exists). */
export default async function globalTeardown(): Promise<void> {
  sweepFixtures();
}
