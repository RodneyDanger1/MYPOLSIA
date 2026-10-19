/**
 * V23/L4 — observability: the worker's publish-breaker card and the feed's declared total.
 *
 * COMPLETENESS #8 and #6.  Every production path runs for real here:
 *  - the REAL `lib/api` client (no `@/lib/api` mock) over a stubbed `fetch`, so the
 *    `onHeaders` capture is the shipped one and the `X-Polsia-Total` header is read from the
 *    SAME response as the rows;
 *  - the REAL `useActivityFeed` hook and `ActivityFeed` header, driven by a test-only
 *    WebSocket (jsdom has no socket) exactly the way `u1-realtime.test.tsx` drives it;
 *  - the REAL `OpsPanel` on the REAL `/usage` page, reading `/ops/activity-publish`.
 *
 * The pinned honesty rules:
 *  - worker never wrote  -> `data: null` -> "has not published stats yet", NEVER four zeros;
 *  - Redis unreachable   -> the named `E_REDIS` refusal, rendered with its code + fix hint;
 *  - header absent       -> no count at all (never a guess from the rows on hand);
 *  - header present      -> "N events" from the response's own `X-Polsia-Total`.
 */
import { act, render, renderHook, screen, waitFor, within } from "@testing-library/react";

import { ActivityFeed } from "@/components/dashboard/ActivityFeed";
import { useActivityFeed } from "@/hooks/useActivityFeed";
import UsagePage from "@/app/usage/page";

/* ── test-only transports ───────────────────────────────────────────────────────────────────── */

type WireResponse = { status?: number; body: unknown; headers?: Record<string, string> };

/** The slice of `Response` lib/api reads: ok / status / statusText / headers.get / text / json. */
function wireResponse({ status = 200, body, headers = {} }: WireResponse) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : `HTTP ${status}`,
    headers: { get: (name: string) => headers[name] ?? null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

function installFetch(route: (url: string) => WireResponse): jest.Mock {
  const mock = jest.fn(async (url: string) => wireResponse(route(String(url))));
  (globalThis as { fetch?: unknown }).fetch = mock;
  return mock;
}

/** The four reads the Usage page issues, with the worker read supplied per test. */
const STATS = {
  failures: 3,
  skipped_while_open: 12,
  breaker_open: true,
  cooldown_seconds: 30,
  last_error: "redis connection refused",
};

function usageRoute(worker: WireResponse) {
  return (url: string): WireResponse => {
    if (url.includes("/api/v1/runs")) return { body: { items: [], total: 0 } };
    if (url.includes("/ops/agent-quality")) return { body: { agents: [] } };
    if (url.includes("/ops/events/stats")) return { body: STATS };
    if (url.includes("/ops/activity-publish")) return worker;
    throw new Error(`unexpected fetch in test: ${url}`);
  };
}

class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.CONNECTING;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(readonly url: string) {
    FakeWebSocket.instances.push(this);
  }

  close = (): void => {
    this.readyState = FakeWebSocket.CLOSED;
  };

  serverOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
}

function lastSocket(): FakeWebSocket {
  const socket = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
  if (!socket) throw new Error("no FakeWebSocket was constructed");
  return socket;
}

const realFetch = (globalThis as { fetch?: unknown }).fetch;
const realWebSocket = (globalThis as { WebSocket?: unknown }).WebSocket;

beforeEach(() => {
  FakeWebSocket.instances = [];
  (globalThis as { WebSocket?: unknown }).WebSocket =
    FakeWebSocket as unknown as typeof WebSocket;
  window.localStorage.clear();
});

afterEach(() => {
  (globalThis as { fetch?: unknown }).fetch = realFetch;
  (globalThis as { WebSocket?: unknown }).WebSocket = realWebSocket;
  jest.restoreAllMocks();
});

/* ═══════════════════════════════════════════════════════════════════════════════════════════════
 * 1. The worker-process card on the Usage page's Ops panel
 * ═══════════════════════════════════════════════════════════════════════════════════════════════ */

