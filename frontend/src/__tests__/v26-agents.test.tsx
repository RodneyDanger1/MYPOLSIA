/**
 * WAVE V26 / lane L1 — F1: the Agents page's mount / visibility contract.
 *
 * Measured live before the fix (parent, 2026-10-19, `wave-v26/live/probe-f1.json`): a `/agents`
 * tab forced `hidden` BEFORE hydration still showed the 9 skeletons and
 * `LOADING AGENT STATUS…` after 7 s, with 0 cards — the mount read was skipped by the V24/L5
 * hidden guard in `useAgentStatus`, and `setLoading(false)` lived only in the skipped `finally`.
 *
 * This file pins the repaired contract, each leg against the REAL hook/roster/page:
 *   - a hidden mount still performs EXACTLY ONE `GET /agents/status`, reaches a terminal
 *     `loading` state and renders the nine real cards;
 *   - hidden is not a polling clock: interval ticks while hidden add ZERO reads;
 *   - `visibilitychange` -> visible adds ONE immediate read (and none when one is already in
 *     flight — the hook's `inFlight` dedupe);
 *   - an answered-but-empty read says "no agents reported" — never a spinner, never an error;
 *   - a failed read is named and its Retry control really issues a second read.
 *
 * Test doubles: ONLY the `api` seam is doubled (`jest.mock("@/lib/api")`). The hook, the roster,
 * the toolbox read and the page all run for real — no product code is stubbed.
 */
import { act, fireEvent, render, renderHook, screen } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() } };
});

import { api } from "@/lib/api";
import AgentsPage from "@/app/agents/page";
import { useAgentStatus } from "@/hooks/useAgentStatus";

const mockGet = api.get as jest.Mock;

/** The nine registry rows `GET /agents/status` answers with (task_service.VALID_AGENT_TYPES). */
const STATUS_ROWS = [
  "orchestrator",
  "business_planning",
  "competitor_research",
  "social_media",
  "ads_management",
  "email_outreach",
  "code_generation",
  "customer_support",
  "finance",
].map((agent_type) => ({
  agent_type,
  last_run_at: null,
  last_run_status: null,
  tasks_today: 0,
  tasks_total: 0,
}));

/** The roster's toolbox read rides the same mount; an honest empty map is a real answer. */
const CAPABILITIES = {
  ok: true,
  data: {
    agents: STATUS_ROWS.map((row) => ({
      agent_type: row.agent_type,
      tools: [],
      action_classes: ["read"],
      channels: [],
    })),
    registry_tools: [],
  },
};

/** One resolve/reject handle — the only way to observe the true loading state deterministically. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** jsdom's `visibilityState` is a prototype getter; shadow it per test and restore afterwards. */
function setVisibility(state: "visible" | "hidden") {
  Object.defineProperty(document, "visibilityState", {
    configurable: true,
    get: () => state,
  });
}

/** `delete` removes the own shadowing property, so jsdom's own getter (visible) is back. */
function restoreVisibility() {
  delete (document as unknown as { visibilityState?: unknown }).visibilityState;
}

/** Reads of ONE path only — the toolbox read must never be miscounted as a status poll. */
function statusCalls() {
  return mockGet.mock.calls.filter(([path]) => path === "/agents/status").length;
}

function answerReads(status: () => Promise<unknown>) {
  mockGet.mockImplementation((path: string) =>
    path === "/agents/status" ? status() : Promise.resolve(CAPABILITIES),
  );
}

afterEach(() => {
  jest.useRealTimers();
  restoreVisibility();
  mockGet.mockReset();
});

