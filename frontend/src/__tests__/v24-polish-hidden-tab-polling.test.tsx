import { act, renderHook } from "@testing-library/react";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api } from "@/lib/api";
import { useAgentStatus } from "@/hooks/useAgentStatus";

const mockApiGet = api.get as jest.MockedFunction<typeof api.get>;

/** jsdom's visibilityState is a prototype getter; override it per test and restore after. */
function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
}

beforeEach(() => {
  jest.useFakeTimers();
  mockApiGet.mockReset();
  mockApiGet.mockResolvedValue([]);
});

afterEach(() => {
  jest.useRealTimers();
  setVisibility("visible");
});

describe("useAgentStatus — a hidden tab does not poll the dashboard's status read", () => {
  it("skips interval ticks while hidden and resumes when visible", async () => {
    renderHook(() => useAgentStatus(1000));
    await act(async () => {});

    const mounted = mockApiGet.mock.calls.length;
    expect(mounted).toBe(1);

    setVisibility("hidden");
    await act(async () => {
      jest.advanceTimersByTime(3000);
    });
    expect(mockApiGet.mock.calls.length).toBe(mounted);

    setVisibility("visible");
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });
    expect(mockApiGet.mock.calls.length).toBe(mounted + 1);
  });

  it("a tick that starts while visible still lands normally", async () => {
    renderHook(() => useAgentStatus(1000));
    await act(async () => {});
    await act(async () => {
      jest.advanceTimersByTime(1000);
    });
    expect(mockApiGet.mock.calls.length).toBe(2);
  });
});
