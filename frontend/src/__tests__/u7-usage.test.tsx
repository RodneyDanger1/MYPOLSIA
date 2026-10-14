import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { NOT_MEASURED } from "@/components/dashboard/format";
import { NAV_ITEMS } from "@/components/layout/nav-items";
import type { RunsRow } from "@/components/usage/useRuns";
import UsagePage from "@/app/usage/page";

/**
 * Wave V7 — seat S5's own suite for the /usage ledger page (V7-T15).
 *
 * Scope discipline: run with `node node_modules/jest/bin/jest.js --ci u7-usage`.
 *
 * What is pinned here:
 *  - the live read contract: GET /runs serves `{items, total}` where `total` is the FULL filtered
 *    count, and the page renders every row field off those exact wire keys;
 *  - the null-vs-zero law: a null `cost_usd` renders the em dash and NOT "0" — while a measured
 *    cost keeps its NUMERIC(14,4) precision (a sub-cent cost must not collapse to $0.00);
 *  - the declared-total law: a truncated read shows "the first N of M" with the REAL numbers, a
 *    full/longer ledger is labelled, a complete ledger says "all N", and paging (NEXT/PREV)
 *    issues real limit/offset reads with disabled edges;
 *  - Export: fetches /runs/export?format=csv through the API key lib/api.ts owns, and triggers a
 *    REAL download (object URL + anchor click); a failure surfaces ApiError.display — no dead
 *    button, no silent no-op;
 *  - failures are named (role=alert, ApiError.display) and a never-resolving read is a skeleton —
 *    never an em-dash grid, never a blank screen;
 *  - the MENU table carries the /usage item (add-one-nav-item task).
 */

const mockApiGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;

/** One ledger row with every wire field measured; overrides drive the law tests. */
function run(id: number, overrides: Partial<RunsRow> = {}): RunsRow {
  return {
    id,
    task_id: 7,
    task_title: `Run ${id}`,
    agent_type: "social_media",
    run_type: "task",
    status: "success",
    attempt: 1,
    started_at: "2026-10-13T18:21:39Z",
    ended_at: "2026-10-13T18:22:10Z",
    duration_secs: 31.2,
    tokens_input: 1200,
    tokens_output: 800,
    tokens_used: 2000,
    cost_usd: 0.4213,
    cost_source: "priced",
    pricing_version: "2026-10-01",
    ...overrides,
  };
}

function runsPage(items: RunsRow[], total: number): { items: RunsRow[]; total: number } {
  return { items, total };
}

/** One response body per exact request path — an unexpected path is a test bug, not a pass. */
function wire(paths: Record<string, unknown>): void {
  mockApiGet.mockImplementation((path: string) => {
    if (!(path in paths)) return Promise.reject(new Error(`unexpected path in test: ${path}`));
    return Promise.resolve(paths[path]);
  });
}

const FETCH_MOCK = jest.fn();

beforeEach(() => {
  mockApiGet.mockReset();
  FETCH_MOCK.mockReset();
  (globalThis as unknown as { fetch: unknown }).fetch = FETCH_MOCK;
});

/* ---------------------------------------------------------------------------
 * The ledger renders the live read (test list 1)
 * ------------------------------------------------------------------------- */

