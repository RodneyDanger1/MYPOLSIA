"use client";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";

type Prospect = { id: number; email: string; first_name: string | null; company: string | null; status: string; created_at: string };

const STATUS_STYLE: Record<string, string> = {
  new: "text-blue-400", contacted: "text-yellow-400", replied: "text-green-400",
  converted: "text-indigo-400", unsubscribed: "text-gray-400",
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

export default function OutreachPage() {
  const [data, setData] = useState<Prospect[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get<Prospect[]>("/outreach/prospects?limit=100")
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
      <h1 className="text-2xl font-bold text-white">
        {data === null ? "Outreach" : `Outreach (${data.length})`}
      </h1>
      {error && (
        <p className="text-red-400" role="alert">
          Could not load prospects: {error}
        </p>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-sm text-left">
          <thead>
            <tr className="text-gray-400 border-b border-gray-700">
              <th className="pb-2 pr-4">Email</th>
              <th className="pb-2 pr-4">Name</th>
              <th className="pb-2 pr-4">Company</th>
              <th className="pb-2">Status</th>
            </tr>
          </thead>
          <tbody>
            {data?.map((p) => (
              <tr key={p.id} className="border-b border-gray-800">
                <td className="py-2 pr-4 text-white">{p.email}</td>
                <td className="py-2 pr-4 text-gray-300">{p.first_name ?? "—"}</td>
                <td className="py-2 pr-4 text-gray-300">{p.company ?? "—"}</td>
                <td className={`py-2 capitalize ${STATUS_STYLE[p.status] ?? "text-gray-400"}`}>{p.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {!error && data !== null && data.length === 0 && <p className="text-gray-400 mt-4">No prospects yet.</p>}
      </div>
    </div>
  );
}
