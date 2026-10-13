"use client";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";

// status/daily_budget_usd/total_spent_usd are NULLABLE in the schema (a raw-SQL/ops row can hold
// NULL) — null means "not recorded", and it is rendered as a dash, never as a fabricated $0.00.
type Campaign = {
  id: number;
  platform: string;
  name: string;
  status: string | null;
  daily_budget_usd: number | null;
  total_spent_usd: number | null;
};

/**
 * Client component on purpose.
 *
 * This page used to be an async Server Component doing `await api.get(...)`. Next prerenders this route
 * (`○ (Static)` in the build output), so that fetch ran at BUILD time and the empty result was baked into
 * the HTML: the page showed its empty state forever, even while the API served rows. The repo's e2e run
 * caught it — `GET /api/v1/tasks` returned 11 tasks and the page still rendered "No tasks yet."
 *
 * The old `catch { return [] }` made it worse: a failed read became the words "no data", which is the one
 * thing this project forbids. Three states now, and they are different on purpose:
 *   loading  -> a skeleton, because "not read yet" is not "empty"
 *   error    -> the failure named on the page, never an empty list standing in for it
 *   empty    -> printed ONLY when the read succeeded and really returned nothing
 */

export default function AdsPage() {
  const [data, setData] = useState<Campaign[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get<Campaign[]>("/ads/campaigns")
      .then((rows) => {
        if (!cancelled) setData(rows);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(e instanceof Error ? e.message : String(e));
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <div className="p-6 space-y-4">
      <h1 className="text-2xl font-bold text-white">Ad Campaigns</h1>
      <div className="space-y-3">
        {error && (
          <p className="text-red-400" role="alert">
            Could not load campaigns: {error}
          </p>
        )}
        {!error && data === null && (
          <div className="space-y-3" aria-busy="true">
            {Array.from({ length: 2 }).map((_, i) => (
              <div key={i} className="h-20 bg-gray-800 rounded-lg animate-pulse" />
            ))}
          </div>
        )}
        {!error && data !== null && data.length === 0 && <p className="text-gray-400">No campaigns yet.</p>}
        {data?.map((c) => (
          <div key={c.id} className="bg-gray-800 rounded-lg p-4 flex items-center justify-between">
            <div>
              <p className="text-white font-medium">{c.name}</p>
              <p className="text-gray-400 text-sm capitalize">{c.platform} · {c.status ?? "—"}</p>
            </div>
            <div className="text-right">
              <p className="text-white">{c.total_spent_usd === null ? "—" : `$${c.total_spent_usd.toFixed(2)}`} spent</p>
              <p className="text-gray-400 text-sm">{c.daily_budget_usd === null ? "—" : `$${c.daily_budget_usd}`}/day budget</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
