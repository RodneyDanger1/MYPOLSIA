/**
 * U1 realtime correctness — coder-c-realtime (audit Q-02/Q-03/Q-04/Q-07/Q-10/Q-30).
 *
 * The fake WebSocket and the stubbed fetch live ONLY in this file (test infrastructure); every
 * production path runs the real transports. What is pinned here:
 *  - Q-02  the socket URL is absolute even when NEXT_PUBLIC_WS_URL resolves to "", and a constructor
 *          failure becomes honest feed state instead of a crash into the route error boundary.
 *  - Q-07  the feed UI renders the hook's error and never claims an empty-but-fine feed before the
 *          socket is actually live.
 *  - Q-10  a StrictMode double-invoked state updater cannot drop an event (dedupe lives outside it).
 *  - Q-30  the CONNECTING phase is bounded; a stuck handshake is closed, reported, and retried.
 *  - storm duplicate open/close cycles cannot pile up snapshot fetches.
 *  - Q-03  a consumer abort reports "cancelled", never "timed out"; a real timeout still says so.
 *  - Q-04  the read retry chain is bounded by the total budget (the ≈46 s burn is gone).
 */
import { StrictMode, type ReactNode } from "react";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import { ActivityFeed } from "@/components/dashboard/ActivityFeed";
import {
  CONNECT_TIMEOUT_MS,
  resolveWsBase,
  useActivityFeed,
} from "@/hooks/useActivityFeed";
import { ApiError, api, type ActivityEvent } from "@/lib/api";

const realFetch = (globalThis as { fetch?: unknown }).fetch;
const realWebSocket = (globalThis as { WebSocket?: unknown }).WebSocket;

/* --------------------------------------------------------------------------------------------- */
/* Fake WebSocket — test-only. Models the real one where it matters for this hook: events are
 * queued asynchronously (never delivered from the constructor), and close() transitions state
 * before the close event is delivered. */
class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];
  static throwOnConstruct = false;

  readyState: number = FakeWebSocket.CONNECTING;
  readonly url: string;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    if (FakeWebSocket.throwOnConstruct) {
      throw new Error("Failed to construct 'WebSocket': Invalid URL");
    }
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  /** The client-side close: transitions immediately, delivers the event asynchronously (as real
   *  browsers do). A connecting socket surfaces as an abnormal (1006) close. */
  close = (): void => {
    if (this.readyState === FakeWebSocket.CLOSED || this.readyState === FakeWebSocket.CLOSING) return;
    const wasConnecting = this.readyState === FakeWebSocket.CONNECTING;
    this.readyState = FakeWebSocket.CLOSED;
    setTimeout(() => this.onclose?.({ code: wasConnecting ? 1006 : 1000, reason: "" }), 0);
  };

  /* ---- test drivers ("server side") ---- */
  serverOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  serverMessage(payload: unknown): void {
    this.onmessage?.({ data: JSON.stringify(payload) });
  }
  serverClose(code = 1006, reason = ""): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code, reason });
  }
}

function lastSocket(): FakeWebSocket {
  const s = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
  if (!s) throw new Error("no FakeWebSocket was constructed");
  return s;
}

function installFakeWebSocket(): void {
  (globalThis as { WebSocket?: unknown }).WebSocket =
    FakeWebSocket as unknown as typeof WebSocket;
}

