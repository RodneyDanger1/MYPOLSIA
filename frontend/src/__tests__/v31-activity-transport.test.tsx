/**
 * WAVE 2 / F-03 — ONE activity transport per page, and a status that cannot lie.
 *
 * The measured defect (Visuals F-03 + `visuals/raw/net/ws-probe.json`): the root layout mounts
 * `ActivityStrip` and `/dashboard` mounts `ActivityFeed`; each `useActivityFeed` consumer ran its
 * own connect effect, so one page opened TWO sockets and could render THREE status words
 * (`LIVE`, `LIVE`, `RECONNECTING`) — a green LIVE badge still showing after the backend rejected
 * every handshake (its 4-sockets-per-proxy-IP cap).  A green badge beside a refused socket is a
 * lying control, and two transports on one page are the reason it could stay green while another
 * view grieved.
 *
 * These tests pin the fixed contract:
 *   1. two consumers -> ONE ticket mint, ONE socket, one snapshot read;
 *   2. the views agree: both report the same status word, and a refused/closed handshake moves
 *      BOTH off `live` (no green LIVE survives a refusal);
 *   3. unmounting one consumer does NOT tear the page's socket down; the LAST one does;
 *   4. a page reconnects ONCE per failure, not once per mounted view.
 *
 * The fake WebSocket + fetch stub are TEST INFRASTRUCTURE only (operating contract §7: a
 * third-party transport is a permitted double); the production hook runs its real logic.
 */
import { act, render, screen } from "@testing-library/react";

import { ActivityFeed } from "@/components/dashboard/ActivityFeed";
import { ActivityStrip } from "@/components/shell/ActivityStrip";
import { useActivityFeed } from "@/hooks/useActivityFeed";

/* --------------------------------------------------------------------------------------------- */
class FakeWebSocket {
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSING = 2;
  static CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState: number = FakeWebSocket.CONNECTING;
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
    if (this.readyState === FakeWebSocket.CLOSED || this.readyState === FakeWebSocket.CLOSING) return;
    this.readyState = FakeWebSocket.CLOSED;
    setTimeout(() => this.onclose?.({ code: 1006, reason: "" }), 0);
  };

  serverOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.();
  }
  serverClose(code = 4003, reason = ""): void {
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.({ code, reason });
  }
}

function lastSocket(): FakeWebSocket {
  const socket = FakeWebSocket.instances[FakeWebSocket.instances.length - 1];
  if (!socket) throw new Error("no FakeWebSocket was constructed");
  return socket;
}

function jsonResponse(body: unknown) {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  };
}

const realWebSocket = (globalThis as { WebSocket?: unknown }).WebSocket;
const realFetch = (globalThis as { fetch?: unknown }).fetch;

let ticketCalls = 0;
let activityReads = 0;

function installFetch(): void {
  (globalThis as { fetch: unknown }).fetch = jest.fn(async (url: string) => {
    if (String(url).includes("/ws/ticket")) {
      ticketCalls += 1;
      return jsonResponse({ ticket: "test-ticket", expires_in: 30 });
    }
    if (String(url).includes("/dashboard/activity")) {
      activityReads += 1;
      return jsonResponse([]);
    }
    return jsonResponse([]);
  });
}