describe("Usage — the ledger renders the live GET /runs read", () => {
  it("renders the rows' real wire fields under ALL-CAPS headers, newest page first", async () => {
    wire({
      "/runs?limit=50&offset=0": runsPage(
        [run(1, { task_title: "Draft October newsletter", status: "failed" }), run(2)],
        2,
      ),
    });

    render(<UsagePage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");
    expect(rows).toHaveLength(3); // header + 2 data rows

    for (const header of [
      "TITLE",
      "AGENT",
      "STATUS",
      "STARTED",
      "FINISHED",
      "TOKENS",
      "COST",
      "SOURCE",
      "ATTEMPT",
    ]) {
      expect(within(rows[0]).getByText(header)).toBeInTheDocument();
    }

    expect(within(rows[1]).getByText("Draft October newsletter")).toBeInTheDocument();
    expect(within(rows[1]).getByText("social_media")).toBeInTheDocument();
    expect(within(rows[1]).getByText("failed")).toBeInTheDocument();
    expect(within(rows[1]).getByText("2,000")).toBeInTheDocument();
    expect(within(rows[1]).getByText("$0.4213")).toBeInTheDocument();
    expect(within(rows[1]).getByText("priced")).toBeInTheDocument();

    // The page must actually READ the ledger — with the explicit page size.
    expect(mockApiGet).toHaveBeenCalledWith("/runs?limit=50&offset=0");
  });
});

/* ---------------------------------------------------------------------------
 * Null vs zero on the cost column (test list 2)
 * ------------------------------------------------------------------------- */

describe("Usage — a null cost is the dash, NEVER 0", () => {
  it("null renders the em dash; a measured cost keeps its 4-decimal precision", async () => {
    wire({
      "/runs?limit=50&offset=0": runsPage(
        [
          run(1, { cost_usd: null, cost_source: null }), // UNMEASURED
          run(2, { cost_usd: 0.4213 }), // stored precision
          run(3, { cost_usd: 0 }), // a MEASURED zero is a reading
        ],
        3,
      ),
    });

    render(<UsagePage />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row");

    // Row 1: cost and source are both UNMEASURED; the COST cell (7th) is the dash, and no
    // zero-shaped money print may stand in for a value that was never measured.
    expect(within(rows[1]).getAllByRole("cell")[6].textContent).toBe(NOT_MEASURED);
    expect(rows[1].textContent ?? "").not.toContain("$0.00");

    // Row 2: precision survives — never rounded away.
    expect(within(rows[2]).getByText("$0.4213")).toBeInTheDocument();

    // Row 3: a measured zero renders as a reading, never as the dash.
    expect(within(rows[3]).getByText("$0.0000")).toBeInTheDocument();
    expect(within(rows[3]).queryAllByText(NOT_MEASURED)).toHaveLength(0);
  });
});

/* ---------------------------------------------------------------------------
 * The declared-total law (test list 3)
 * ------------------------------------------------------------------------- */

describe("Usage — the declared-total footer never drifts from the server's total", () => {
  it("a truncated read shows 'the first N of M' with the REAL numbers", async () => {
    wire({ "/runs?limit=50&offset=0": runsPage([run(1), run(2)], 7) });

    render(<UsagePage />);

    expect(await screen.findByTestId("usage-count")).toHaveTextContent("Showing the first 2 of 7");
  });

  it("a full first page of a longer ledger is labelled as a first page", async () => {
    wire({
      "/runs?limit=50&offset=0": runsPage(
        Array.from({ length: 50 }, (_, i) => run(i + 1)),
        120,
      ),
    });

    render(<UsagePage />);

    expect(await screen.findByTestId("usage-count")).toHaveTextContent("Showing the first 50 of 120");
  });

  it("a complete ledger says all N — nothing was cut off, nothing invented", async () => {
    wire({ "/runs?limit=50&offset=0": runsPage([run(1)], 1) });

    render(<UsagePage />);

    expect(await screen.findByTestId("usage-count")).toHaveTextContent("Showing all 1");
  });

  it("an empty ledger is an honest sentence plus a 0-runs line, never a fake row", async () => {
    wire({ "/runs?limit=50&offset=0": runsPage([], 0) });

    render(<UsagePage />);

    expect(await screen.findByTestId("usage-count")).toHaveTextContent("0 runs in the ledger");
    expect(screen.getByText(/No runs recorded yet/)).toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
 * Paging (test list 4)
 * ------------------------------------------------------------------------- */

describe("Usage — NEXT/PREV issue limit/offset reads with truthful disabled edges", () => {
  it("pages forward and back by PAGE_SIZE=50 and disables at both edges", async () => {
    wire({
      "/runs?limit=50&offset=0": runsPage(
        Array.from({ length: 50 }, (_, i) => run(i + 1)),
        120,
      ),
      "/runs?limit=50&offset=50": runsPage(
        Array.from({ length: 50 }, (_, i) => run(51 + i)),
        120,
      ),
      "/runs?limit=50&offset=100": runsPage(
        Array.from({ length: 20 }, (_, i) => run(101 + i)),
        120,
      ),
    });

    render(<UsagePage />);
    const user = userEvent.setup();

    // Page 1 of 3: no previous page exists.
    expect(await screen.findByTestId("usage-prev")).toBeDisabled();
    expect(screen.getByTestId("usage-next")).toBeEnabled();

    await user.click(screen.getByTestId("usage-next"));
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledWith("/runs?limit=50&offset=50"));
    expect(screen.getByTestId("usage-prev")).toBeEnabled();

    await user.click(screen.getByTestId("usage-next"));
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledWith("/runs?limit=50&offset=100"));
    // Last page: the footer names the real slice, and NEXT is disabled.
    await screen.findByText("Showing rows 101–120 of 120");
    expect(screen.getByTestId("usage-next")).toBeDisabled();
    expect(screen.getByTestId("usage-prev")).toBeEnabled();

    await user.click(screen.getByTestId("usage-prev"));
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledWith("/runs?limit=50&offset=50"));
    expect(await screen.findByTestId("usage-next")).toBeEnabled();
  });
});

/* ---------------------------------------------------------------------------
 * Export (test list 5 + failure surface)
 * ------------------------------------------------------------------------- */

describe("Usage — Export really downloads via the API", () => {
  let anchorClick: jest.SpyInstance;

  beforeEach(() => {
    anchorClick = jest
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    Object.defineProperty(URL, "createObjectURL", {
      configurable: true,
      writable: true,
      value: jest.fn(() => "blob:usage-export"),
    });
    Object.defineProperty(URL, "revokeObjectURL", {
      configurable: true,
      writable: true,
      value: jest.fn(),
    });
  });

  afterEach(() => {
    anchorClick.mockRestore();
  });

  it("fetches /runs/export?format=csv and triggers a real download (object URL + anchor click)", async () => {
    wire({ "/runs?limit=50&offset=0": runsPage([run(1)], 7) });
    FETCH_MOCK.mockResolvedValue({
      ok: true,
      status: 200,
      statusText: "OK",
      headers: { get: (name: string) => (name === "X-Polsia-Row-Count" ? "7" : null) },
      blob: () => Promise.resolve(new Blob(["id,cost_usd\n1,0.4213\n"], { type: "text/csv" })),
      text: () => Promise.resolve(""),
    });

    render(<UsagePage />);
    const user = userEvent.setup();
    await screen.findByRole("table");

    await user.click(screen.getByTestId("usage-export"));

    await waitFor(() => expect(FETCH_MOCK).toHaveBeenCalledTimes(1));
    const [url] = FETCH_MOCK.mock.calls[0] as [string];
    expect(url).toMatch(/\/api\/v1\/runs\/export\?format=csv$/);

    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
    expect(URL.createObjectURL).toHaveBeenCalledTimes(1);
    expect(URL.revokeObjectURL).toHaveBeenCalledTimes(1);

    // The declared row count off the response header is surfaced — the declared-total law.
    expect(await screen.findByTestId("usage-export-done")).toHaveTextContent(
      "Exported 7 rows to runs.csv.",
    );
  });

  it("a refused export surfaces ApiError.display — never a dead button", async () => {
    wire({ "/runs?limit=50&offset=0": runsPage([run(1)], 1) });
    FETCH_MOCK.mockResolvedValue({
      ok: false,
      status: 503,
      statusText: "Service Unavailable",
      headers: { get: () => null },
      text: () =>
        Promise.resolve(
          JSON.stringify({
            ok: false,
            error: { code: "E_DB", message: "database unavailable", how_to_fix: "check postgres" },
          }),
        ),
      blob: () => Promise.resolve(new Blob([])),
    });

    render(<UsagePage />);
    const user = userEvent.setup();
    await screen.findByRole("table");

    await user.click(screen.getByTestId("usage-export"));

    const alert = await screen.findByTestId("usage-export-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("E_DB: database unavailable — check postgres");
    expect(anchorClick).not.toHaveBeenCalled();
  });
});

/* ---------------------------------------------------------------------------
 * Failure and loading states (test list 6)
 * ------------------------------------------------------------------------- */

describe("Usage — failures are named, loading is a skeleton, never a blank screen", () => {
  it("a rejected read renders the ApiError.display failure (role=alert)", async () => {
    mockApiGet.mockRejectedValue(
      new ApiError(503, "database unavailable", "E_DB", "check postgres"),
    );

    render(<UsagePage />);

    const alert = await screen.findByText(/Could not load the usage ledger/);
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("E_DB: database unavailable — check postgres");
    expect(screen.queryByTestId("usage-loading")).toBeNull();
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("a body that is not the frozen {items,total} wrapper is named, not rendered", async () => {
    wire({ "/runs?limit=50&offset=0": { rows: [] } });

    render(<UsagePage />);

    const alert = await screen.findByText(/Could not load the usage ledger/);
    expect(alert).toHaveTextContent("the server returned no runs page");
  });

  it("a never-resolving read is a busy skeleton — not a dash grid, not an empty ledger", async () => {
    mockApiGet.mockImplementation(() => new Promise<never>(() => undefined));

    render(<UsagePage />);

    expect(screen.getByTestId("usage-loading")).toHaveAttribute("aria-busy", "true");
    expect(document.body.textContent ?? "").not.toContain(NOT_MEASURED);
    expect(screen.queryByRole("table")).toBeNull();
  });
});

/* ---------------------------------------------------------------------------
 * Refresh (the only re-read trigger; nothing polls)
 * ------------------------------------------------------------------------- */

describe("Usage — explicit Refresh", () => {
  it("re-issues the ledger read", async () => {
    wire({ "/runs?limit=50&offset=0": runsPage([run(1)], 1) });

    render(<UsagePage />);
    await screen.findByTestId("usage-count");

    await userEvent.setup().click(screen.getByTestId("usage-refresh"));

    await waitFor(() => {
      const calls = mockApiGet.mock.calls.filter((call) =>
        String(call[0]).startsWith("/runs?limit=50&offset=0"),
      );
      expect(calls).toHaveLength(2);
    });
  });
});

/* ---------------------------------------------------------------------------
 * The MENU item (nav-items.ts — this wave's add-one-item task)
 * ------------------------------------------------------------------------- */

describe("Usage — the MENU table carries the /usage route", () => {
  it("NAV_ITEMS has exactly one Usage item with an existing lucide icon", () => {
    const items = NAV_ITEMS.filter((entry) => entry.href === "/usage");
    expect(items).toHaveLength(1);
    expect(items[0].label).toBe("Usage");
    expect(items[0].icon).toBeDefined();
  });
});
