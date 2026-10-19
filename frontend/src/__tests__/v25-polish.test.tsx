/**
 * WAVE V25 / lane L5 — three measured polish fixes in surfaces this lane owns.
 *
 * Each case is RED-first: the assertion names the exact defect that existed before the fix.
 *
 *   1. dashboard `BusinessPanel` — a failed summary read had no way back (no polling, no panel
 *      retry): the panel stayed failed until a full page reload. The failure now offers a Retry
 *      that re-issues BOTH reads that feed it (`/dashboard/summary` and `/finance/planning`).
 *   2. memory — a failed search named the error but offered no retry: the only way forward was to
 *      retype and press Search again. The failure now re-runs the query that failed.
 *   3. finance `HumanRateCard` — a failed `/config` read had no local recovery; the card owns its
 *      own read/write so it now owns its own Retry instead of relying on the page's Refresh.
 *
 * House mocking pattern: keep the real `@/lib/api` classes, replace only the `api` object.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), patch: jest.fn(), delete: jest.fn() },
  };
});

import { act, fireEvent, render, screen } from "@testing-library/react";

import MemoryPage from "@/app/memory/page";
import { BusinessPanel } from "@/components/dashboard/BusinessPanel";
import { HumanRateCard } from "@/components/finance/HumanRateCard";
import { ApiError, api } from "@/lib/api";

const mockGet = api.get as jest.Mock;

async function flush(times = 8) {
  await act(async () => {
    for (let i = 0; i < times; i += 1) await Promise.resolve();
  });
}

const SUMMARY = {
  tasks_today_total: 5,
  tasks_today_completed: 3,
  tasks_today_pending: 1,
  tasks_today_failed: 1,
  active_agents: ["competitor_research"],
  kpis: {},
  last_report_date: null,
};

beforeEach(() => {
  mockGet.mockReset();
});

describe("dashboard BusinessPanel — a failed read is not a dead end (V25/L5)", () => {
  it("offers Retry and re-issues both reads that feed the panel", async () => {
    let summaryReads = 0;
    let planningReads = 0;
    mockGet.mockImplementation((path: string) => {
      if (path === "/dashboard/summary") {
        summaryReads += 1;
        return summaryReads === 1
          ? Promise.reject(new ApiError(503, "the summary is unreachable", "E_DB"))
          : Promise.resolve(SUMMARY);
      }
      if (path === "/finance/planning") {
        planningReads += 1;
        return Promise.resolve({ margin_template: { margin: 0.4 }, reasons: {} });
      }
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<BusinessPanel />);

    expect(await screen.findByText(/Could not load the dashboard summary: E_DB:/)).toBeInTheDocument();
    // Pre-fix the panel rendered no control at all: queryByTestId would have been null.
    fireEvent.click(screen.getByTestId("business-panel-retry"));
    await flush();

    expect(summaryReads).toBe(2);
    expect(planningReads).toBe(2);
    expect(await screen.findByText("Tasks today")).toBeInTheDocument();
  });
});

describe("memory search — a failed search can be retried (V25/L5)", () => {
  it("Retry re-runs the SAME query and renders the successful empty answer", async () => {
    let reads = 0;
    mockGet.mockImplementation((path: string) => {
      if (path === "/memory?q=roadmap&limit=20") {
        reads += 1;
        return reads === 1
          ? Promise.reject(new ApiError(503, "the memory index is unreachable", "E_DB"))
          : Promise.resolve([]);
      }
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<MemoryPage />);

    fireEvent.change(screen.getByLabelText("Search company memory"), {
      target: { value: "roadmap" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Search" }));
    await flush();

    expect(await screen.findByText(/Search failed/)).toBeInTheDocument();
    expect(screen.getByText(/E_DB: the memory index is unreachable/)).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("memory-search-retry"));
    await flush();

    expect(reads).toBe(2);
    expect(
      await screen.findByText(/No entries matched that search\./),
    ).toBeInTheDocument();
  });
});

describe("finance HumanRateCard — the card owns a local retry (V25/L5)", () => {
  it("Retry re-reads /config and renders the value it could not read", async () => {
    let reads = 0;
    mockGet.mockImplementation((path: string) => {
      if (path === "/config") {
        reads += 1;
        return reads === 1
          ? Promise.reject(new ApiError(503, "the config table is unreachable", "E_DB"))
          : Promise.resolve({ human_hourly_rate: 42 });
      }
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<HumanRateCard reloadKey={0} />);

    expect(await screen.findByTestId("human-rate-read-error")).toHaveTextContent(/E_DB/);
    fireEvent.click(screen.getByTestId("human-rate-retry"));
    await flush();

    expect(reads).toBe(2);
    expect(await screen.findByTestId("human-rate-value")).toHaveTextContent("$42.00 per hour");
  });
});
