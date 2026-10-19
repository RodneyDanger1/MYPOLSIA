"use client";
import { useCallback, useEffect, useState } from "react";

import { ProspectRow, type Prospect } from "@/components/outreach/ProspectRow";
import { ApiError, api } from "@/lib/api";

/** The read's page size — matches the server default and the page's historical `?limit=100`. */
const LIST_LIMIT = 100;

/** ApiError carries a named `.display` (code + message + fix hint); anything else shows its own
 * message rather than a generic apology (the agents/settings pages' convention). */
function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  return e instanceof Error ? e.message : String(e);
}

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
 *   error    -> the failure NAMED on the page (ApiError.display), never an empty list standing in for it
 *   empty    -> printed ONLY when the read succeeded and really returned nothing
 *
 * Q-08: the old page had NO loading state at all — for the whole read it showed column headers over
 * an empty body (its own docstring claimed a skeleton). This page renders a real skeleton with
 * `aria-busy` while the read is in flight, the named failure when it fails, and the
 * producer-naming empty copy only after a successful 200 that really returned nothing.
 *
 * The live Email widget's actions (`COLD OUTREACH`, `SUPPORT` + their frequency modals — DOSSIER
 * §1.6; cap-07/cap-08) are NOT built: collecting a cadence nothing can store would be a fake
 * capability, because the clone has no schedule/recurrence model (outreach.md R5,
 * GAP-NO-ENDPOINT). The live `SENT` counter is likewise refused — `email_logs` rows exist but no
 * route serves them — so no counter block renders at all, and certainly no fabricated `0`.
 */

export default function OutreachPage() {
  const [data, setData] = useState<Prospect[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** The server's full filtered count from THIS read's `X-Polsia-Total` (same response as the
   *  rows). null = no readable header; the count then states only the page length + cap. */
  const [total, setTotal] = useState<number | null>(null);
  const [reloadCount, setReloadCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    // A refresh goes back through "not read yet" on purpose: the skeleton is the one honest way
    // to say "the list you are looking at is being replaced" without faking progress.
    setData(null);
    setError(null);
    setTotal(null);
    api
      .get<Prospect[]>(`/outreach/prospects?limit=${LIST_LIMIT}`, {
        onHeaders: (headers) => {
          if (cancelled) return;
          const raw = headers.get("X-Polsia-Total");
          if (raw === null) return;
          const parsed = Number(raw.trim());
          if (Number.isSafeInteger(parsed) && parsed >= 0) setTotal(parsed);
        },
      })
      .then((rows) => {
        if (!cancelled) setData(rows);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [reloadCount]);

  const refresh = useCallback(() => setReloadCount((n) => n + 1), []);
  const truncated = data !== null && data.length === LIST_LIMIT;

  return (
    <div className="space-y-4">
      <div className="flex items-end gap-2 border-b border-line pb-1">
        <h1 className="mr-auto text-3xl font-bold text-ink">Outreach</h1>
        <button type="button" className="link ui-label" onClick={refresh} data-testid="outreach-refresh">
          Refresh
        </button>
      </div>

      {/* Q-12 + W1 GAP-1c: a count of what THIS read measured — never a claimed total. When the
          response declared the full filtered count, both numbers are stated ("100 of 137"); when
          it did not, a full page still discloses its cap. */}
      {data !== null ? (
        <p className="ui-label text-ink-muted" data-testid="outreach-count">
          {total !== null && total > data.length
            ? `${data.length} of ${total} prospects`
            : `${data.length} ${data.length === 1 ? "prospect" : "prospects"}${
                truncated ? ` · showing the first ${LIST_LIMIT}` : ""
              }`}
        </p>
      ) : null}

      {error ? (
        <p role="alert" className="text-sm text-alert">
          Could not load prospects: {error}
        </p>
      ) : null}

      {!error && data === null ? (
        <div className="space-y-2" data-testid="outreach-loading" aria-busy="true">
          <p className="ui-label text-ink-muted">Loading…</p>
          {Array.from({ length: 4 }).map((_, index) => (
            <div key={index} className="flex items-center justify-between gap-3 border border-line px-3 py-3">
              <div className="h-3 w-2/5 animate-pulse bg-surface-strong" />
              <div className="h-3 w-16 animate-pulse bg-surface-strong" />
            </div>
          ))}
        </div>
      ) : null}

      {!error && data !== null && data.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No prospects yet. The outreach agent finds prospects when it runs — dispatch it from the
          Agents page.
        </p>
      ) : null}

      {data !== null && data.length > 0 ? (
        <ul className="card-surface" data-testid="outreach-list">
          {data.map((prospect) => (
            <ProspectRow key={prospect.id} prospect={prospect} />
          ))}
        </ul>
      ) : null}
    </div>
  );
}
