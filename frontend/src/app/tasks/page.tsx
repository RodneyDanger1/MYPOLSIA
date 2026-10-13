"use client";
import { useEffect, useState } from "react";

import { api, type Task } from "@/lib/api";

const STATUS_STYLE: Record<string, string> = {
  completed: "bg-green-500/20 text-green-400",
  in_progress: "bg-blue-500/20 text-blue-400",
  pending: "bg-yellow-500/20 text-yellow-400",
  failed: "bg-red-500/20 text-red-400",
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

export default function TasksPage() {
  const [data, setData] = useState<Task[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get<Task[]>("/tasks?limit=100")
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
      <h1 className="text-2xl font-bold text-white">Tasks</h1>
      <div className="space-y-2">
        {error && (
          <p className="text-red-400" role="alert">
            Could not load tasks: {error}
          </p>
        )}
        {!error && data === null && (
          <div className="space-y-2" aria-busy="true">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-16 bg-gray-800 rounded-lg animate-pulse" />
            ))}
          </div>
        )}
        {!error && data !== null && data.length === 0 && (
          <p className="text-gray-400">No tasks yet. Agents will create tasks during their cycles.</p>
        )}
        {data?.map((t) => (
          <div key={t.id} className="bg-gray-800 rounded-lg p-4 flex items-start justify-between gap-4">
            <div>
              <p className="text-white font-medium">{t.title}</p>
              <p className="text-gray-400 text-sm capitalize">
                {t.agent_type.replace(/_/g, " ")} · Priority {t.priority}
              </p>
              <p className="text-gray-500 text-xs mt-1">
                {/* `created_at` is nullable on the wire (api.ts): the placeholder below, not the
                    "Invalid Date" that `new Date(null)` produces. */}
                {t.created_at ? new Date(t.created_at).toLocaleString() : "date unknown"}
              </p>
            </div>
            <span
              className={`flex-shrink-0 px-2 py-1 rounded text-xs font-medium ${STATUS_STYLE[t.status] ?? "bg-gray-700 text-gray-400"}`}
            >
              {t.status}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
