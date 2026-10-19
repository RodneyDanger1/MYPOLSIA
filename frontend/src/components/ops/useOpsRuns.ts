"use client";

import { useEffect, useRef, useState } from "react";

import { errorText } from "@/components/finance/useFinanceRead";
import { api } from "@/lib/api";

/**
 * The /ops live-runs read + detail enrichment.
 *
 * `GET /runs` serves the frozen ledger row set (id, task, agent, status, duration, cost) but
 * `tools_used` is DETAIL-ONLY by contract (`runs.py` module docstring: "the list/export row set is
 * UNCHANGED: `tools_used` is detail-only"). The cockpit needs the chips, so each listed run is
 * enriched from its own `GET /runs/{id}` — the only real source — with three honest cell states:
 *
 *   absent from `tools`        the detail read is still in flight (`reading…` in the table)
 *   {kind:"value", tools:null} the detail answered `tools_used: null` — NEVER RECORDED
 *   {kind:"value", tools:[]}   the detail answered an empty list — RECORDED AND NONE
 *   {kind:"value", tools:[…]}  the recorded names
 *   {kind:"error", message}    the detail read failed; the cell says so (it never reads as "none")
 *
 * Caching: a TERMINAL run's tool list cannot change, so it is cached by (id, status, started_at,
 * ended_at) and reused across polls. A non-terminal run is re-read on each tick (its tools are still
 * arriving). Detail reads are bounded to 3 in flight so a 20-row page cannot fan out at once.
 *
 * The list read keeps the previous page visible during a poll (`loading` is the FIRST-read state
 * only); a failed poll replaces it with the named error rather than showing stale rows as current.
 */
export const OPS_RUN_LIMIT = 20;

export type OpsRunRow = {
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

export type OpsRunsPage = { items: OpsRunRow[]; total: number };

export type OpsToolsCell =
  | { kind: "value"; tools: string[] | null }
  | { kind: "error"; message: string };

export type OpsRunsRead = {
  data: OpsRunsPage | null;
  error: string | null;
  loading: boolean;
  tools: Record<number, OpsToolsCell>;
};

const TERMINAL_RUN_STATUSES = new Set(["completed", "failed", "cancelled", "rejected"]);

function isRunsPage(body: unknown): body is OpsRunsPage {
  if (body === null || typeof body !== "object" || Array.isArray(body)) return false;
  const candidate = body as { items?: unknown; total?: unknown };
  return Array.isArray(candidate.items) && typeof candidate.total === "number";
}

/** The detail's persisted tool list: the three states preserved, hostile shapes read as null. */
function toolsFromDetail(body: unknown): string[] | null {
  if (body === null || typeof body !== "object") return null;
  const raw = (body as { tools_used?: unknown }).tools_used;
  if (!Array.isArray(raw)) return null;
  return raw.filter((name): name is string => typeof name === "string" && name !== "");
}

/** A terminal run's tool list is final; a running one may still grow. */
function cacheKey(row: OpsRunRow): string {
  return `${row.id}:${row.status ?? ""}:${row.started_at ?? ""}:${row.ended_at ?? ""}`;
}

/** Run detail reads at a bounded concurrency (worker pool; order is irrelevant to the table). */
async function eachLimit<T>(items: T[], limit: number, fn: (item: T) => Promise<void>): Promise<void> {
  let next = 0;
  const workers = Array.from({ length: Math.min(limit, items.length) }, async () => {
    for (;;) {
      const index = next;
      next += 1;
      if (index >= items.length) return;
      await fn(items[index]);
    }
  });
  await Promise.all(workers);
}

export function useOpsRuns(tick: number): OpsRunsRead {
  const [data, setData] = useState<OpsRunsPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [tools, setTools] = useState<Record<number, OpsToolsCell>>({});
  const cache = useRef(new Map<number, { key: string; tools: string[] | null }>());

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const body = await api.get<unknown>(`/runs?limit=${OPS_RUN_LIMIT}`);
        if (cancelled) return;
        if (!isRunsPage(body)) {
          setError("the server returned no runs page");
          setLoading(false);
          return;
        }
        setData(body);
        setError(null);
        setLoading(false);

        // Seed from the immutable cache and queue only what is absent/stale.
        const seeded: Record<number, OpsToolsCell> = {};
        const missing: OpsRunRow[] = [];
        for (const row of body.items) {
          const hit = cache.current.get(row.id);
          if (hit !== undefined && hit.key === cacheKey(row)) {
            seeded[row.id] = { kind: "value", tools: hit.tools };
          } else {
            missing.push(row);
          }
        }
        setTools(seeded);

        await eachLimit(missing, 3, async (row) => {
          try {
            const detail = await api.get<unknown>(`/runs/${row.id}`);
            if (cancelled) return;
            const used = toolsFromDetail(detail);
            if (TERMINAL_RUN_STATUSES.has(row.status ?? "")) {
              cache.current.set(row.id, { key: cacheKey(row), tools: used });
            }
            setTools((previous) => ({ ...previous, [row.id]: { kind: "value", tools: used } }));
          } catch (e) {
            if (cancelled) return;
            setTools((previous) => ({
              ...previous,
              [row.id]: { kind: "error", message: errorText(e) },
            }));
          }
        });
      } catch (e) {
        if (cancelled) return;
        setError(errorText(e));
        setLoading(false);
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [tick]);

  return { data, error, loading, tools };
}