/* --------------------------------------------------------------------------------------------- */
/* fetch stub — test-only, shaped for the one code path api.ts reads (ok/status/headers/text/json). */
function jsonResponse(body: unknown, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : `HTTP ${status}`,
    headers: { get: (_name: string) => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function okFetch(body: unknown = []): jest.Mock {
  return jest.fn(async () => jsonResponse(body));
}

/** A 500 that arrives after `ms`, abortable like a real fetch. */
function fetch500After(ms: number) {
  return (_url: string, init: RequestInit): Promise<unknown> =>
    new Promise((resolve, reject) => {
      const t = setTimeout(() => resolve(jsonResponse({ detail: "boom" }, 500)), ms);
      const signal = init.signal;
      const fail = () => {
        clearTimeout(t);
        reject(Object.assign(new Error("Aborted"), { name: "AbortError" }));
      };
      if (signal?.aborted) fail();
      else signal?.addEventListener("abort", fail);
    });
}

/** A fetch that never resolves unless aborted — models a wedged transport. */
function fetchPending() {
  return (_url: string, init: RequestInit): Promise<unknown> =>
    new Promise((_resolve, reject) => {
      const fail = () => reject(Object.assign(new Error("Aborted"), { name: "AbortError" }));
      const signal = init.signal;
      if (signal?.aborted) fail();
      else signal?.addEventListener("abort", fail);
    });
}

function activityFetchCalls(fetchMock: jest.Mock): number {
  return fetchMock.mock.calls.filter(([url]) => String(url).includes("/dashboard/activity")).length;
}

let eventSeq = 0;
function activityEvent(over: Partial<ActivityEvent> = {}): ActivityEvent {
  eventSeq += 1;
  return {
    id: eventSeq,
    agent_type: "social_media",
    action: "post",
    summary: `event ${eventSeq}`,
    level: "info",
    created_at: new Date().toISOString(),
    ...over,
  };
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  FakeWebSocket.throwOnConstruct = false;
  installFakeWebSocket();
  (globalThis as { fetch?: unknown }).fetch = okFetch([]);
  window.localStorage.clear();
});

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
  (globalThis as { WebSocket?: unknown }).WebSocket =
    realWebSocket as typeof WebSocket | undefined;
  (globalThis as { fetch?: unknown }).fetch = realFetch;
});

/* ============================================================================================= */
describe("Q-02 — the socket URL is absolute and honest", () => {
  it("derives ws/wss from the serving origin when NEXT_PUBLIC_WS_URL is empty", () => {
    // The documented deployment default is the EMPTY STRING (Dockerfile ARG / docker-compose /
    // next.config.js); `?? "ws://localhost"` never caught it.
    expect(resolveWsBase("", { protocol: "http:", host: "localhost:3000" })).toBe("ws://localhost:3000");
    expect(resolveWsBase("", { protocol: "https:", host: "app.example.com" })).toBe(
      "wss://app.example.com",
    );
    expect(resolveWsBase("ws://explicit:9000", { protocol: "https:", host: "app.example.com" })).toBe(
      "ws://explicit:9000",
    );
    expect(resolveWsBase(undefined, null)).toBe("");
  });

  it("the hook constructs an ABSOLUTE ws:// URL (never a bare relative path)", () => {
    const { unmount } = renderHook(() => useActivityFeed());
    const url = lastSocket().url;
    expect(url).toMatch(/^wss?:\/\//);
    expect(url).toContain("/ws/activity?key=");
    unmount();
  });

  it("a constructor failure becomes honest error state — not a crash (Q-02 regression)", () => {
    FakeWebSocket.throwOnConstruct = true;
    const { result } = renderHook(() => useActivityFeed());
    expect(result.current.status).toBe("reconnecting");
    expect(result.current.error).toMatch(/could not open a WebSocket/);
    // The credential must never surface in a user-visible message.
    expect(result.current.error).not.toContain("key=");
  });

  it("the feed renders the constructor failure instead of claiming an empty-but-fine feed", () => {
    FakeWebSocket.throwOnConstruct = true;
    render(<ActivityFeed />);
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent(/could not open a WebSocket/);
    expect(screen.queryByText(/No activity yet/i)).toBeNull();
    expect(screen.getByText(/RECONNECTING/)).toBeInTheDocument();
  });
});

/* ============================================================================================= */
describe("healthy socket — states are TRUE", () => {
  it("connecting → live → reconnecting, messages merge, snapshot fetched once", async () => {
    const fetchMock = okFetch([]);
    (globalThis as { fetch?: unknown }).fetch = fetchMock;

    const { result, unmount } = renderHook(() => useActivityFeed());
    expect(result.current.status).toBe("connecting");
    expect(result.current.connected).toBe(false);

    act(() => lastSocket().serverOpen());
    expect(result.current.status).toBe("live");
    expect(result.current.connected).toBe(true);

    await waitFor(() => expect(activityFetchCalls(fetchMock)).toBe(1));

    const evt = activityEvent({ summary: "Revenue captured" });
    act(() => lastSocket().serverMessage(evt));
    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0].summary).toBe("Revenue captured");

    // Duplicate frames must not duplicate rows.
    act(() => lastSocket().serverMessage(evt));
    expect(result.current.events).toHaveLength(1);

    act(() => lastSocket().serverClose(1006));
    expect(result.current.status).toBe("reconnecting");
    expect(result.current.connected).toBe(false);
    expect(result.current.attempts).toBe(1);
    expect(result.current.error).toMatch(/connection closed \(code 1006\)/);
    unmount();
  });

  it("recovery: a later open returns to live and clears the error", () => {
    const { result, unmount } = renderHook(() => useActivityFeed());
    act(() => lastSocket().serverClose(1006));
    expect(result.current.status).toBe("reconnecting");
    expect(result.current.error).not.toBeNull();

    act(() => lastSocket().serverOpen());
    expect(result.current.status).toBe("live");
    expect(result.current.error).toBeNull();
    expect(result.current.attempts).toBe(0);
    unmount();
  });
});