describe("F1 — the mount read does not depend on tab visibility", () => {
  it("a hidden mount performs exactly ONE status read, reaches a terminal loading state, renders the nine cards, and refetches only when the tab becomes visible", async () => {
    jest.useFakeTimers();
    setVisibility("hidden");
    const first = deferred<unknown>();
    answerReads(() => first.promise);

    const { unmount } = render(<AgentsPage />);

    // RED at 298bb4a: this is 0 — the hidden guard skipped the mount read entirely.
    expect(statusCalls()).toBe(1);
    // The page's real loading copy. `.ui-label` is `text-transform: uppercase`, so the live
    // browser renders it as `LOADING AGENT STATUS…` (the string the parent measured live).
    const loadingLine = screen.getByText(/^loading agent status…$/i);
    expect(loadingLine).toHaveClass("ui-label");

    // A visibilitychange that arrives while the tab is STILL hidden is not a reason to read.
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(statusCalls()).toBe(1);

    await act(async () => {
      first.resolve(STATUS_ROWS);
    });

    // The hidden mount reached its terminal state and rendered the real roster.
    expect(screen.getAllByRole("article")).toHaveLength(9);
    expect(screen.getByText("Finance")).toBeInTheDocument();
    expect(screen.queryByText(/^loading agent status…$/i)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/no agents reported/i)).toBeNull();

    // Hidden is not a polling clock: three interval windows produce ZERO extra reads.
    await act(async () => {
      jest.advanceTimersByTime(90_000);
    });
    expect(statusCalls()).toBe(1);

    // Coming back to the tab is ONE immediate read...
    setVisibility("visible");
    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(statusCalls()).toBe(2);

    // ...and the interval clock is live again.
    await act(async () => {
      jest.advanceTimersByTime(30_000);
    });
    expect(statusCalls()).toBe(3);

    unmount();
    await act(async () => {
      jest.advanceTimersByTime(90_000);
    });
    expect(statusCalls()).toBe(3);
  });

  it("a visibilitychange while a read is already in flight adds no second request", async () => {
    jest.useFakeTimers();
    const inFlight = deferred<unknown>();
    answerReads(() => inFlight.promise);

    const { result } = renderHook(() => useAgentStatus(1000));
    await act(async () => {});
    expect(statusCalls()).toBe(1);
    expect(result.current.loading).toBe(true);

    await act(async () => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(statusCalls()).toBe(1);

    await act(async () => {
      inFlight.resolve(STATUS_ROWS);
    });
    expect(result.current.loading).toBe(false);
    expect(result.current.statuses).toEqual(STATUS_ROWS);
  });
});

describe("F1 — loading is terminal on every outcome", () => {
  it("a hidden mount reaches loading=false on success", async () => {
    setVisibility("hidden");
    answerReads(() => Promise.resolve(STATUS_ROWS));

    const { result } = renderHook(() => useAgentStatus(1000));
    expect(result.current.loading).toBe(true);

    await act(async () => {});

    expect(result.current.loading).toBe(false);
    expect(result.current.statuses).toEqual(STATUS_ROWS);
    expect(result.current.error).toBeNull();
  });

  it("reaches loading=false with the failure named when the read rejects", async () => {
    setVisibility("hidden");
    answerReads(() => Promise.reject(new Error("E_NETWORK: request timed out")));

    const { result } = renderHook(() => useAgentStatus(1000));
    await act(async () => {});

    expect(result.current.loading).toBe(false);
    expect(result.current.error).toContain("E_NETWORK: request timed out");
    expect(result.current.statuses).toEqual([]);
  });

  it("reaches loading=false on an empty answer — the empty read is not a stuck spinner", async () => {
    setVisibility("hidden");
    answerReads(() => Promise.resolve([]));

    const { result } = renderHook(() => useAgentStatus(1000));
    await act(async () => {});

    expect(result.current.loading).toBe(false);
    expect(result.current.statuses).toEqual([]);
    expect(result.current.error).toBeNull();
  });
});

describe("F1 — honest empty state and a working error retry", () => {
  it("an answered-but-empty read says 'no agents reported' — never a spinner, never an error", async () => {
    answerReads(() => Promise.resolve([]));

    render(<AgentsPage />);

    expect(await screen.findByText(/no agents reported/i)).toBeInTheDocument();
    expect(screen.queryByText(/^loading agent status…$/i)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryAllByRole("article")).toHaveLength(0);
    expect(statusCalls()).toBe(1);
  });

  it("names a failed read and its Retry control really issues a second read", async () => {
    answerReads(() => Promise.reject(new Error("E_NETWORK: request timed out")));

    render(<AgentsPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load agent status: E_NETWORK: request timed out");
    expect(statusCalls()).toBe(1);

    // The retry must go back to the wire, not just clear the message.
    answerReads(() => Promise.resolve(STATUS_ROWS));
    fireEvent.click(screen.getByRole("button", { name: /^retry$/i }));

    expect(await screen.findByText("Finance")).toBeInTheDocument();
    expect(statusCalls()).toBe(2);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
