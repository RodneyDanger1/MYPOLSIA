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
 * search actually succeeded and returned nothing.  A slowly-answering index has its own state: past
 * WARMING_AFTER_MS in flight the page says the index is warming (it is not a failure), because the
 * first search against a cold store can outlive the client default — measured >30 s on this host
 * (G-e2e-pages/RECEIPT.md §3; the lab proxy caps at 30 s on a cold Chroma load).
 */
type Memory = {
  id: number | string;
  category: string;
  title: string;
  content: string;
  created_at: string | null;
};

/**
 * The search call's options.  `timeoutMs` is `apiFetch`'s real option (lib/api.ts reads it off the
 * same object it spreads into RequestInit), even though `api.get`'s declared parameter is a bare
 * `RequestInit` — naming it here mirrors the real signature without a cast.  120 s so the CLIENT is
 * never the first to give up: the semantic search loads Chroma's embedding model on its first call,
 * and the 15 s default aborted while the server was still working — which this page then reported as
 * "request timed out after 15000ms", a failure the user did not have.
 */
type MemorySearchInit = RequestInit & { timeoutMs?: number };
const WARM_SEARCH: MemorySearchInit = { timeoutMs: 120_000 };
/** After this long in flight the page says "warming", not nothing. */
const WARMING_AFTER_MS = 10_000;

export default function MemoryPage() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Memory[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [warming, setWarming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);

  const search = async () => {
    if (!query.trim()) return;
    setLoading(true);
    setError(null);
    setWarming(false);
    // The timer is the state machine's "slow, not failed" edge; it is cleared on every settle.
    const warmingTimer = setTimeout(() => setWarming(true), WARMING_AFTER_MS);
    try {
      const data = await api.get<Memory[]>(
        `/memory?q=${encodeURIComponent(query)}&limit=20`,
        WARM_SEARCH,
      );
      setResults(data);
      setSearched(true);
    } catch (e: unknown) {
      setError(e instanceof Error ? e.message : String(e));
      setResults(null);
    } finally {
      clearTimeout(warmingTimer);
      setWarming(false);
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
        {loading && warming && (
          <p className="text-amber-400 text-sm" role="status">
            Still searching — the first search after a restart can take ~30 s while the memory index
            warms up. A slow answer is not a failed one.
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
