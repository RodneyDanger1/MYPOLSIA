"use client";

import { useEffect, useState } from "react";

import { errorText } from "@/components/finance/useFinanceRead";
import type { RunsRow } from "@/components/usage/useRuns";
import { api } from "@/lib/api";

/**
 * `/runs/[id]` — the live read of `GET /api/v1/runs/{id}` (backend `app/api/v1/runs.py`, V21/L2;
 * the tool list closed by V22/L1).
 *
 * THE SHAPE: the frozen ledger row + the stored `num_turns` / `is_error` counters + the persisted
 * `tools_used` column (migration 0010).  `tools_used` has THREE wire states, read here without
 * collapsing them:
 *
 *   * `null`  — never recorded (legacy row / a run with no tools).  An absent field is the SAME
 *               fact, so it too reads as `null`.
 *   * `[]`    — recorded and no tool ran (a measured zero).
 *   * names   — the recorded tool names.
 *
 *   A value that is not an array is NOT a recorded list (`"web_search"` is not a tool); it reads
 *   as `null`, never as `[]`.  A counter that is absent OR not the stored type renders as
 *   unmeasured (`null` -> the app's em dash): `"3"` is not the stored integer 3, and `"no"` is not
 *   the stored boolean false.  No coercion, no default `0`/`false` (the repo's null-vs-zero law).
 *
 * Reads go through the shared `api.get` + `errorText` (the `useFinanceRead` idiom) so the failure
 * vocabulary cannot fork.  A 2xx body that is not a run is a BROKEN READ and is named as one.
 *
 * F-36 (wave 5) — THE INTERRUPTION MARKERS.  `GET /runs/{id}` also carries SV-W4-04's F-26 fields
 * `cost_scope` (`"unmeasured"` / `"in_flight_partial"`) and `interrupted_reason` (`"E_RUN_STALE"`),
 * both `null` when the row carries no interruption marker.  Until wave 5 no screen read them, so a
 * sweep-closed run's `COST —` was indistinguishable from "not measured yet".  They are read with
 * the same junk rule as every other field: a missing key (a pre-SV-W4-04 server), a non-string or
 * an empty string reads `null` — never coerced into a marker this build would then explain.
 */

export type RunDetailRow = RunsRow & {
  /** Stored `usage_json.num_turns`; `null` = not recorded. */
  num_turns: number | null;
  /** Stored `usage_json.is_error`; `null` = not recorded (never a fabricated `false`). */
  is_error: boolean | null;
  /** Stored `agent_runs.tools_used` (V22); `null` = never recorded, `[]` = recorded and none ran. */
  tools_used: string[] | null;
  /** F-26 marker (SV-W4-04): what the interrupted run's cost covers; absent/`null` = no marker. */
  cost_scope?: string | null;
  /** F-26 marker: the coded interruption reason (e.g. `E_RUN_STALE`); absent/`null` = none. */
  interrupted_reason?: string | null;
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

/** A stored marker string (F-36): a non-empty string, or `null`.  Numbers/arrays/`""` are junk. */
function asMarker(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

/**
 * A stored tool list: an ARRAY of names, or `null`.  The three states never collapse:
 * a missing/non-array value is `null` (never recorded), an empty array stays `[]` (recorded and
 * none ran), and non-string entries are dropped (a number is not a tool name).  An array with no
 * real name left after junk is unmeasured -> `null`, the same rule the backend serves.
 */
function asToolsUsed(value: unknown): string[] | null {
  if (!Array.isArray(value)) return null;
  const names = value.filter(
    (item): item is string => typeof item === "string" && item.length > 0,
  );
  if (names.length > 0) return names;
  return value.length === 0 ? [] : null;
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
          tools_used: asToolsUsed(record.tools_used),
          cost_scope: asMarker(record.cost_scope),
          interrupted_reason: asMarker(record.interrupted_reason),
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
