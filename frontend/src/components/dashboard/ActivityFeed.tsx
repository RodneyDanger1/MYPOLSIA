"use client";
import { useEffect, useMemo, useState } from "react";
import { useActivityFeed, type FeedStatus } from "@/hooks/useActivityFeed";
import type { ActivityEvent } from "@/lib/api";

// Lookup tables, not a closed union: `ActivityEvent.level` is an open string on the wire
// (`app/schemas/report.py` types it `str`), so a level this build does not know must still render —
// falling through to the gray dot/text below — rather than being treated as impossible.
// Light-theme palette per the live captures (white canvas, gray mono meta, dark-red unread mark).
const LEVEL_DOT: Record<string, string> = {
  success: "bg-green-600",
  error: "bg-red-700",
  warning: "bg-amber-600",
  info: "bg-blue-600",
};

const LEVEL_LABEL: Record<string, string> = {
  success: "text-green-700",
  error: "text-red-700",
  warning: "text-amber-700",
  info: "text-blue-700",
};

const FALLBACK_DOT = "bg-gray-400"; // unknown level -> still rendered, gray
const FALLBACK_LABEL = "text-gray-500";

/** Local-storage key for the previous visit's timestamp — the feed's only honest "unread" source:
 *  the backend has no read-model, so "unread" is defined as `created_at` newer than the last time
 *  this device visited the dashboard. No stored value (first visit / storage unavailable) = no
 *  markers, rather than inventing them. */
const LAST_SEEN_KEY = "polsia.dashboard.feed.lastSeenAt";

function readLastSeenAt(): number | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(LAST_SEEN_KEY);
    if (!raw) return null;
    const parsed = Number(raw);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  } catch {
    // localStorage can be unavailable (privacy mode): the NEW markers switch off; the feed does not.
    return null;
  }
}

function markSeenNow(): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(LAST_SEEN_KEY, String(Date.now()));
  } catch {
    // Same as above: losing the marker bookkeeping must never affect the feed itself.
  }
}

/** The live feed grammar shows times as `2:36 PM` (mono). Null/invalid timestamps keep the
 *  app-wide honest placeholder — never `Invalid Date` or 1/1/1970. */
function clockText(iso: string | null): string {
  if (!iso) return "date unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "date unknown";
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

function dayKeyOf(iso: string | null): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "unknown";
  return `${d.getFullYear()}-${d.getMonth()}-${d.getDate()}`;
}

/** Divider labels observed live: `TODAY` and `SUNDAY, OCTOBER 11`. */
function dayLabelOf(iso: string | null, now: Date): string {
  if (!iso) return "DATE UNKNOWN";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "DATE UNKNOWN";
  const startOfDay = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime();
  const diffDays = Math.round((startOfDay(now) - startOfDay(d)) / 86_400_000);
  if (diffDays === 0) return "TODAY";
  if (diffDays === 1) return "YESTERDAY";
  return d
    .toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" })
    .toUpperCase();
}

const STATUS_PILL: Record<FeedStatus, { dot: string; textClass: string }> = {
  connecting: { dot: "bg-[#8b8b8b]", textClass: "text-[#8b8b8b]" },
  live: { dot: "bg-green-600", textClass: "text-green-700" },
  reconnecting: { dot: "bg-amber-500", textClass: "text-amber-700" },
  offline: { dot: "bg-[#B00020]", textClass: "text-[#B00020]" },
};

function pillText(status: FeedStatus, attempts: number): string {
  switch (status) {
    case "connecting":
      return "CONNECTING…";
    case "live":
      return "LIVE";
    case "reconnecting":
      return attempts > 0 ? `RECONNECTING (ATTEMPT ${attempts})` : "RECONNECTING";
    case "offline":
      return "OFFLINE";
  }
}

type DayGroup = { key: string; label: string; items: ActivityEvent[] };

