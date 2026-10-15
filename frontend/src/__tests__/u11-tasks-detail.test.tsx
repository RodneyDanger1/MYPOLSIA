import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * Wave V11 / unit 2 — the task detail route, the status write and the New Task dialog
 * (wiring audit F6/F7). Suite `u11-tasks-detail`.
 *
 * House mocking pattern: the factory replaces `@/lib/api`'s `api` with jest.fn()s while keeping
 * the actual module's classes (`ApiError` stays the real class for `errorText`'s instanceof).
 * Every write is asserted as the EXACT call — path AND body `toEqual` (no objectContaining), so
 * an added/renamed payload key fails here.
 */
jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), patch: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import TasksPage from "@/app/tasks/page";
import TaskDetailPage from "@/app/tasks/[id]/page";
import { parseTaskId } from "@/app/tasks/[id]/parseTaskId";
import { TASK_STATUS_VALUES } from "@/components/tasks/TaskStatusControl";
import { VALID_AGENT_TYPES } from "@/components/tasks/NewTaskDialog";
import type { TaskRow } from "@/components/tasks/types";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;
/** api.patch is the frozen W3 interface; read through a cast so this suite compiles before W3's
 *  merge lands (runtime always comes from the mock above — the brief's isolation rule). */
const mockPatch = (api as unknown as { patch: jest.Mock }).patch;

const HOUR_MS = 60 * 60 * 1000;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

beforeEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
  mockPatch.mockReset();
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function taskRow(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 1,
    title: "Task one",
    agent_type: "orchestrator",
    status: "pending",
    priority: 3,
    created_at: ago(2 * HOUR_MS),
    ...overrides,
  };
}

// ---------------------------------------------------------------------------------------------
// /tasks/[id] — the addressable detail (F6)
// ---------------------------------------------------------------------------------------------

