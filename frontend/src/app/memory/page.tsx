"use client";
import { useEffect, useRef, useState } from "react";

import { ApiError, api, type ApiFetchOptions } from "@/lib/api";
import { Button, Chip } from "@/components/ui";
import { absoluteStamp, relativeTime } from "@/components/dashboard/format";

/**
 * Company-memory search, on the U1 design language.
 *
 * The search is explicit — nothing is fetched before the operator asks — and the states are honest:
 * never-searched (guidance), searching, failed (NAMED), and "no matches" printed only when a search
 * actually succeeded and returned nothing. A slowly-answering index has its own state: past
 * WARMING_AFTER_MS in flight the page says the index is warming (it is not a failure), because the
 * first search against a cold store can outlive the client default — measured >30 s on this host
 * (G-e2e-pages/RECEIPT.md §3; the lab proxy caps at 30 s on a cold Chroma load).
 *
 * Q-11: Enter used to bypass the button's disabled guard, so two searches could overlap and the
 * older response could land last. `search()` now self-guards on a synchronous `inFlight` ref (the
 * button and Enter share ONE guard) AND carries a `requestSeq` that every state write is compared
 * against (the `useAgentStatus` pattern) — a superseded response can never render, even if a future
 * caller finds another way to overlap two calls. The unmount guard closes the 120 s window.
 */
/**
 * The search wire shape (`MemoryItem`, `backend/app/schemas/memory.py`): `category`, `title`, `id`
 * and `created_at` are nullable server-side — the semantic path builds each hit from Chroma metadata
 * with `meta.get("category")` / `meta.get("title")`, `id: int | str | None`, and the timestamp is
 * `datetime | None`. `content` is required as a str on the wire, but the renderer survives a null
 * without claiming text the server did not send.
 *
 * Exported (type-only — erased at build time, invisible to Next's page-export validator) so this
 * page's tests can pin the nullable contract at COMPILE time: a fixture declaring `category: null`
 * stops compiling the moment the type drops the null.
 */
export type Memory = {
  id: number | string | null;
  category: string | null;
  title: string | null;
  content: string | null;
  created_at: string | null;
  /** The service's DEGRADATION MARKER, carried on the wire as `MemoryItem.fallback`: `"sql_like"`
   *  means the vector store was unreachable and this hit came from an ILIKE keyword scan;
   *  `"sql_unfiltered"` means the collection rejected `where` and the category filter was dropped.
   *  A null/absent marker means the hit came from the real similarity search — the badge below is
   *  the only thing that lets the operator tell the two apart (a keyword scan must not pass as a
   *  semantic match). */
  fallback?: string | null;
};

/** Honest label for a degraded hit.  An unknown marker is shown as-is rather than hidden: the
 *  contract is an open string, so a new marker must stay visible, not silently dropped. */
const FALLBACK_LABEL: Record<string, string> = {
  sql_like: "keyword match — memory index unavailable",
  sql_unfiltered: "keyword match — category filter dropped",
};

function fallbackLabel(marker: string): string {
  return FALLBACK_LABEL[marker] ?? `degraded result (${marker})`;
}

/** A category that is null, absent or blank renders the gray "unknown" chip — never an empty chip
 *  pretending to be a category, and never a chip for whitespace. */
function memoryCategory(category: string | null | undefined): string | null {
  if (category === null || category === undefined) return null;
  return category.trim() === "" ? null : category;
}

/**
 * The search call's options — the REAL shape (`ApiFetchOptions` in lib/api.ts): `timeoutMs` is read
 * off the same object that is spread into `RequestInit`, and the client raises its whole-read budget
 * to the caller's intent, so an explicit 120 s timeout genuinely buys 120 s. 120 s so the CLIENT is
 * never the first to give up: the semantic search loads Chroma's embedding model on its first call,
 * and the 15 s default aborted while the server was still working — the pre-U1 client reported that
 * as "request timed out after 15000ms", a failure the user did not have.
 */
const WARM_SEARCH_OPTIONS: ApiFetchOptions = { timeoutMs: 120_000 };
/** After this long in flight the page says "warming", not nothing. */
const WARMING_AFTER_MS = 10_000;
/** The read's page size (backend default; the label below discloses a full page, Q-12's idiom). */
const MEMORY_SEARCH_LIMIT = 20;

/** The house error idiom (`ApiError.display`), with a truly honest fallback: a non-Error throw is
 *  rendered as its JSON when it serializes — never laundered into "[object Object]". */
function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

