"use client";

import { useEffect, useState } from "react";

import { errorText } from "@/components/finance/useFinanceRead";
import type { RunsRow } from "@/components/usage/useRuns";
import { api } from "@/lib/api";

/**
 * `/runs/[id]` — the live read of `GET /api/v1/runs/{id}` (backend `app/api/v1/runs.py`, V21/L2).
 *
 * THE SHAPE, and why it has no `tools_used`:
 *   The run record stores `num_turns` and `is_error` in `agent_runs.usage_json`; it does NOT store
 *   the runtime's `tools_used` envelope key (the persistence boundary drops it — see the lane
 *   receipt and `run_service.py`'s DETAIL section).  So `RunDetailRow` is the frozen ledger row +
 *   those two counters, and nothing tool-shaped.  A page-level chip list needs a stored tool list;
 *   inventing one from an off-contract body would present an unmeasured value as measured.
 *
 *   A counter that is absent OR not the stored type renders as unmeasured (`null` -> the app's em
 *   dash): `"3"` is not the stored integer 3, and `"no"` is not the stored boolean false.  No
 *   coercion, no default `0`/`false` (the repo's null-vs-zero law).
 *
 * Reads go through the shared `api.get` + `errorText` (the `useFinanceRead` idiom) so the failure
 * vocabulary cannot fork.  A 2xx body that is not a run is a BROKEN READ and is named as one.
 */

export type RunDetailRow = RunsRow & {
  /** Stored `usage_json.num_turns`; `null` = not recorded. */
  num_turns: number | null;
  /** Stored `usage_json.is_error`; `null` = not recorded (never a fabricated `false`). */
  is_error: boolean | null;
};

export type RunDetailRead = {
  /** The last successful run; `null` while nothing has been read yet OR the read failed —
   *  which of the two it is, `error` says. */
  data: RunDetailRow | null;
  error: string | null;
  loading: boolean;
};

/** A stored turn count: a non-negative integer, or `null`. Strings/floats are NOT coerced. */
function asCount(value: unknown): number | null {
  return typeof value === "number" && Number.isInteger(value) && value >= 0 ? value : null;
}

/** A stored flag: a real boolean, or `null`. `"no"`/0/1 are not booleans. */
function asFlag(value: unknown): boolean | null {
  return typeof value === "boolean" ? value : null;
}

export function useRunDetail(runId: number | null, reloadKey: number): RunDetailRead {
  const [data, setData] = useState<RunDetailRow | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    // An invalid id never issues a request (the page renders its own grammar message).
    if (runId === null) {
      setData(null);
      setError(null);
      return;
    }
    let cancelled = false;
    // A different run (or a refresh) goes back through "not read yet": the previous run must
    // never stand under a new id, and the skeleton is the one honest way to say "being read".
    setData(null);
    setError(null);
    api
      .get<RunDetailRow>(`/runs/${runId}`)
      .then((body) => {
        if (cancelled) return;
        if (body === null || body === undefined || typeof body !== "object" || Array.isArray(body)) {
          setError("the server returned no run");
          return;
        }
        const record = body as unknown as Record<string, unknown>;
        if (typeof record.id !== "number" || typeof record.agent_type !== "string") {
          setError("the server returned no run");
          return;
        }
        setData({
          ...(body as RunsRow),
          num_turns: asCount(record.num_turns),
          is_error: asFlag(record.is_error),
        });
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [runId, reloadKey]);

  return { data, error, loading: data === null && error === null && runId !== null };
}
