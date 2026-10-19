import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * Wave V24 / L1 — runnable tasks (frontend half). Suite `v24-task-run`.
 *
 * The behaviour under test is the NEW verb `POST /api/v1/tasks/{id}/run` (202) and its one
 * honest surface: a `pending` row/card gains **Run this task**; the button is disabled with
 * `aria-busy` while the request is in flight; a refusal renders inline through the shared
 * `readErrorText` (`ApiError.display` — code + how_to_fix); a 202 triggers the board's ONE
 * refetch path and the card then shows whatever the SERVER says (`in_progress`, later
 * `completed`) — never an optimistic status invented by the client.
 *
 * Mocking strategy: `fetch` is mocked (the `u7-task-actions` idiom), so the REAL client path
 * runs — URL building, method, refusal parsing — instead of a mocked `api` module.
 */
const mockRouter = { push: jest.fn() };

jest.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
  useSearchParams: () => null,
}));

jest.mock("next/link", () => ({
  __esModule: true,
  default: function MockLink(props: { href: string; children: React.ReactNode }) {
    const { href, children } = props;
    return <a href={href}>{children}</a>;
  },
}));

import TasksPage from "@/app/tasks/page";
import { NewTaskDialog } from "@/components/tasks/NewTaskDialog";
import { TaskCard } from "@/components/tasks/TaskCard";
import { TaskDetailContent } from "@/components/tasks/TaskDetailContent";
import type { TaskRow } from "@/components/tasks/types";

type FetchLikeResponse = {
  ok: boolean;
  status: number;
  statusText: string;
  headers: { get: (name: string) => string | null };
  text: () => Promise<string>;
  json: () => Promise<unknown>;
};

function jsonResponse(status: number, body: unknown): FetchLikeResponse {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "mock status",
    headers: { get: () => null },
    text: async () => JSON.stringify(body),
    json: async () => body,
  };
}

const fetchMock = jest.fn();

beforeEach(() => {
  fetchMock.mockReset();
  mockRouter.push.mockReset();
  (global as unknown as { fetch: jest.Mock }).fetch = fetchMock;
});

function task(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 31,
    title: "Draft the launch checklist",
    agent_type: "business_planning",
    status: "pending",
    priority: 3,
    created_at: new Date(Date.now() - 60_000).toISOString(),
    ...overrides,
  };
}

const runButton = () => screen.getByRole("button", { name: /^run this task$/i });

// ---------------------------------------------------------------------------------------------
// The action exists ONLY where it can work: pending rows/cards.
// ---------------------------------------------------------------------------------------------

describe("Run this task — visibility", () => {
  it.each(["in_progress", "completed", "failed", "awaiting_approval", "cancelled"])(
    "%s: no Run this task control (nothing dispatchable)",
    (status) => {
      render(<TaskCard task={task({ status })} isNext={false} onOpen={jest.fn()} />);
      expect(screen.queryByTestId("task-run-this")).toBeNull();
      expect(screen.queryByRole("button", { name: /run this task/i })).toBeNull();
    },
  );

  it("pending: the control is rendered — and it is NOT one of the pinned quick-action chips", () => {
    render(<TaskCard task={task()} isNext={false} onOpen={jest.fn()} />);

    expect(within(screen.getByTestId("task-run-this")).getByRole("button", { name: /run this task/i })).toBeInTheDocument();
    // The V7 quick-action row keeps exactly its shipped labels (Open + Run now).
    expect(within(screen.getByTestId("task-actions")).getAllByRole("button").map((b) => b.textContent)).toEqual([
      "Open",
      "Run now",
    ]);
  });
});

// ---------------------------------------------------------------------------------------------
// Exact POST + in-flight busy state + the refetch callback (no optimistic status).
// ---------------------------------------------------------------------------------------------