export default function MemoryPage() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Memory[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [warming, setWarming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);

  const inFlight = useRef(false);
  const requestSeq = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const search = async () => {
    const q = query.trim();
    if (!q) return; // an empty query is not a search — and must not clear what is on screen
    if (inFlight.current) return; // Enter shares the button's disabled guard (Q-11)
    inFlight.current = true;
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    setWarming(false);
    // The timer is the state machine's "slow, not failed" edge; it is cleared on every settle.
    const warmingTimer = setTimeout(() => {
      if (mounted.current && seq === requestSeq.current) setWarming(true);
    }, WARMING_AFTER_MS);
    try {
      const data = await api.get<Memory[]>(
        `/memory?q=${encodeURIComponent(q)}&limit=${MEMORY_SEARCH_LIMIT}`,
        WARM_SEARCH_OPTIONS,
      );
      if (!mounted.current || seq !== requestSeq.current) return; // superseded/unmounted: never render
      if (!Array.isArray(data)) {
        // A non-list payload is a broken contract, not "no results" — say so.
        throw new Error(
          `the server answered with ${data === null ? "null" : typeof data} instead of a list of entries`,
        );
      }
      setResults(data);
      setSearched(true);
    } catch (e: unknown) {
      if (!mounted.current || seq !== requestSeq.current) return;
      setError(errorText(e));
      setResults(null);
    } finally {
      clearTimeout(warmingTimer);
      inFlight.current = false;
      if (mounted.current && seq === requestSeq.current) {
        setWarming(false);
        setLoading(false);
      }
    }
  };

  return (
    <div className="max-w-3xl space-y-4">
      <div className="border-b border-line pb-2">
        <h1 className="text-3xl font-bold text-ink">Memory</h1>
      </div>

      <div className="flex flex-wrap items-end gap-2">
        <div className="min-w-0 flex-1">
          {/* Q-16: the input's accessible name is a real label — the placeholder is a hint, not a name. */}
          <label htmlFor="memory-search" className="ui-label block text-ink-muted">
            Search company memory
          </label>
          <input
            id="memory-search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                void search();
              }
            }}
            placeholder="Search company memory…"
            autoComplete="off"
            className="mt-1 h-[49px] w-full border border-line-strong bg-surface px-3 text-sm text-ink"
          />
        </div>
        <Button variant="glossy" onClick={() => void search()} disabled={loading}>
          {loading ? "Searching…" : "Search"}
        </Button>
      </div>

      <div aria-busy={loading ? true : undefined} className="space-y-3">
        {error ? (
          <div role="alert" className="border border-alert bg-surface p-3">
            <p className="ui-label text-alert">Search failed</p>
            <p className="mt-1 text-sm text-ink">{error}</p>
          </div>
        ) : null}

        {loading && warming ? (
          <p role="status" className="ui-label text-ink-muted">
            Still searching — the first search after a restart can take a while (~30 s measured on
            this host) while the memory index warms up. A slow answer is not a failed one.
          </p>
        ) : null}

        {!searched && !loading && !error ? (
          <p className="ui-label text-ink-muted">
            Search company memory to see what the agents have recorded. The first search after a
            restart can take a while while the index warms up.
          </p>
        ) : null}

        {searched && !error && results !== null && results.length === 0 ? (
          // Q-26: the empty state says what to do next.
          <p className="ui-label text-ink-muted">
            No entries matched that search. Try a broader word — agents record memory as they work.
          </p>
        ) : null}

        {results !== null && results.length === MEMORY_SEARCH_LIMIT ? (
          // Q-12's idiom, memory-sized: a full page is disclosed, never presented as the whole list.
          <p className="ui-label text-ink-muted">
            Showing the first {MEMORY_SEARCH_LIMIT} matches — the API reports no total, so more may
            exist.
          </p>
        ) : null}

        {results !== null && results.length > 0 ? (
          <ul className="space-y-3">
            {results.map((m, index) => (
              // `id` is nullable on the wire; a missing one falls back to the row's position.
              <li key={m.id ?? `memory-row-${index}`} className="card-surface p-3">
                <div className="flex flex-wrap items-center gap-2">
                  {/* `category` is nullable on the wire: a null renders the gray "unknown" chip (the
                      tasks/social pages' doctrine) instead of an empty chip pretending to be a
                      category. */}
                  <Chip variant={memoryCategory(m.category) === null ? "soft" : "dark"}>
                    {memoryCategory(m.category) ?? "unknown"}
                  </Chip>
                  {/* The degradation marker, rendered where the results are: a keyword scan must not
                      look like a semantic match. */}
                  {m.fallback ? <Chip variant="accent">{fallbackLabel(m.fallback)}</Chip> : null}
                  <Chip variant="soft" title={absoluteStamp(m.created_at)}>
                    {relativeTime(m.created_at)}
                  </Chip>
                </div>
                {/* A null title (the metadata may simply not carry one) is named "untitled", never an
                    empty line and never the literal word "null". */}
                <p className="mt-1 font-bold text-ink">{m.title ?? "untitled"}</p>
                <p className="mt-1 text-sm text-ink">{m.content ?? "No content recorded."}</p>
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </div>
  );
}
