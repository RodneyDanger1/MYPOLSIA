import { act, renderHook } from "@testing-library/react";
import { useActivityFeed } from "@/hooks/useActivityFeed";

let mockWsInstance: {
  onopen: (() => void) | null;
  onmessage: ((e: { data: string }) => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
  close: jest.Mock;
  readyState: number;
};

const MockWebSocket = jest.fn().mockImplementation(() => {
  mockWsInstance = { onopen: null, onmessage: null, onclose: null, onerror: null, close: jest.fn(), readyState: 0 };
  return mockWsInstance;
});

(global as any).WebSocket = MockWebSocket;

async function flushMint(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

async function mount() {
  const rendered = renderHook(() => useActivityFeed());
  await flushMint();
  return rendered;
}

describe("useActivityFeed", () => {
  beforeEach(() => {
    MockWebSocket.mockClear();
    // F-46: the hook mints a ticket before it opens the socket.
    (global as any).fetch = jest.fn(async (url: string) =>
      String(url).includes("/ws/ticket")
        ? { ok: true, status: 200, headers: { get: () => null }, json: async () => ({ ticket: "test-ticket", expires_in: 30 }) }
        : { ok: true, status: 200, headers: { get: () => null }, json: async () => [] },
    );
  });

  it("initialises with empty events and disconnected state", async () => {
    const { result } = await mount();
    expect(result.current.events).toEqual([]);
    expect(result.current.connected).toBe(false);
  });

  it("sets connected=true on WebSocket open", async () => {
    const { result } = await mount();
    act(() => { mockWsInstance.onopen?.(); });
    expect(result.current.connected).toBe(true);
  });

  it("adds events when messages arrive", async () => {
    const { result } = await mount();
    act(() => { mockWsInstance.onopen?.(); });

    const event = { id: 1, agent_type: "finance", action: "snapshot", summary: "Revenue captured", level: "info", created_at: new Date().toISOString() };
    act(() => { mockWsInstance.onmessage?.({ data: JSON.stringify(event) }); });

    expect(result.current.events).toHaveLength(1);
    expect(result.current.events[0].summary).toBe("Revenue captured");
  });

  it("sets connected=false on WebSocket close", async () => {
    const { result } = await mount();
    act(() => { mockWsInstance.onopen?.(); });
    act(() => { mockWsInstance.onclose?.(); });
    expect(result.current.connected).toBe(false);
  });

  it("ignores malformed WebSocket messages", async () => {
    const { result } = await mount();
    act(() => { mockWsInstance.onopen?.(); });
    act(() => { mockWsInstance.onmessage?.({ data: "not json {{{" }); });
    expect(result.current.events).toHaveLength(0);
  });
});