describe("Run this task — the request and the busy state", () => {
  it("POSTs /tasks/{id}/run exactly once, disables the button with aria-busy, then refetches", async () => {
    const user = userEvent.setup();
    let resolveFetch!: (value: FetchLikeResponse) => void;
    fetchMock.mockImplementationOnce(
      () =>
        new Promise<FetchLikeResponse>((resolve) => {
          resolveFetch = resolve;
        }),
    );
    const onTaskQueued = jest.fn();

    render(<TaskCard task={task({ id: 31 })} isNext={false} onOpen={jest.fn()} onTaskQueued={onTaskQueued} />);

    await user.click(runButton());

    // In flight: one request, disabled, aria-busy, and a label that states the work.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/api\/v1\/tasks\/31\/run$/);
    expect(init.method).toBe("POST");

    const busy = screen.getByRole("button", { name: /dispatching/i });
    expect(busy).toBeDisabled();
    expect(busy).toHaveAttribute("aria-busy", "true");

    resolveFetch(jsonResponse(202, { task_id: 31, status: "dispatched" }));
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Task #31 dispatched");
    expect(onTaskQueued).toHaveBeenCalledTimes(1);

    // No optimistic status: the card still renders the SERVER's status word.
    expect(screen.getByText("pending")).toBeInTheDocument();
    expect(screen.queryByText("in_progress")).toBeNull();
    // The latch keeps a second enqueue of the same pending row off the wire.
    expect(screen.getByRole("button", { name: /run this task/i })).toBeDisabled();
  });

  it("a refusal renders inline via the shared readErrorText and re-arms the control", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValueOnce(
      jsonResponse(409, {
        detail: {
          code: "E_TASK_NOT_RUNNABLE",
          message: "task 31 is 'completed', not 'pending'; only a queued task can be run",
          how_to_fix: "inspect the task's status first",
          context: { task_id: 31, status: "completed" },
        },
      }),
    );
    const onTaskQueued = jest.fn();

    render(<TaskCard task={task()} isNext={false} onOpen={jest.fn()} onTaskQueued={onTaskQueued} />);
    await user.click(runButton());

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_TASK_NOT_RUNNABLE:");
    expect(alert).toHaveTextContent("inspect the task's status first");
    expect(onTaskQueued).not.toHaveBeenCalled();
    // A refusal dispatched nothing: the control is usable again.
    expect(screen.getByRole("button", { name: /run this task/i })).toBeEnabled();
  });
});

// ---------------------------------------------------------------------------------------------
// The board's refetch path: the list reflects the SERVER's transition in_progress -> completed.
// ---------------------------------------------------------------------------------------------

describe("Run this task — the board refetch", () => {
  it("re-reads after the dispatch and renders in_progress, then completed after Refresh", async () => {
    const user = userEvent.setup();
    let gets = 0;
    fetchMock.mockImplementation((url: string, init?: RequestInit) => {
      if (init?.method === "POST") {
        return Promise.resolve(jsonResponse(202, { task_id: 31, status: "dispatched" }));
      }
      gets += 1;
      if (gets === 1) return Promise.resolve(jsonResponse(200, [task({ status: "pending" })]));
      if (gets === 2) return Promise.resolve(jsonResponse(200, [task({ status: "in_progress" })]));
      return Promise.resolve(jsonResponse(200, [task({ status: "completed", completed_at: new Date().toISOString() })]));
    });

    render(<TasksPage />);
    await screen.findByText("Draft the launch checklist");
    expect(screen.getByText("pending")).toBeInTheDocument();

    await user.click(runButton());
    // The 202 triggers the page's one-shot refetch: the status shown is the server's answer.
    expect(await screen.findByText("in_progress")).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /^refresh$/i }));
    expect(await screen.findByText("completed")).toBeInTheDocument();
    expect(gets).toBeGreaterThanOrEqual(3);
  });
});

// ---------------------------------------------------------------------------------------------
// The detail surface (sheet body + /tasks/[id]) gains the same action, pending only.
// ---------------------------------------------------------------------------------------------

describe("Run this task — the detail surface", () => {
  it("pending: posts the exact route; completed: no control", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValueOnce(jsonResponse(202, { task_id: 7, status: "dispatched" }));

    const { unmount } = render(<TaskDetailContent task={task({ id: 7 })} />);
    const section = screen.getByTestId("run-this-task");
    await user.click(within(section).getByRole("button", { name: /run this task/i }));

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/api\/v1\/tasks\/7\/run$/);
    expect(init.method).toBe("POST");
    await screen.findByRole("status");
    expect(screen.getByTestId("run-this-task")).toBeInTheDocument();
    unmount();

    render(<TaskDetailContent task={task({ id: 8, status: "completed", completed_at: new Date().toISOString() })} />);
    expect(screen.queryByTestId("run-this-task")).toBeNull();
  });
});

// ---------------------------------------------------------------------------------------------
// The New Task dialog says what creation does (and does not do).
// ---------------------------------------------------------------------------------------------

describe("New task dialog copy", () => {
  it("states that a created task waits in the queue until it is run (or an agent cycle claims it)", () => {
    render(<NewTaskDialog onClose={jest.fn()} />);
    expect(
      screen.getByText(/waits there.*until you run it \(or an agent cycle claims it\)/i),
    ).toBeInTheDocument();
  });
});
