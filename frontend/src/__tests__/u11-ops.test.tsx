import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { NOT_MEASURED } from "@/components/dashboard/format";
import type { OpsQualityRow } from "@/components/usage/useOpsRead";
import UsagePage from "@/app/usage/page";

/**
 * Wave V11 unit 2 — seat W6's suite for the Usage page's OPS panel (ops.py's read/reset surface
 * plus the per-agent quality read; wiring audit F5, capability audit B1/B2).
 *
 * Scope discipline: run with `node node_modules/jest/bin/jest.js --ci u11-ops`.
 *
 * What is pinned here:
 *  - the panel is MOUNTED on the live Usage page and reads the REAL paths:
 *    GET /ops/agent-quality, GET /ops/events/stats, POST /ops/events/reset (never a mock or a
 *    placeholder path);
 *  - the null-vs-zero law per quality cell: a null success_rate / last_failure_at /
 *    cost_per_completed_task renders the app's markers, a MEASURED zero stays a reading, and a
 *    measured sub-cent average keeps its 4-decimal precision;
 *  - honest states: both reads load as busy skeletons (chrome free of the unmeasured marker), a
 *    failure is NAMED per read (role=alert, ApiError.display) and does not take the sibling card
 *    down with it, an empty agent list is a sentence, and a hostile body is named, not rendered;
 *  - the breaker card states the PROCESS BOUNDARY (counters live in the API process; the worker
 *    keeps its own) and renders a null last_error as "none recorded";
 *  - the reset is GUARDED: open guard -> explicit confirm copy -> POST only on confirm; cancel
 *    posts nothing; a success adopts the server's fresh stats; a refusal is named and leaves the
 *    guard open with the pre-reset counters (no false success).
 */

const mockApiGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;
const mockApiPost = api.post as jest.MockedFunction<(path: string) => Promise<unknown>>;

/** One response body per exact request path — an unexpected path is a test bug, not a pass. */
function wire(gets: Record<string, unknown>, posts: Record<string, unknown> = {}): void {
  mockApiGet.mockImplementation((path: string) => {
    if (!(path in gets)) return Promise.reject(new Error(`unexpected GET path in test: ${path}`));
    return Promise.resolve(gets[path]);
  });
  mockApiPost.mockImplementation((path: string) => {
    if (!(path in posts)) return Promise.reject(new Error(`unexpected POST path in test: ${path}`));
    return Promise.resolve(posts[path]);
  });
}

/** The page's own ledger read, quieted; the ops assertions never need its rows. */
const LEDGER: Record<string, unknown> = {
  "/runs?limit=50&offset=0": { items: [], total: 0 },
};

/** The live GET /ops/events/stats body (events.publish_stats()), mid-incident. */
const STATS = {
  failures: 3,
  skipped_while_open: 12,
  breaker_open: true,
  cooldown_seconds: 30,
  last_error: "redis connection refused",
};

function qualityRow(overrides: Partial<OpsQualityRow> = {}): OpsQualityRow {
  return {
    agent_type: "social_media",
    runs_7d: 4,
    success_rate: 0.75,
    last_failure_at: "2026-10-13T18:22:10Z",
    cost_per_completed_task: 0.0042,
    ...overrides,
  };
}

/** Breaker card + quality card, pointed at the real wires. */
function opsWire(agents: OpsQualityRow[], stats: unknown = STATS): Record<string, unknown> {
  return {
    ...LEDGER,
    "/ops/agent-quality": { agents },
    "/ops/events/stats": stats,
    // V23/L4: the worker-process card's read (the honest never-published state here).
    "/ops/activity-publish": { data: null },
  };
}

beforeEach(() => {
  mockApiGet.mockReset();
  mockApiPost.mockReset();
});

/* ---------------------------------------------------------------------------
 * Mount + the real read contract (test list 1)
 * ------------------------------------------------------------------------- */

describe("Ops panel — mounted on the Usage page, reads the REAL ops endpoints", () => {
  it("renders the quality rows and breaker counters off the real wire keys", async () => {
    wire(
      opsWire([
        qualityRow(),
        qualityRow({
          agent_type: "finance",
          runs_7d: 0,
          success_rate: null,
          last_failure_at: null,
          cost_per_completed_task: null,
        }),
      ]),
    );

    render(<UsagePage />);

    expect(await screen.findByTestId("ops-panel")).toBeInTheDocument();
    expect(mockApiGet).toHaveBeenCalledWith("/ops/agent-quality");
    expect(mockApiGet).toHaveBeenCalledWith("/ops/events/stats");

    const quality = await screen.findByTestId("ops-quality");
    const table = within(quality).getByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3); // header + 2 agent rows

    for (const header of ["AGENT", "RUNS (7D)", "SUCCESS RATE", "LAST FAILURE", "COST / COMPLETED"]) {
      expect(within(rows[0]).getByText(header)).toBeInTheDocument();
    }

    expect(within(rows[1]).getByText("social_media")).toBeInTheDocument();
    expect(within(rows[1]).getByText("4")).toBeInTheDocument();
    expect(within(rows[1]).getByText("75.0%")).toBeInTheDocument();
    expect(within(rows[1]).getByText("$0.0042")).toBeInTheDocument();
    // The last failure renders as an absolute stamp (the shared formatter), not a raw ISO string.
    const failureCell = within(rows[1]).getAllByRole("cell")[3];
    expect(failureCell.textContent).not.toBe("2026-10-13T18:22:10Z");
    expect(failureCell.textContent).toMatch(/OCT 13, 2026/);

    // The breaker card renders the real counters, and the row's measured zero is a reading.
    expect(await screen.findByTestId("ops-breaker-state")).toHaveTextContent("OPEN");
    const breaker = screen.getByTestId("ops-breaker");
    expect(breaker).toHaveTextContent("FAILED PUBLISHES");
    expect(breaker).toHaveTextContent("12");
    expect(breaker).toHaveTextContent("30s");
    expect(breaker).toHaveTextContent("redis connection refused");
  });
});

