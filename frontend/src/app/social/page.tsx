"use client";
import { useEffect, useState } from "react";

import { api } from "@/lib/api";

type Post = { id: number; content: string; status: string; published_at: string | null; engagement: Record<string, number> | null };

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

export default function SocialPage() {
  const [data, setData] = useState<Post[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    api
      .get<Post[]>("/social/posts?limit=50")
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
      <h1 className="text-2xl font-bold text-white">Social Media</h1>
      <div className="space-y-3">
        {error && (
          <p className="text-red-400" role="alert">
            Could not load posts: {error}
          </p>
        )}
        {!error && data === null && (
          <div className="space-y-3" aria-busy="true">
            {Array.from({ length: 3 }).map((_, i) => (
              <div key={i} className="h-20 bg-gray-800 rounded-lg animate-pulse" />
            ))}
          </div>
        )}
        {!error && data !== null && data.length === 0 && <p className="text-gray-400">No posts yet.</p>}
        {data?.map((p) => (
          <div key={p.id} className="bg-gray-800 rounded-lg p-4">
            <p className="text-white">{p.content}</p>
            <div className="flex gap-4 mt-2 text-sm text-gray-400">
              <span className={`capitalize ${p.status === "published" ? "text-green-400" : "text-yellow-400"}`}>{p.status}</span>
              {p.published_at && <span>{new Date(p.published_at).toLocaleDateString()}</span>}
              {p.engagement && Object.entries(p.engagement).map(([k, v]) => (
                <span key={k}>{k}: {v}</span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
