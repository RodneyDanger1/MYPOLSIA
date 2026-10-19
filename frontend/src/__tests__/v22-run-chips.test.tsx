import { render, screen } from "@testing-library/react";

/**
 * V22 / L1 — the run detail's tool chips: the three states, and the law that keeps them distinct.
 *
 * The backend now PERSISTS `agent_runs.tools_used` (migration 0010) and serves it on
 * `GET /api/v1/runs/{id}` as `string[] | null`.  The page must render, exactly:
 *
 *   1. names recorded   -> one chip per tool, each with a COUNT (`×N`) — real chips, no filler;
 *   2. `[]` recorded    -> "No tools were called in this run."  (a measured zero)
 *   3. `null`           -> "Tool activity: not recorded for this run."  (never a fabricated [])
 *   4. a junk shape     -> null semantics: chips require an ARRAY of names; a string does not
 *                          become a tool, and a marker is not a tool either.
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

import { api } from "@/lib/api";
import RunDetailPage from "@/app/runs/[id]/page";
import type { RunDetailRow } from "@/components/runs/useRunDetail";

const mockGet = api.get as jest.Mock;

beforeEach(() => {
  mockGet.mockReset();
});

function runDetail(overrides: Partial<RunDetailRow> = {}): RunDetailRow {
  return {
    id: 5,
    task_id: 42,
    task_title: "Tool-using ledger task",
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
    tools_used: null,
    ...overrides,
  };
}

// ---------------------------------------------------------------------------------------------
// 1. recorded names -> real chips with counts
// ---------------------------------------------------------------------------------------------

it("renders one chip per recorded tool, with the count from the stored list", async () => {
  mockGet.mockResolvedValueOnce(
    runDetail({ tools_used: ["web_search", "web_search", "calculator"] }),
  );

  render(<RunDetailPage params={{ id: "5" }} />);

  const panel = await screen.findByTestId("run-tool-activity");
  expect(panel).toBeInTheDocument();

  const chips = screen.getAllByTestId("run-tool-chip");
  expect(chips).toHaveLength(2);

  const search = screen.getByTestId("run-tool-chip-web_search");
  expect(search).toHaveTextContent("web_search");
  expect(search).toHaveTextContent("×2");

  const calc = screen.getByTestId("run-tool-chip-calculator");
  expect(calc).toHaveTextContent("calculator");
  expect(calc).toHaveTextContent("×1");

  // The other two states must be absent when names are recorded.
  expect(screen.queryByTestId("run-tools-not-recorded")).not.toBeInTheDocument();
  expect(screen.queryByTestId("run-tools-none")).not.toBeInTheDocument();
});

it("renders the truncation marker as a note, never as a tool chip", async () => {
  mockGet.mockResolvedValueOnce(
    runDetail({ tools_used: ["web_search", "… (+11 more; capped at 50)"] }),
  );

  render(<RunDetailPage params={{ id: "5" }} />);

  await screen.findByTestId("run-tool-activity");
  expect(screen.getAllByTestId("run-tool-chip")).toHaveLength(1);
  expect(screen.getByTestId("run-tools-truncated")).toHaveTextContent(/capped at 50/i);
  expect(screen.getByTestId("run-tools-truncated")).toHaveTextContent(/11 more/i);
});

// ---------------------------------------------------------------------------------------------
// 2. recorded-empty -> the measured zero
// ---------------------------------------------------------------------------------------------

it("renders 'no tools were called in this run' for a recorded empty list", async () => {
  mockGet.mockResolvedValueOnce(runDetail({ tools_used: [] }));

  render(<RunDetailPage params={{ id: "5" }} />);

  await screen.findByTestId("run-tool-activity");
  expect(screen.getByTestId("run-tools-none")).toHaveTextContent(/no tools were called/i);
  expect(screen.queryAllByTestId("run-tool-chip")).toHaveLength(0);
  expect(screen.queryByTestId("run-tools-not-recorded")).not.toBeInTheDocument();
});

// ---------------------------------------------------------------------------------------------
// 3. never recorded -> the honest null state
// ---------------------------------------------------------------------------------------------

it("renders the 'not recorded' state for null and for an absent field", async () => {
  mockGet.mockResolvedValueOnce(runDetail({ tools_used: null }));
  const { unmount } = render(<RunDetailPage params={{ id: "5" }} />);

  await screen.findByTestId("run-tool-activity");
  expect(screen.getByTestId("run-tools-not-recorded")).toHaveTextContent(/not recorded/i);
  expect(screen.queryAllByTestId("run-tool-chip")).toHaveLength(0);
  expect(screen.queryByTestId("run-tools-none")).not.toBeInTheDocument();
  unmount();

  // An old backend body with no `tools_used` key at all is the SAME fact: never recorded.
  const { tools_used: _dropped, ...withoutField } = runDetail();
  mockGet.mockResolvedValueOnce(withoutField as unknown as RunDetailRow);
  render(<RunDetailPage params={{ id: "6" }} />);

  await screen.findByTestId("run-tool-activity");
  expect(screen.getByTestId("run-tools-not-recorded")).toHaveTextContent(/not recorded/i);
  expect(screen.queryAllByTestId("run-tool-chip")).toHaveLength(0);
});

// ---------------------------------------------------------------------------------------------
// 4. hostile shapes never become chips
// ---------------------------------------------------------------------------------------------

it("does not invent chips from a junk tools_used shape", async () => {
  mockGet.mockResolvedValueOnce({
    ...runDetail(),
    tools_used: "web_search",
  } as unknown as RunDetailRow);

  render(<RunDetailPage params={{ id: "5" }} />);

  await screen.findByTestId("run-tool-activity");
  expect(screen.queryAllByTestId("run-tool-chip")).toHaveLength(0);
  // A non-array cannot be read as "recorded" — it falls back to the honest null state.
  expect(screen.getByTestId("run-tools-not-recorded")).toHaveTextContent(/not recorded/i);
});

it("drops non-string entries instead of rendering them as tools", async () => {
  mockGet.mockResolvedValueOnce({
    ...runDetail(),
    tools_used: ["web_search", 7, null],
  } as unknown as RunDetailRow);

  render(<RunDetailPage params={{ id: "5" }} />);

  await screen.findByTestId("run-tool-activity");
  const chips = screen.getAllByTestId("run-tool-chip");
  expect(chips).toHaveLength(1);
  expect(chips[0]).toHaveTextContent("web_search");
});