/* ---------------------------------------------------------------------------
 * Null vs zero on every nullable quality field (test list 2)
 * ------------------------------------------------------------------------- */

describe("Ops panel — a null quality field is a marker, NEVER a fabricated value", () => {
  it("measured zeros stay readings; nulls render the dash / 'none recorded'; precision survives", async () => {
    wire(
      opsWire([
        qualityRow({
          agent_type: "ads_management",
          runs_7d: 0,
          success_rate: 0,
          last_failure_at: null,
          cost_per_completed_task: null,
        }),
        qualityRow({
          agent_type: "social_media",
          runs_7d: null,
          success_rate: null,
          last_failure_at: null,
          cost_per_completed_task: 0,
        }),
      ]),
    );

    render(<UsagePage />);

    const table = within(await screen.findByTestId("ops-quality")).getByRole("table");
    const rows = within(table).getAllByRole("row");

    // Row 1: a measured 0 runs / 0% success rate / no failed run on record are READINGS.
    const cells1 = within(rows[1]).getAllByRole("cell");
    expect(cells1[1].textContent).toBe("0");
    expect(cells1[2].textContent).toBe("0.0%");
    expect(cells1[3].textContent).toBe("none recorded");
    expect(cells1[4].textContent).toBe(NOT_MEASURED);
    expect(cells1[1].textContent).not.toBe(NOT_MEASURED);

    // Row 2: unmeasured counts/rates are the dash; a measured $0 average is a reading.
    const cells2 = within(rows[2]).getAllByRole("cell");
    expect(cells2[1].textContent).toBe(NOT_MEASURED);
    expect(cells2[2].textContent).toBe(NOT_MEASURED);
    expect(cells2[4].textContent).toBe("$0.0000");
  });
});

/* ---------------------------------------------------------------------------
 * Loading / empty / error (test list 3)
 * ------------------------------------------------------------------------- */

describe("Ops panel — honest states", () => {
  it("a never-resolving ops read is a busy skeleton; chrome carries no unmeasured marker", async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path.startsWith("/runs")) return Promise.resolve({ items: [], total: 0 });
      return new Promise<never>(() => undefined);
    });

    render(<UsagePage />);

    expect(await screen.findByTestId("ops-quality-loading")).toHaveAttribute("aria-busy", "true");
    expect(screen.getByTestId("ops-breaker-loading")).toHaveAttribute("aria-busy", "true");
    // The dash is reserved for unmeasured VALUES; the panel's own chrome never shows one.
    expect(screen.getByTestId("ops-panel").textContent ?? "").not.toContain(NOT_MEASURED);
  });

  it("a failed quality read is NAMED and does not take the breaker card down with it", async () => {
    mockApiGet.mockImplementation((path: string) => {
      if (path.startsWith("/runs")) return Promise.resolve({ items: [], total: 0 });
      if (path === "/ops/events/stats") return Promise.resolve(STATS);
      return Promise.reject(new ApiError(503, "database unavailable", "E_DB", "check postgres"));
    });

    render(<UsagePage />);

    const alert = await screen.findByText(/Could not load the agent quality view/);
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("E_DB: database unavailable — check postgres");
    expect(await screen.findByTestId("ops-breaker-state")).toHaveTextContent("OPEN");
  });

  it("a hostile quality body is named, not rendered as rows", async () => {
    wire({ ...LEDGER, "/ops/agent-quality": { agents: "nope" }, "/ops/events/stats": STATS });

    render(<UsagePage />);

    const alert = await screen.findByText(/Could not load the agent quality view/);
    expect(alert).toHaveTextContent("the server returned no agent-quality rows");
    expect(within(screen.getByTestId("ops-quality")).queryByRole("table")).toBeNull();
  });

  it("a hostile stats body is named, not rendered as counters", async () => {
    wire({ ...LEDGER, "/ops/agent-quality": { agents: [] }, "/ops/events/stats": { breaker_open: "yes" } });

    render(<UsagePage />);

    const alert = await screen.findByText(/Could not load the activity breaker/);
    expect(alert).toHaveTextContent("the server returned no breaker stats");
    expect(screen.queryByTestId("ops-breaker-state")).toBeNull();
  });

  it("an empty agent list is an honest sentence, not a fake row", async () => {
    wire(opsWire([]));

    render(<UsagePage />);

    expect(await screen.findByText(/No agent rows came back in the quality read/)).toBeInTheDocument();
    expect(within(screen.getByTestId("ops-quality")).queryByRole("alert")).toBeNull();
  });
});