/* ============================================================================================= */
describe("Q-30 — the CONNECTING phase is bounded", () => {
  it("closes a handshake that never opens, reports the real reason, and retries", async () => {
    jest.useFakeTimers();
    const { result } = renderHook(() => useActivityFeed());
    const stuck = lastSocket();
    expect(stuck.readyState).toBe(FakeWebSocket.CONNECTING);

    await act(async () => {
      await jest.advanceTimersByTimeAsync(CONNECT_TIMEOUT_MS + 1);
    });

    expect(stuck.readyState).toBe(FakeWebSocket.CLOSED);
    expect(result.current.status).toBe("reconnecting");
    expect(result.current.error).toMatch(new RegExp(`did not open within ${CONNECT_TIMEOUT_MS}ms`));
    expect(result.current.error).not.toContain("key=");

    // The retry is a NEW socket (bounded by backoff, not a dead socket reused forever).
    await act(async () => {
      await jest.advanceTimersByTimeAsync(31_000);
    });
    expect(FakeWebSocket.instances.length).toBeGreaterThanOrEqual(2);
  });
});

/* ============================================================================================= */
describe("flapping socket — duplicate open/close cycles cannot pile up snapshot fetches", () => {
  it("4 open/close cycles in ~8s produce fewer activity fetches than opens", async () => {
    jest.useFakeTimers();
    jest.spyOn(Math, "random").mockReturnValue(0.5); // deterministic jitter -> delays 750/1500/3000/6000ms

    const fetchMock = okFetch([]);
    (globalThis as { fetch?: unknown }).fetch = fetchMock;

    const { result } = renderHook(() => useActivityFeed());

    // The observed live defect: every open refetched the snapshot and the backoff reset on open, so
    // a flapping server produced ~4 fetches in ~8s at near-minimal retry delay.
    await act(async () => {
      lastSocket().serverOpen();
    });
    await act(async () => {
      lastSocket().serverClose(1006);
      await jest.advanceTimersByTimeAsync(800);
    });
    await act(async () => {
      lastSocket().serverOpen();
      lastSocket().serverClose(1006);
      await jest.advanceTimersByTimeAsync(1600);
    });
    await act(async () => {
      lastSocket().serverOpen();
      lastSocket().serverClose(1006);
      await jest.advanceTimersByTimeAsync(3100);
    });
    await act(async () => {
      lastSocket().serverOpen();
      lastSocket().serverClose(1006);
      await jest.advanceTimersByTimeAsync(2000);
    });

    const opens = FakeWebSocket.instances.length;
    const fetches = activityFetchCalls(fetchMock);
    expect(opens).toBe(4);
    expect(fetches).toBeLessThanOrEqual(3);
    expect(fetches).toBeLessThan(opens); // never one fetch per open under flapping
    expect(result.current.status).toBe("reconnecting");
  });
});

/* ============================================================================================= */
describe("Q-10 — StrictMode cannot drop events", () => {
  it("an event survives the double-invoked updater (dedupe lives outside setState)", () => {
    const wrapper = ({ children }: { children?: ReactNode }) => <StrictMode>{children}</StrictMode>;
    const { result } = renderHook(() => useActivityFeed(), { wrapper });

    // StrictMode double-mounts: the first engine's socket is closed by its cleanup and a second
    // socket takes over — this also proves the remount cycle does not confuse the engine.
    expect(FakeWebSocket.instances.length).toBe(2);

    act(() => lastSocket().serverOpen());
    const evt = activityEvent({ summary: "must not vanish" });
    act(() => lastSocket().serverMessage(evt));

    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0].summary).toBe("must not vanish");
  });
});

