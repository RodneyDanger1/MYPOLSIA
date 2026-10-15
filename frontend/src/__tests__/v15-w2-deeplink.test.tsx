/**
 * WAVE V15 / seat W2 — item 5-02 (S2-F4): the tasks page's `?task=N` deep link must work for ids
 * OUTSIDE the loaded board window. Before this wave the deep-link effect was a documented no-op
 * for any id not among the newest `TASKS_BOARD_LIMIT = 500` rows (`app/tasks/page.tsx:120`
 * pre-fix: `if (!rows.some(...)) return;`), so a bell notification pointing at an older task
 * opened nothing at all. The backend route ships — `GET /tasks/{task_id}` answers the full
 * `TaskResponse` (`tasks.py:113-114`) and a missing id is the named `E_NOT_FOUND` envelope — so
 * the page now READS the row by id:
 *
 *   - found            -> the detail sheet opens with the FETCHED row + a labelled context note;
 *   - 404              -> the NAMED miss ("Task #N not found" + the server's own E_NOT_FOUND);
 *   - any other failure-> the failure's own name; never a silent nothing;
 *   - a JUNK param (`abc`, `-3`, `5.5`, overflow) stays the documented no-op: nothing requested,
 *     nothing claimed.
 *
 * RED leg (raw output in wave-v15/w2/raw/): written before the fix; pre-fix the outside-set and
 * miss pins fail on the absent dialog / absent miss block, while the junk-param and in-set pins
 * (the behaviour V14 shipped) already pass — that asymmetry is the diff this item closes.
 */
import { render, screen, waitFor, within } from "@testing-library/react";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

/** The mutable search string the `useSearchParams` mock answers from. */
const mockSearch = { task: null as string | null };

jest.mock("next/navigation", () => ({
  usePathname: () => "/tasks",
  // Mirrors the REAL hook's observable behaviour for this page: a `ReadonlyURLSearchParams`
  // built from the current URL (the page null-guards the no-provider case itself).
  useSearchParams: () => {
    const params = new URLSearchParams();
    if (mockSearch.task !== null) params.set("task", mockSearch.task);
    return params;
  },
}));

jest.mock("next/link", () => ({
  __esModule: true,
  default: function MockLink(props: { href: string; children: React.ReactNode } & Record<string, unknown>) {
    const { href, children, ...rest } = props;
    return (
      <a href={href} {...(rest as Record<string, string>)}>
        {children}
      </a>
    );
  },
}));

import { ApiError, api } from "@/lib/api";
import type { TaskRow } from "@/components/tasks/types";
import TasksPage from "@/app/tasks/page";

const mockApiGet = api.get as jest.Mock;

function taskRow(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 7,
    title: "fixture: board task",
    agent_type: "orchestrator",
    status: "pending",
    priority: 3,
    created_at: new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString(),
    ...overrides,
  };
}

/** Path-keyed reads; an unlisted path rejects LOUDLY so a page that starts reading something new
 *  cannot pass this suite silently. An `Error` value becomes a rejection; anything else resolves. */
function answerWith(overrides: Record<string, unknown> = {}) {
  const paths: Record<string, unknown> = {
    "/tasks?limit=500": [taskRow()],
    ...overrides,
  };
  mockApiGet.mockImplementation((path: string) => {
    const value = paths[path];
    if (value === undefined) return Promise.reject(new Error(`unexpected read: ${path}`));
    return value instanceof Error ? Promise.reject(value) : Promise.resolve(value);
  });
}

beforeEach(() => {
  mockApiGet.mockReset();
  mockSearch.task = null;
});

describe("5-02 — ?task=N deep link beyond the loaded board", () => {
  it("an id outside the loaded set opens the detail from the REAL by-id read (network boundary stubbed)", async () => {
    mockSearch.task = "999";
    answerWith({
      "/tasks/999": taskRow({ id: 999, title: "fixture: fetched task", status: "completed" }),
    });

    render(<TasksPage />);

    // The board's own reads still happened...
    await screen.findByRole("button", { name: "Open task: fixture: board task" });
    // ...and the by-id read is the REAL route, called with the requested id.
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledWith("/tasks/999"));

    const dialog = await screen.findByRole("dialog");
    expect(
      within(dialog).getByRole("heading", { name: "fixture: fetched task" }),
    ).toBeInTheDocument();
    // The one-row context note: labelled, so the sheet never pretends the row came from the board.
    const note = within(dialog).getByTestId("task-context-note");
    expect(note).toHaveTextContent("outside the loaded board window");
    expect(note).toHaveTextContent("#999");
    // No alert rode along with a successful fetch.
    expect(screen.queryByTestId("tasks-deeplink-miss")).toBeNull();
  });

  it("a 404 from the by-id read is the NAMED miss — visible, never silent", async () => {
    mockSearch.task = "999";
    answerWith({
      "/tasks/999": new ApiError(
        404,
        "task 999 does not exist",
        "E_NOT_FOUND",
        "check the id — GET /tasks lists the existing tasks",
      ),
    });

    render(<TasksPage />);

    const miss = await screen.findByTestId("tasks-deeplink-miss");
    expect(miss).toHaveTextContent("Task #999 not found");
    expect(miss).toHaveTextContent("E_NOT_FOUND");
    expect(miss).toHaveTextContent("task 999 does not exist");
    expect(miss).toHaveTextContent("check the id");
    // Nothing opened, and the board itself stayed healthy.
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByText(/Could not load tasks/)).toBeNull();
  });

  it("a failed (non-404) by-id read is named too — the failure's own words", async () => {
    mockSearch.task = "999";
    answerWith({
      "/tasks/999": new ApiError(0, "network unreachable", "E_NETWORK"),
    });

    render(<TasksPage />);

    const miss = await screen.findByTestId("tasks-deeplink-miss");
    expect(miss).toHaveTextContent("Could not open task #999");
    expect(miss).toHaveTextContent("E_NETWORK");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("a by-id read that answers something that is not a task row is a named miss — never a rendered undefined", async () => {
    mockSearch.task = "999";
    answerWith({ "/tasks/999": { unexpected: true } });

    render(<TasksPage />);

    const miss = await screen.findByTestId("tasks-deeplink-miss");
    expect(miss).toHaveTextContent("Could not open task #999");
    expect(miss).toHaveTextContent("the API answered no task row for this id");
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it.each(["abc", "-3", "5.5", "99999999999999999999"])(
    "a junk task param (%s) requests NOTHING — the documented no-op, unchanged",
    async (junk) => {
      mockSearch.task = junk;
      answerWith();

      render(<TasksPage />);

      await screen.findByRole("button", { name: "Open task: fixture: board task" });
      expect(mockApiGet.mock.calls.map((call) => call[0])).toEqual(["/tasks?limit=500"]);
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(screen.queryByTestId("tasks-deeplink-miss")).toBeNull();
      expect(screen.queryByRole("alert")).toBeNull();
    },
  );

  it("an id the loaded set DOES contain opens from the set — no by-id read rides along", async () => {
    mockSearch.task = "7";
    answerWith();

    render(<TasksPage />);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "fixture: board task" })).toBeInTheDocument();
    // ONE read only: the board's. A by-id fetch for an already-loaded row would be a second call.
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(1));
    expect(mockApiGet).toHaveBeenCalledWith("/tasks?limit=500");
    // No context note: this row came from the board, and the sheet must not claim otherwise.
    expect(within(dialog).queryByTestId("task-context-note")).toBeNull();
  });
});
