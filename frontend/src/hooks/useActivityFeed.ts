"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { API_KEY, api, type ActivityEvent } from "@/lib/api";

/**
 * The feed's transport state as ONE honest value (audit Q-07/Q-30):
 *  - `connecting`   — the first handshake is in flight; nothing has failed yet.
 *  - `live`         — the socket is OPEN right now. Nothing else may claim this.
 *  - `reconnecting` — a failure happened; the next attempt is scheduled or in flight
 *                     (`attempts` carries the count of consecutive failures).
 *  - `offline`      — ≥ OFFLINE_AFTER_FAILURES consecutive failures. Retries CONTINUE at the capped
 *                     backoff so recovery is automatic; the label says disconnected, not "gave up".
 */
export type FeedStatus = "connecting" | "live" | "reconnecting" | "offline";

/** Q-30: a handshake that never completes is closed after this bound and retried via the backoff. */
export const CONNECT_TIMEOUT_MS = 10_000;
/** The backoff counter resets only after a connection has PROVEN stable this long. Resetting it on
 *  every open meant a server that accepts then immediately drops reproduced a reconnect (and
 *  snapshot-refetch) storm at near-minimal delay forever — the observed live defect. */
export const STABLE_RESET_MS = 5_000;
/** Snapshot fetches are coalesced: at most one in flight, and no two closer than this window, so
 *  duplicate open/close cycles cannot pile up REST fetches. */
export const SNAPSHOT_MIN_INTERVAL_MS = 5_000;
/** Consecutive failures after which the status reads `offline` (retries continue regardless). */
export const OFFLINE_AFTER_FAILURES = 5;

const BASE_RETRY_MS = 1_000;
const MAX_RETRY_MS = 30_000;

/**
 * Exponential backoff with full jitter, capped (kept from the original hook: a backend restart must
 * not produce a synchronised reconnect stampede from every open dashboard; the jitter spreads the
 * retries and the cap keeps recovery bounded).
 */
export function backoffDelay(attempt: number, random: () => number = Math.random): number {
  const ceiling = Math.min(MAX_RETRY_MS, BASE_RETRY_MS * 2 ** Math.min(attempt, 6));
  return Math.floor(ceiling / 2 + random() * (ceiling / 2));
}

/**
 * Q-02: the WS base must never resolve to a RELATIVE URL, because `new WebSocket("<relative>")`
 * throws outside the browser and the throw used to reach the route error boundary. The documented
 * deployment default is the EMPTY STRING (Dockerfile ARG / docker-compose / next.config.js), which
 * `?? "ws://localhost"` does not catch. A non-empty env value wins; otherwise derive from the
 * serving origin — so the socket follows whatever host the page was actually opened with — and fall
 * back to "" only in a non-browser context where no origin exists.
 */
export function resolveWsBase(
  envUrl: string | undefined,
  loc: { protocol: string; host: string } | null,
): string {
  if (envUrl) return envUrl;
  if (loc && loc.host) return `${loc.protocol === "https:" ? "wss:" : "ws:"}//${loc.host}`;
  return "";
}

function wsBase(): string {
  return resolveWsBase(
    process.env.NEXT_PUBLIC_WS_URL,
    typeof window !== "undefined" ? window.location : null,
  );
}

function socketUrl(): string {
  // Auth parity with every REST call (audit L1-01/L3-08): browsers cannot set WebSocket handshake
  // headers, so the key rides the query string — the same NEXT_PUBLIC_API_KEY the REST client
  // already ships to the browser (no new exposure).
  return `${wsBase()}/ws/activity?key=${encodeURIComponent(API_KEY)}`;
}

/** The URL used in user-visible error strings. Deliberately WITHOUT the `?key=` query parameter:
 *  the key is a credential and must not surface in a message (or a screenshot of one). */
function displaySocketUrl(): string {
  return `${wsBase()}/ws/activity`;
}

