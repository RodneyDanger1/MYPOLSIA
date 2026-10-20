import { act, renderHook } from "@testing-library/react";

jest.mock("@/lib/api", () => ({ api: { get: jest.fn() } }));

import { api } from "@/lib/api";
import { useAutoMode } from "@/components/auto_mode/useAutoMode";

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
  mockApiGet.mockResolvedValue({ active: false, session: null });
});

afterEach(() => {
  jest.useRealTimers();
  setVisibility("visible");
});

/**
 * V28 / L1 — the `/auto-mode` poll is the dashboard's only remaining ungated interval.
 *
 * The V24/L5 rule every other poller implements (`hooks/useVisibilityPoll`, `hooks/useAgentStatus`)
 * is: a hidden tab is not watching, so its interval ticks are SKIPPED — the request is not made
 * for a surface nobody can see. The MOUNT read stays exempt, because the panel must render the
 * same state whether it was opened in front or in the background.
 */
describe("useAutoMode — a hidden tab does not poll /auto-mode", () => {
  it("skips interval ticks while hidden and reads again once visible", async () => {
    setVisibility("hidden");
    renderHook(() => useAutoMode());
    await act(async () => {});

    // The mount read is exempt on purpose.
    expect(mockApiGet).toHaveBeenCalledTimes(1);
    expect(mockApiGet).toHaveBeenCalledWith("/auto-mode");

    await act(async () => {
      jest.advanceTimersByTime(180_000);
    });
    expect(mockApiGet).toHaveBeenCalledTimes(1);

    setVisibility("visible");
    await act(async () => {
      jest.advanceTimersByTime(60_000);
    });
    expect(mockApiGet).toHaveBeenCalledTimes(2);
  });

  it("a tick that starts while visible still lands normally", async () => {
    renderHook(() => useAutoMode());
    await act(async () => {});
    await act(async () => {
      jest.advanceTimersByTime(60_000);
    });
    expect(mockApiGet).toHaveBeenCalledTimes(2);
  });
});
