"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { Bell } from "lucide-react";

import { usePopover } from "@/components/layout/usePopover";
import { Button, Chip } from "@/components/ui";
import { cn } from "@/components/ui/cn";
import { absoluteStamp, relativeTime } from "@/components/dashboard/format";
import { ApiError, api } from "@/lib/api";

/**
 * The topbar bell and its panel — wired to `GET /api/v1/notifications` (wave U2 backend
 * unblocker; frozen contract in WAVE-PLAN):
 *
 *   200 {"items": [...], "count": N} — newest first, at most 20, window = last 48 h.
 *   item: id (stable string), kind (task_completed | task_failed | awaiting_approval |
 *         run_requeued), title (the task's title — a run_requeued title names the recovered
 *         task id), task_id (int), created_at (ISO-8601).
 *
 * What it deliberately does NOT do:
 * - never fabricates a count: the badge shows a number ONLY from a validated 200 whose `count`
 *   equals its `items` length. While loading there is no badge; on a failed/malformed read the
 *   badge is the dotless dash and the panel says, in `role="alert"`, exactly what failed —
 *   never a fake `0`, never a silent empty popover (`.catch` everywhere names its reason);
 * - no polling (C7): the read happens on mount (for the badge) and again each time the popover
 *   OPENS (the explicit user action, the live popover's own behaviour — `Loading notifications…`
 *   — DOSSIER §E/SURFACE 7), plus the explicit RETRY in the error state.
 *
 * The unread marker is the SAME honest mechanism the activity feed uses: a localStorage
 * last-seen timestamp filled at each visit (`ActivityFeed.tsx` pattern), so "New" means "newer
 * than this device's last visit" — not a read-model the backend does not have. A first visit
 * (no stored value, or storage unavailable) shows no markers rather than inventing them.
 *
 * The panel's items link to the tasks page (`/tasks?task=<id>`) where `task_id` exists — the
 * live popover's `View task` affordance (DOSSIER SURFACE 7), pointed at this repo's real tasks
 * route.
 */

export type NotificationItem = {
  id: string;
  kind: string;
  title: string;
  task_id: number;
  created_at: string;
};

export type NotificationsPayload = { items: NotificationItem[]; count: number };

export type NotificationsState =
  | { status: "loading" }
  | { status: "ready"; items: NotificationItem[]; count: number }
  | { status: "error"; message: string };

/** Local-storage key for the previous visit's timestamp — the bell's only honest "unread"
 *  source, mirroring `ActivityFeed`'s `polsia.dashboard.feed.lastSeenAt`. */
const LAST_SEEN_KEY = "polsia.topbar.notifications.lastSeenAt";

/** The route serves at most this many items in the 48-hour window (`NOTIFICATIONS_LIMIT = 20`,
 *  `backend/app/api/v1/notifications.py`). A served count AT the cap is "up to N", never a
 *  claimed window total — the route sends no total (W2 D6). */
const SERVED_CAP = 20;

function readLastSeenAt(): number | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(LAST_SEEN_KEY);
    if (!raw) return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  } catch {
    // localStorage can be unavailable (privacy mode): the NEW markers switch off; the bell does not.
    return null;
  }
}

function markSeenNow(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LAST_SEEN_KEY, String(Date.now()));
  } catch {
    // Same as above: losing the marker bookkeeping must never affect the bell itself.
  }
}

/** `created_at` newer than the last visit = unread. Mirrors the feed's comparison (an unparseable
 *  timestamp is 0 → not unread; a missing last-seen value → no markers at all). */
export function isUnread(item: NotificationItem, lastSeenAt: number | null): boolean {
  return lastSeenAt !== null && (Date.parse(item.created_at) || 0) > lastSeenAt;
}

type KindLookup = { glyph: string; label: string; chipClassName: string };

/** The FOUR contract kinds, each with its own glyph/label (✓ completed — the live popover's
 *  check glyph, DOSSIER SURFACE 7; ↻ run_requeued — wave U4/B7: the hung-run watchdog line, one
 *  per task a stale-claim sweep requeued). An unknown kind is an open-string situation: rendered
 *  neutral with its raw word, never recast into a known kind. */
