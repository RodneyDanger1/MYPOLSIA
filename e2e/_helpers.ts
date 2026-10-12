/**
 * Shared helpers for the page specs. NOT a spec file (the runner only matches *.spec.ts).
 *
 * Three jobs:
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
import { DatabaseSync } from "node:sqlite";
import { request as playwrightRequest } from "@playwright/test";

export const PREFIX = "[e2e-fixture]";

/** The SQLite file the e2e backend serves from (docker-free local stack; see polsia-work/build). */
export const SQLITE_PATH =
  process.env.E2E_SQLITE ?? "C:/Users/Riley/Desktop/polsia-work/build/e2e.sqlite";

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
