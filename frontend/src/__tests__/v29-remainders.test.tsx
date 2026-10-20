/**
 * WAVE V29 / lane L2 — the frontend remainders that are real.
 *
 *  T2.1  a deep-linked task whose by-id read failed must offer Retry, and that Retry must re-issue
 *        `GET /tasks/{id}`. Before the fix the miss block was a dead end: the by-id effect is keyed
 *        to the FETCHING id, so a button that only bumped a counter (or nothing at all) changed no
 *        dependency and the read was never repeated. The board-level error Retry is a different
 *        surface and stays untouched.
 *
 *  T2.2  the retention panel's name lists are capped by the server. When the response says a list
 *        was truncated (`removed_truncated` / `skipped_truncated`), the panel must SAY so and say
 *        which number is still complete. When the flags are absent the panel makes no claim at all
 *        — absent is not "not truncated", it is "the server did not say".
 *
 * RED leg: run before the production hunks; T2.1 fails on the missing `task-deep-link-retry`
 * control, T2.2 fails on the missing sentence, and the absent-flag pin already passes (it pins
 * today's silence so the fix cannot introduce an unconditional claim).
 *
 * House mocking pattern (v15-w2-deeplink, v27-retention): keep the real `@/lib/api` classes and
 * replace only the `api` object; `useSearchParams` answers from a mutable fixture.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), patch: jest.fn(), delete: jest.fn() },
  };
});

/** The mutable search string the `useSearchParams` mock answers from. */
const mockSearch = { task: null as string | null };

jest.mock("next/navigation", () => ({
  usePathname: () => "/tasks",
  useSearchParams: () => {
    const params = new URLSearchParams();
    if (mockSearch.task !== null) params.set("task", mockSearch.task);
    return params;
  },
}));

import { fireEvent, render, screen, within } from "@testing-library/react";

import TasksPage from "@/app/tasks/page";
import { OpsRetention } from "@/components/ops/OpsRetention";
import type { TaskRow } from "@/components/tasks/types";
import { ApiError, api } from "@/lib/api";

const mockGet = api.get as jest.Mock;

function taskRow(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 1,
    title: "fixture: board task",
    agent_type: "orchestrator",
    status: "pending",
    priority: 3,
    created_at: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
    ...overrides,
  };
}

const RESULT_BASE = {
  runs_dir: "C:/data/workspace/runs",
  count: 2,
  kept: 21,
  removed: ["run-old-00", "run-old-01"],
  skipped: [{ name: "not a run", reason: "invalid_name" }],
  policy: { keep_latest: 20, max_age_hours: 72 },
  applied: true,
};

function storedResult(result: Record<string, unknown>) {
  return {
    data: {
      activity_id: 12,
      created_at: "2026-10-19T10:00:00Z",
      trigger: "beat",
      result,
    },
  };
}

beforeEach(() => {
  mockGet.mockReset();
  mockSearch.task = null;
});

// ---------------------------------------------------------------------------------------------
// T2.1 — the deep link's Retry is a real re-read.
// ---------------------------------------------------------------------------------------------

describe("T2.1 — deep-linked task retry", () => {
  it("a failed deep-linked task offers Retry and refetches", async () => {
    mockSearch.task = "999";
    let byIdCalls = 0;
    mockGet.mockImplementation((path: string) => {
      if (path === "/tasks?limit=500") return Promise.resolve([taskRow()]);
      if (path === "/tasks/999") {
        byIdCalls += 1;
        if (byIdCalls === 1) {
          return Promise.reject(
            new ApiError(500, "the by-id read failed", "E_UPSTREAM", "retry the read"),
          );
        }
        return Promise.resolve(taskRow({ id: 999, title: "fixture: recovered task" }));
      }
      return Promise.reject(new Error(`unexpected read: ${path}`));
    });

    render(<TasksPage />);

    // The failed by-id read is the NAMED miss (named, never silent) and it is where Retry lives.
    const miss = await screen.findByTestId("tasks-deeplink-miss");
    expect(miss).toHaveTextContent("Could not open task #999");
    expect(miss).toHaveTextContent("the by-id read failed");

    fireEvent.click(within(miss).getByTestId("task-deep-link-retry"));

    // The retry actually re-issued the by-id read...
    expect(await screen.findByRole("dialog")).toBeInTheDocument();
    const byId = mockGet.mock.calls.filter((call) => call[0] === "/tasks/999");
    expect(byId).toHaveLength(2);
    // ...and the recovered row, not a stand-in, opened the sheet.
    const dialog = screen.getByRole("dialog");
    expect(
      within(dialog).getByRole("heading", { name: "fixture: recovered task" }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("tasks-deeplink-miss")).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// T2.2 — truncation copy: say what was capped, and which number is still complete.
// ---------------------------------------------------------------------------------------------

describe("T2.2 — retention truncation copy", () => {
  it("a capped name list says the counts are complete", async () => {
    const removed = Array.from({ length: 50 }, (_, index) => `run-old-${String(index).padStart(2, "0")}`);
    mockGet.mockResolvedValue(
      storedResult({ ...RESULT_BASE, count: 73, removed, removed_truncated: true }),
    );

    render(<OpsRetention />);

    expect(
      await screen.findByTestId("ops-retention-truncated"),
    ).toHaveTextContent("The name list was capped at 50; the count of 73 is complete.");
  });

  it("a capped skipped list says the counts above are complete, never that names were deleted", async () => {
    const skipped = Array.from({ length: 50 }, (_, index) => ({
      name: `run-bad-${String(index).padStart(2, "0")}`,
      reason: "invalid_name",
    }));
    mockGet.mockResolvedValue(
      storedResult({ ...RESULT_BASE, skipped, skipped_truncated: true }),
    );

    render(<OpsRetention />);

    const note = await screen.findByTestId("ops-retention-skipped-truncated");
    expect(note).toHaveTextContent(
      "The skipped name list was capped at 50; the counts above are complete.",
    );
    expect(note.textContent ?? "").not.toMatch(/deleted|removed/i);
  });

  it("absent truncation flags make no claim at all", async () => {
    mockGet.mockResolvedValue(storedResult({ ...RESULT_BASE }));

    render(<OpsRetention />);

    await screen.findByTestId("ops-retention-result");
    expect(screen.queryByTestId("ops-retention-truncated")).toBeNull();
    expect(screen.queryByTestId("ops-retention-skipped-truncated")).toBeNull();
    expect(screen.queryByText(/name list was capped/i)).toBeNull();
  });
});
