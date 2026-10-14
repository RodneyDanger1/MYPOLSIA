import { render, screen, act } from "@testing-library/react";
import { ActivityFeed } from "@/components/dashboard/ActivityFeed";

// Mock WebSocket
let mockWsInstance: {
  onopen: (() => void) | null;
  onmessage: ((e: { data: string }) => void) | null;
  onclose: (() => void) | null;
  onerror: (() => void) | null;
  close: jest.Mock;
  readyState: number;
};

const MockWebSocket = jest.fn().mockImplementation(() => {
  mockWsInstance = {
    onopen: null,
    onmessage: null,
    onclose: null,
    onerror: null,
    close: jest.fn(),
    readyState: 1,
  };
  return mockWsInstance;
});

(global as any).WebSocket = MockWebSocket;

describe("ActivityFeed", () => {
  beforeEach(() => {
    MockWebSocket.mockClear();
    // The hook pulls a REST snapshot when the socket opens; jsdom has no network. Stub it so these
    // tests exercise the intended path (the snapshot-FAILURE surface has its own test in
    // u1-realtime.test.tsx, where the hook's error states live).
    (global as any).fetch = jest.fn(async () => ({
      ok: true,
      status: 200,
      statusText: "OK",
      headers: { get: () => null },
      json: async () => [],
      text: async () => "[]",
    }));
  });

  // UPDATED for the honest-state change (Q-07): the "No activity yet" copy is a claim that now
  // requires a LIVE socket. Old behavior pinned: empty copy rendered before any connection.
  it("claims the empty-but-fine copy only once live; CONNECTING before that", () => {
    render(<ActivityFeed />);
    expect(screen.getByText(/CONNECTING/)).toBeInTheDocument();
    expect(screen.queryByText(/No activity yet/i)).toBeNull();

    act(() => { mockWsInstance.onopen?.(); });
    expect(screen.getByText(/No activity yet/i)).toBeInTheDocument();
  });

  it("shows 'LIVE' status when connected", () => {
    render(<ActivityFeed />);
    act(() => { mockWsInstance.onopen?.(); });
    expect(screen.getByText("LIVE")).toBeInTheDocument();
  });

  it("renders events received via WebSocket", async () => {
    render(<ActivityFeed />);
    act(() => { mockWsInstance.onopen?.(); });

    const event = {
      id: 1,
      agent_type: "social_media",
      action: "post_tweet",
      summary: "Posted a tweet about our launch",
      level: "success",
      created_at: new Date().toISOString(),
    };

    act(() => {
      mockWsInstance.onmessage?.({ data: JSON.stringify(event) });
    });

    expect(screen.getByText("social_media")).toBeInTheDocument();
    expect(screen.getByText("Posted a tweet about our launch")).toBeInTheDocument();
  });

  it("shows reconnecting state when disconnected", () => {
    render(<ActivityFeed />);
    act(() => { mockWsInstance.onclose?.(); });
    expect(screen.getByText(/Reconnecting/i)).toBeInTheDocument();
  });

  it("renders the placeholder for an event whose created_at is null on the wire", async () => {
    render(<ActivityFeed />);
    act(() => { mockWsInstance.onopen?.(); });

    // `ActivityEventResponse.created_at` is `datetime | None` (`app/schemas/report.py`) — the column
    // is nullable — so a null really arrives, and it must not reach `new Date(null)` (1/1/1970).
    const event = {
      id: 2,
      agent_type: "finance",
      action: "snapshot",
      summary: "Revenue captured without a timestamp",
      level: "info",
      created_at: null,
    };

    act(() => {
      mockWsInstance.onmessage?.({ data: JSON.stringify(event) });
    });

    expect(screen.getByText("Revenue captured without a timestamp")).toBeInTheDocument();
    expect(screen.getByText("date unknown")).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
  });

  it("renders a level this build does not know instead of dropping the event", async () => {
    const { container } = render(<ActivityFeed />);
    act(() => { mockWsInstance.onopen?.(); });

    // `level` is a plain `str` server-side (`app/schemas/report.py` only normalises NULL -> "info"),
    // so an unknown word is a legal wire value: the lookup tables fall through to their gray
    // branches and the event still renders.
    const event = {
      id: 3,
      agent_type: "ads_management",
      action: "budget_shift",
      summary: "Budget shifted with an unrecognised level",
      level: "critical",
      created_at: new Date().toISOString(),
    };

    act(() => {
      mockWsInstance.onmessage?.({ data: JSON.stringify(event) });
    });

    expect(screen.getByText("Budget shifted with an unrecognised level")).toBeInTheDocument();
    expect(container.querySelector("span.bg-gray-400")).not.toBeNull(); // dot fallback
    // UPDATED for the light-theme restyle: the label fallback class is now gray-500 (was gray-300,
    // which is illegible on the white feed surface).
    expect(container.querySelector("span.text-gray-500")).not.toBeNull(); // label fallback
  });
});
