import { render, screen, waitFor } from "@testing-library/react";

/**
 * V21 / L2 — the run detail's tool-transparency surface.
 *
 * THE CONTRACT THIS SUITE PINS (see `wave-v21/l2-run-transparency/RECEIPT.md`):
 * the run record stores `num_turns` and `is_error`, but it does NOT store `tools_used` — the
 * DeepSeek runtime reports the list on its envelope and the persistence boundary drops it. So
 * the page must render an HONEST "not recorded" state, the two stored counters, and NOTHING
 * chip-shaped: one chip per tool would need a stored tool list that does not exist.
 *
 * Cases:
 *   1. loading -> GET /runs/{id} -> the stored counters render, zero chips;
 *   2. legacy row (both counters null) -> the em dash, still "not recorded", zero chips;
 *   3. an off-contract body that DID carry `tools_used` renders NO chip (nothing is invented);
 *   4. a junk counter type ("3", "no") renders as unmeasured, never coerced;
 *   5. the shared error path names the failure (ApiError.display via `errorText`);
 *   6. a 2xx body that is not a run is a named broken read, not an empty detail;
 *   7. a non-numeric `[id]` issues NO request and says what the route expects.
 *
 * House mocking pattern: keep the real `@/lib/api` classes, replace only `api`.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), patch: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import RunDetailPage from "@/app/runs/[id]/page";
import { parseRunId } from "@/components/runs/parseRunId";
import type { RunDetailRow } from "@/components/runs/useRunDetail";

const mockGet = api.get as jest.Mock;

beforeEach(() => {
  mockGet.mockReset();
});

function runDetail(overrides: Partial<RunDetailRow> = {}): RunDetailRow {
  return {
    id: 5,
    task_id: 42,
    task_title: "Backfilled ledger task",
    agent_type: "content",
    run_type: "task",
    status: "completed",
    attempt: 1,
    started_at: "2026-10-14T09:00:00Z",
    ended_at: "2026-10-14T09:05:00Z",
    duration_secs: 300,
    tokens_input: 120,
    tokens_output: 30,
    tokens_used: 150,
    cost_usd: 0.0123,
    cost_source: "token_estimate",
    pricing_version: "2026-10-01",
    num_turns: 3,
    is_error: false,
    // V22: the run record now carries the tool list; `null` = never recorded.
    tools_used: null,
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

// ---------------------------------------------------------------------------------------------
// the happy path: stored counters, no fabricated chips
// ---------------------------------------------------------------------------------------------

it("reads GET /runs/{id} after a real loading state and renders the stored counters", async () => {
  const d = deferred<RunDetailRow>();
  mockGet.mockReturnValueOnce(d.promise);

  render(<RunDetailPage params={{ id: "5" }} />);

  expect(screen.getByTestId("run-detail-loading")).toHaveAttribute("aria-busy", "true");
  expect(mockGet).toHaveBeenCalledWith("/runs/5");

  d.resolve(runDetail());

  const panel = await screen.findByTestId("run-tool-activity");
  expect(panel).toBeInTheDocument();
  expect(screen.getByTestId("run-tools-not-recorded")).toHaveTextContent(/not recorded/i);
  expect(screen.getByTestId("run-turns")).toHaveTextContent("3");
  expect(screen.getByTestId("run-error-flag")).toHaveTextContent(/no error envelope/i);
  // The transparency law: no chip exists without a stored tool list.
  expect(screen.queryAllByTestId("run-tool-chip")).toHaveLength(0);
  expect(screen.getByText("Backfilled ledger task")).toBeInTheDocument();
  expect(screen.getByText(/content/)).toBeInTheDocument();
});

// ---------------------------------------------------------------------------------------------
// legacy row: nothing stored -> nulls render as the app's dash, never 0 / false
// ---------------------------------------------------------------------------------------------

it("renders a legacy row's null counters as unmeasured, never 0 or false", async () => {
  mockGet.mockResolvedValueOnce(runDetail({ num_turns: null, is_error: null, cost_usd: null }));

  render(<RunDetailPage params={{ id: "6" }} />);

  await screen.findByTestId("run-tool-activity");
  expect(screen.getByTestId("run-turns")).toHaveTextContent("—");
  expect(screen.getByTestId("run-error-flag")).toHaveTextContent("—");
  expect(screen.queryAllByTestId("run-tool-chip")).toHaveLength(0);
});

// ---------------------------------------------------------------------------------------------
// hostile: an off-contract tools_used SHAPE must NOT become chips (V22: the array shape is now
// on-contract and renders real chips — see v22-run-chips.test.tsx; a non-array is still not a list)
// ---------------------------------------------------------------------------------------------

it("ignores an off-contract tools_used shape: no chip without a recorded array", async () => {
  mockGet.mockResolvedValueOnce({
    ...runDetail(),
    // A body from a different contract. The page must not launder it into "tools were used".
    tools_used: "web_fetch, pricing_catalog",
  } as unknown as RunDetailRow);

  render(<RunDetailPage params={{ id: "5" }} />);

  await screen.findByTestId("run-tool-activity");
  expect(screen.queryAllByTestId("run-tool-chip")).toHaveLength(0);
  expect(screen.getByTestId("run-tools-not-recorded")).toBeInTheDocument();
});

it("never coerces a junk counter type into a reading", async () => {
  mockGet.mockResolvedValueOnce({
    ...runDetail(),
    num_turns: "3",
    is_error: "no",
  } as unknown as RunDetailRow);

  render(<RunDetailPage params={{ id: "5" }} />);

  await screen.findByTestId("run-tool-activity");
  expect(screen.getByTestId("run-turns")).toHaveTextContent("—");
  expect(screen.getByTestId("run-error-flag")).toHaveTextContent("—");
});

// ---------------------------------------------------------------------------------------------
// the shared error path
// ---------------------------------------------------------------------------------------------

it("names a missing run through the shared ApiError display", async () => {
  mockGet.mockRejectedValueOnce(
    new ApiError(404, "run 7 does not exist", "E_NOT_FOUND", "check the id, or seed the row first (make seed)"),
  );

  render(<RunDetailPage params={{ id: "7" }} />);

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent("Could not load this run");
  expect(alert).toHaveTextContent("E_NOT_FOUND");
  expect(alert).toHaveTextContent("check the id");
});

it("treats a 2xx body that is not a run as a named broken read", async () => {
  mockGet.mockResolvedValueOnce({ id: 5, agent_type: 9 });

  render(<RunDetailPage params={{ id: "5" }} />);

  const alert = await screen.findByRole("alert");
  expect(alert).toHaveTextContent(/the server returned no run/i);
});

// ---------------------------------------------------------------------------------------------
// route grammar
// ---------------------------------------------------------------------------------------------

it("rejects a non-numeric id without issuing a request", async () => {
  render(<RunDetailPage params={{ id: "five" }} />);

  expect(await screen.findByRole("alert")).toHaveTextContent(/not a run id/i);
  expect(mockGet).not.toHaveBeenCalled();
});

it("parses only positive integer ids", () => {
  expect(parseRunId("5")).toBe(5);
  expect(parseRunId("005")).toBe(5);
  expect(parseRunId("0")).toBeNull();
  expect(parseRunId("-2")).toBeNull();
  expect(parseRunId("5.0")).toBeNull();
  expect(parseRunId("5abc")).toBeNull();
  expect(parseRunId(undefined)).toBeNull();
});

// ---------------------------------------------------------------------------------------------
// no stale read when the id changes (the detail page is addressable)
// ---------------------------------------------------------------------------------------------

it("re-reads when the route id changes and never shows the previous run under the new id", async () => {
  mockGet.mockResolvedValueOnce(runDetail({ id: 5, task_title: "Run five" }));
  const { rerender } = render(<RunDetailPage params={{ id: "5" }} />);
  expect(await screen.findByText("Run five")).toBeInTheDocument();

  const d = deferred<RunDetailRow>();
  mockGet.mockReturnValueOnce(d.promise);
  rerender(<RunDetailPage params={{ id: "8" }} />);

  await waitFor(() => expect(mockGet).toHaveBeenLastCalledWith("/runs/8"));
  expect(screen.getByTestId("run-detail-loading")).toBeInTheDocument();
  expect(screen.queryByText("Run five")).not.toBeInTheDocument();

  d.resolve(runDetail({ id: 8, task_title: "Run eight" }));
  expect(await screen.findByText("Run eight")).toBeInTheDocument();
});