/* ============================================================================================= */
describe("feed UI — honest badge + live grammar", () => {
  async function renderLiveFeed(fetchBody: unknown = []) {
    const fetchMock = okFetch(fetchBody);
    (globalThis as { fetch?: unknown }).fetch = fetchMock;
    render(<ActivityFeed />);
    await act(async () => {
      lastSocket().serverOpen();
    });
    return fetchMock;
  }

  it("shows CONNECTING (not an empty-but-fine claim) before the socket is open", () => {
    render(<ActivityFeed />);
    expect(screen.getByText(/CONNECTING/)).toBeInTheDocument();
    expect(screen.queryByText(/No activity yet/i)).toBeNull();
    expect(document.querySelector('[data-testid="activity-feed"]')?.getAttribute("data-status")).toBe(
      "connecting",
    );
  });

  it("shows LIVE only while the socket is open, and the honest empty copy then", async () => {
    await renderLiveFeed([]);
    expect(screen.getByText("LIVE")).toBeInTheDocument();
    expect(screen.getByText(/No activity yet/i)).toBeInTheDocument();

    await act(async () => {
      lastSocket().serverClose(1006);
    });
    expect(screen.queryByText("LIVE")).toBeNull();
    expect(screen.getByText(/RECONNECTING \(ATTEMPT 1\)/)).toBeInTheDocument();
    // The empty-but-fine copy must be gone: a dead feed is not an empty-but-fine feed.
    expect(screen.queryByText(/No activity yet/i)).toBeNull();
  });

  it("goes OFFLINE (with the real close reason) after repeated failures", async () => {
    jest.useFakeTimers();
    render(<ActivityFeed />);
    for (let i = 0; i < 5; i += 1) {
      act(() => lastSocket().serverClose(1008, "bad key"));
      if (i < 4) {
        await act(async () => {
          await jest.advanceTimersByTimeAsync(9_000);
        });
      }
    }
    expect(screen.getByText("OFFLINE")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/connection closed \(1008: bad key\)/);
    expect(screen.queryByText(/No activity yet/i)).toBeNull();
  });

  it("surfaces a failed REST snapshot even while the socket is live (Q-07)", async () => {
    const fetchMock = jest.fn(async () => {
      throw new Error("boom");
    });
    (globalThis as { fetch?: unknown }).fetch = fetchMock;
    render(<ActivityFeed />);
    await act(async () => {
      lastSocket().serverOpen();
    });
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/activity snapshot failed/));
    expect(screen.getByText("LIVE")).toBeInTheDocument();
  });

  it("renders the live grammar: day dividers, mono time, dark-red NEW for unread, honest null date", async () => {
    // Seed the previous visit just BEFORE local midnight so every event dated today is unread —
    // deterministic at any wall-clock time (a now-minus-60s seed makes "today 2:36 PM" future or
    // past depending on when the suite runs).
    const midnight = new Date();
    midnight.setHours(0, 0, 0, 0);
    window.localStorage.setItem(
      "polsia.dashboard.feed.lastSeenAt",
      String(midnight.getTime() - 1_000),
    );
    render(<ActivityFeed />);
    await act(async () => {
      lastSocket().serverOpen();
    });

    const todayAt = new Date();
    todayAt.setHours(14, 36, 0, 0);
    const expectedClock = todayAt.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });

    const oldAt = new Date(Date.now() - 3 * 86_400_000);
    const expectedDayLabel = oldAt
      .toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" })
      .toUpperCase();

    act(() => {
      lastSocket().serverMessage(
        activityEvent({ id: 9001, summary: "Fresh event", created_at: new Date().toISOString() }),
      );
      lastSocket().serverMessage(
        activityEvent({ id: 9002, summary: "Timed today event", created_at: todayAt.toISOString() }),
      );
      lastSocket().serverMessage(
        activityEvent({ id: 9003, summary: "Old event", created_at: oldAt.toISOString() }),
      );
      lastSocket().serverMessage(
        activityEvent({ id: 9004, summary: "Timeless event", created_at: null }),
      );
    });

    expect(screen.getByText("TODAY")).toBeInTheDocument();
    expect(screen.getByText(expectedDayLabel)).toBeInTheDocument();
    expect(screen.getByText("DATE UNKNOWN")).toBeInTheDocument();
    expect(screen.getByText("date unknown")).toBeInTheDocument();
    expect(screen.getByText(expectedClock)).toBeInTheDocument();
    expect(screen.getByText("Fresh event")).toBeInTheDocument();
    // Unread marker: once on the TODAY divider, once on each of the two today-dated cards. The
    // three-days-old event and the null-dated event are not newer than the previous visit.
    expect(screen.getAllByText("NEW")).toHaveLength(3);
  });

  it("shows no NEW markers on a first visit (nothing is fabricated)", async () => {
    render(<ActivityFeed />);
    await act(async () => {
      lastSocket().serverOpen();
    });
    act(() => {
      lastSocket().serverMessage(activityEvent({ summary: "First visit event" }));
    });
    expect(screen.getByText("First visit event")).toBeInTheDocument();
    expect(screen.queryAllByText("NEW")).toHaveLength(0);
  });
});