const KIND_LOOKUP: Record<string, KindLookup> = {
  task_completed: { glyph: "✓", label: "Completed", chipClassName: "text-green-800" },
  task_failed: { glyph: "✕", label: "Failed", chipClassName: "text-red-800" },
  awaiting_approval: { glyph: "◷", label: "Awaiting approval", chipClassName: "text-amber-800" },
  run_requeued: { glyph: "↻", label: "Run requeued", chipClassName: "text-orange-800" },
};

export function kindLookup(kind: string): KindLookup {
  return (
    KIND_LOOKUP[kind] ?? {
      glyph: "·",
      label: kind.trim() === "" ? "unknown" : kind,
      chipClassName: "text-ink-muted",
    }
  );
}

function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  return e instanceof Error ? e.message : String(e);
}

function isNotificationItem(value: unknown): value is NotificationItem {
  if (value === null || typeof value !== "object") return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.id === "string" &&
    typeof v.kind === "string" &&
    typeof v.title === "string" &&
    typeof v.task_id === "number" &&
    typeof v.created_at === "string"
  );
}

/** Validate the payload BEFORE anything is displayed: a shape the contract does not promise is a
 *  named error, never a silently partial list or an invented count. */
export function parsePayload(payload: unknown): NotificationsPayload | string {
  if (payload === null || typeof payload !== "object") return "the response was not a JSON object";
  const p = payload as { items?: unknown; count?: unknown };
  if (!Array.isArray(p.items)) return "the response did not carry an items array";
  if (typeof p.count !== "number") return "the response did not carry a numeric count";
  if (p.items.length !== p.count) {
    return `the response's count (${p.count}) did not match its items (${p.items.length})`;
  }
  const malformed = p.items.find((item) => !isNotificationItem(item));
  if (malformed !== undefined) return "an item did not match the notifications contract";
  return { items: p.items as NotificationItem[], count: p.count as number };
}

