"use client";
import { useCallback, useSyncExternalStore } from "react";
import { api, type ActivityEvent } from "@/lib/api";

/**
 * The activity feed's transport — ONE engine per browser page (F-03, wave 2).
 *
 * WHAT THIS FILE IS.  `useActivityFeed` is the only consumer-facing surface, but the socket,
 * the ticket mint, the reconnect ladder and the REST snapshot live in a MODULE-LEVEL engine
 * (`ActivityTransport`).  Every mounted consumer subscribes to that one engine with
 * `useSyncExternalStore`; the hook is a view.
 *
 * WHY (the measured defect).  The root layout mounts `ActivityStrip` and `/dashboard` mounts
 * `ActivityFeed` — two consumers of this hook on one page — and each consumer ran its OWN copy of
 * the connect effect: two tickets, two `new WebSocket`s, two reconnect ladders.  On the live box
 * that produced 17 handshakes in 15 s and, once the backend's 4-per-proxy cap was hit, THREE
 * status words on ONE screen (LIVE / LIVE / RECONNECTING) — a green LIVE badge beside a refused
 * handshake, because each transport reported its own truth.  Snapshot reads were deduped, the
 * TRANSPORT was not.
 *
 * The fix is the wave-1 design (R-F03-1): one engine, `useSyncExternalStore` subscribers.
 *   - one ticket mint + one socket per page, closed when the LAST consumer unmounts;
 *   - ONE status, one error and one attempt count, so two views cannot disagree;
 *   - the snapshot is read once per open (as before) — the per-page REST dedupe is no longer
 *     needed because there is only one reader.
 *
 * Status vocabulary is unchanged and still honest (audit Q-07/Q-30):
 *  - `connecting`   — the first handshake is in flight; nothing has failed yet.
 *  - `live`         — the socket is OPEN right now. Nothing else may claim this.
 *  - `reconnecting` — a failure happened; the next attempt is scheduled or in flight
 *                     (`attempts` carries the count of consecutive failures).
 *  - `offline`      — ≥ OFFLINE_AFTER_FAILURES consecutive failures. Retries CONTINUE at the capped
 *                     backoff so recovery is automatic; the label says disconnected, not "gave up".
 *
 * Per-instance options: `maxEvents` slices each consumer's view (the engine buffers the largest
 * requested window); `snapshot`/`snapshotLimit` are fixed by the FIRST subscriber, because the
 * page has one transport and a second view cannot renegotiate the shared read.  No new request is
 * issued by subscribing.
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

/** F-46: the key never rides the URL. Mint a short-lived ticket over the authenticated REST
 *  surface (`POST /api/v1/ws/ticket`, key in the header) and hand THAT to the handshake, which
 *  accepts `?ticket=`. A ticket is not a credential: it expires in seconds and reveals nothing
 *  about the key (HMAC). Returns the ticket string or throws with the server's named refusal. */
export async function mintSocketTicket(): Promise<string> {
  const res = await api.post<{ ticket?: unknown }>("/ws/ticket", {});
  const ticket = res?.ticket;
  if (typeof ticket !== "string" || ticket === "") {
    throw new Error("the ticket endpoint answered without a ticket");
  }
  return ticket;
}

function socketUrl(ticket: string): string {
  return `${wsBase()}/ws/activity?ticket=${encodeURIComponent(ticket)}`;
}

/** The URL used in user-visible error strings. Deliberately WITHOUT any credential parameter:
 *  a ticket or a key must not surface in a message (or a screenshot of one). */
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

/** The declared FULL match count from ONE response's `X-Meridian-Total` header (V23/L4,
 *  COMPLETENESS #6), or null when the header is absent or does not carry a real count.
 *
 *  NEVER inferred from the rows on hand: a page shows at most `limit` rows, so their count is a
 *  page length, not a total.  An absent header -> null -> the feed prints no count at all. */
export function declaredTotal(raw: string | null): number | null {
  if (raw === null || raw.trim() === "") return null;
  const value = Number(raw);
  return Number.isInteger(value) && value >= 0 ? value : null;
}

/** The shared shape of one activity snapshot read. `ok` says the body was a list; a malformed body
 *  keeps its old total (the pre-dedupe behaviour) instead of committing a count nobody measured. */
type ActivitySnapshot = { rows: ActivityEvent[]; total: number | null; ok: boolean };

function readActivitySnapshot(limit: number): Promise<ActivitySnapshot> {
  const path = `/dashboard/activity?limit=${limit}`;
  // V23/L4: the declared total rides the SAME response as the rows (the shared onHeaders hook), so
  // the label can never describe a different read.
  const captured: { total: number | null } = { total: null };
  return api
    .get<ActivityEvent[]>(path, {
      onHeaders: (headers) => {
        captured.total = declaredTotal(headers.get("X-Meridian-Total"));
      },
    })
    .then((body) => ({
      rows: Array.isArray(body) ? body : [],
      total: captured.total,
      ok: Array.isArray(body),
    }));
}

