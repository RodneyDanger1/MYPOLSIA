"use client";
import { useCallback, useEffect, useState } from "react";

import { SocialPostCard, type SocialPost } from "@/components/social/SocialPostCard";
import { ApiError, api } from "@/lib/api";

/** The read's page size — matches the server default and the page's historical `?limit=50`. */
const LIST_LIMIT = 50;

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
 * The live Twitter widget's actions (`TWEET`, `Delete tweet`, `CONNECT X ACCOUNT` — DOSSIER §1.5)
 * have no endpoint here (route inventory this wave: `GET /social/posts` only, `social.py:14`) and
 * are refused by omission rather than drawn as dead controls. The only button on this page is
 * REFRESH, which re-issues the list read. No polling (C7).
 *
 * Q-12: the API serves no total, so a full page of `LIST_LIMIT` rows renders the truncation
 * marker below the list; anything shorter renders no marker (nothing was cut off).
 */

export default function SocialPage() {
  const [data, setData] = useState<SocialPost[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadCount, setReloadCount] = useState(0);

  useEffect(() => {
    let cancelled = false;
    // A refresh goes back through "not read yet" on purpose: the skeleton is the one honest way
    // to say "the list you are looking at is being replaced" without faking progress.
    setData(null);
    setError(null);
    api
      .get<SocialPost[]>(`/social/posts?limit=${LIST_LIMIT}`)
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
        {/* Wording kept as-is: social.md OPEN DECISION #1 leaves the live title (`Twitter` vs
            `Social`) to Riley; the restyle is token/region-level (R1: serif h1 + hairline). */}
        <h1 className="mr-auto text-3xl font-bold text-ink">Social Media</h1>
        <button type="button" className="link ui-label" onClick={refresh} data-testid="social-refresh">
          Refresh
        </button>
      </div>

      {error ? (
        <p role="alert" className="text-sm text-alert">
          Could not load posts: {error}
        </p>
      ) : null}

      {!error && data === null ? (
        <div className="space-y-3" data-testid="social-loading" aria-busy="true">
          <p className="ui-label text-ink-muted">Loading…</p>
          {Array.from({ length: 3 }).map((_, index) => (
            <div key={index} className="h-24 animate-pulse border border-line bg-surface-strong" />
          ))}
        </div>
      ) : null}

      {!error && data !== null && data.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No posts yet. The social media agent drafts posts during its cycles — run it from the
          Agents page.
        </p>
      ) : null}

      {data !== null && data.length > 0 ? (
        <div className="space-y-3" data-testid="social-posts">
          {data.map((post) => (
            <SocialPostCard key={post.id} post={post} />
          ))}
          {/* Q-12: the read is capped at LIST_LIMIT and the API serves no total, so a full page
              says exactly that instead of presenting the cap as the whole list. */}
          {truncated ? (
            <p className="ui-label text-ink-muted" data-testid="social-truncation">
              Showing the first {LIST_LIMIT} — the API caps this read at {LIST_LIMIT} per request.
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