/** The feed's error sentence for any thrown value. A refusal keeps its NAMED `display` (code +
 *  message + how_to_fix) — the V17 L4-02 contract the rest of the dashboard already follows: a
 *  403 from the snapshot read must not degrade to a code-less sentence. Any other error keeps its
 *  own message, exactly as before. Read structurally (no `instanceof ApiError`) so suite mocks
 *  that replace `@/lib/api` with `{api}` cannot make this throw. */
function message(e: unknown): string {
  const display = (e as { display?: unknown } | null)?.display;
  if (typeof display === "string" && display !== "") return display;
  return e instanceof Error ? e.message : String(e);
}

/** The declared FULL match count from ONE response's `X-Polsia-Total` header (V23/L4,
 *  COMPLETENESS #6), or null when the header is absent or does not carry a real count.
 *
 *  NEVER inferred from the rows on hand: a page shows at most `limit` rows, so their count is a
 *  page length, not a total.  An absent header -> null -> the feed prints no count at all. */
export function declaredTotal(raw: string | null): number | null {
  if (raw === null || raw.trim() === "") return null;
  const value = Number(raw);
  return Number.isInteger(value) && value >= 0 ? value : null;
}

/** Close description for a CloseEvent that may be missing fields (close code 1006 = abnormal). */
function closeText(event: CloseEvent | undefined): string {
  const code = event?.code;
  const reason = event?.reason;
  if (reason) return `connection closed (${code ?? "?"}: ${reason})`;
  if (code) return `connection closed (code ${code})`;
  return "connection closed";
}

export type UseActivityFeedOptions = {
  /** Pull recent history over REST on connect, so the feed is not empty until the next event. */
  snapshot?: boolean;
  /** How many rows that snapshot requests. */
  snapshotLimit?: number;
};

