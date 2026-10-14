import type { HTMLAttributes } from "react";

import { Card, Chip, type ChipVariant } from "@/components/ui";
import { cn } from "@/components/ui/cn";
import { absoluteStamp } from "@/components/dashboard/format";

/**
 * One post row of `GET /social/posts` (`SocialPostResponse`, `app/schemas/social.py`) and its
 * card renderer — the live product's tweet-draft card grammar (white, 1px dark border, serif
 * body, mono-caps meta row; agent1-live-chrome/DOSSIER.md §1.5; agent2-code/CAPTURES.md
 * cap-10/cap-11/cap-12), applied to real wire data.
 *
 * The fields the old page-local type dropped are carried here because the wire serves them:
 * `platform`, `tweet_id`, `scheduled_for`, `created_at` (`schemas/social.py:17-25`). Every one is
 * nullable server-side (`0001`'s `CREATE TABLE social_posts` has `server_default`s but no
 * `NOT NULL`), so every null renders the app-wide honest placeholder — never a deleted cell,
 * never an empty chip, never `Invalid Date`.
 *
 * Deliberately presentational and hook-free (usable from the dashboard widget's client tree and
 * from server components), and deliberately action-free: the live draft box's `TWEET` /
 * `Delete tweet` / `CONNECT X ACCOUNT` controls have no endpoint in this backend
 * (route inventory: `GET /social/posts` only — `backend/app/api/v1/social.py`), so they are not
 * rendered at all (refused by omission, outreach-style; see the page for the named reason).
 */
export type SocialPost = {
  id: number;
  content: string;
  /** Open string on the wire (`str | None`): `published`/`draft` are what today's writers
   *  produce, anything else renders neutral with its raw word, null renders gray `unknown`. */
  status: string | null;
  platform: string | null;
  tweet_id: string | null;
  scheduled_for: string | null;
  published_at: string | null;
  created_at: string | null;
  /** `dict[str, Any] | None`: per-counter values are as untyped as the dashboard's `kpis`. */
  engagement: Record<string, unknown> | null;
};

type ChipTreatment = { variant: ChipVariant; className: string };

/** Named statuses the writers produce today — each with its own treatment. `published` is the
 *  only value `social_service.py:124` writes; `draft` is the column's `server_default` and the
 *  agent's draft semantics (`app/agents/social_media/agent.py`). */
const NAMED_STATUS_TREATMENT: Record<string, ChipTreatment> = {
  published: { variant: "soft", className: "text-green-800" },
  draft: { variant: "soft", className: "text-blue-800" },
};

/** A null status is "not recorded" — a gray, dashed chip reading `unknown`, distinct from every
 *  named treatment (Q-25: no colour may be shared with a named status). */
const UNKNOWN_TREATMENT: ChipTreatment = {
  variant: "soft",
  className: "border-dashed border-line-strong bg-transparent text-ink-muted",
};

/** Any other non-null value is a real word from an open-string column — rendered neutral (white
 *  fill, dark hairline, muted ink) carrying its raw word, never recast into a known status. */
const OTHER_TREATMENT: ChipTreatment = {
  variant: "soft",
  className: "border-line-strong bg-surface text-ink-muted",
};

/** The status chip, with the lookup + gray fallback Q-25 asks for (the old binary colour rule
 *  would have dressed a future `failed`/`scheduled` value as "warning"). */
export function StatusChip({ status, className, ...props }: { status: string | null } & HTMLAttributes<HTMLSpanElement>) {
  if (status === null) {
    return (
      <Chip
        variant={UNKNOWN_TREATMENT.variant}
        data-status-tone="unmeasured"
        className={cn(UNKNOWN_TREATMENT.className, className)}
        {...props}
      >
        unknown
      </Chip>
    );
  }
  const named = NAMED_STATUS_TREATMENT[status];
  if (named) {
    return (
      <Chip variant={named.variant} data-status-tone={status} className={cn(named.className, className)} {...props}>
        {status}
      </Chip>
    );
  }
  return (
    <Chip variant={OTHER_TREATMENT.variant} data-status-tone="other" className={cn(OTHER_TREATMENT.className, className)} {...props}>
      {status}
    </Chip>
  );
}

export type TimestampCell = { label: string | null; stamp: string };

/**
 * The card's timestamp cell (Q-25; DOSSIER §1.5's `2:30 PM · OCT 11, 2026` fixed format via
 * `absoluteStamp`, never bare `toLocaleString`). Priority: `published_at` > `scheduled_for` >
 * `created_at`; all null renders the `—` placeholder — the cell is never deleted.
 */
export function timestampCell(post: Pick<SocialPost, "published_at" | "scheduled_for" | "created_at">): TimestampCell {
  if (post.published_at) return { label: "Published", stamp: absoluteStamp(post.published_at) };
  if (post.scheduled_for) return { label: "Scheduled", stamp: absoluteStamp(post.scheduled_for) };
  if (post.created_at) return { label: "Created", stamp: absoluteStamp(post.created_at) };
  return { label: null, stamp: "—" };
}

/**
 * Q-22 fix: a displayable counter is shown as it is (`likes: 11`, never `likes: 11.00`); a null
 * is the em dash; anything that is not a single displayable value (an object/array — exactly what
 * `String(value)` turned into `[object Object]`) renders the em dash with the real payload in the
 * `title`, so the fix neither prints garbage nor hides data.
 */
export function engagementValue(value: unknown): { text: string; title?: string } {
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return { text: String(value) };
  }
  if (value === null || value === undefined) return { text: "—" };
  let title: string;
  try {
    title = JSON.stringify(value) ?? String(value);
  } catch {
    // A circular/non-serializable payload: name that in the title rather than pretending.
    title = "value could not be serialized for display";
  }
  return { text: "—", title };
}

export function SocialPostCard({ post }: { post: SocialPost }) {
  const ts = timestampCell(post);
  return (
    <Card data-testid="social-post" className="border-line-strong">
      <p className="whitespace-pre-wrap text-sm leading-relaxed text-ink">{post.content}</p>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <Chip variant="soft" data-testid="post-platform" className="text-ink-muted">
          {post.platform ?? "unknown"}
        </Chip>
        <StatusChip status={post.status} data-testid="post-status" />
        {/* Q-25: the removed cell came back — a labelled stamp, or the em dash when the row
            carries no timestamp at all (never an empty meta slot, never "Invalid Date"). */}
        <span
          data-testid="post-timestamp"
          className="ui-label text-ink-muted"
          title={ts.label === null ? "No timestamp recorded on this row" : undefined}
        >
          {ts.label === null ? ts.stamp : `${ts.label} ${ts.stamp}`}
        </span>
        {post.tweet_id ? <span className="ui-label text-ink-muted">X ID {post.tweet_id}</span> : null}
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-line pt-2">
        {/* A null engagement is "no counters recorded", not an error: the em dash is the same
            "not measured" placeholder the finance cards use. */}
        {post.engagement === null ? (
          <span className="ui-label text-ink-muted">engagement: —</span>
        ) : (
          Object.entries(post.engagement).map(([key, value]) => {
            const display = engagementValue(value);
            return (
              <span key={key} className="ui-label text-ink-muted" title={display.title}>
                {key}: {display.text}
              </span>
            );
          })
        )}
      </div>
    </Card>
  );
}
