/**
 * V29 follow-up C — the tasks page's read contract and its cap line.
 *
 * Two proven defects, both measured with the page's own wire:
 *
 *  1. THE BODY IS NEVER CHECKED TO BE A LIST. `GET /tasks` answers an array, but the page hands
 *     whatever came back straight to `setRows`. A 200 carrying `null` (an empty body, a proxy that
 *     re-serialises it) leaves `rows === null` with no error — the page renders the loading skeleton
 *     FOREVER, which is the forbidden "not read yet" state standing in for a broken read. A 200
 *     carrying an OBJECT is worse: `TaskBoard` iterates the body, the render throws, the committed
 *     tree is torn down and the owner is left with a header and two buttons — no board, no alert,
 *     nothing named. The sites page has guarded this since V24 ("a 200 whose body is not a list is
 *     named as a broken read"); this page is the one list surface that never did.
 *
 *  2. THE CAP LINE CLAIMS THE API REPORTED NO TOTAL WHILE THE API DID. `X-Polsia-Total` declares
 *     the FULL count of the same query. When exactly `TASKS_BOARD_LIMIT` tasks exist, the header
 *     says 500 and the page prints "the API reports no total, so more may exist" — two false
 *     statements (the header was read; nothing more exists) about a response it is holding.
 *
 * House mocking: the real `@/lib/api` classes with only the `api` object replaced, and
 * `useSearchParams` answering from a mutable fixture (same pattern as v15-w2-deeplink).
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), patch: jest.fn(), delete: jest.fn() },
  };
});

const mockSearch = { task: null as string | null };

jest.mock("next/navigation", () => ({
  usePathname: () => "/tasks",
  useSearchParams: () => {
    const params = new URLSearchParams();
    if (mockSearch.task !== null) params.set("task", mockSearch.task);
    return params;
  },
}));

import { render, screen, waitFor } from "@testing-library/react";

import TasksPage from "@/app/tasks/page";
import { TASKS_BOARD_LIMIT } from "@/components/tasks/TaskBoard";
import type { TaskRow } from "@/components/tasks/types";
import { api } from "@/lib/api";

const mockGet = api.get as jest.Mock;

function taskRow(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 1,
    title: "fixture: board task",
    agent_type: "orchestrator",
    status: "pending",
    priority: 3,
    created_at: "2026-10-20T00:00:00Z",
    ...overrides,
  };
}

/** A board read that answers `body`, and (optionally) a declared total header. */
function boardRead(body: unknown, total?: number) {
  mockGet.mockImplementation((_path: string, options?: { onHeaders?: (h: Headers) => void }) => {
    if (total !== undefined) {
      options?.onHeaders?.(new Headers({ "X-Polsia-Total": String(total) }));
    }
    return Promise.resolve(body);
  });
}

beforeEach(() => {
  mockGet.mockReset();
  mockSearch.task = null;
});

// ── 1. a 200 whose body is not a list is a BROKEN READ, named ────────────────────────────────
describe("a body that is not a list of tasks", () => {
  it("names an empty (null) body instead of rendering the loading state forever", async () => {
    boardRead(null);

    const { container } = render(<TasksPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load tasks");
    expect(alert).toHaveTextContent("the server returned no list of tasks");
    // The defect: the skeleton stayed up forever with no failure named. It must be gone.
    expect(container.querySelector('[aria-busy="true"]')).toBeNull();
    expect(screen.queryByText(/Loading tasks/)).toBeNull();
  });

  it("names an object body instead of blanking the board", async () => {
    boardRead({ detail: "an error page, not a task list" });

    render(<TasksPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not load tasks");
    expect(alert).toHaveTextContent("the server returned no list of tasks");
    // Never the honest-empty sentence for a body that was never a list.
    expect(screen.queryByText(/No tasks yet/)).toBeNull();
  });
});

// ── 2. the cap line reads the declared total it was given ──────────────────────────────────
describe("the limit-hit cap line", () => {
  it("says all N are shown when the declared total equals the page", async () => {
    const rows = Array.from({ length: TASKS_BOARD_LIMIT }, (_, index) =>
      taskRow({ id: index + 1, title: `Task ${index + 1}` }),
    );
    boardRead(rows, TASKS_BOARD_LIMIT);

    render(<TasksPage />);

    const cap = await screen.findByText(/Showing/);
    await waitFor(() => expect(cap).toHaveTextContent(`Showing all ${TASKS_BOARD_LIMIT} tasks`));
    // The defect: two false statements about a response whose total was read.
    expect(cap.textContent ?? "").not.toMatch(/reports no total/);
    expect(cap.textContent ?? "").not.toMatch(/more may exist/);
  }, 60_000);

  it("still names the first page of a larger table, and keeps the no-total wording when no header came", async () => {
    const rows = Array.from({ length: TASKS_BOARD_LIMIT }, (_, index) =>
      taskRow({ id: index + 1, title: `Task ${index + 1}` }),
    );
    boardRead(rows, TASKS_BOARD_LIMIT + 37);

    const { unmount } = render(<TasksPage />);

    expect(await screen.findByText(/Showing/)).toHaveTextContent(
      `Showing the first ${TASKS_BOARD_LIMIT} of ${TASKS_BOARD_LIMIT + 37} tasks`,
    );

    // Control leg: with no declared total the honest sentence stays exactly as it was.
    unmount();
    mockGet.mockReset();
    boardRead(rows);
    render(<TasksPage />);
    expect(await screen.findByText(/Showing/)).toHaveTextContent(
      `Showing the first ${TASKS_BOARD_LIMIT} tasks — the API reports no total, so more may exist`,
    );
  }, 60_000);
});