export function useActivityFeed(maxEvents = 100, options: UseActivityFeedOptions = {}) {
  const { snapshot = true, snapshotLimit = 50 } = options;
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [status, setStatus] = useState<FeedStatus>("connecting");
  const [error, setError] = useState<string | null>(null);
  const [attempts, setAttempts] = useState(0);
  /** The server's own full match count from the LAST successful snapshot's `X-Polsia-Total`
   *  (V23/L4).  null = the response declared none; the header region then prints no count. */
  const [total, setTotal] = useState<number | null>(null);

  const seen = useRef<Set<string>>(new Set());
  const mounted = useRef(true);
  const closedByUs = useRef(false);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const connectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const stableTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const snapshotTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  /** Merge incoming events: drop duplicates by id, newest first, cap at maxEvents. */
  const merge = useCallback(
    (incoming: ActivityEvent[]) => {
      if (!Array.isArray(incoming) || incoming.length === 0) return;
      // Q-10: dedupe OUTSIDE the state updater. React 18 StrictMode invokes updaters twice and keeps
      // the second result; the old in-updater `seen` mutation dropped every event on the second pass.
      const fresh: ActivityEvent[] = [];
      for (const e of incoming) {
        if (!e) continue;
        const key = String(e.id ?? `${e.created_at}-${e.summary}`);
        if (seen.current.has(key)) continue;
        seen.current.add(key);
        fresh.push(e);
      }
      if (fresh.length === 0) return;
      // Bound the dedupe set: the feed itself is capped at maxEvents, so remembering twice that is
      // strictly more than can ever render; the oldest keys (Sets iterate in insertion order) fall
      // out first.
      if (seen.current.size > maxEvents * 2) {
        seen.current = new Set(Array.from(seen.current).slice(-maxEvents * 2));
      }
      setEvents((prev) => {
        const next = [...fresh, ...prev];
        next.sort((a, b) => {
          const ta = Date.parse(a?.created_at ?? "") || 0;
          const tb = Date.parse(b?.created_at ?? "") || 0;
          return tb - ta || (b?.id ?? 0) - (a?.id ?? 0);
        });
        return next.slice(0, maxEvents);
      });
    },
    [maxEvents],
  );

  // The mount-lifetime engine below reads the LATEST merge/options at event time through this ref,
  // so no re-render can ever need to restart the socket. (The old effect depended on a `useCallback`
  // chain — `[connect]` — and any identity churn closed/reopened the socket, which is exactly the
  // reconnect-loop class observed live.)
  const engineOptions = useRef({ merge, snapshot, snapshotLimit });
  useEffect(() => {
    engineOptions.current = { merge, snapshot, snapshotLimit };
  });

  useEffect(() => {
    mounted.current = true;
    closedByUs.current = false;

    let generation = 0; // bumped per connect() and on unmount — stale handlers are inert
    let currentSocket: WebSocket | null = null;
    let consecutiveFailures = 0; // for the honest status label ("attempt N")
    let backoffAttempt = 0; // for the retry delay; reset only after STABLE_RESET_MS of live
    let snapshotInFlight = false;
    let snapshotQueued = false;
    let lastSnapshotAt = 0;

    const clearTimer = (t: { current: ReturnType<typeof setTimeout> | null }) => {
      if (t.current) {
        clearTimeout(t.current);
        t.current = null;
      }
    };

    const runSnapshot = async () => {
      snapshotInFlight = true;
      lastSnapshotAt = Date.now();
      try {
        const { snapshotLimit: limit, merge: mergeNow } = engineOptions.current;
        // V23/L4: the declared total rides the SAME response as the rows (the shared onHeaders
        // hook), so the label can never describe a different read. A local capture is committed
        // only with a parsed body: a malformed snapshot fails the read and keeps the old total.
        const captured: { total: number | null } = { total: null };
        const recent = await api.get<ActivityEvent[]>(`/dashboard/activity?limit=${limit}`, {
          onHeaders: (headers) => {
            captured.total = declaredTotal(headers.get("X-Polsia-Total"));
          },
        });
        if (mounted.current && Array.isArray(recent)) {
          setTotal(captured.total);
          mergeNow(recent);
        }
      } catch (e) {
        // The live socket is the primary channel: a failed snapshot must not break the feed — but it
        // must not be invisible either (audit Q-07), so it surfaces through `error` like any failure.
        if (mounted.current) {
          // The count is only ever THIS read's evidence: with the read failed, a number left on
          // screen would be a guess about rows nobody measured.
          setTotal(null);
          setError(`activity snapshot failed: ${message(e)}`);
        }
      } finally {
        snapshotInFlight = false;
        if (snapshotQueued) {
          snapshotQueued = false;
          scheduleSnapshot();
        }
      }
    };

    const scheduleSnapshot = () => {
      if (!engineOptions.current.snapshot) return;
      if (snapshotInFlight) {
        // Coalesce: one queued follow-up at most, however many opens flap past while in flight.
        snapshotQueued = true;
        return;
      }
      const dueIn = lastSnapshotAt + SNAPSHOT_MIN_INTERVAL_MS - Date.now();
      if (lastSnapshotAt > 0 && dueIn > 0) {
        // Coalesce across time as well: opens inside the window schedule ONE trailing fetch.
        if (!snapshotTimer.current) {
          snapshotTimer.current = setTimeout(() => {
            snapshotTimer.current = null;
            void runSnapshot();
          }, dueIn);
        }
        return;
      }
      void runSnapshot();
    };

    function reportFailure(why: string): void {
      if (!mounted.current || closedByUs.current) return;
      consecutiveFailures += 1;
      backoffAttempt += 1;
      setAttempts(consecutiveFailures);
      setError(why);
      setStatus(consecutiveFailures >= OFFLINE_AFTER_FAILURES ? "offline" : "reconnecting");
      clearTimer(reconnectTimer); // never two pending retries
      reconnectTimer.current = setTimeout(() => {
        reconnectTimer.current = null;
        connect();
      }, backoffDelay(backoffAttempt - 1));
    }

    function connect(): void {
      if (!mounted.current || closedByUs.current) return;
      if (
        currentSocket &&
        (currentSocket.readyState === WebSocket.OPEN ||
          currentSocket.readyState === WebSocket.CONNECTING)
      ) {
        return;
      }

      const myGeneration = ++generation;
      const url = socketUrl();

      let socket: WebSocket;
      try {
        socket = new WebSocket(url);
      } catch (e) {
        // Q-02: a relative/misconfigured URL throws synchronously. Caught here so the dashboard can
        // never die on it; the honest reason goes to `error` and the retry is scheduled. The message
        // carries the URL WITHOUT the key.
        reportFailure(`could not open a WebSocket to ${displaySocketUrl()}: ${message(e)}`);
        return;
      }
      currentSocket = socket;

      let connectTimedOut = false;

      // Q-30: bound the CONNECTING phase. A handshake that never completes is closed, which routes
      // through onclose (flagged via `connectTimedOut`) into the normal backoff retry.
      connectTimer.current = setTimeout(() => {
        if (myGeneration !== generation) return;
        if (socket.readyState === WebSocket.CONNECTING) {
          connectTimedOut = true;
          try {
            socket.close();
          } catch {
            /* already closed */
          }
        }
      }, CONNECT_TIMEOUT_MS);

      socket.onopen = () => {
        if (myGeneration !== generation) {
          // A stale socket that opened after its engine moved on: close it, write nothing.
          try {
            socket.close();
          } catch {
            /* already closed */
          }
          return;
        }
        if (!mounted.current || closedByUs.current) {
          try {
            socket.close();
          } catch {
            /* already closed */
          }
          return;
        }
        clearTimer(connectTimer);
        clearTimer(stableTimer);
        consecutiveFailures = 0;
        setAttempts(0);
        setStatus("live");
        setError(null);
        stableTimer.current = setTimeout(() => {
          if (myGeneration === generation) backoffAttempt = 0;
        }, STABLE_RESET_MS);
        scheduleSnapshot();
      };

      socket.onmessage = (e) => {
        if (myGeneration !== generation) return;
        try {
          const parsed = JSON.parse(e.data as string) as ActivityEvent | ActivityEvent[];
          if (mounted.current) engineOptions.current.merge(Array.isArray(parsed) ? parsed : [parsed]);
        } catch {
          // A malformed frame is ignored on purpose: one bad message must not kill the socket.
        }
      };

      socket.onclose = (event: CloseEvent | undefined) => {
        // A stale socket must not write state, nor schedule a retry (duplicate close cycles used to
        // pile up reconnect timers and snapshot fetches this way).
        if (myGeneration !== generation) return;
        clearTimer(connectTimer);
        clearTimer(stableTimer);
        if (currentSocket === socket) currentSocket = null;
        if (!mounted.current || closedByUs.current) return;
        reportFailure(
          connectTimedOut
            ? `socket to ${displaySocketUrl()} did not open within ${CONNECT_TIMEOUT_MS}ms`
            : closeText(event),
        );
      };

      socket.onerror = () => {
        // The browser fires close after a failed socket; closing routes through the single onclose
        // retry path instead of double-scheduling one.
        try {
          socket.close();
        } catch {
          /* already closed */
        }
      };
    }

    connect();

    return () => {
      closedByUs.current = true;
      mounted.current = false;
      generation += 1; // everything in flight from this engine is now stale
      clearTimer(connectTimer);
      clearTimer(reconnectTimer);
      clearTimer(stableTimer);
      clearTimer(snapshotTimer);
      try {
        currentSocket?.close();
      } catch {
        /* already closed */
      }
      currentSocket = null;
    };
    // The engine lives for the MOUNT's lifetime: nothing about a re-render can restart the socket.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { events, connected: status === "live", status, error, attempts, total };
}