/* ============================================================================================= */
describe("api client honesty (Q-03 / Q-04)", () => {
  it("Q-03: a consumer abort reports 'cancelled', never 'timed out'", async () => {
    (globalThis as { fetch?: unknown }).fetch = fetchPending();
    const controller = new AbortController();
    const pending = api.get("/x", { signal: controller.signal }).catch((e) => e);
    setTimeout(() => controller.abort(), 20);
    const err = (await pending) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("E_ABORTED");
    expect(err.message).toMatch(/cancelled/);
    expect(err.message).not.toMatch(/timed out/i);
  });

  it("Q-03: a real timeout still says timeout, with the attempt's own bound", async () => {
    (globalThis as { fetch?: unknown }).fetch = fetchPending();
    const err = (await api.get("/x", { timeoutMs: 150 }).catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("E_TIMEOUT");
    expect(err.message).toBe("no response within 150ms");
  });

  it("Q-04: a fast-500 read retries exactly RETRY_ATTEMPTS times, then throws the real error", async () => {
    const fetchMock = jest.fn(async () => jsonResponse({ detail: "boom" }, 500));
    (globalThis as { fetch?: unknown }).fetch = fetchMock;
    const err = (await api.get("/x").catch((e) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("E_HTTP_500");
    expect(fetchMock).toHaveBeenCalledTimes(3); // 1 attempt + 2 retries — unchanged count
  });

  it("Q-04: the DEFAULT read budget bounds a late-5xx chain at ≈20 s (the ≈46 s burn is gone)", async () => {
    jest.useFakeTimers();
    const fetchMock = jest.fn(fetch500After(14_000));
    (globalThis as { fetch?: unknown }).fetch = fetchMock;
    const started = Date.now();
    let settledAt = 0;
    const pending = api.get("/x").catch((e) => {
      settledAt = Date.now();
      return e;
    });

    // 14 s attempt 1 (500) + 300 ms backoff + attempt 2 bounded by the remaining budget -> the chain
    // must still be in flight at 19 s and settled by ≈20 s.
    await jest.advanceTimersByTimeAsync(19_000);
    expect(settledAt).toBe(0);
    await jest.advanceTimersByTimeAsync(1_100);

    const err = (await pending) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("E_TIMEOUT"); // attempt 2's bound = the remainder of the budget
    expect(err.message).toMatch(/no response within \d+ms/);
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(settledAt - started).toBeLessThanOrEqual(21_000);
    expect(settledAt - started).toBeGreaterThan(19_000);
  });

  it("Q-04: the budget stops a retry that cannot fit it — and the real 500 is what surfaces", async () => {
    const fetchMock = jest.fn(fetch500After(500));
    (globalThis as { fetch?: unknown }).fetch = fetchMock;
    const started = Date.now();
    const err = (await api.get("/x", { timeoutMs: 1500, budgetMs: 2000 }).catch((e) => e)) as ApiError;
    const elapsed = Date.now() - started;
    expect(err.code).toBe("E_HTTP_500"); // not a fake timeout: the latest real failure
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(elapsed).toBeLessThan(2_500);
  });

  it("Q-04: a hopeless first retry is not attempted at all", async () => {
    const fetchMock = jest.fn(fetch500After(500));
    (globalThis as { fetch?: unknown }).fetch = fetchMock;
    const err = (await api.get("/x", { timeoutMs: 1000, budgetMs: 1200 }).catch((e) => e)) as ApiError;
    expect(err.code).toBe("E_HTTP_500");
    expect(fetchMock).toHaveBeenCalledTimes(1); // 500ms elapsed + 300ms backoff + 1000ms min > 1200ms
  });
});