async function flushMint(): Promise<void> {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function Probe({ label }: { label: string }) {
  const feed = useActivityFeed();
  return (
    <div data-testid={`probe-${label}`} data-status={feed.status} data-attempts={feed.attempts}>
      {feed.error ?? "no-error"}
    </div>
  );
}

function probe(label: string): HTMLElement {
  return screen.getByTestId(`probe-${label}`);
}

beforeEach(() => {
  FakeWebSocket.instances = [];
  (globalThis as { WebSocket: unknown }).WebSocket = FakeWebSocket;
  ticketCalls = 0;
  activityReads = 0;
  installFetch();
});

afterEach(() => {
  jest.useRealTimers();
  jest.restoreAllMocks();
  (globalThis as { WebSocket?: unknown }).WebSocket = realWebSocket;
  (globalThis as { fetch?: unknown }).fetch = realFetch;
});

/* --------------------------------------------------------------------------------------------- */
describe("F-03 — one transport per page", () => {
  it("two consumers mint ONE ticket, open ONE socket and read ONE snapshot", async () => {
    render(
      <>
        <Probe label="a" />
        <Probe label="b" />
      </>,
    );
    await flushMint();

    expect(ticketCalls).toBe(1);
    expect(FakeWebSocket.instances).toHaveLength(1);

    act(() => lastSocket().serverOpen());
    expect(probe("a").getAttribute("data-status")).toBe("live");
    expect(probe("b").getAttribute("data-status")).toBe("live");
    await flushMint();
    expect(activityReads).toBe(1);
  });

  it("a refused handshake moves BOTH views off live — no green LIVE survives it", async () => {
    render(
      <>
        <Probe label="a" />
        <Probe label="b" />
        <ActivityStrip />
        <ActivityFeed />
      </>,
    );
    await flushMint();
    // Nothing has opened. The page must not claim anything is live, and there is exactly one
    // socket to refuse.
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(screen.queryByText("LIVE")).toBeNull();

    // The backend refuses the upgrade (the live box answered 403 once the 4-per-IP cap was hit).
    act(() => lastSocket().serverClose(4003, "socket cap"));

    for (const id of ["probe-a", "probe-b"]) {
      expect(screen.getByTestId(id).getAttribute("data-status")).toBe("reconnecting");
      expect(screen.getByTestId(id).getAttribute("data-attempts")).toBe("1");
      expect(screen.getByTestId(id)).toHaveTextContent("connection closed (4003: socket cap)");
    }
    // Both REAL dashboard views agree, and neither wears the green badge.
    expect(document.querySelector('[data-testid="activity-strip"]')?.getAttribute("data-status")).toBe(
      "reconnecting",
    );
    expect(document.querySelector('[data-testid="activity-feed"]')?.getAttribute("data-status")).toBe(
      "reconnecting",
    );
    expect(screen.queryByText("LIVE")).toBeNull();
    expect(screen.getAllByText(/RECONNECTING \(ATTEMPT 1\)/)).toHaveLength(2);
    // The strip and the feed both name the real reason (two views of ONE error, not two errors).
    const alerts = screen.getAllByRole("alert");
    expect(alerts).toHaveLength(2);
    for (const alert of alerts) {
      expect(alert).toHaveTextContent(/connection closed \(4003: socket cap\)/);
    }
  });

  it("one view unmounting keeps the page socket; the LAST one closes it", async () => {
    const view = render(
      <>
        <Probe label="a" />
        <Probe label="b" />
      </>,
    );
    await flushMint();
    const socket = lastSocket();
    act(() => socket.serverOpen());
    expect(socket.readyState).toBe(FakeWebSocket.OPEN);

    // The dashboard hides one widget: the shared transport must survive for the other view.
    view.rerender(<Probe label="b" />);
    expect(probe("b").getAttribute("data-status")).toBe("live");
    expect(socket.readyState).toBe(FakeWebSocket.OPEN);
    expect(FakeWebSocket.instances).toHaveLength(1);

    // The last consumer leaves: the page's socket is closed.
    view.unmount();
    expect(socket.readyState).toBe(FakeWebSocket.CLOSED);
  });

  it("a page reconnects ONCE per failure, not once per mounted view", async () => {
    jest.useFakeTimers();
    jest.spyOn(Math, "random").mockReturnValue(0.5); // deterministic jitter -> 750ms first retry
    render(
      <>
        <Probe label="a" />
        <Probe label="b" />
      </>,
    );
    await flushMint();
    act(() => lastSocket().serverOpen());
    act(() => lastSocket().serverClose(1006));

    await act(async () => {
      await jest.advanceTimersByTimeAsync(1_000);
    });
    await flushMint();

    // Two views, one failure, one new socket attempt. (The old per-consumer transports produced
    // two — and, with the proxy cap, a refusal storm.)
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(probe("a").getAttribute("data-attempts")).toBe("1");
    expect(probe("b").getAttribute("data-attempts")).toBe("1");
  });
});
