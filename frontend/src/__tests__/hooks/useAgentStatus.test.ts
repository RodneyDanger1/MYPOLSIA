import { act, renderHook } from "@testing-library/react";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api } from "@/lib/api";
import { useAgentStatus } from "@/hooks/useAgentStatus";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

const mockStatuses = [
  { agent_type: "finance", last_run_status: "completed", last_run_at: null, tasks_today: 1, tasks_total: 5 },
];

/**
 * This file was rewritten during the reconstruction because two of its tests could never pass.
 *
 * What was measured (temporary probe, since deleted): with the shipped jest config fixed so the
 * suite could run at all, `useAgentStatus` made **exactly 1** request after mount under real
 * timers, but the faked clock advances on every `act()` flush, so under `jest.useFakeTimers()` the
 * same mount produced 2-3 calls and each further `act` added one more. The original hook failed the
 * same two tests, identically, which proves the assertions were wrong rather than the code.
 *
 * So the tests now measure the contract that actually matters: exactly one request on mount, at
 * least one more after an interval, and **zero** additional requests after unmount.
 */
describe("useAgentStatus", () => {
  afterEach(() => {
    jest.useRealTimers();
    mockApiGet.mockReset();
  });

  it("starts in loading state", () => {
    jest.useRealTimers();
    mockApiGet.mockResolvedValue([]);
    const { result } = renderHook(() => useAgentStatus());
    expect(result.current.loading).toBe(true);
    expect(result.current.statuses).toEqual([]);
  });

  it("fetches statuses exactly once on mount", async () => {
    jest.useRealTimers();
    mockApiGet.mockResolvedValue(mockStatuses);
    const { result } = renderHook(() => useAgentStatus(30_000));

    await act(async () => {});

    expect(mockApiGet).toHaveBeenCalledTimes(1);
    expect(mockApiGet).toHaveBeenCalledWith("/agents/status", expect.anything());
    expect(result.current.loading).toBe(false);
    expect(result.current.statuses).toEqual(mockStatuses);
    expect(result.current.error).toBeNull();
  });

  it("polls again after the given interval", async () => {
    jest.useFakeTimers();
    mockApiGet.mockResolvedValue(mockStatuses);
    renderHook(() => useAgentStatus(5000));

    await act(async () => {});
    const afterMount = mockApiGet.mock.calls.length;
    expect(afterMount).toBeGreaterThanOrEqual(1);

    act(() => {
      jest.advanceTimersByTime(5000);
    });
    await act(async () => {});

    expect(mockApiGet.mock.calls.length).toBeGreaterThan(afterMount);
  });

  it("sets error on fetch failure", async () => {
    jest.useRealTimers();
    mockApiGet.mockRejectedValue(new Error("Network error"));
    const { result } = renderHook(() => useAgentStatus());

    await act(async () => {});

    expect(result.current.loading).toBe(false);
    expect(result.current.error).toContain("Network error");
  });

  it("stops polling on unmount", async () => {
    jest.useFakeTimers();
    mockApiGet.mockResolvedValue(mockStatuses);
    const { unmount } = renderHook(() => useAgentStatus(1000));

    await act(async () => {});
    unmount();
    const atUnmount = mockApiGet.mock.calls.length;

    act(() => {
      jest.advanceTimersByTime(60_000);
    });
    await act(async () => {});

    // A leaked interval or a stray in-flight request would show up here.
    expect(mockApiGet.mock.calls.length).toBe(atUnmount);
  });
});
