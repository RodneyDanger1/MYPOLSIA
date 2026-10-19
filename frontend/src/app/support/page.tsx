"use client";

import { useEffect, useState } from "react";

import { InboxComposer } from "@/components/support/InboxComposer";
import { InboxItemRow } from "@/components/support/InboxItemRow";
import type { SupportInboxItem, SupportInboxStatus } from "@/components/support/types";
import { ApiError, api } from "@/lib/api";

/**
 * `/support` — the inbox mirror (wiring audit F4). Client component on purpose: the API base
 * URL is only known at runtime, and a prerendered server fetch would bake the read-time answer
 * into the build (the lesson the tasks page records).
 *
 * The read is `GET /api/v1/support/inbox` (`support.py:48-58`): newest first, `?status=` limited
 * to the service's `VALID_STATUSES` ("pending"/"dispatched" — an unknown value is refused by
 * name, so the filter offers exactly those), `limit` 1..200. States are the house three:
 * loading (skeleton, `aria-busy`), error (the named failure under `role="alert"` — never empty
 * copy), empty (only after a successful read that returned nothing).
 */

const LIST_LIMIT = 50;

function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  return e instanceof Error ? e.message : String(e);
}

export default function SupportPage() {
  const [items, setItems] = useState<SupportInboxItem[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** The server's full filtered count from THIS read's `X-Polsia-Total` (same response as the
   *  rows). null = no readable header; the count then states only the page length + cap. */
  const [total, setTotal] = useState<number | null>(null);
  const [filter, setFilter] = useState<"" | SupportInboxStatus>("");
  const [reloadCount, setReloadCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    // A filter change or refresh goes back through "not read yet": the skeleton is the one
    // honest way to say "the list you are looking at is being replaced" (outreach-page rule).
    setItems(null);
    setError(null);
    setTotal(null);
    const path =
      filter === ""
        ? `/support/inbox?limit=${LIST_LIMIT}`
        : `/support/inbox?status=${filter}&limit=${LIST_LIMIT}`;
    api
      .get<SupportInboxItem[]>(path, {
        onHeaders: (headers) => {
          if (cancelled) return;
          const raw = headers.get("X-Polsia-Total");
          if (raw === null) return;
          const parsed = Number(raw.trim());
          if (Number.isSafeInteger(parsed) && parsed >= 0) setTotal(parsed);
        },
      })
      .then((rows) => {
        if (!cancelled) setItems(rows);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [filter, reloadCount]);

  const refresh = () => setReloadCount((n) => n + 1);
  const truncated = items !== null && items.length === LIST_LIMIT;

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end gap-2 border-b border-line pb-1">
        <h1 className="mr-auto text-3xl font-bold text-ink">Support</h1>
        <label htmlFor="support-filter" className="ui-label text-ink-muted">
          Status
        </label>
        <select
          id="support-filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value as "" | SupportInboxStatus)}
          className="border border-line-strong bg-surface px-2 py-1 text-sm text-ink"
        >
          <option value="">All</option>
          <option value="pending">pending</option>
          <option value="dispatched">dispatched</option>
        </select>
        <button type="button" className="link ui-label" onClick={refresh}>
          Refresh
        </button>
      </div>

      <InboxComposer onFiled={refresh} />

      {/* A count of what THIS read measured — never a claimed total (Q-12). When the response
          declared the full filtered count, both numbers are stated ("50 of 137"); when it did
          not, a full page still discloses its cap. */}
      {items !== null ? (
        <p className="ui-label text-ink-muted" data-testid="support-count">
          {total !== null && total > items.length
            ? `${items.length} of ${total} messages`
            : `${items.length} ${items.length === 1 ? "message" : "messages"}${
                truncated ? ` · showing the first ${LIST_LIMIT}` : ""
              }`}
        </p>
      ) : null}

      {error ? (
        <div role="alert" className="border border-alert bg-surface p-3">
          <p className="ui-label text-alert">Could not load the inbox</p>
          <p className="mt-1 text-sm text-ink">{error}</p>
        </div>
      ) : null}

      {error === null && items === null ? (
        <div className="space-y-2" data-testid="support-loading" aria-busy="true">
          <p className="ui-label text-ink-muted">Loading the inbox…</p>
          {Array.from({ length: 4 }).map((_, index) => (
            <div key={index} className="card-surface px-3 py-3">
              <div className="h-3 w-2/5 animate-pulse bg-surface-strong" />
              <div className="mt-2 h-3 w-3/5 animate-pulse bg-surface-strong" />
            </div>
          ))}
        </div>
      ) : null}

      {items !== null && items.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No messages in the inbox{filter === "" ? "" : ` with status "${filter}"`} yet. File one
          above — or leave it empty; the 3h sweep only attaches what is here.
        </p>
      ) : null}

      {items !== null && items.length > 0 ? (
        <ul className="card-surface" data-testid="support-list">
          {items.map((item) => (
            <InboxItemRow key={item.id} item={item} />
          ))}
        </ul>
      ) : null}
    </div>
  );
}
