"use client";

import { useEffect, useState } from "react";

import { errorText } from "@/components/finance/useFinanceRead";
import { api } from "@/lib/api";

/**
 * The /usage ledger's live read — GET /api/v1/runs (backend `app/api/v1/runs.py`, the U5/B8 read
 * surface this page consumes VERBATIM).
 *
 * The frozen wire shape: `{"items": [...], "total": N}` where `total` is the FULL filtered count
 * and `items` is the requested limit/offset page, newest first. Every row field is nullable
 * because the backend serves `null` for UNMEASURED values — the page renders those as the app's
 * em dash, never as a fabricated 0 (the null-vs-zero law).
 *
 * `RunsRow` mirrors `RunRow` (runs.py:108-129) key by key; if the backend adds a field, this type
 * must be extended rather than guessed at.
 */
export type RunsRow = {
  id: number;
  task_id: number | null;
  task_title: string | null;
  agent_type: string;
  run_type: string | null;
  status: string | null;
  attempt: number | null;
  started_at: string | null;
  ended_at: string | null;
  duration_secs: number | null;
  tokens_input: number | null;
  tokens_output: number | null;
  tokens_used: number | null;
  cost_usd: number | null;
  cost_source: string | null;
  pricing_version: string | null;
};

/** The frozen wrapper object: `total` is the FULL filtered count, never the page size. */
export type RunsPage = {
  items: RunsRow[];
  total: number;
};

export type RunsRead = {
  /** The last successful page; `null` while nothing has been read yet OR the read failed —
   *  which of the two it is, `error` says. */
  data: RunsPage | null;
  error: string | null;
  loading: boolean;
};

/**
 * One ledger read with the page's three states (the `useFinanceRead` idiom, narrowed to the runs
 * contract so a hostile body cannot masquerade as a page):
 *
 *   loading -> `data === null && error === null` — the consumer renders a skeleton;
 *   error   -> the failure named (ApiError.display via the shared `errorText`);
 *   success -> the validated page.
 *
 * A 2xx body that is not the frozen `{items, total}` wrapper is a broken READ and is named as one
 * — `total` drives the footer's declared-total line and the paging edges, so a body without a
 * numeric `total` must never render as one. `reloadKey` re-issues the read (the Refresh
 * affordance); nothing is polled (C7).
 */
export function useRuns(path: string, reloadKey: number): RunsRead {
  const [data, setData] = useState<RunsPage | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    api
      .get<RunsPage>(path)
      .then((body) => {
        if (cancelled) return;
        if (body === null || body === undefined) {
          setError("the server returned an empty body");
          return;
        }
        if (
          typeof body !== "object" ||
          Array.isArray(body) ||
          !Array.isArray(body.items) ||
          typeof body.total !== "number"
        ) {
          setError("the server returned no runs page");
          return;
        }
        setData({ items: body.items, total: body.total });
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [path, reloadKey]);

  return { data, error, loading: data === null && error === null };
}