/** Close description for a CloseEvent that may be missing fields (close code 1006 = abnormal). */
function closeText(event: CloseEvent | undefined): string {
  const code = event?.code;
  const reason = event?.reason;
  if (reason) return `connection closed (${code ?? "?"}: ${reason})`;
  if (code) return `connection closed (code ${code})`;
  return "connection closed";
}

/** F-41: is this tab hidden right now? False outside a browser (a non-DOM caller is never hidden
 *  by definition, so it keeps the old always-retry behaviour). */
function tabHidden(): boolean {
  return typeof document !== "undefined" && document.visibilityState === "hidden";
}

export type UseActivityFeedOptions = {
  /** Pull recent history over REST on connect, so the feed is not empty until the next event. */
  snapshot?: boolean;
  /** How many rows that snapshot requests. */
  snapshotLimit?: number;
};

/** The immutable state one render reads. Identity changes IFF something changed. */
type FeedState = {
  events: ActivityEvent[];
  status: FeedStatus;
  error: string | null;
  attempts: number;
  total: number | null;
};

const INITIAL_STATE: FeedState = {
  events: [],
  status: "connecting",
  error: null,
  attempts: 0,
  total: null,
};

type TransportOptions = { maxEvents: number; snapshot: boolean; snapshotLimit: number };

/** One-page transport: ONE ticket mint, ONE socket, ONE status, N subscribers. */
class ActivityTransport {
  private state: FeedState = INITIAL_STATE;
  private readonly listeners = new Set<() => void>();
  private options: TransportOptions = { maxEvents: 100, snapshot: true, snapshotLimit: 50 };

  private running = false;
  private generation = 0;
  private socket: WebSocket | null = null;

  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private connectTimer: ReturnType<typeof setTimeout> | null = null;
  private stableTimer: ReturnType<typeof setTimeout> | null = null;
  private snapshotTimer: ReturnType<typeof setTimeout> | null = null;

  private consecutiveFailures = 0;
  private backoffAttempt = 0;
  private snapshotInFlight = false;
  private snapshotQueued = false;
  private lastSnapshotAt = 0;
  private pausedForHidden = false;
  private seen = new Set<string>();

  /** Stable identity so `document.addEventListener` and `removeEventListener` pair exactly. */
  private readonly onVisibilityChange = (): void => {
    if (!this.running) return;
    if (tabHidden()) {
      if (this.reconnectTimer) {
        this.clearTimer("reconnect");
        this.pausedForHidden = true;
      }
      return;
    }
    const idle =
      this.socket === null ||
      this.socket.readyState === WebSocket.CLOSED;
    if (!this.pausedForHidden && !idle) return;
    this.pausedForHidden = false;
    this.backoffAttempt = 0;
    this.connect();
  };

  /** `useSyncExternalStore` reads THIS. Stable identity; state object swaps only on change. */
  getSnapshot = (): FeedState => this.state;

  /** Called by every consumer's subscription; the engine starts on the first, stops on the last. */
  subscribe(listener: () => void, options: TransportOptions): () => void {
    this.listeners.add(listener);
    if (this.listeners.size === 1) {
      // The FIRST subscriber fixes the shared read. Later views cannot renegotiate one transport's
      // snapshot; they may only widen the in-memory event window.
      this.options = { ...options };
      this.start();
    } else if (options.maxEvents > this.options.maxEvents) {
      this.options = { ...this.options, maxEvents: options.maxEvents };
    }
    return () => {
      this.listeners.delete(listener);
      if (this.listeners.size === 0) this.stop();
    };
  }

  // ── engine lifecycle ───────────────────────────────────────────────────────

