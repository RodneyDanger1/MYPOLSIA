"use client";

import { useCallback, useEffect, useState } from "react";

import { errorText } from "@/components/finance/useFinanceRead";
import { api } from "@/lib/api";

/**
 * The Usage page's OPS reads (V11 unit 2) — GET /api/v1/ops/agent-quality,
 * GET /api/v1/ops/events/stats and POST /api/v1/ops/events/reset (backend `app/api/v1/ops.py`,
 * consumed VERBATIM).
 *
 * PROCESS BOUNDARY, stated not hidden (ops.py module docstring, verbatim spirit): the breaker's
 * counters are module globals in `app.core.events`, i.e. PER PROCESS. These routes report and
 * reset the API process's state; the 15-minute beat tick reports the WORKER process's state;
 * neither surface claims to see the other's numbers. The panel renders exactly that, and the
 * reset copy repeats it: a reset that silently implied a global fix would be a lie, because the
 * worker's counters would survive it.
 *
 * Honest states, the useRuns idiom: `loading` is `data === null && error === null`; a 2xx body
 * that is not the frozen shape is NAMED as a broken read; every nullable FIELD stays null through
 * validation (null is "not measured") and a hostile field type degrades to null rather than a
 * fabricated 0. Nothing is polled (C7); the reload key is the explicit Refresh, and the reset is
 * a deliberate operator action.
 */

/** GET /ops/events/stats -> `events.publish_stats()` (app/core/events.py:47-55), key by key. */
export type OpsBreakerStats = {
  failures: number;
  skipped_while_open: number;
  breaker_open: boolean;
  cooldown_seconds: number;
  last_error: string | null;
};

/**
 * One row of GET /ops/agent-quality -> `_agent_quality_rows` (ops.py:82-148). `runs_7d` is an
 * integer on the wire (a registered agent with no runs reports a measured 0); its companions stay
 * null when unmeasured: `success_rate` (no terminal run in the 7-day window),
 * `last_failure_at` (no failed run on record, all time) and `cost_per_completed_task` (no
 * completion measured). They are typed nullable here on purpose: a hostile body must degrade to
 * "not measured", never to a fabricated value.
 */
export type OpsQualityRow = {
  agent_type: string;
  runs_7d: number | null;
  success_rate: number | null;
  last_failure_at: string | null;
  cost_per_completed_task: number | null;
};

/** GET /ops/agent-quality wrapper: one row per registered agent, sorted by the backend. */
export type OpsQuality = { agents: OpsQualityRow[] };

export type OpsRead<T> = {
  /** The last successful body; `null` while nothing has been read yet OR the read failed —
   *  which of the two it is, `error` says. */
  data: T | null;
  error: string | null;
  loading: boolean;
};

/** A finite number, or null (never NaN / Infinity / a string that merely looks numeric). */
function finiteOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** The frozen stats body, or null when the wire is not it. Exported for the panel's tests. */
export function breakerStatsOf(body: unknown): OpsBreakerStats | null {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return null;
  const b = body as Record<string, unknown>;
  const failures = finiteOrNull(b.failures);
  const skipped = finiteOrNull(b.skipped_while_open);
  const cooldown = finiteOrNull(b.cooldown_seconds);
  if (typeof b.breaker_open !== "boolean" || failures === null || skipped === null || cooldown === null) {
    return null;
  }
  return {
    failures,
    skipped_while_open: skipped,
    breaker_open: b.breaker_open,
    cooldown_seconds: cooldown,
    last_error: typeof b.last_error === "string" ? b.last_error : null,
  };
}

/**
 * The frozen agent-quality body, or null when the wire is not it. A row without a string
 * `agent_type` carries no identity on this table and is dropped (the one field that cannot render
 * as "not measured"); every other field is normalized to a finite number, a string or null.
 */
export function agentQualityOf(body: unknown): OpsQuality | null {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return null;
  const b = body as Record<string, unknown>;
  if (!Array.isArray(b.agents)) return null;
  const agents: OpsQualityRow[] = [];
  for (const raw of b.agents) {
    if (raw === null || typeof raw !== "object" || Array.isArray(raw)) continue;
    const row = raw as Record<string, unknown>;
    if (typeof row.agent_type !== "string") continue;
    agents.push({
      agent_type: row.agent_type,
      runs_7d: finiteOrNull(row.runs_7d),
      success_rate: finiteOrNull(row.success_rate),
      last_failure_at: typeof row.last_failure_at === "string" ? row.last_failure_at : null,
      cost_per_completed_task: finiteOrNull(row.cost_per_completed_task),
    });
  }
  return { agents };
}

/** GET /ops/agent-quality — the deterministic per-agent quality read (no LLM, ops.py). */
export function useOpsQuality(reloadKey: number): OpsRead<OpsQuality> {
  const [data, setData] = useState<OpsQuality | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    api
      .get<unknown>("/ops/agent-quality")
      .then((body) => {
        if (cancelled) return;
        const quality = agentQualityOf(body);
        if (quality === null) {
          setError("the server returned no agent-quality rows");
          return;
        }
        setData(quality);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  return { data, error, loading: data === null && error === null };
}

export type OpsBreakerRead = OpsRead<OpsBreakerStats> & {
  /** POST /ops/events/reset; resolves true when the fresh stats were adopted. Never throws. */
  reset: () => Promise<boolean>;
  resetting: boolean;
  resetError: string | null;
  /** Clear a previous reset failure (the guard panel calls this on open/cancel). */
  dismissResetError: () => void;
};

/**
 * GET /ops/events/stats + POST /ops/events/reset. The reset returns the FRESH stats
 * (`reset_event_publish_breaker` returns `publish_stats()` after clearing), so a successful reset
 * adopts that measured body directly and a previous read error is superseded by it. The verb is a
 * mutation: lib/api.ts never retries it, and it needs no refusal_guard server-side because it
 * flips in-memory counters and nothing else.
 */
export function useOpsBreaker(reloadKey: number): OpsBreakerRead {
  const [data, setData] = useState<OpsBreakerStats | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [resetting, setResetting] = useState(false);
  const [resetError, setResetError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError(null);
    api
      .get<unknown>("/ops/events/stats")
      .then((body) => {
        if (cancelled) return;
        const stats = breakerStatsOf(body);
        if (stats === null) {
          setError("the server returned no breaker stats");
          return;
        }
        setData(stats);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const reset = useCallback(async (): Promise<boolean> => {
    setResetting(true);
    setResetError(null);
    try {
      const fresh = breakerStatsOf(await api.post<unknown>("/ops/events/reset"));
      if (fresh === null) {
        setResetError("the server returned no breaker stats after the reset");
        return false;
      }
      setData(fresh);
      setError(null);
      return true;
    } catch (e: unknown) {
      setResetError(errorText(e));
      return false;
    } finally {
      setResetting(false);
    }
  }, []);

  const dismissResetError = useCallback(() => setResetError(null), []);

  return {
    data,
    error,
    loading: data === null && error === null,
    reset,
    resetting,
    resetError,
    dismissResetError,
  };
}