export function ActivityFeed() {
  const { events, status, error, attempts, total } = useActivityFeed();

  // Read ONCE on mount, then immediately book this visit — so NEXT visit's events newer than this
  // moment carry the NEW mark. (Deliberately not cleared during the visit: an event that arrives
  // while you watch is still newer than your last visit.)
  const [lastSeenAt] = useState<number | null>(() => readLastSeenAt());
  useEffect(() => {
    markSeenNow();
  }, []);

  const isUnread = (e: ActivityEvent): boolean =>
    lastSeenAt !== null && e.created_at !== null && (Date.parse(e.created_at) || 0) > lastSeenAt;

  const groups = useMemo<DayGroup[]>(() => {
    const now = new Date();
    const byKey = new Map<string, DayGroup>();
    const out: DayGroup[] = [];
    for (const e of events) {
      const key = dayKeyOf(e.created_at);
      let group = byKey.get(key);
      if (!group) {
        group = { key, label: dayLabelOf(e.created_at, now), items: [] };
        byKey.set(key, group);
        out.push(group);
      }
      group.items.push(e);
    }
    return out;
  }, [events]);

  const pill = STATUS_PILL[status];

  return (
    <section
      aria-label="Live activity"
      data-testid="activity-feed"
      data-status={status}
      className="flex h-full flex-col rounded-sm border border-[#E3E3E3] bg-white"
    >
      <div className="flex items-center justify-between border-b border-[#E3E3E3] px-3 py-2.5">
        <div className="flex items-baseline gap-2">
          <h2 className="font-serif text-base text-[#111]">Live Activity</h2>
          {/* V23/L4 (COMPLETENESS #6): the server's OWN declared total for the snapshot read
              (`X-Polsia-Total` on the same response as the rows). Absent header -> no count at
              all: the rows on hand are a page, never a total. */}
          {typeof total === "number" && (
            <span
              className="font-mono text-[10px] text-[#8b8b8b]"
              data-testid="activity-feed-total"
            >
              {total === 1 ? "1 event" : `${total} events`}
            </span>
          )}
        </div>
        <span
          role="status"
          className={`flex items-center gap-1.5 font-mono text-[10px] uppercase tracking-wider ${pill.textClass}`}
        >
          <span aria-hidden="true" className={`h-2 w-2 rounded-full ${pill.dot}`} />
          {pillText(status, attempts)}
        </span>
      </div>

      <div className="flex-1 overflow-y-auto px-3 py-2">
        {error && (
          <p
            role="alert"
            className="mb-2 rounded-sm border border-red-200 bg-red-50 px-2 py-1.5 font-mono text-[10px] leading-relaxed text-red-800"
          >
            {error}
          </p>
        )}

        {/* Empty states are honest per transport status (Q-07): the "nothing here yet" copy is a
            claim that requires a LIVE socket first. */}
        {events.length === 0 && status === "live" && (
          <p className="font-serif text-xs text-[#6e6e6e]">No activity yet — agents will post here.</p>
        )}
        {events.length === 0 && status === "connecting" && (
          <p className="font-serif text-xs text-[#6e6e6e]">Connecting to the activity stream…</p>
        )}
        {events.length === 0 && status === "reconnecting" && (
          <p className="font-serif text-xs text-[#6e6e6e]">
            Waiting for the activity stream to come back…
          </p>
        )}
        {events.length === 0 && status === "offline" && (
          <p className="font-serif text-xs text-[#6e6e6e]">
            The activity stream is offline. The reason above is the real one; retrying in the background.
          </p>
        )}

        {groups.map((group, gi) => (
          <div key={`${group.key}-${gi}`}>
            <div role="separator" className="flex items-center gap-2 py-2">
              <span aria-hidden="true" className="h-px flex-1 bg-[#dcdcdc]" />
              <span className="font-mono text-[10px] uppercase tracking-widest text-[#6e6e6e]">
                {group.label}
              </span>
              {group.items.some(isUnread) && (
                <span className="font-mono text-[10px] tracking-widest text-[#B00020]">NEW</span>
              )}
              <span aria-hidden="true" className="h-px flex-1 bg-[#dcdcdc]" />
            </div>

            <ul className="space-y-2 pb-2">
              {group.items.map((e, i) => (
                <li
                  key={String(e.id ?? `${group.key}-${i}`)}
                  className="rounded-sm border border-[#e3e3e3] bg-white px-3 py-2.5"
                >
                  <div className="flex items-baseline justify-between gap-2">
                    <span
                      className={`font-mono text-[10px] uppercase tracking-wider ${LEVEL_LABEL[e.level] ?? FALLBACK_LABEL}`}
                    >
                      {e.agent_type}
                    </span>
                    {isUnread(e) && (
                      <span className="font-mono text-[10px] tracking-widest text-[#B00020]">NEW</span>
                    )}
                  </div>
                  <p className="mt-1 font-serif text-sm leading-snug text-[#111]">{e.summary}</p>
                  <div className="mt-1.5 flex items-center gap-2">
                    <span
                      aria-hidden="true"
                      className={`h-1.5 w-1.5 rounded-full ${LEVEL_DOT[e.level] ?? FALLBACK_DOT}`}
                    />
                    {/* `created_at` is nullable on the wire (`ActivityEventResponse.created_at`): the
                        helper returns the app-wide "date unknown" placeholder, never "Invalid Date". */}
                    <time className="font-mono text-[10px] text-[#8b8b8b]" dateTime={e.created_at ?? undefined}>
                      {clockText(e.created_at)}
                    </time>
                  </div>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </section>
  );
}
