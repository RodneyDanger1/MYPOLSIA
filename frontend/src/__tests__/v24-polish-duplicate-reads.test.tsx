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
  (globalThis as { fetch: unknown }).fetch = jest.fn().mockResolvedValue({
    ok: true,
    status: 200,
    headers: { get: () => null },
    json: async () => [],
  });
  mockApiGet.mockReset();
  mockApiGet.mockResolvedValue([]);
});

afterEach(() => {
  (globalThis as { WebSocket?: unknown }).WebSocket = realWebSocket;
  (globalThis as { fetch?: unknown }).fetch = realFetch;
});

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
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(snapshotCalls).toHaveLength(1);
    // Both views received nothing (the shared read resolved [] twice) but stayed mounted.
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
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(snapshotCalls).toHaveLength(1);
  });

  it("a later consumer still performs its own read once the first request settled", async () => {
    const first = render(<FeedView label="first" />);
    act(() => {
      for (const socket of FakeWebSocket.instances) socket.serverOpen();
    });
    await act(async () => {
      await Promise.resolve();
      await Promise.resolve();
    });
    first.unmount();

    render(<FeedView label="second" />);
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
