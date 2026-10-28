import { act, render, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { ...actual.api, get: jest.fn() } };
});

import { api } from "@/lib/api";
import { useActivityFeed } from "@/hooks/useActivityFeed";
import { ActivityFeed } from "@/components/dashboard/ActivityFeed";
import { ActivityStrip } from "@/components/shell/ActivityStrip";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/** Minimal WebSocket double: only what useActivityFeed touches (open drives the snapshot). */
class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.CONNECTING;
  readonly url: string;
  onopen: (() => void) | null = null;
  onmessage: ((e: { data: string }) => void) | null = null;
  onclose: ((e: { code: number; reason: string }) => void) | null = null;
  onerror: (() => void) | null = null;

  constructor(url: string) {
    this.url = url;
    FakeWebSocket.instances.push(this);
  }

  close = (): void => {
    this.readyState = FakeWebSocket.CLOSED;
    setTimeout(() => this.onclose?.({ code: 1000, reason: "" }), 0);
  };

  serverOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
}

const realWebSocket = (globalThis as { WebSocket?: unknown }).WebSocket;
const realFetch = (globalThis as { fetch?: unknown }).fetch;

beforeEach(() => {
  FakeWebSocket.instances = [];
  (globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket;
  (globalThis as { fetch: unknown }).fetch = jest.fn(async (url: string) =>
    String(url).includes("/ws/ticket")
      ? { ok: true, status: 200, headers: { get: () => null }, json: async () => ({ ticket: "test-ticket", expires_in: 30 }) }
      : { ok: true, status: 200, headers: { get: () => null }, json: async () => [] },
  );
  mockApiGet.mockReset();
  mockApiGet.mockResolvedValue([]);
});

afterEach(() => {
  (globalThis as { WebSocket?: unknown }).WebSocket = realWebSocket;
  (globalThis as { fetch?: unknown }).fetch = realFetch;
});

async function flushMint(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function FeedView({ label }: { label: string }) {
  const { events } = useActivityFeed();
  return <div data-testid={`feed-${label}`}>{events.length}</div>;
}

describe("activity snapshot — two mounted consumers share one read", () => {
  it("dashboard mounts ActivityStrip + ActivityFeed: one /dashboard/activity read, not two", async () => {
    render(
      <>
        <FeedView label="strip" />
        <FeedView label="feed" />
      </>,
    );
    await flushMint();

    // Both sockets are open in the same commit; each used to fire its own snapshot GET.
    act(() => {
      for (const socket of FakeWebSocket.instances) socket.serverOpen();
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const snapshotCalls = mockApiGet.mock.calls.filter(([path]) =>
      String(path).startsWith("/dashboard/activity"),
    );
    // F-03 wave 2 REPAIR (old -> new arithmetic): this pinned `2` sockets — one per consumer —
    // because each `useActivityFeed` mount ran its own connect effect. That is the defect the
    // finding is about (two transports on one page, three status words on one screen); the
    // transport is now module-level with `useSyncExternalStore` subscribers, so the arithmetic is
    // ONE shared socket + ONE shared snapshot read for two mounted views. The read count is
    // unchanged; only the socket count drops 2 -> 1.
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(snapshotCalls).toHaveLength(1);
    // Both views received nothing (the shared read resolved [] once) but stayed mounted.
    expect(screen.getByTestId("feed-strip")).toHaveTextContent("0");
    expect(screen.getByTestId("feed-feed")).toHaveTextContent("0");
  });

  it("the REAL dashboard pair (ActivityStrip + ActivityFeed) issues one snapshot read", async () => {
    render(
      <>
        <ActivityStrip />
        <ActivityFeed />
      </>,
    );
    await flushMint();
    act(() => {
      for (const socket of FakeWebSocket.instances) socket.serverOpen();
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const snapshotCalls = mockApiGet.mock.calls.filter(([path]) =>
      String(path).startsWith("/dashboard/activity"),
    );
    // F-03 wave 2 REPAIR (old -> new arithmetic): 2 sockets -> 1 shared transport socket; the
    // snapshot read stays 1 (it is read by the engine, not by each consumer).
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(snapshotCalls).toHaveLength(1);
  });

  it("a later consumer still performs its own read once the first request settled", async () => {
    const first = render(<FeedView label="first" />);
    await flushMint();
    act(() => {
      for (const socket of FakeWebSocket.instances) socket.serverOpen();
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    first.unmount();

    render(<FeedView label="second" />);
    await flushMint();
    act(() => {
      const latest = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
      latest.serverOpen();
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });

    const snapshotCalls = mockApiGet.mock.calls.filter(([path]) =>
      String(path).startsWith("/dashboard/activity"),
    );
    expect(snapshotCalls).toHaveLength(2);
  });
});
