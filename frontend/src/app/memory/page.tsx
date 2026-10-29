"use client";
import { useEffect, useRef, useState } from "react";

import { ApiError, api, type ApiFetchOptions } from "@/lib/api";
import { Button, Chip } from "@/components/ui";
import { MemoryTags } from "@/components/memory/MemoryTags";
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
 * F-42 (wave 8): the page ALSO offers an explicit "Recent entries" read — the LIST branch of the
 * same endpoint (`GET /memory?limit=…`, no `q`), which is the branch that carries each row's stored
 * `tags`. The screen used to serve only the semantic branch, whose hits carry none, so every stored
 * tag was invisible on the one surface built to inspect memory. Both reads are click-only and share
 * one in-flight guard; the disclosure under the list states which read answered it (a semantic
 * search serves no total; the list read declares `X-Meridian-Total`).
 *
 * Q-11: Enter used to bypass the button's disabled guard, so two searches could overlap and the
 * older response could land last. `search()` now self-guards on a synchronous `inFlight` ref (the
 * button and Enter share ONE guard) AND carries a `requestSeq` that every state write is compared
 * against (the `useAgentStatus` pattern) — a superseded response can never render, even if a future
 * caller finds another way to overlap two calls. The unmount guard closes the 120 s window.
 *
 * V15/W1 (4-05(c) / S3-03, decision #6 LOCKED) adds the owner's ADD + FORGET controls against the
 * SHIPPING routes (`backend/app/api/v1/memory.py`): POST /memory (`MemoryCreate` — category/title/
 * content required; source/tags optional; 201 = the stored `MemoryEntryResponse`) and
 * DELETE /memory/{id:int} (200 = the REMOVED entry's representation, named `E_NOT_FOUND` when it
 * is not there). Both are REAL writes to both stores (sqlite + ChromaDB) — the page never mocks an
 * outcome: a success notice names the entry the server returned, a refusal renders the server's
 * named error, and a failed add keeps the draft. The per-row Forget control only exists for hits
 * with an addressable INTEGER id (the route takes `memory_id: int`); a hit whose id is a Chroma
 * string (or absent) says why it cannot be forgotten instead of offering a button that would 422.
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
  /** F-42 (wave 8): the row's stored tags (`MemoryItem.tags`, `list[str] | None`). The LIST branch
   *  (`GET /memory?limit=…`, no `q`) carries them — measured live 2026-10-29 on 50/50 rows — while
   *  the SEMANTIC branch is built from Chroma metadata and carries none today
   *  (`memory_service.search_memory`). Absent is absence: the row renders no tag claim for it
   *  (see `MemoryTags`). */
  tags?: string[] | null;
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

/** The POST's 201 body (`MemoryEntryResponse` in `backend/app/schemas/memory.py`): the stored row;
 *  `id` is always an int there, and the DELETE 200 answers the REMOVED row in this same shape. */
type MemoryEntry = {
  id: number;
  category: string;
  title: string;
  content: string;
  source?: string | null;
  tags?: string[] | null;
  chroma_id?: string | null;
  created_at?: string | null;
};

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
/** F-42: the browse read's page size and path — the LIST branch, which carries each row's tags.
 *  No `q` key at all: `?q=` is the semantic branch and it serves no tags today. */
const MEMORY_BROWSE_LIMIT = 20;
const MEMORY_BROWSE_PATH = `/memory?limit=${MEMORY_BROWSE_LIMIT}`;

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

/** Client-side required-field check for the add form — `MemoryCreate` requires the three strings,
 *  and a blank among them is not a memory worth storing. Returns the NAMED missing fields, or null
 *  when the form may be sent. Nothing is sent while this returns a message. */
function addIssue(category: string, title: string, content: string): string | null {
  const missing: string[] = [];
  if (!category.trim()) missing.push("category");
  if (!title.trim()) missing.push("title");
  if (!content.trim()) missing.push("content");
  if (missing.length === 0) return null;
  return `Missing required ${missing.length === 1 ? "field" : "fields"}: ${missing.join(
    ", ",
  )} — nothing was sent.`;
}

/** The house input recipe (same measured 49 px density as the Settings form). */
const ADD_INPUT_CLASS =
  "mt-1 h-[49px] w-full border border-line-strong bg-surface px-3 text-sm text-ink";

export default function MemoryPage() {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<Memory[] | null>(null);
  const [loading, setLoading] = useState(false);
  const [warming, setWarming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [searched, setSearched] = useState(false);
  /** V25/L5: the query the last failed search used — Retry re-runs THAT query, not whatever the
   *  input happens to contain now (the failure named a specific read; the retry must be the same
   *  read). */
  const [failedQuery, setFailedQuery] = useState<string | null>(null);
  /** F-42: which read produced what is on screen. `browse` is the explicit LIST read (newest
   *  stored rows, each with its own tags); `search` is the semantic hit list. The disclosure text
   *  must say the truth that belongs to the mode — a search serves no total, the list read does. */
  const [mode, setMode] = useState<"search" | "browse" | null>(null);
  const [browseTotal, setBrowseTotal] = useState<number | null>(null);
  const [browseLoading, setBrowseLoading] = useState(false);
  const [browseError, setBrowseError] = useState<string | null>(null);

  // ── V15/W1 (4-05(c)): add-form state — local drafts only; no request fires before Store. ──
  const [addOpen, setAddOpen] = useState(false);
  const [addCategory, setAddCategory] = useState("");
  const [addTitle, setAddTitle] = useState("");
  const [addContent, setAddContent] = useState("");
  const [addSource, setAddSource] = useState("");
  const [addTags, setAddTags] = useState("");
  const [adding, setAdding] = useState(false);
  const [addError, setAddError] = useState<string | null>(null);
  const [addNotice, setAddNotice] = useState<string | null>(null);
  // ── V15/W1 (4-05(c)): per-row forget state — `forgetTarget` is the ARMED confirm (first click
  //    arms, the second fires: forgetting is a real delete of both stores, so one click is not
  //    enough); `forgetting` is the id currently in flight. ──
  const [forgetTarget, setForgetTarget] = useState<number | null>(null);
  const [forgetting, setForgetting] = useState<number | null>(null);
  const [forgetError, setForgetError] = useState<string | null>(null);
  const [forgetNotice, setForgetNotice] = useState<string | null>(null);

  const inFlight = useRef(false);
  const requestSeq = useRef(0);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const search = async (queryOverride?: string) => {
    const q = (queryOverride ?? query).trim();
    if (!q) return; // an empty query is not a search — and must not clear what is on screen
    if (inFlight.current) return; // Enter shares the button's disabled guard (Q-11)
    inFlight.current = true;
    const seq = ++requestSeq.current;
    setLoading(true);
    setError(null);
    setFailedQuery(null);
    setWarming(false);
    // F-42: a search answer replaces a stale browse failure — the alert names the READ that
    // failed, and this is a newer read of the same screen.
    setBrowseError(null);
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
      setMode("search");
      setBrowseTotal(null);
      setBrowseError(null);
    } catch (e: unknown) {
      if (!mounted.current || seq !== requestSeq.current) return;
      setError(errorText(e));
      setFailedQuery(q);
      setResults(null);
      setMode(null);
    } finally {
      clearTimeout(warmingTimer);
      inFlight.current = false;
      if (mounted.current && seq === requestSeq.current) {
        setWarming(false);
        setLoading(false);
      }
    }
  };

  /**
   * F-42 (wave 8) — the explicit LIST read: the newest stored entries, each with the tags the row
   * stores. This is the branch of `GET /memory` that actually carries `tags` (the semantic branch
   * does not today, measured 2026-10-29), so the screen can show them without inventing a join the
   * wire does not support.
   *
   * It is CLICK-ONLY, like the search: the page still fires no read on mount. It shares the one
   * `inFlight` guard with the search, so two result reads can never interleave and an older answer
   * can never land over a newer one. The declared total comes from THIS response's
   * `X-Meridian-Total` header (the same read the rows came from), and a missing/unreadable header is
   * `null` — the label then says only what the page length proves.
   */
  const browse = async () => {
    if (inFlight.current) return; // one result read at a time (the search shares this guard)
    inFlight.current = true;
    setBrowseLoading(true);
    setBrowseError(null);
    setError(null); // a browse answer replaces the search alert: this read is the newer intent
    setFailedQuery(null);
    setWarming(false);
    let total: number | null = null;
    try {
      const data = await api.get<Memory[]>(MEMORY_BROWSE_PATH, {
        onHeaders: (headers) => {
          const raw = headers.get("X-Meridian-Total");
          const parsed = raw === null || raw === "" ? Number.NaN : Number(raw);
          total = Number.isInteger(parsed) && parsed >= 0 ? parsed : null;
        },
      });
      if (!mounted.current) return;
      if (!Array.isArray(data)) {
        // A non-list payload is a broken contract, not "no entries" — say so.
        throw new Error(
          `the server answered with ${data === null ? "null" : typeof data} instead of a list of entries`,
        );
      }
      setResults(data);
      setSearched(true);
      setMode("browse");
      setBrowseTotal(total);
    } catch (e: unknown) {
      if (!mounted.current) return;
      setBrowseError(errorText(e));
      setResults(null);
      setMode(null);
      setBrowseTotal(null);
    } finally {
      inFlight.current = false;
      if (mounted.current) setBrowseLoading(false);
    }
  };

  /** Store one owner memory against the REAL route (a dual sqlite + Chroma write server-side).
   *  Success NAMES the entry the server returned; a failure renders the server's named refusal and
   *  keeps the draft; a currently-displayed search is re-run so what is on screen stays true. */
  const add = async () => {
    if (adding) return;
    const issue = addIssue(addCategory, addTitle, addContent);
    if (issue !== null) {
      setAddError(issue);
      return;
    }
    setAdding(true);
    setAddError(null);
    setAddNotice(null);
    try {
      const body: {
        category: string;
        title: string;
        content: string;
        source?: string;
        tags?: string[];
      } = {
        category: addCategory.trim(),
        title: addTitle.trim(),
        content: addContent.trim(),
      };
      const source = addSource.trim();
      if (source !== "") body.source = source;
      const tags = addTags
        .split(",")
        .map((tag) => tag.trim())
        .filter((tag) => tag !== "");
      if (tags.length > 0) body.tags = tags;
      const entry = await api.post<MemoryEntry>("/memory", body);
      if (!mounted.current) return;
      setAddNotice(
        `Stored “${entry.title}” (id ${entry.id}) — it is in the store now; the next search reads it.`,
      );
      // Clear the identifying fields; keep category/source/tags as the operator's context.
      setAddTitle("");
      setAddContent("");
      setAddTags("");
      // A displayed result set is now stale: refresh the SAME read (an explicit refresh of what is
      // on screen, not a surprise search). F-42: the browse read refreshes itself, so a stored row's
      // new tags cannot be left out of the view that shows tags.
      if (mode === "browse") {
        void browse();
      } else if (query.trim() !== "" && searched) {
        void search();
      }
    } catch (e: unknown) {
      if (!mounted.current) return;
      setAddError(errorText(e));
    } finally {
      if (mounted.current) setAdding(false);
    }
  };

  /** Forget one memory against the REAL DELETE route. The 200 carries the removed entry, so the
   *  notice names exactly what was removed; the row leaves the list only on that success. */
  const forget = async (id: number) => {
    if (forgetting !== null) return;
    setForgetting(id);
    setForgetError(null);
    setForgetNotice(null);
    try {
      const removed = await api.delete<MemoryEntry>(`/memory/${id}`);
      if (!mounted.current) return;
      setResults((current) =>
        current === null ? current : current.filter((row) => row.id !== id),
      );
      setForgetNotice(`Forgotten “${removed.title}” (id ${removed.id}) — removed from the store.`);
    } catch (e: unknown) {
      if (!mounted.current) return;
      setForgetError(errorText(e));
    } finally {
      if (mounted.current) {
        setForgetting(null);
        setForgetTarget((current) => (current === id ? null : current));
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
        <Button variant="glossy" onClick={() => void search()} disabled={loading || browseLoading}>
          {loading ? "Searching…" : "Search"}
        </Button>
        {/* F-42: the explicit LIST read. Search answers the semantic index (whose hits carry no
            tags on this server); the browse read serves the newest stored rows WITH their tags. */}
        <Button
          data-testid="memory-browse"
          onClick={() => void browse()}
          disabled={loading || browseLoading}
          title="Read the newest stored entries — the list branch, which carries each row's tags"
        >
          {browseLoading ? "Loading…" : "Recent entries"}
        </Button>
      </div>
      <p className="ui-label text-ink-muted" data-testid="memory-browse-note">
        Recent entries reads the stored rows themselves, so each shows its own stored tags;
        the semantic search above answers from the memory index, whose hits carry no tags on this
        server.
      </p>

      {/* V15/W1 (4-05(c)): the owner's REAL write path — POST /memory. Collapsed until asked;
          nothing is requested while it is closed. */}
      <section className="card-surface p-3 space-y-2" aria-label="Record a memory">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div>
            <p className="ui-label text-ink">Record a memory</p>
            <p className="ui-label mt-1 text-ink-muted">
              Owner entries store for real — the same store the agents search. The three required
              fields are refused by name when blank; nothing is half-recorded.
            </p>
          </div>
          <Button size="sm" onClick={() => setAddOpen((open) => !open)} aria-expanded={addOpen}>
            {addOpen ? "Close add form" : "Add memory"}
          </Button>
        </div>

        {addOpen ? (
          <div className="space-y-2" data-testid="memory-add-form">
            <div>
              <label htmlFor="memory-add-category" className="ui-label block text-ink-muted">
                Category
              </label>
              <input
                id="memory-add-category"
                value={addCategory}
                onChange={(e) => setAddCategory(e.target.value)}
                autoComplete="off"
                className={ADD_INPUT_CLASS}
              />
            </div>
            <div>
              <label htmlFor="memory-add-title" className="ui-label block text-ink-muted">
                Title
              </label>
              <input
                id="memory-add-title"
                value={addTitle}
                onChange={(e) => setAddTitle(e.target.value)}
                autoComplete="off"
                className={ADD_INPUT_CLASS}
              />
            </div>
            <div>
              <label htmlFor="memory-add-content" className="ui-label block text-ink-muted">
                Content
              </label>
              <input
                id="memory-add-content"
                value={addContent}
                onChange={(e) => setAddContent(e.target.value)}
                autoComplete="off"
                className={ADD_INPUT_CLASS}
              />
            </div>
            <div>
              <label htmlFor="memory-add-source" className="ui-label block text-ink-muted">
                Source (optional)
              </label>
              <input
                id="memory-add-source"
                value={addSource}
                onChange={(e) => setAddSource(e.target.value)}
                autoComplete="off"
                className={ADD_INPUT_CLASS}
              />
            </div>
            <div>
              <label htmlFor="memory-add-tags" className="ui-label block text-ink-muted">
                Tags (optional, comma-separated)
              </label>
              <input
                id="memory-add-tags"
                value={addTags}
                onChange={(e) => setAddTags(e.target.value)}
                autoComplete="off"
                className={ADD_INPUT_CLASS}
              />
            </div>
            {addError ? (
              <div
                role="alert"
                data-testid="memory-add-error"
                className="border border-alert bg-surface p-3"
              >
                <p className="ui-label text-alert">Add refused</p>
                <p className="mt-1 text-sm text-ink">{addError}</p>
              </div>
            ) : null}
            <Button variant="glossy" size="sm" onClick={() => void add()} disabled={adding}>
              {adding ? "Storing…" : "Store memory"}
            </Button>
          </div>
        ) : null}

        {addNotice ? (
          <p
            role="status"
            data-testid="memory-add-notice"
            className="border border-line-strong bg-surface-sunk px-3 py-2 text-sm text-ink"
          >
            {addNotice}
          </p>
        ) : null}
      </section>

      <div
        aria-busy={loading || browseLoading ? true : undefined}
        className="space-y-3"
      >
        {error ? (
          <div role="alert" className="border border-alert bg-surface p-3">
            <p className="ui-label text-alert">Search failed</p>
            <p className="mt-1 text-sm text-ink">{error}</p>
            {/* V25/L5: a failed search is retryable in place — it re-runs the query that failed,
                rather than making the operator retype it and hope. */}
            <button
              type="button"
              className="link ui-label mt-2"
              data-testid="memory-search-retry"
              onClick={() => void search(failedQuery ?? undefined)}
              disabled={loading}
            >
              Retry
            </button>
          </div>
        ) : null}

        {/* F-42: the browse read's own failure — named, and never rendered as "no entries". */}
        {browseError ? (
          <div
            role="alert"
            data-testid="memory-browse-error"
            className="border border-alert bg-surface p-3"
          >
            <p className="ui-label text-alert">Could not load recent entries</p>
            <p className="mt-1 text-sm text-ink">{browseError}</p>
            <button
              type="button"
              className="link ui-label mt-2"
              data-testid="memory-browse-retry"
              onClick={() => void browse()}
              disabled={browseLoading || loading}
            >
              Retry
            </button>
          </div>
        ) : null}

        {forgetError ? (
          <div
            role="alert"
            data-testid="memory-forget-error"
            className="border border-alert bg-surface p-3"
          >
            <p className="ui-label text-alert">Forget refused</p>
            <p className="mt-1 text-sm text-ink">{forgetError}</p>
          </div>
        ) : null}

        {forgetNotice ? (
          <p
            role="status"
            data-testid="memory-forget-notice"
            className="border border-line-strong bg-surface-sunk px-3 py-2 text-sm text-ink"
          >
            {forgetNotice}
          </p>
        ) : null}

        {loading && warming ? (
          <p role="status" className="ui-label text-ink-muted">
            Still searching — the first search after a restart can take a while (~30 s measured on
            this host) while the memory index warms up. A slow answer is not a failed one.
          </p>
        ) : null}

        {browseLoading ? (
          <p role="status" data-testid="memory-browse-loading" className="ui-label text-ink-muted">
            Reading the newest stored entries…
          </p>
        ) : null}

        {!searched && !loading && !browseLoading && !error && !browseError ? (
          <p className="ui-label text-ink-muted">
            Search company memory to see what the agents have recorded, or open Recent entries to
            read the newest stored rows with their tags. The first search after a restart can take
            a while while the index warms up.
          </p>
        ) : null}

        {mode === "search" && !error && results !== null && results.length === 0 ? (
          // Q-26: the empty state says what to do next.
          <p className="ui-label text-ink-muted">
            No entries matched that search. Try a broader word — agents record memory as they work.
          </p>
        ) : null}

        {mode === "browse" && results !== null && results.length === 0 ? (
          <p className="ui-label text-ink-muted" data-testid="memory-browse-empty">
            No memory entries are stored yet, so there is nothing to browse. Agents record memory
            as they work.
          </p>
        ) : null}

        {mode === "search" && results !== null && results.length === MEMORY_SEARCH_LIMIT ? (
          // Q-12's idiom, memory-sized: a full page is disclosed, never presented as the whole
          // list. Precise wording (V22/L3, W1 GAP-1b): the SEMANTIC branch (`?q=`) serves no
          // total by design (`backend/app/api/v1/memory.py:43-46` — a hit list is not a table
          // page); only the unfiltered list branch emits X-Meridian-Total. Saying "the API reports
          // no total" overstated that.
          <p className="ui-label text-ink-muted">
            Showing the first {MEMORY_SEARCH_LIMIT} matches — this semantic search serves no
            total, so more may exist.
          </p>
        ) : null}

        {/* F-42: the browse read's disclosure. The list branch DOES declare a total
            (`X-Meridian-Total`, `backend/app/api/v1/memory.py:57`), so a truncated browse is
            distinguishable from the whole table; a missing/unreadable header says only what the
            page length proves. */}
        {mode === "browse" && results !== null && browseTotal !== null ? (
          <p className="ui-label text-ink-muted" data-testid="memory-browse-total">
            {results.length >= browseTotal
              ? `Showing all ${browseTotal} stored ${browseTotal === 1 ? "entry" : "entries"}.`
              : `Showing the newest ${results.length} of ${browseTotal} stored entries — ${
                  browseTotal - results.length
                } older ${browseTotal - results.length === 1 ? "entry is" : "entries are"} not shown.`}
          </p>
        ) : null}
        {mode === "browse" && results !== null && browseTotal === null ? (
          <p className="ui-label text-ink-muted" data-testid="memory-browse-no-total">
            Showing the newest {results.length} {results.length === 1 ? "entry" : "entries"} — this
            read declared no total, so more may exist.
          </p>
        ) : null}

        {results !== null && results.length > 0 ? (
          <ul className="space-y-3">
            {results.map((m, index) => {
              // `id` is nullable on the wire; a missing one falls back to the row's position.
              // V15/W1 (4-05(c)): only an INTEGER id is addressable by `DELETE /memory/{id:int}`;
              // a Chroma string id (the semantic path may carry one) cannot be forgotten through
              // this API, and the row says so instead of offering a button that would 422.
              const rowId = typeof m.id === "number" && Number.isInteger(m.id) ? m.id : null;
              return (
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
                    <span className="ml-auto">
                      {rowId !== null ? (
                        forgetTarget === rowId ? (
                          <span className="flex items-center gap-2">
                            {/* Forgetting removes the row AND its indexed document server-side —
                                one click arms, the second fires. */}
                            <Button
                              size="sm"
                              onClick={() => void forget(rowId)}
                              disabled={forgetting === rowId}
                            >
                              {forgetting === rowId ? "Forgetting…" : "Confirm forget"}
                            </Button>
                            <Button
                              size="sm"
                              onClick={() => setForgetTarget(null)}
                              disabled={forgetting === rowId}
                            >
                              Cancel
                            </Button>
                          </span>
                        ) : (
                          <Button
                            size="sm"
                            onClick={() => setForgetTarget(rowId)}
                            disabled={forgetting !== null}
                          >
                            Forget
                          </Button>
                        )
                      ) : (
                        <span
                          data-testid="memory-forget-unavailable"
                          className="ui-label text-ink-muted"
                          title="this hit carries no integer id, so the memory API has nothing addressable to remove"
                        >
                          no addressable id — cannot forget
                        </span>
                      )}
                    </span>
                  </div>
                  {/* A null title (the metadata may simply not carry one) is named "untitled", never an
                      empty line and never the literal word "null". */}
                  <p className="mt-1 font-bold text-ink">{m.title ?? "untitled"}</p>
                  {/* F-42: the row's own stored tags — rendered when the wire carries them (the
                      list branch), absent on a search hit that does not carry them. Bounded: see
                      `MemoryTags` (6 chips + "+N more"). */}
                  <MemoryTags tags={m.tags} />
                  <p className="mt-1 text-sm text-ink">{m.content ?? "No content recorded."}</p>
                </li>
              );
            })}
          </ul>
        ) : null}
      </div>
    </div>
  );
}
