import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn(), post: jest.fn() } };
});

/** The mutable search string the `useSearchParams` mock answers from. */
const mockSearch = { task: null as string | null };

jest.mock("next/navigation", () => ({
  usePathname: () => "/dashboard",
  // Mirrors the REAL hook's observable behaviour for this page: a `ReadonlyURLSearchParams`
  // built from the current URL. The page must null-guard (the hook answers `null` when no
  // provider is mounted — the four pre-existing TasksPage test files render it unmocked).
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

import { api } from "@/lib/api";
import type { TaskRow } from "@/components/tasks/types";
import TasksPage from "@/app/tasks/page";
import { Topbar } from "@/components/layout/Topbar";

const mockApiGet = api.get as jest.Mock;

/**
 * Wave U4 — coder C's own suite: (1) the bell renders the NEW `run_requeued` kind (B7's hung-run
 * watchdog, projected by `GET /api/v1/notifications` from `stale_claims_requeued` activity rows),
 * and (2) the tasks page consumes the bell's `?task=N` deep link it has always linked to.
 *
 * RED-leg evidence (each planted, observed, restored — raw output in the lane receipt):
 *   * run before the bell/paged fix: the run_requeued test fails on the missing "Run requeued"
 *     label (the raw kind word renders), and every deep-link test fails on the absent dialog.
 */

const HOUR_MS = 60 * 60 * 1000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

function taskRow(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 7,
    title: "fixture: deep-linked task",
    agent_type: "orchestrator",
    status: "pending",
    priority: 3,
    created_at: ago(2 * HOUR_MS),
    ...overrides,
  };
}

function notif(id: string, title: string, kind: string, taskId: number, createdAt: string) {
  return { id, kind, title, task_id: taskId, created_at: createdAt };
}

beforeEach(() => {
  mockApiGet.mockReset();
  mockSearch.task = null;
  window.localStorage.clear();
});

/* --------------------------------------------------------------------------- 
 * Bell — the run_requeued kind (wave U4/B7)
 * ------------------------------------------------------------------------- */

describe("Bell — run_requeued kind", () => {
  it("renders its own label, glyph and a working View task link — never the raw kind word", async () => {
    const user = userEvent.setup();
    const recent = ago(5 * 60_000);
    mockApiGet.mockResolvedValue({
      items: [
        notif(
          "activity:3:42:run_requeued",
          "Stalled run requeued: task #42",
          "run_requeued",
          42,
          recent,
        ),
      ],
      count: 1,
    });

    render(<Topbar />);

    const badge = await screen.findByTestId("notifications-badge");
    expect(badge).toHaveTextContent("1");

    await user.click(screen.getByTestId("topbar-notifications"));

    const list = await screen.findByTestId("notifications-list");
    const item = within(list).getByRole("listitem");
    expect(item).toHaveAttribute("data-kind", "run_requeued");
    // its own line: the distinct label and glyph from the kind lookup...
    expect(within(list).getByText("Run requeued")).toBeInTheDocument();
    expect(within(list).getByText("↻")).toBeInTheDocument();
    expect(within(list).getByText("Stalled run requeued: task #42")).toBeInTheDocument();
    // ...and NOT the raw kind string as a fallback label.
    expect(within(list).queryByText("run_requeued")).toBeNull();
    // the deep link works exactly like the other kinds'
    const link = within(list).getByRole("link", { name: "View task" });
    expect(link).toHaveAttribute("href", "/tasks?task=42");
  });

  it("keeps the three original kinds rendering beside the new one, order intact", async () => {
    const user = userEvent.setup();
    const now = new Date().toISOString();
    mockApiGet.mockResolvedValue({
      items: [
        notif("a", "Newest requeued", "run_requeued", 1, now),
        notif("b", "Shipped the report", "task_completed", 2, now),
        notif("c", "Blew up", "task_failed", 3, now),
        notif("d", "Waiting on you", "awaiting_approval", 4, now),
      ],
      count: 4,
    });

    render(<Topbar />);
    await screen.findByTestId("notifications-badge");
    await user.click(screen.getByTestId("topbar-notifications"));

    const list = await screen.findByTestId("notifications-list");
    expect(within(list).getAllByRole("listitem").map((li) => li.getAttribute("data-kind"))).toEqual([
      "run_requeued",
      "task_completed",
      "task_failed",
      "awaiting_approval",
    ]);
    for (const label of ["Run requeued", "Completed", "Failed", "Awaiting approval"]) {
      expect(within(list).getByText(label)).toBeInTheDocument();
    }
  });
});

/* --------------------------------------------------------------------------- 
 * Tasks page — the ?task=N deep link the bell's View task links point at
 * ------------------------------------------------------------------------- */

describe("Tasks page — ?task=N deep link", () => {
  it("opens the requested task's detail sheet on load when the loaded set contains it", async () => {
    mockSearch.task = "42";
    mockApiGet.mockResolvedValue([
      taskRow({ id: 7, title: "fixture: other task" }),
      taskRow({ id: 42, title: "fixture: deep-linked task" }),
    ]);

    render(<TasksPage />);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "fixture: deep-linked task" })).toBeInTheDocument();
    // no error surface rode along with the deep link
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/Could not load tasks/)).toBeNull();
  });

  it("an id outside the loaded set is an honest no-op — no sheet, no alert, board stays healthy", async () => {
    mockSearch.task = "999";
    mockApiGet.mockResolvedValue([taskRow({ id: 7, title: "fixture: other task" })]);

    render(<TasksPage />);

    await screen.findByRole("button", { name: "Open task: fixture: other task" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it.each(["abc", "-3", "5.5", "99999999999999999999"])(
    "a junk task param (%s) is the same honest no-op",
    async (junk) => {
      mockSearch.task = junk;
      mockApiGet.mockResolvedValue([taskRow()]);

      render(<TasksPage />);

      await screen.findByRole("button", { name: "Open task: fixture: deep-linked task" });
      expect(screen.queryByRole("dialog")).toBeNull();
      expect(screen.queryByRole("alert")).toBeNull();
    },
  );

  it("a param change while mounted (a bell click on this route) opens the newly requested task", async () => {
    mockApiGet.mockResolvedValue([
      taskRow({ id: 7, title: "fixture: first task" }),
      taskRow({ id: 9, title: "fixture: second task" }),
    ]);

    const { rerender } = render(<TasksPage />);
    await screen.findByRole("button", { name: "Open task: fixture: first task" });
    expect(screen.queryByRole("dialog")).toBeNull();

    mockSearch.task = "9";
    rerender(<TasksPage />);

    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByRole("heading", { name: "fixture: second task" })).toBeInTheDocument();
  });

  it("closing the sheet and refreshing with the same param does NOT re-open it", async () => {
    const user = userEvent.setup();
    mockSearch.task = "7";
    mockApiGet.mockResolvedValue([taskRow({ id: 7, title: "fixture: deep-linked task" })]);

    render(<TasksPage />);

    await screen.findByRole("dialog");
    await user.click(screen.getByRole("button", { name: "Close task detail" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    // the explicit refresh re-reads the rows (same param still in the URL)...
    await user.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(mockApiGet).toHaveBeenCalledTimes(2));

    // ...and the closed sheet stays closed: a consumed param is not re-consumed.
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