describe("Ops panel — the worker process card", () => {
  it("renders the counters the worker published, plus the publish instant", async () => {
    installFetch(
      usageRoute({
        body: {
          data: {
            failures: 4,
            skipped_while_open: 9,
            breaker_open: true,
            ts: "2026-10-19T17:05:00+00:00",
          },
        },
      }),
    );

    render(<UsagePage />);

    const card = await screen.findByTestId("ops-worker");
    expect(await within(card).findByTestId("ops-worker-failures")).toHaveTextContent("4");
    expect(within(card).getByTestId("ops-worker-skipped")).toHaveTextContent("9");
    expect(within(card).getByTestId("ops-worker-state")).toHaveTextContent("OPEN");
    // The publish instant is rendered by the app's stamp formatter, not as a raw ISO string.
    const published = within(card).getByTestId("ops-worker-published");
    expect(published).not.toHaveTextContent("2026-10-19T17:05:00+00:00");
    expect(published.textContent).toMatch(/OCT 19, 2026/);
    expect(within(card).queryByTestId("ops-worker-never")).toBeNull();
  });

  it("says the worker has not published yet when the key is absent — never four zeros", async () => {
    installFetch(usageRoute({ body: { data: null } }));

    render(<UsagePage />);

    const never = await screen.findByTestId("ops-worker-never");
    expect(never).toHaveTextContent("has not published stats yet");
    // No counter is rendered at all: a zero here would read as a clean, measured worker.
    expect(screen.queryByTestId("ops-worker-failures")).toBeNull();
    expect(screen.queryByTestId("ops-worker-skipped")).toBeNull();
    expect(screen.queryByTestId("ops-worker-state")).toBeNull();
  });

  it("names an unreachable Redis with the code and the fix hint", async () => {
    installFetch(
      usageRoute({
        status: 503,
        body: {
          detail: {
            code: "E_REDIS",
            message: "activity publish stats are unreadable",
            how_to_fix: "check REDIS_URL and that the broker is running",
          },
        },
      }),
    );

    render(<UsagePage />);

    const alert = await screen.findByText(/Could not load the worker's publish stats/);
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent(
      "E_REDIS: activity publish stats are unreadable — check REDIS_URL and that the broker is running",
    );
    expect(screen.queryByTestId("ops-worker-failures")).toBeNull();
  });

  it("no longer claims the worker is invisible — the copy names the published snapshot", async () => {
    installFetch(usageRoute({ body: { data: null } }));

    render(<UsagePage />);

    const scope = await screen.findByTestId("ops-breaker-scope");
    expect(scope).toHaveTextContent("module globals in the API process");
    expect(scope).toHaveTextContent("per process");
    expect(scope).toHaveTextContent("15-minute beat tick");
    expect(scope).not.toHaveTextContent("Neither surface sees");
    expect(scope).toHaveTextContent("worker process card");
    expect(await screen.findByTestId("ops-worker")).toBeInTheDocument();
  });
});

/* ═══════════════════════════════════════════════════════════════════════════════════════════════
 * 2. The feed's declared total, from the response's own X-Polsia-Total
 * ═══════════════════════════════════════════════════════════════════════════════════════════════ */

const EVENT = {
  id: 1,
  agent_type: "social_media",
  action: "post",
  summary: "Published the Monday post",
  level: "info",
  created_at: "2026-10-19T17:00:00+00:00",
};

describe("Activity feed — the declared total", () => {
  it("prints 'N events' from X-Polsia-Total on the snapshot response", async () => {
    installFetch(() => ({ body: [EVENT], headers: { "X-Polsia-Total": "137" } }));

    render(<ActivityFeed />);
    act(() => lastSocket().serverOpen());

    const count = await screen.findByTestId("activity-feed-total");
    expect(count).toHaveTextContent("137 events");
  });

  it("prints NO count when the response carries no header — never the rows on hand", async () => {
    installFetch(() => ({ body: [EVENT] }));

    render(<ActivityFeed />);
    act(() => lastSocket().serverOpen());

    // The snapshot landed (the row renders)…
    expect(await screen.findByText("Published the Monday post")).toBeInTheDocument();
    // …and no count was invented from its one row.
    expect(screen.queryByTestId("activity-feed-total")).toBeNull();
  });

  it("exposes the captured total on the hook, null when the header is absent", async () => {
    installFetch(() => ({ body: [EVENT], headers: { "X-Polsia-Total": "42" } }));

    const present = renderHook(() => useActivityFeed(10));
    act(() => lastSocket().serverOpen());
    await waitFor(() => expect(present.result.current.total).toBe(42));
    present.unmount();

    installFetch(() => ({ body: [EVENT] }));
    const absent = renderHook(() => useActivityFeed(10));
    act(() => lastSocket().serverOpen());
    await waitFor(() => expect(absent.result.current.events).toHaveLength(1));
    expect(absent.result.current.total).toBeNull();
    absent.unmount();
  });
});
