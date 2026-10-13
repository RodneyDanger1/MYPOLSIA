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
  });

  it("renders empty state when no events", () => {
    render(<ActivityFeed />);
    expect(screen.getByText(/No activity yet/i)).toBeInTheDocument();
  });

  it("shows 'Live' status when connected", () => {
    render(<ActivityFeed />);
    act(() => { mockWsInstance.onopen?.(); });
    expect(screen.getByText("Live")).toBeInTheDocument();
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
    expect(container.querySelector("span.text-gray-300")).not.toBeNull(); // label fallback
  });
});
