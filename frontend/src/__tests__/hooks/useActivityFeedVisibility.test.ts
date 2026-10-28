import { act, renderHook } from "@testing-library/react";

/**
 * F-41 — the socket engine must respect `visibilitychange`.
 *
 * Measured defect at HEAD: `reportFailure` always scheduled `connect()` on the backoff, so a tab
 * left in the background kept a dead socket retrying forever (every attempt invisible, every
 * attempt a real request). The engine's lifecycle also never touched `document`, so nothing could
 * pause it.
 *
 * Pins: (1) a close while the tab is HIDDEN schedules nothing, however long the timers run;
 * (2) becoming VISIBLE reconnects at once with the backoff counter RESET (the next retry is the
 * first-attempt delay, not the storm's); (3) the listener is added once and removed on unmount,
 * and a post-unmount `visibilitychange` opens no socket.
 */
type MockSocket = {
  url: string;
  readyState: number;
  onopen: (() => void) | null;
  onmessage: ((e: { data: string }) => void) | null;
  onclose: ((e?: CloseEvent) => void) | null;
  onerror: (() => void) | null;
  close: jest.Mock;
};

const sockets: MockSocket[] = [];

function MockWebSocket(this: unknown, url: string): MockSocket {
  const socket: MockSocket = {
    url,
    readyState: 0,
    onopen: null,
    onmessage: null,
    onclose: null,
    onerror: null,
    close: jest.fn(),
  };
  sockets.push(socket);
  return socket;
}
(MockWebSocket as unknown as { CONNECTING: number }).CONNECTING = 0;
(MockWebSocket as unknown as { OPEN: number }).OPEN = 1;
(MockWebSocket as unknown as { CLOSING: number }).CLOSING = 2;
(MockWebSocket as unknown as { CLOSED: number }).CLOSED = 3;
(global as unknown as { WebSocket: unknown }).WebSocket = MockWebSocket;

import { useActivityFeed, backoffDelay } from "@/hooks/useActivityFeed";
import { api } from "@/lib/api";

/** F-46: the hook mints a ticket over REST before it opens the socket, so every socket assertion
 *  needs the mint's promise flushed first. This also pins the contract the server now enforces:
 *  the handshake URL carries `?ticket=` and NEVER `?key=`. */
async function flushMint(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function setVisibility(state: "visible" | "hidden"): void {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
}

function fireVisibilityChange(): void {
  act(() => {
    document.dispatchEvent(new Event("visibilitychange"));
  });
}

function closeLatest(): void {
  const socket = sockets[sockets.length - 1];
  act(() => {
    socket.onclose?.({ code: 1006, reason: "" } as CloseEvent);
  });
}

beforeEach(() => {
  jest.useFakeTimers();
  sockets.length = 0;
  setVisibility("visible");
  jest.spyOn(api, "post").mockResolvedValue({ ticket: "v1.test.ticket" } as never);
});

afterEach(() => {
  jest.useRealTimers();
  setVisibility("visible");
  jest.restoreAllMocks();
});

describe("F-41 — a background tab does not retry forever", () => {
  it("schedules no reconnect while hidden, and counts the failure honestly", async () => {
    setVisibility("hidden");
    const { result } = renderHook(() => useActivityFeed(100, { snapshot: false }));
    await flushMint();
    expect(sockets).toHaveLength(1);

    // F-46: the handshake carries a short-lived ticket and never the key.
    expect(sockets[0].url).toContain("?ticket=");
    expect(sockets[0].url).not.toContain("key=");

    closeLatest();
    expect(result.current.status).toBe("reconnecting");
    expect(result.current.attempts).toBe(1);

    // Ten minutes of a background tab: the old engine opened ~20 more sockets here.
    act(() => {
      jest.advanceTimersByTime(10 * 60_000);
    });
    expect(sockets).toHaveLength(1);
    expect(result.current.attempts).toBe(1);
  });

  it("resumes on visible at once, with the backoff reset", async () => {
    setVisibility("hidden");
    const { result } = renderHook(() => useActivityFeed(100, { snapshot: false }));
    await flushMint();
    closeLatest();
    act(() => {
      jest.advanceTimersByTime(60_000);
    });
    expect(sockets).toHaveLength(1);

    // Back in front: one immediate attempt, no waiting out the old backoff.
    setVisibility("visible");
    fireVisibilityChange();
    await flushMint();
    expect(sockets).toHaveLength(2);

    act(() => {
      sockets[1].onopen?.();
    });
    expect(result.current.status).toBe("live");

    // A new failure after the pause starts from the FIRST-attempt delay (≤ 1 s), not from an
    // attempt count inherited from the storm the tab slept through.
    closeLatest();
    act(() => {
      jest.advanceTimersByTime(1_000);
    });
    await flushMint();
    expect(sockets).toHaveLength(3);
    expect(backoffDelay(0)).toBeLessThanOrEqual(1_000);
  });

  it("adds the listener once, removes it on unmount, and goes quiet afterwards", async () => {
    const add = jest.spyOn(document, "addEventListener");
    const remove = jest.spyOn(document, "removeEventListener");

    const { unmount } = renderHook(() => useActivityFeed(100, { snapshot: false }));
    await flushMint();
    const added = add.mock.calls.filter(([type]) => type === "visibilitychange");
    expect(added).toHaveLength(1);
    const handler = added[0][1];

    unmount();
    expect(remove).toHaveBeenCalledWith("visibilitychange", handler);

    // Nothing wakes up after unmount.
    fireVisibilityChange();
    expect(sockets).toHaveLength(1);

    add.mockRestore();
    remove.mockRestore();
  });
});
