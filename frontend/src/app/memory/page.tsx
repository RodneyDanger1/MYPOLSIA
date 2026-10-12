"use client";
import { useState } from "react";

import { api } from "@/lib/api";

/**
 * Client component, and the search is explicit — but three things were wrong here:
 *
 *  * `try/finally` with NO `catch`: a failed search became an unhandled rejection while the UI simply
 *    stayed on its previous state, so an outage was indistinguishable from "no matches".
 *  * `created_at` was typed non-null, but the semantic path returns `null` for it — `new Date(null)`
 *    renders the epoch, so the page showed 1/1/1970 for entries whose date is genuinely unknown.
 *  * `id` is typed `number`, while the semantic path returns a string id.
 *
 * The states are now: never-searched, searching, failed (named), and "no matches" — printed only when a
 * search actually succeeded and returned nothing.
 */
type Memory = {
  id: number | string;
  category: string;
  title: string;
  content: string;
  created_at: string | null;
};

export default function MemoryPage() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Memory[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);

  const search = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const data = await api.get<Memory[]>(`/memory?q=${encodeURIComponent(query)}&limit=20`);
      setResults(data);
      setSearched(true);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      setResults(null);
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="p-6 space-y-6">
      <h1 className="text-2xl font-bold text-white">Memory</h1>
      <div className="flex gap-3">
        <input
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && search()}
          placeholder="Search company memory…"
          className="flex-1 bg-gray-800 text-white rounded-lg px-4 py-2 border border-gray-700 focus:border-indigo-500 focus:outline-none"
        />
        <button
          onClick={search}
          disabled={loading}
          className="px-4 py-2 bg-indigo-600 hover:bg-indigo-700 text-white rounded-lg text-sm disabled:opacity-50"
        >
          {loading ? "Searching…" : "Search"}
        </button>
      </div>
      <div className="space-y-3">
        {error && (
          <p className="text-red-400" role="alert">
            Search failed: {error}
          </p>
        )}
        {!searched && !loading && !error && (
          <p className="text-gray-400">Search company memory to see what the agents have recorded.</p>
        )}
        {searched && !error && results !== null && results.length === 0 && (
          <p className="text-gray-400">No entries matched that search.</p>
        )}
        {results?.map((m) => (
          <div key={m.id} className="bg-gray-800 rounded-lg p-4">
            <div className="flex items-center gap-2 mb-1">
              <span className="text-xs bg-indigo-600/30 text-indigo-300 px-2 py-0.5 rounded">
                {m.category}
              </span>
              <span className="text-gray-500 text-xs">
                {m.created_at ? new Date(m.created_at).toLocaleDateString() : "date unknown"}
              </span>
            </div>
            <p className="text-white font-medium text-sm">{m.title}</p>
            <p className="text-gray-400 text-sm mt-1 line-clamp-3">{m.content}</p>
          </div>
        ))}
      </div>
    </div>
  );
}