/** The panel body, split from the trigger so both halves are directly testable. */
export function NotificationsPanel({
  state,
  onRetry,
  lastSeenAt,
}: {
  state: NotificationsState;
  onRetry: () => void;
  lastSeenAt: number | null;
}) {
  if (state.status === "loading") {
    // The live popover's own loading voice (DOSSIER §E: `Loading notifications…`).
    return (
      <p data-testid="notifications-loading" className="ui-label px-3 py-3 text-ink-muted">
        Loading notifications…
      </p>
    );
  }

  if (state.status === "error") {
    return (
      <div className="space-y-2 px-3 py-3">
        <p role="alert" data-testid="notifications-error" className="text-sm text-alert">
          Could not load notifications: {state.message}
        </p>
        <Button size="sm" onClick={onRetry} data-testid="notifications-retry">
          Retry
        </Button>
      </div>
    );
  }

  if (state.items.length === 0) {
    return (
      <p data-testid="notifications-empty" className="px-3 py-3 text-sm text-ink-muted">
        No notifications in the last 48 hours. Items appear when a task completes, fails, waits
        for your approval, or a stalled run is requeued.
      </p>
    );
  }

  return (
    <ul data-testid="notifications-list" className="px-3">
      {state.items.map((item) => {
        const kind = kindLookup(item.kind);
        const unread = isUnread(item, lastSeenAt);
        return (
          <li key={item.id} data-kind={item.kind} className="flex items-start gap-2 border-b border-line py-2.5 last:border-b-0">
            <span aria-hidden="true" className="mt-0.5 font-mono text-xs text-ink-muted">
              {kind.glyph}
            </span>
            <div className="min-w-0 flex-1">
              <p className="text-sm font-bold leading-snug text-ink">{item.title}</p>
              <div className="mt-1 flex flex-wrap items-center gap-2">
                <Chip variant="soft" className={kind.chipClassName} data-testid="notification-kind">
                  {kind.label}
                </Chip>
                <span className="ui-label text-ink-muted" title={absoluteStamp(item.created_at)}>
                  {relativeTime(item.created_at)}
                </span>
                {unread ? (
                  <span className="ui-label text-alert" data-testid="notification-new">
                    New
                  </span>
                ) : null}
              </div>
              {Number.isInteger(item.task_id) ? (
                <Link href={`/tasks?task=${item.task_id}`} className="link ui-label mt-1 inline-block">
                  View task
                </Link>
              ) : null}
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/** The bell trigger + its panel. The topbar renders this unconfigured: the control owns its read,
 *  its count semantics and its three states. */
export function NotificationsBell() {
  const { open, toggle, triggerRef, panelRef, panelId, wrapperProps } = usePopover();
  const [state, setState] = useState<NotificationsState>({ status: "loading" });
  // Read ONCE on mount, then immediately book this visit — so NEXT visit's items newer than this
  // moment carry the New mark (the ActivityFeed pattern, deliberately not cleared mid-visit).
  const [lastSeenAt] = useState<number | null>(() => readLastSeenAt());
  useEffect(() => {
    markSeenNow();
  }, []);

  // Sequence guard: the newest issued read wins; an unmount or a newer read makes older
  // resolutions no-ops instead of stale overwrites (no silent swallowed errors — each attempt
  // that IS current sets a named state).
  const requestSeq = useRef(0);
  const load = useCallback(() => {
    const seq = requestSeq.current + 1;
    requestSeq.current = seq;
    setState({ status: "loading" });
    api
      .get<unknown>("/notifications")
      .then((payload) => {
        if (seq !== requestSeq.current) return;
        const parsed = parsePayload(payload);
        setState(typeof parsed === "string" ? { status: "error", message: parsed } : { status: "ready", items: parsed.items, count: parsed.count });
      })
      .catch((e: unknown) => {
        if (seq !== requestSeq.current) return;
        setState({ status: "error", message: errorText(e) });
      });
  }, []);

  // Mount read: the badge's count. Then a fresh read each time the panel OPENS (the live
  // popover's behaviour; an explicit user action, not polling).
  useEffect(() => {
    load();
    return () => {
      requestSeq.current += 1;
    };
  }, [load]);
  useEffect(() => {
    if (open) load();
  }, [open, load]);

  const count = state.status === "ready" ? state.count : null;
  const unreadCount =
    state.status === "ready" ? state.items.filter((item) => isUnread(item, lastSeenAt)).length : 0;
  // Loading: nothing measured yet -> no badge. Ready with 0 -> nothing to badge. Ready with N ->
  // the count, dark normally and the dark-red NEW mark when something is newer than last visit.
  // Error: the dotless dash — never a fabricated number, never a fake zero.
  const badge =
    state.status === "ready"
      ? count !== null && count > 0
        ? { text: String(count), className: unreadCount > 0 ? "chip-alert" : "chip-dark" }
        : null
      : state.status === "error"
        ? { text: "—", className: "chip-soft text-ink-muted" }
        : null;
  const ariaLabel =
    state.status === "ready" && count !== null && count > 0
      ? `Notifications, ${count >= SERVED_CAP ? `up to ${count}` : count} in the last 48 hours`
      : state.status === "error"
        ? "Notifications, unavailable"
        : "Notifications";

  return (
    <div className="relative" {...wrapperProps}>
      <button
        ref={triggerRef}
        type="button"
        className="btn btn-bevel"
        aria-label={ariaLabel}
        aria-expanded={open}
        aria-controls={panelId}
        onClick={toggle}
        data-testid="topbar-notifications"
      >
        <span aria-hidden="true" className="flex items-center">
          <Bell size={13} />
        </span>
        {badge ? (
          <span aria-hidden="true" data-testid="notifications-badge" className={cn("chip", badge.className)}>
            {badge.text}
          </span>
        ) : null}
      </button>

      {open ? (
        <div ref={panelRef} id={panelId} data-testid="notifications-panel" className="panel-pop absolute right-0 z-50 mt-1 w-80">
          <p className="ui-label border-b border-line px-3 py-2 text-ink">Notifications</p>
          <NotificationsPanel state={state} onRetry={load} lastSeenAt={lastSeenAt} />
        </div>
      ) : null}
    </div>
  );
}