  private start(): void {
    this.running = true;
    // NOTE: `generation` is deliberately NOT reset. It is monotonic for the life of the module,
    // so a close/retry callback left over from a PREVIOUS start can never collide with the new
    // engine's numbering and be mistaken for a current event (measured: a previous mount's
    // close timer overwrote the next mount's real error).
    this.consecutiveFailures = 0;
    this.backoffAttempt = 0;
    this.snapshotInFlight = false;
    this.snapshotQueued = false;
    this.lastSnapshotAt = 0;
    this.pausedForHidden = false;
    this.seen = new Set<string>();
    this.state = INITIAL_STATE;
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", this.onVisibilityChange);
    }
    this.connect();
  }

  private stop(): void {
    this.running = false;
    this.generation += 1; // everything in flight from this engine is now stale
    if (typeof document !== "undefined") {
      document.removeEventListener("visibilitychange", this.onVisibilityChange);
    }
    this.clearTimer("connect");
    this.clearTimer("reconnect");
    this.clearTimer("stable");
    this.clearTimer("snapshot");
    try {
      this.socket?.close();
    } catch {
      /* already closed */
    }
    this.socket = null;
    // No listener remains: reset without notifying, so a later mount starts from a clean page
    // (no event/attempt residue from a previous page or test).
    this.state = INITIAL_STATE;
    this.seen = new Set<string>();
  }

  private emit(patch: Partial<FeedState>): void {
    this.state = { ...this.state, ...patch };
    for (const listener of Array.from(this.listeners)) listener();
  }

  private clearTimer(which: "connect" | "reconnect" | "stable" | "snapshot"): void {
    const timer =
      which === "connect"
        ? this.connectTimer
        : which === "reconnect"
          ? this.reconnectTimer
          : which === "stable"
            ? this.stableTimer
            : this.snapshotTimer;
    if (timer) clearTimeout(timer);
    if (which === "connect") this.connectTimer = null;
    else if (which === "reconnect") this.reconnectTimer = null;
    else if (which === "stable") this.stableTimer = null;
    else this.snapshotTimer = null;
  }

  // ── the snapshot ───────────────────────────────────────────────────────────

  private scheduleSnapshot(): void {
    if (!this.options.snapshot) return;
    if (this.snapshotInFlight) {
      // Coalesce: one queued follow-up at most, however many opens flap past while in flight.
      this.snapshotQueued = true;
      return;
    }
    const dueIn = this.lastSnapshotAt + SNAPSHOT_MIN_INTERVAL_MS - Date.now();
    if (this.lastSnapshotAt > 0 && dueIn > 0) {
      // Coalesce across time as well: opens inside the window schedule ONE trailing fetch.
      if (!this.snapshotTimer) {
        this.snapshotTimer = setTimeout(() => {
          this.snapshotTimer = null;
          void this.runSnapshot();
        }, dueIn);
      }
      return;
    }
    void this.runSnapshot();
  }

  private async runSnapshot(): Promise<void> {
    this.snapshotInFlight = true;
    this.lastSnapshotAt = Date.now();
    try {
      const snapshot = await readActivitySnapshot(this.options.snapshotLimit);
      if (!this.running) return;
      if (snapshot.ok) {
        this.emit({ total: snapshot.total });
        this.merge(snapshot.rows);
      }
    } catch (e) {
      // The live socket is the primary channel: a failed snapshot must not break the feed — but it
      // must not be invisible either (audit Q-07), so it surfaces through `error` like any failure.
      if (this.running) {
        // The count is only ever THIS read's evidence: with the read failed, a number left on
        // screen would be a guess about rows nobody measured.
        this.emit({ total: null, error: `activity snapshot failed: ${message(e)}` });
      }
    } finally {
      this.snapshotInFlight = false;
      if (this.snapshotQueued) {
        this.snapshotQueued = false;
        this.scheduleSnapshot();
      }
    }
  }

  // ── events ─────────────────────────────────────────────────────────────────

  /** Merge incoming events: drop duplicates by id, newest first, cap at the widest view. */
  private merge(incoming: ActivityEvent[]): void {
    if (!this.running || !Array.isArray(incoming) || incoming.length === 0) return;
    // Q-10: dedupe OUTSIDE React state (StrictMode invokes updaters twice and keeps the second
    // result; the old in-updater set mutation dropped every event on the second pass).
    const fresh: ActivityEvent[] = [];
    for (const e of incoming) {
      if (!e) continue;
      const key = String(e.id ?? `${e.created_at}-${e.summary}`);
      if (this.seen.has(key)) continue;
      this.seen.add(key);
      fresh.push(e);
    }
    if (fresh.length === 0) return;
    const cap = this.options.maxEvents;
    // Bound the dedupe set: the feed itself is capped at `cap`, so remembering twice that is
    // strictly more than can ever render; the oldest keys (Sets iterate in insertion order) fall
    // out first.
    if (this.seen.size > cap * 2) {
      this.seen = new Set(Array.from(this.seen).slice(-cap * 2));
    }
    const next = [...fresh, ...this.state.events];
    next.sort((a, b) => {
      const ta = Date.parse(a?.created_at ?? "") || 0;
      const tb = Date.parse(b?.created_at ?? "") || 0;
      return tb - ta || (b?.id ?? 0) - (a?.id ?? 0);
    });
    this.emit({ events: next.slice(0, cap) });
  }

  private reportFailure(why: string): void {
    if (!this.running) return;
    this.consecutiveFailures += 1;
    this.backoffAttempt += 1;
    this.emit({
      attempts: this.consecutiveFailures,
      error: why,
      status: this.consecutiveFailures >= OFFLINE_AFTER_FAILURES ? "offline" : "reconnecting",
    });
    this.clearTimer("reconnect"); // never two pending retries
    // F-41: a hidden tab has nobody to notify and browsers throttle its timers, so the old loop
    // kept a dead socket retrying forever behind a background page. While hidden NOTHING is
    // scheduled; `onVisibilityChange` resumes the engine (with a reset backoff) on the way back.
    if (tabHidden()) {
      this.pausedForHidden = true;
      return;
    }
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, backoffDelay(this.backoffAttempt - 1));
  }

  // ── the socket ─────────────────────────────────────────────────────────────

  private connect(ticket?: string): void {
    if (!this.running) return;
    if (
      this.socket &&
      (this.socket.readyState === WebSocket.OPEN ||
        this.socket.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }

    const myGeneration = ++this.generation;

    // F-46: mint first, connect second. The key goes to the ticket endpoint in a header; the
    // socket URL carries only the short-lived ticket. A mint failure is a named, retried failure
    // like any other — never a silent fallback to `?key=`, which the server now refuses.
    if (ticket === undefined) {
      void mintSocketTicket()
        .then((fresh) => {
          if (myGeneration !== this.generation || !this.running) return;
          this.connect(fresh);
        })
        .catch((e: unknown) => {
          if (myGeneration !== this.generation || !this.running) return;
          this.reportFailure(`could not mint a socket ticket: ${message(e)}`);
        });
      return;
    }

    const url = socketUrl(ticket);

    let socket: WebSocket;
    try {
      socket = new WebSocket(url);
    } catch (e) {
      // Q-02: a relative/misconfigured URL throws synchronously. Caught here so the dashboard can
      // never die on it; the honest reason goes to `error` and the retry is scheduled. The message
      // carries the URL WITHOUT the key.
      this.reportFailure(`could not open a WebSocket to ${displaySocketUrl()}: ${message(e)}`);
      return;
    }
    this.socket = socket;

    let connectTimedOut = false;

    // Q-30: bound the CONNECTING phase. A handshake that never completes is closed, which routes
    // through onclose (flagged via `connectTimedOut`) into the normal backoff retry.
    this.connectTimer = setTimeout(() => {
      if (myGeneration !== this.generation) return;
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
      if (myGeneration !== this.generation) {
        // A stale socket that opened after its engine moved on: close it, write nothing.
        try {
          socket.close();
        } catch {
          /* already closed */
        }
        return;
      }
      if (!this.running) {
        try {
          socket.close();
        } catch {
          /* already closed */
        }
        return;
      }
      this.clearTimer("connect");
      this.clearTimer("stable");
      this.consecutiveFailures = 0;
      this.emit({ attempts: 0, status: "live", error: null });
      this.stableTimer = setTimeout(() => {
        if (myGeneration === this.generation) this.backoffAttempt = 0;
      }, STABLE_RESET_MS);
      this.scheduleSnapshot();
    };

    socket.onmessage = (e) => {
      if (myGeneration !== this.generation) return;
      try {
        const parsed = JSON.parse(e.data as string) as ActivityEvent | ActivityEvent[];
        this.merge(Array.isArray(parsed) ? parsed : [parsed]);
      } catch {
        // A malformed frame is ignored on purpose: one bad message must not kill the socket.
      }
    };

    socket.onclose = (event: CloseEvent | undefined) => {
      // A stale socket must not write state, nor schedule a retry (duplicate close cycles used to
      // pile up reconnect timers and snapshot fetches this way).
      if (myGeneration !== this.generation) return;
      this.clearTimer("connect");
      this.clearTimer("stable");
      if (this.socket === socket) this.socket = null;
      if (!this.running) return;
      this.reportFailure(
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
}

/** The page's ONE transport. Module-level: two consumers in one page share it. */
const transport = new ActivityTransport();

/** A consumer's view of the shared state, sliced to its own event ceiling. */
export function useActivityFeed(maxEvents = 100, options: UseActivityFeedOptions = {}) {
  const { snapshot = true, snapshotLimit = 50 } = options;

  // The options ride the SUBSCRIBE call (not a render-time mutation): React invokes subscribe in
  // an effect, so the engine is configured exactly when it starts, and a changed option set
  // re-subscribes instead of silently restarting a socket mid-render.
  const subscribe = useCallback(
    (onChange: () => void) => transport.subscribe(onChange, { maxEvents, snapshot, snapshotLimit }),
    [maxEvents, snapshot, snapshotLimit],
  );

  const state = useSyncExternalStore(
    subscribe,
    transport.getSnapshot,
    () => INITIAL_STATE,
  );

  return {
    events: state.events.length > maxEvents ? state.events.slice(0, maxEvents) : state.events,
    connected: state.status === "live",
    status: state.status,
    error: state.error,
    attempts: state.attempts,
    total: state.total,
  };
}