/* ---------------------------------------------------------------------------
 * The breaker card's honesty copy (test list 4)
 * ------------------------------------------------------------------------- */

describe("Ops panel — the breaker card states the process boundary", () => {
  it("says the counters are per-process and renders a null last_error as 'none recorded'", async () => {
    wire(
      opsWire([], { ...STATS, breaker_open: false, last_error: null }),
    );

    render(<UsagePage />);

    const scope = await screen.findByTestId("ops-breaker-scope");
    expect(scope).toHaveTextContent("module globals in the API process");
    expect(scope).toHaveTextContent("per process");
    expect(scope).toHaveTextContent("worker");
    expect(scope).toHaveTextContent("15-minute beat tick");

    expect(screen.getByTestId("ops-breaker-state")).toHaveTextContent("closed");
    expect(screen.getByTestId("ops-breaker-last-error")).toHaveTextContent("none recorded");
  });
});

/* ---------------------------------------------------------------------------
 * The guarded reset (test list 5)
 * ------------------------------------------------------------------------- */

describe("Ops panel — the reset is guarded and calls the REAL verb", () => {
  it("opens an explicit confirm; cancel posts NOTHING", async () => {
    wire(opsWire([]));

    render(<UsagePage />);
    const user = userEvent.setup();
    await screen.findByTestId("ops-breaker-state");

    expect(screen.queryByTestId("ops-reset-guard")).toBeNull();
    await user.click(screen.getByTestId("ops-reset"));

    const guard = await screen.findByTestId("ops-reset-guard");
    expect(guard).toHaveTextContent("Clear the API process's in-memory breaker");
    expect(guard).toHaveTextContent("worker's counters are untouched");
    expect(mockApiPost).not.toHaveBeenCalled();

    await user.click(screen.getByTestId("ops-reset-cancel"));
    expect(screen.queryByTestId("ops-reset-guard")).toBeNull();
    expect(mockApiPost).not.toHaveBeenCalled();
  });

  it("confirm posts /ops/events/reset and adopts the fresh stats it returns", async () => {
    wire(opsWire([]), {
      "/ops/events/reset": {
        failures: 0,
        skipped_while_open: 0,
        breaker_open: false,
        cooldown_seconds: 30,
        last_error: null,
      },
    });

    render(<UsagePage />);
    const user = userEvent.setup();
    expect(await screen.findByTestId("ops-breaker-state")).toHaveTextContent("OPEN");

    await user.click(screen.getByTestId("ops-reset"));
    await user.click(screen.getByTestId("ops-reset-confirm"));

    await waitFor(() => expect(mockApiPost).toHaveBeenCalledWith("/ops/events/reset"));
    expect(mockApiPost).toHaveBeenCalledTimes(1);

    // The panel now shows the server's post-reset counters, not the stale incident ones.
    await waitFor(() => expect(screen.getByTestId("ops-breaker-state")).toHaveTextContent("closed"));
    expect(screen.getByTestId("ops-breaker-last-error")).toHaveTextContent("none recorded");
    expect(screen.getByTestId("ops-reset-done")).toBeInTheDocument();
    expect(screen.queryByTestId("ops-reset-guard")).toBeNull();
  });

  it("a refused reset is named and leaves the guard open with the pre-reset counters", async () => {
    wire(opsWire([]));
    mockApiPost.mockRejectedValue(new ApiError(503, "redis unavailable", "E_DB", "check redis"));

    render(<UsagePage />);
    const user = userEvent.setup();
    expect(await screen.findByTestId("ops-breaker-state")).toHaveTextContent("OPEN");

    await user.click(screen.getByTestId("ops-reset"));
    await user.click(screen.getByTestId("ops-reset-confirm"));

    const alert = await screen.findByTestId("ops-reset-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("E_DB: redis unavailable — check redis");
    expect(screen.queryByTestId("ops-reset-done")).toBeNull();
    expect(screen.getByTestId("ops-reset-guard")).toBeInTheDocument();
    expect(screen.getByTestId("ops-breaker-state")).toHaveTextContent("OPEN");
  });
});

/* ---------------------------------------------------------------------------
 * The panel's own Refresh (nothing polls)
 * ------------------------------------------------------------------------- */

describe("Ops panel — explicit Refresh", () => {
  it("re-issues every ops read", async () => {
    wire(opsWire([]));

    render(<UsagePage />);
    const user = userEvent.setup();
    await screen.findByTestId("ops-breaker-state");

    const opsCalls = () =>
      mockApiGet.mock.calls.filter((call) => String(call[0]).startsWith("/ops/")).length;
    const before = opsCalls();

    await user.click(screen.getByTestId("ops-refresh"));

    // V23/L4 added the worker-process read: three ops reads per Refresh, never a poll.
    await waitFor(() => expect(opsCalls()).toBe(before + 3));
  });
});
