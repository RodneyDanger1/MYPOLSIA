import { act, render, renderHook, screen } from "@testing-library/react";
import { useState } from "react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { ...actual.api, get: jest.fn(), post: jest.fn() } };
});

import { api } from "@/lib/api";
import { useRefreshOnFocus, REFRESH_THROTTLE_MS } from "@/hooks/useRefreshOnFocus";
import FinancePage from "@/app/finance/page";
import SocialPage from "@/app/social/page";
import SupportPage from "@/app/support/page";
import UsagePage from "@/app/usage/page";
import CapabilitiesPage from "@/app/capabilities/page";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/** Flush the microtask queue inside act() so settled reads commit their state. */
async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
  });
}

function focusWindow() {
  act(() => {
    window.dispatchEvent(new Event("focus"));
  });
}

function advance(ms: number) {
  act(() => {
    jest.advanceTimersByTime(ms);
  });
}

beforeEach(() => {
  jest.useFakeTimers();
  jest.setSystemTime(new Date("2026-10-19T12:00:00.000Z"));
  mockApiGet.mockReset();
});

afterEach(() => {
  jest.useRealTimers();
});

describe("useRefreshOnFocus — the shared focus/visibility re-read", () => {
  it("does not re-read on focus inside the throttle window, and does after it", () => {
    function Harness() {
      const [count, setCount] = useState(0);
      useRefreshOnFocus(() => setCount((n) => n + 1));
      return <span data-testid="count">{count}</span>;
    }

    render(<Harness />);
    expect(screen.getByTestId("count")).toHaveTextContent("0");

    focusWindow();
    expect(screen.getByTestId("count")).toHaveTextContent("0");

    advance(REFRESH_THROTTLE_MS - 1);
    focusWindow();
    expect(screen.getByTestId("count")).toHaveTextContent("0");

    advance(1);
    focusWindow();
    expect(screen.getByTestId("count")).toHaveTextContent("1");

    // Immediately after a throttled re-read another focus is still silent.
    focusWindow();
    expect(screen.getByTestId("count")).toHaveTextContent("1");
  });

  it("treats becoming visible again as a focus (visibilitychange), still throttled", () => {
    const onChange = jest.fn();
    renderHook(() => useRefreshOnFocus(onChange));

    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(onChange).not.toHaveBeenCalled();

    act(() => {
      jest.advanceTimersByTime(REFRESH_THROTTLE_MS);
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(onChange).toHaveBeenCalledTimes(1);
  });

  it("never fires while the document is hidden", () => {
    const onChange = jest.fn();
    const own = Object.getOwnPropertyDescriptor(document, "visibilityState");
    Object.defineProperty(document, "visibilityState", { configurable: true, get: () => "hidden" });
    try {
      renderHook(() => useRefreshOnFocus(onChange));
      act(() => {
        jest.advanceTimersByTime(REFRESH_THROTTLE_MS);
        document.dispatchEvent(new Event("visibilitychange"));
      });
      expect(onChange).not.toHaveBeenCalled();
    } finally {
      if (own) Object.defineProperty(document, "visibilityState", own);
      else delete (document as unknown as Record<string, unknown>).visibilityState;
    }
  });
});

describe("list pages re-read on focus after the 30 s throttle", () => {
  it("Social: one read on mount, no read on an immediate focus, one more after 30 s", async () => {
    mockApiGet.mockResolvedValue([]);
    render(<SocialPage />);
    await flush();
    expect(mockApiGet).toHaveBeenCalledTimes(1);

    focusWindow();
    await flush();
    expect(mockApiGet).toHaveBeenCalledTimes(1);

    advance(REFRESH_THROTTLE_MS);
    focusWindow();
    await flush();
    expect(mockApiGet).toHaveBeenCalledTimes(2);
    expect(mockApiGet).toHaveBeenLastCalledWith("/social/posts?limit=50");
  });

  it("Support: focus after 30 s re-reads the inbox", async () => {
    mockApiGet.mockResolvedValue([]);
    render(<SupportPage />);
    await flush();
    expect(mockApiGet).toHaveBeenCalledTimes(1);

    advance(REFRESH_THROTTLE_MS);
    focusWindow();
    await flush();
    expect(mockApiGet).toHaveBeenCalledTimes(2);
  });

  it("Usage: focus after 30 s re-reads the current page without resetting it", async () => {
    mockApiGet.mockResolvedValue({ items: [], total: 0 });
    render(<UsagePage />);
    await flush();
    const ledger = () =>
      mockApiGet.mock.calls.filter(([path]) => String(path).startsWith("/runs?")).length;
    // The page's own ledger read plus the Ops panel's three reads.
    expect(ledger()).toBe(1);
    const opsCalls = mockApiGet.mock.calls.filter(([path]) => String(path).startsWith("/ops/")).length;
    expect(opsCalls).toBe(3);

    advance(REFRESH_THROTTLE_MS);
    focusWindow();
    await flush();
    expect(ledger()).toBe(2);
    // Only the ledger re-reads on focus; the Ops panel keeps its explicit Refresh.
    expect(mockApiGet.mock.calls.filter(([path]) => String(path).startsWith("/ops/")).length).toBe(3);
  });

  it("Capabilities: focus after 30 s re-reads the map", async () => {
    mockApiGet.mockResolvedValue({ agents: [] });
    render(<CapabilitiesPage />);
    await flush();
    expect(mockApiGet).toHaveBeenCalledTimes(1);

    advance(REFRESH_THROTTLE_MS);
    focusWindow();
    await flush();
    expect(mockApiGet).toHaveBeenCalledTimes(2);
  });

  it("Finance: one focus re-reads every region of the page exactly once", async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path === "/finance/summary") {
        return Promise.resolve({
          mrr_cents: null,
          arr_cents: null,
          active_subscribers: null,
          total_ad_spend_usd: null,
          total_expenses_month_cents: null,
          stripe_balance_cents: null,
          last_snapshot_date: "2026-10-19",
        });
      }
      if (path === "/config") return Promise.resolve({ human_hourly_rate: null });
      if (path === "/finance/planning") return Promise.resolve({});
      if (path === "/economics/context") return Promise.resolve({});
      return Promise.resolve([]);
    });
    render(<FinancePage />);
    await flush();
    const callsOnMount = mockApiGet.mock.calls.length;
    expect(callsOnMount).toBe(7);

    const summaryCalls = () =>
      mockApiGet.mock.calls.filter(([path]) => path === "/finance/summary").length;
    expect(summaryCalls()).toBe(1);

    advance(REFRESH_THROTTLE_MS);
    focusWindow();
    await flush();
    expect(summaryCalls()).toBe(2);
    expect(mockApiGet.mock.calls.length).toBe(callsOnMount * 2);
  });
});