describe("/tasks/[id] detail page", () => {
  it("reads GET /tasks/{id} after a real loading state and renders the fields null-safe", async () => {
    const d = deferred<TaskRow>();
    mockGet.mockReturnValueOnce(d.promise);

    render(<TaskDetailPage params={{ id: "5" }} />);

    expect(screen.getByTestId("task-detail-loading")).toHaveAttribute("aria-busy", "true");
    expect(mockGet).toHaveBeenCalledWith("/tasks/5");

    d.resolve(
      taskRow({ id: 5, title: "Deep-linked detail", status: null, priority: null, created_at: null }),
    );

    await screen.findByRole("heading", { name: "Deep-linked detail" });
    // A null status is the word "unknown" on the muted chip — never an empty element.
    expect(screen.getByText("unknown", { selector: ".chip" })).toHaveClass("text-ink-muted");
    // A null priority is the em dash — never a fabricated 3.
    expect(screen.getByText("—")).toBeInTheDocument();
    expect(screen.getByText("date unknown")).toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toMatch(/Invalid Date/);
  });

  it("a junk id is an honest no-request: nothing is fetched and the route says what it expects", () => {
    render(<TaskDetailPage params={{ id: "abc" }} />);

    expect(mockGet).not.toHaveBeenCalled();
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("Not a task id");
    expect(alert).toHaveTextContent("abc");
    expect(screen.getByRole("link", { name: "the tasks board" })).toHaveAttribute("href", "/tasks");
  });

  it("a missing task renders the E_NOT_FOUND refusal by name, with the way back", async () => {
    mockGet.mockRejectedValueOnce(
      new ApiError(404, "task 999 does not exist", "E_NOT_FOUND", "check the task id against GET /api/v1/tasks"),
    );

    render(<TaskDetailPage params={{ id: "999" }} />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_NOT_FOUND");
    expect(alert).toHaveTextContent("task 999 does not exist");
    expect(alert).toHaveTextContent("check the task id against GET /api/v1/tasks");
    expect(screen.getByRole("link", { name: "All tasks" })).toHaveAttribute("href", "/tasks");
  });

  it("parseTaskId accepts only positive integers (the same contract as ?task=N)", () => {
    expect(parseTaskId("5")).toBe(5);
    expect(parseTaskId("0")).toBeNull();
    expect(parseTaskId("-3")).toBeNull();
    expect(parseTaskId("5.5")).toBeNull();
    expect(parseTaskId("abc")).toBeNull();
    expect(parseTaskId(null)).toBeNull();
    expect(parseTaskId(undefined)).toBeNull();
  });

  it("offers exactly the seven wire statuses the service's guard accepts, current one marked", async () => {
    mockGet.mockResolvedValueOnce(taskRow({ id: 5, status: "pending" }));
    render(<TaskDetailPage params={{ id: "5" }} />);
    await screen.findByRole("heading", { name: "Task one" });

    const select = screen.getByLabelText("New status");
    expect(within(select).getAllByRole("option").map((o) => (o as HTMLOptionElement).value)).toEqual([
      "",
      ...TASK_STATUS_VALUES,
    ]);
    expect(within(select).getByRole("option", { name: "pending (current)" })).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------------------------
// The status write — PATCH /tasks/{id}/status via api.patch (frozen interface)
// ---------------------------------------------------------------------------------------------

describe("status write", () => {
  it("sends EXACTLY {status} to /tasks/{id}/status, then shows the server's returned state", async () => {
    const row = taskRow({ id: 5, title: "Status target", status: "pending" });
    mockGet.mockResolvedValue(row);
    mockPatch.mockResolvedValueOnce({ ...row, status: "cancelled" });
    const user = userEvent.setup();

    render(<TaskDetailPage params={{ id: "5" }} />);
    await screen.findByRole("heading", { name: "Status target" });

    await user.selectOptions(screen.getByLabelText("New status"), "cancelled");
    await user.click(screen.getByRole("button", { name: /^set status$/i }));

    // The frozen payload shape: path + body exactly, one argument each, nothing else sent.
    expect(mockPatch.mock.calls[0]).toEqual(["/tasks/5/status", { status: "cancelled" }]);

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent('Task #5 status set to "cancelled"');
    expect(notice).toHaveTextContent("the server's response, not a local guess");

    // Success updates the VISIBLE state — the chip now carries the server's returned status.
    expect(screen.getByText("cancelled", { selector: ".chip" })).toHaveClass("text-neutral-600");
    // ...and the surface re-reads the task (read-after-write).
    await waitFor(() => expect(mockGet).toHaveBeenCalledTimes(2));
  });

  it("a refused transition renders by name, moves no visible state, and stays armed", async () => {
    const row = taskRow({ id: 5, title: "Refused target", status: "pending" });
    mockGet.mockResolvedValue(row);
    mockPatch.mockRejectedValueOnce(
      new ApiError(
        422,
        "unknown task status 'cancelledd'; valid: pending, in_progress, completed, failed, cancelled, awaiting_approval, rejected",
        "E_VALIDATION",
        "use one of task_service.VALID_TASK_STATUSES; nothing was written",
      ),
    );
    const user = userEvent.setup();

    render(<TaskDetailPage params={{ id: "5" }} />);
    await screen.findByRole("heading", { name: "Refused target" });

    await user.selectOptions(screen.getByLabelText("New status"), "cancelled");
    await user.click(screen.getByRole("button", { name: /^set status$/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_VALIDATION");
    expect(alert).toHaveTextContent("nothing was written");

    // Nothing was written: the chip still reads the row's real status...
    expect(screen.getByText("pending", { selector: ".chip" })).toHaveClass("text-amber-800");
    expect(screen.queryByText("cancelled", { selector: ".chip" })).toBeNull();
    // ...and a different choice can still be attempted.
    expect(screen.getByRole("button", { name: /^set status$/i })).toBeEnabled();
  });

  it("the board's sheet carries the SAME control and the same payload (one write, two surfaces)", async () => {
    const row = taskRow({ id: 21, title: "Board row", status: "pending" });
    mockGet.mockResolvedValue([row]);
    mockPatch.mockResolvedValueOnce({ ...row, status: "in_progress" });
    const user = userEvent.setup();

    render(<TasksPage />);
    await user.click(await screen.findByRole("button", { name: "Open task: Board row" }));
    const dialog = await screen.findByRole("dialog");

    await user.selectOptions(within(dialog).getByLabelText("New status"), "in_progress");
    await user.click(within(dialog).getByRole("button", { name: /^set status$/i }));

    expect(mockPatch.mock.calls[0]).toEqual(["/tasks/21/status", { status: "in_progress" }]);
    // The sheet's chip follows the server's response (the content's override row).
    expect(within(dialog).getByText("in_progress", { selector: ".chip" })).toHaveClass("text-blue-800");
  });
});

// ---------------------------------------------------------------------------------------------
// New Task dialog — POST /tasks with the real create schema (F7)
// ---------------------------------------------------------------------------------------------

describe("New Task dialog", () => {
  it("POSTs the real create schema, reports the server's task, and re-reads the board", async () => {
    mockGet.mockResolvedValue([]);
    mockPost.mockResolvedValueOnce(
      taskRow({ id: 21, title: "Draft the press kit", agent_type: "social_media", created_at: null }),
    );
    const user = userEvent.setup();

    render(<TasksPage />);
    await screen.findByText(/No tasks yet/);

    await user.click(screen.getByRole("button", { name: /^new task$/i }));
    const dialog = await screen.findByRole("dialog");

    const agentSelect = within(dialog).getByLabelText(/^Agent/);
    expect(within(agentSelect).getAllByRole("option").map((o) => (o as HTMLOptionElement).value)).toEqual([
      "",
      ...VALID_AGENT_TYPES,
    ]);

    await user.type(within(dialog).getByLabelText(/^Title/), "Draft the press kit");
    await user.selectOptions(agentSelect, "social_media");
    await user.type(within(dialog).getByLabelText(/^Description/), "One press kit.");
    await user.click(within(dialog).getByRole("button", { name: /^create task$/i }));

    // The exact body: source/scheduled_date never ride (schema defaults), priority is the
    // integer the field carries (3 = the schema's own default).
    expect(mockPost.mock.calls[0]).toEqual([
      "/tasks",
      {
        title: "Draft the press kit",
        agent_type: "social_media",
        description: "One press kit.",
        priority: 3,
      },
    ]);

    expect(await within(dialog).findByText(/Task #21 created/)).toBeInTheDocument();
    await waitFor(() => expect(mockGet.mock.calls.length).toBeGreaterThanOrEqual(2));
  });

  it("a blank description stays off the wire (stored as not stated, never an empty string)", async () => {
    mockGet.mockResolvedValue([]);
    mockPost.mockResolvedValueOnce(taskRow({ id: 22, title: "Minimal", created_at: null }));
    const user = userEvent.setup();

    render(<TasksPage />);
    await screen.findByText(/No tasks yet/);
    await user.click(screen.getByRole("button", { name: /^new task$/i }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByLabelText(/^Title/), "Minimal");
    await user.selectOptions(within(dialog).getByLabelText(/^Agent/), "finance");
    await user.click(within(dialog).getByRole("button", { name: /^create task$/i }));

    expect(mockPost.mock.calls[0]).toEqual([
      "/tasks",
      { title: "Minimal", agent_type: "finance", priority: 3 },
    ]);
  });

  it("keeps Create disabled until the required fields are real (no dead button)", async () => {
    mockGet.mockResolvedValue([]);
    const user = userEvent.setup();

    render(<TasksPage />);
    await screen.findByText(/No tasks yet/);
    await user.click(screen.getByRole("button", { name: /^new task$/i }));
    const dialog = await screen.findByRole("dialog");

    const create = within(dialog).getByRole("button", { name: /^create task$/i });
    expect(create).toBeDisabled();

    await user.type(within(dialog).getByLabelText(/^Title/), "Needs an agent");
    expect(create).toBeDisabled();

    await user.selectOptions(within(dialog).getByLabelText(/^Agent/), "code_generation");
    expect(create).toBeEnabled();
  });

  it("a refusal renders by name and creates nothing (no board refetch)", async () => {
    mockGet.mockResolvedValue([]);
    mockPost.mockRejectedValueOnce(
      new ApiError(
        422,
        "Unknown agent type: 'qa'. Valid: orchestrator, business_planning, ...",
        "E_AGENT_UNKNOWN",
        "use an agent_type from task_service.VALID_AGENT_TYPES",
      ),
    );
    const user = userEvent.setup();

    render(<TasksPage />);
    await screen.findByText(/No tasks yet/);
    await user.click(screen.getByRole("button", { name: /^new task$/i }));
    const dialog = await screen.findByRole("dialog");

    await user.type(within(dialog).getByLabelText(/^Title/), "Doomed draft");
    await user.selectOptions(within(dialog).getByLabelText(/^Agent/), "finance");
    await user.click(within(dialog).getByRole("button", { name: /^create task$/i }));

    const alert = await within(dialog).findByRole("alert");
    expect(alert).toHaveTextContent("E_AGENT_UNKNOWN");
    expect(alert).toHaveTextContent("task_service.VALID_AGENT_TYPES");
    expect(mockGet).toHaveBeenCalledTimes(1);
  });
});
