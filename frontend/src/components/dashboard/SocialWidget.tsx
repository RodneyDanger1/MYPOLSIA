"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { Card, Chip } from "@/components/ui";
import { api } from "@/lib/api";

import { absoluteStamp } from "./format";
import { PanelError, PanelSkeleton, readErrorText } from "./states";

/**
 * The wire row of `GET /social/posts` (`SocialPostResponse`): `status`/`published_at`/
 * `scheduled_for` are nullable server-side, so null is a real served value. Declared locally
 * because the shared api module (another lane's file) does not carry the type.
 */
type SocialPost = {
  id: number;
  content: string;
  status: string | null;
  scheduled_for: string | null;
  published_at: string | null;
  engagement: Record<string, unknown> | null;
};

/** Q-25 applied here: a lookup with a fallback, so an unrecognised status can never borrow another
 *  status's colour, and a null `published_at` renders "date unknown" instead of deleting the
 *  cell. The 800-step palette keeps every hue ≥ 4.5:1 on the chip's light fill. */
const STATUS_TEXT: Record<string, string> = {
  published: "text-green-800",
  draft: "text-ink-muted",
  scheduled: "text-blue-800",
  failed: "text-red-800",
};

const WIDGET_LIMIT = 5;

/**
 * Right column, top: the live `Twitter` widget's slot, adapted to the real surface this repo has —
 * `GET /social/posts` (newest first). The live widget's `CONNECT X ACCOUNT` / `TWEET` actions have
 * no endpoint here (no X transport exists in this backend), so they are refused by omission rather
 * than drawn as dead buttons.
 */
export function SocialWidget() {
  const [posts, setPosts] = useState<SocialPost[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // V24/L5: the failure's Retry re-issues this read.
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setPosts(null);
    setError(null);
    api
      .get<SocialPost[]>(`/social/posts?limit=${WIDGET_LIMIT}`)
      .then((data) => {
        if (!cancelled) setPosts(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(readErrorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const loading = posts === null && !error;

  return (
    <Card
      title="Social"
      action={
        <Link href="/social" className="ui-label text-link hover:underline">
          View all posts
        </Link>
      }
    >
      {loading ? <PanelSkeleton rows={2} /> : null}
      {!loading && error ? (
        <PanelError
          label="social posts"
          message={error}
          onRetry={() => setAttempt((n) => n + 1)}
          retryTestId="social-widget-retry"
        />
      ) : null}
      {!loading && !error && posts && posts.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No posts yet. The social media agent drafts posts during its cycles — run it from the
          Agents page, or open Social for the full list.
        </p>
      ) : null}
      {!loading && !error && posts && posts.length > 0 ? <LatestPost posts={posts} /> : null}
    </Card>
  );
}

function LatestPost({ posts }: { posts: SocialPost[] }) {
  // The endpoint orders by `created_at DESC`, so the first row is the newest.
  const latest = posts[0];
  const publishedCount = posts.filter((post) => post.status === "published").length;
  const statusText = STATUS_TEXT[latest.status ?? ""] ?? "text-ink-muted";

  return (
    <div>
      <p className="line-clamp-4 text-sm leading-relaxed text-ink">{latest.content}</p>
      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Chip variant="soft" className={statusText}>
          {latest.status ?? "unknown"}
        </Chip>
        {/* A null `published_at` is "not recorded", never a removed cell (Q-25). */}
        <span className="ui-label text-ink-muted">{absoluteStamp(latest.published_at)}</span>
        {latest.scheduled_for ? (
          <span className="ui-label text-ink-muted">
            Scheduled {absoluteStamp(latest.scheduled_for)}
          </span>
        ) : null}
      </div>
      <p className="ui-label mt-2 text-ink-muted">
        {publishedCount} published of {posts.length} loaded
      </p>
    </div>
  );
}
