import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), delete: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import type { AgentStatus } from "@/lib/api";
import AgentsPage from "@/app/agents/page";
import TasksPage from "@/app/tasks/page";
import { TASKS_BOARD_LIMIT } from "@/components/tasks/TaskBoard";
import type { TaskRow } from "@/components/tasks/types";

const mockGet = api.get as jest.Mock;
const mockPost = api.post as jest.Mock;

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

function task(overrides: Partial<TaskRow> = {}): TaskRow {
  return {
    id: 1,
    title: "Guide owners to their first connection",
    agent_type: "orchestrator",
    status: "pending",
    priority: 3,
    created_at: ago(2 * DAY_MS),
    ...overrides,
  };
}

const AGENT_NAMES = [
  "orchestrator",
  "business_planning",
  "competitor_research",
  "social_media",
  "ads_management",
  "email_outreach",
  "code_generation",
  "customer_support",
  "finance",
];

function nineAgents(): AgentStatus[] {
  return AGENT_NAMES.map((type, index) => ({
    agent_type: type,
    last_run_at: index === 0 ? null : ago(index * HOUR_MS + HOUR_MS),
    last_run_status: index === 0 ? null : "completed",
    tasks_today: index,
    tasks_total: index * 2,
  }));
}

const statusCalls = () =>
  mockGet.mock.calls.filter((call) => String(call[0]).startsWith("/agents/status")).length;

afterEach(() => {
  mockGet.mockReset();
  mockPost.mockReset();
});

// ---------------------------------------------------------------------------------------------
// tasks.md §5 — the acceptance bar.
// ---------------------------------------------------------------------------------------------

describe("tasks board (tasks.md ACC 1-3)", () => {
  it("derives TO DO / DONE counts from the wire and renders the non-completed vocabularies (ACC 1)", async () => {
    mockGet.mockResolvedValue([
      task({ id: 1, title: "Open one", status: "pending" }),
      task({ id: 2, title: "Open two", status: "in_progress" }),
      task({ id: 3, title: "Open three", status: "awaiting_approval" }),
      task({ id: 4, title: "Done one", status: "completed" }),
      task({ id: 5, title: "Done two", status: "completed" }),
      task({ id: 6, title: "Refused one", status: "rejected" }),
      task({ id: 7, title: "Failed one", status: "failed" }),
      task({ id: 8, title: "Statless one", status: null }),
    ]);

    render(<TasksPage />);
    expect(await screen.findByText("Open one")).toBeInTheDocument();

    const todo = document.querySelector('[data-group="todo"]') as HTMLElement;
    const done = document.querySelector('[data-group="done"]') as HTMLElement;
    expect(within(todo).getByRole("button", { name: "TO DO" })).toBeInTheDocument();
    expect(within(todo).getByText("3")).toBeInTheDocument();
    expect(within(done).getByRole("button", { name: "DONE" })).toBeInTheDocument();
    expect(within(done).getByText("2")).toBeInTheDocument();

    // Ours-only states render honestly, and the terminal-negative + unknown buckets are visible
    // rather than dropped.
    const notDone = document.querySelector('[data-group="notdone"]') as HTMLElement;
    expect(within(notDone).getByText("rejected")).toBeInTheDocument();
    expect(within(notDone).getByText("failed")).toBeInTheDocument();
    expect(within(todo).getByText("awaiting_approval")).toBeInTheDocument();
    const unknown = document.querySelector('[data-group="unknown"]') as HTMLElement;
    expect(within(unknown).getByText("unknown")).toBeInTheDocument();

    // A short page is NOT labelled as truncated.
    expect(screen.queryByText(/Showing the first 500/)).toBeNull();
  });

  it("labels a limit-hit fetch instead of inventing a total (ACC 1, Q-12)", async () => {
    mockGet.mockResolvedValue(
      Array.from({ length: TASKS_BOARD_LIMIT }, (_, index) =>
        task({ id: index + 1, title: `Task ${index + 1}`, status: "pending" }),
      ),
    );

    render(<TasksPage />);

    expect(await screen.findByText(/Showing the first 500 tasks/)).toBeInTheDocument();
  });

  it("clamps the card, keeps the raw agent_type chip, and never prints an Invalid Date (ACC 2)", async () => {
    const longDescription = "x".repeat(400);
    mockGet.mockResolvedValue([
      task({ id: 7, title: "Long one", description: longDescription, created_at: null, status: null }),
    ]);

    const user = userEvent.setup();
    render(<TasksPage />);

    const card = await screen.findByRole("button", { name: "Open task: Long one" });
    const description = card.querySelector(".line-clamp-2");
    expect(description).not.toBeNull();
    expect(description?.textContent).toBe(longDescription);
    expect(card.querySelector(".truncate")).not.toBeNull();
    expect(within(card).getByText("orchestrator")).toBeInTheDocument();
    expect(within(card).getByText("unknown")).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();

    // The null date surfaces as the placeholder where a date WOULD be shown (the sheet's row).
    await user.click(card);
    const dialog = await screen.findByRole("dialog");
    expect(within(dialog).getByText("date unknown")).toBeInTheDocument();
    expect(screen.queryByText(/Invalid Date/)).toBeNull();
  });

  it("draws no invented controls or prices, and exactly one primary per task (ACC 3)", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue([
      task({ id: 1, title: "First task" }),
      task({ id: 2, title: "Parked task", status: "awaiting_approval", result_summary: null }),
    ]);

    render(<TasksPage />);
    await screen.findByText("First task");

    const pageText = document.body.textContent ?? "";
    expect(pageText).not.toMatch(/credit/i);
    expect(pageText).not.toMatch(/\$\s?\d/);
    // No control for the endpoints that do not exist, and no dead-control tokens either.
    const names = screen.getAllByRole("button").map((button) => (button.textContent ?? "").trim());
    for (const name of names) expect(name).not.toMatch(/^(edit|delete)$/i);
    expect(pageText).not.toMatch(/\bEDIT\b|\bDELETE\b/);
    // The board itself carries no primary action.
    expect(document.querySelectorAll(".btn-glossy")).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Open task: First task" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog.querySelectorAll(".btn-glossy")).toHaveLength(1);
    expect(within(dialog).getByRole("button", { name: /run now/i })).toBeInTheDocument();
  });

  it("issues the trigger with the task title and shows the server's message on 202 (ACC 4)", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue([
      task({ id: 4, title: "Draft the launch checklist", agent_type: "business_planning" }),
    ]);
    mockPost.mockResolvedValueOnce({
      status: "queued",
      task_id: 77,
      message: "Task #77 queued for business_planning.",
    });

    render(<TasksPage />);
    await user.click(await screen.findByRole("button", { name: "Open task: Draft the launch checklist" }));
    await user.click(within(await screen.findByRole("dialog")).getByRole("button", { name: /run now/i }));

    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith(
      "/agents/business_planning/trigger",
      expect.objectContaining({ task_title: "Draft the launch checklist" }),
    );
    expect(await screen.findByRole("status")).toHaveTextContent("Task #77 queued for business_planning.");
  });

  it("shows a trigger refusal with its code + fix hint and re-enables the button (ACC 4)", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue([task({ id: 4, title: "Retry me" })]);
    mockPost.mockRejectedValueOnce(
      new ApiError(
        503,
        "the broker refused the enqueue",
        "E_REDIS",
        "check REDIS_URL and that the broker is running",
      ),
    );

    render(<TasksPage />);
    await user.click(await screen.findByRole("button", { name: "Open task: Retry me" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /run now/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_REDIS");
    expect(alert).toHaveTextContent("check REDIS_URL and that the broker is running");
    // The failure wears the alert tone (data-tone="alert" + border-alert), never the success one.
    expect(alert).toHaveAttribute("data-tone", "alert");
    expect(screen.queryByRole("status")).toBeNull();
    expect(within(dialog).getByRole("button", { name: /run now/i })).toBeEnabled();
  });

  it("renders the approve control for a parked task and posts fingerprint + task_id (ACC 5)", async () => {
    const user = userEvent.setup();
    const fingerprint = "9f2c1e4a5b6d7c8e";
    mockGet.mockResolvedValue([
      task({
        id: 42,
        title: "Parked on a gated action",
        status: "awaiting_approval",
        result_summary: `awaiting approval: 1 gated action(s) need a token; request one for fingerprint(s) ${fingerprint}`,
      }),
    ]);
    mockPost.mockResolvedValueOnce({
      token: "tok_secret_value_123",
      action_fingerprint: fingerprint,
      expires_at: Math.floor(Date.now() / 1000) + 3600,
      ttl_s: 3600,
    });

    render(<TasksPage />);
    await user.click(await screen.findByRole("button", { name: "Open task: Parked on a gated action" }));
    const dialog = await screen.findByRole("dialog");
    // A parked task's primary is the approval — it is never also offered a new-run button.
    expect(within(dialog).queryByRole("button", { name: /run now/i })).toBeNull();

    await user.click(within(dialog).getByRole("button", { name: /approve/i }));
    expect(mockPost).toHaveBeenCalledWith("/approvals", {
      action_fingerprint: fingerprint,
      task_id: 42,
    });
    const status = await screen.findByRole("status");
    expect(status).toHaveTextContent(/attached to task #42/);
    // The minted token is a secret: the response carries it, the sheet never renders it.
    expect(screen.queryByText(/tok_secret_value_123/)).toBeNull();
  });

  it("shows the E_CONFIG settings hint (not a dead button) when the approvals channel is unset (ACC 5)", async () => {
    const user = userEvent.setup();
    const fingerprint = "9f2c1e4a5b6d7c8e";
    mockGet.mockResolvedValue([
      task({
        id: 42,
        title: "Parked on a gated action",
        status: "awaiting_approval",
        result_summary: `awaiting approval: 1 gated action(s) need a token; request one for fingerprint(s) ${fingerprint}`,
      }),
    ]);
    mockPost.mockRejectedValueOnce(
      new ApiError(
        503,
        "the approvals channel is not configured: APPROVAL_SECRET is unset, so no token could ever verify",
        "E_CONFIG",
        "set APPROVAL_SECRET in .env (see .env.example) and restart the service",
      ),
    );

    render(<TasksPage />);
    await user.click(await screen.findByRole("button", { name: "Open task: Parked on a gated action" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: /approve/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_CONFIG");
    expect(alert).toHaveTextContent(/APPROVAL_SECRET/);
    expect(within(dialog).getByRole("button", { name: /approve/i })).toBeEnabled();
  });

  it("renders three distinct states: skeleton, named failure, and the empty sentence (ACC 6)", async () => {
    mockGet.mockImplementation(() => new Promise(() => {}));
    const { container, unmount } = render(<TasksPage />);
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(container.querySelectorAll(".animate-pulse").length).toBeGreaterThan(0);
    expect(screen.queryByText(/No tasks yet/)).toBeNull();
    expect(screen.queryByRole("alert")).toBeNull();
    unmount();

    mockGet.mockReset();
    mockGet.mockRejectedValue(
      new ApiError(503, "the task list is unreadable right now", "E_REDIS", "check REDIS_URL and that the broker is running"),
    );
    const { unmount: unmountError } = render(<TasksPage />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(
      "E_REDIS: the task list is unreadable right now — check REDIS_URL and that the broker is running",
    );
    expect(screen.queryByText(/No tasks yet/)).toBeNull();
    unmountError();

    mockGet.mockReset();
    mockGet.mockResolvedValue([]);
    render(<TasksPage />);
    expect(
      await screen.findByText("No tasks yet. Agents will create tasks during their cycles."),
    ).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("sheet mechanics: scrim token, modal semantics, Esc close, focus return (sheets §8)", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue([task({ id: 3, title: "Inspect me" })]);

    render(<TasksPage />);
    const card = await screen.findByRole("button", { name: "Open task: Inspect me" });
    await user.click(card);

    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    const scrim = dialog.parentElement as HTMLElement;
    expect(scrim.getAttribute("style")).toContain("rgba(0, 0, 0, 0.55)");
    expect(dialog.contains(document.activeElement)).toBe(true);
    expect(card.closest('[aria-hidden="true"]')).not.toBeNull();

    await user.keyboard("{Escape}");
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(document.activeElement).toBe(card);
  });
});

// ---------------------------------------------------------------------------------------------
// agents-page.md §5 — the acceptance bar.
// ---------------------------------------------------------------------------------------------

describe("agents roster (agents-page.md ACC 1-6)", () => {
  it("renders the nine registry rows from the wire, with NEVER for a never-run agent (ACC 1)", async () => {
    mockGet.mockResolvedValue(nineAgents());

    render(<AgentsPage />);
    expect(await screen.findByText("Orchestrator")).toBeInTheDocument();

    const rows = screen.getAllByRole("article");
    expect(rows).toHaveLength(9);

    const never = rows.find((row) => row.getAttribute("data-agent") === "orchestrator") as HTMLElement;
    expect(within(never).getByText("NEVER RUN")).toBeInTheDocument();
    expect(within(never).getByText("LAST RUN NEVER")).toBeInTheDocument();
    expect(never.textContent).toContain("0 TODAY");
    expect(never.textContent).toContain("0 TOTAL");

    const finance = rows.find((row) => row.getAttribute("data-agent") === "finance") as HTMLElement;
    expect(finance.textContent).toContain("8 TODAY");
    expect(finance.textContent).toContain("16 TOTAL");

    // Q-21's exact prohibition.
    expect(document.body.textContent ?? "").not.toMatch(/idle/i);
  });

  it("renders an unknown agent type without an empty description line (ACC 2)", async () => {
    mockGet.mockResolvedValue([
      ...nineAgents(),
      {
        agent_type: "mystery_agent",
        last_run_at: null,
        last_run_status: null,
        tasks_today: 0,
        tasks_total: 0,
      },
    ]);

    render(<AgentsPage />);
    const rows = await screen.findAllByRole("article");
    expect(rows).toHaveLength(10);

    const unknown = rows.find((row) => row.getAttribute("data-agent") === "mystery_agent") as HTMLElement;
    expect(unknown.textContent).toContain("mystery agent");
    // Only the facts paragraph — the description paragraph is omitted, never empty.
    expect(unknown.querySelectorAll("p")).toHaveLength(1);
  });

  it("fires one trigger per confirmed run, keeps the row pending, and refetches after the 202 (ACC 3 + 5)", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue(nineAgents());
    let resolvePost: ((value: unknown) => void) | null = null;
    mockPost.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          resolvePost = resolve;
        }),
    );

    render(<AgentsPage />);
    await screen.findByText("Orchestrator");
    const row = screen
      .getAllByRole("article")
      .find((candidate) => candidate.getAttribute("data-agent") === "orchestrator") as HTMLElement;

    // The confirm gate: Run now opens it, nothing is sent by opening it, and cancelling sends nothing.
    await user.click(within(row).getByRole("button", { name: /run now/i }));
    expect(mockPost).not.toHaveBeenCalled();
    expect(within(row).getByText(/creates a NEW task/i)).toBeInTheDocument();
    await user.click(within(row).getByRole("button", { name: /cancel/i }));
    expect(within(row).queryByRole("button", { name: /create task/i })).toBeNull();
    expect(mockPost).not.toHaveBeenCalled();
    expect(statusCalls()).toBe(1);

    // Confirming fires exactly one POST; a second click while in flight does not fire another.
    await user.click(within(row).getByRole("button", { name: /run now/i }));
    await user.click(within(row).getByRole("button", { name: /create task/i }));
    expect(mockPost).toHaveBeenCalledTimes(1);
    expect(mockPost).toHaveBeenCalledWith(
      "/agents/orchestrator/trigger",
      expect.objectContaining({ task_title: expect.stringContaining("Orchestrator") }),
    );
    const pendingButton = within(row).getByRole("button", { name: /triggering/i });
    expect(pendingButton).toBeDisabled();
    await user.click(pendingButton);
    expect(mockPost).toHaveBeenCalledTimes(1);

    await act(async () => {
      resolvePost?.({ status: "queued", task_id: 7, message: "Task #7 queued for orchestrator." });
    });

    expect(await screen.findByRole("status")).toHaveTextContent("Task #7 queued for orchestrator.");
    // The 202 triggers a fresh GET /agents/status (Q-20: no refetch after a trigger).
    await waitFor(() => expect(statusCalls()).toBe(2));
  });

  it("renders a refused trigger with its code + fix hint, not the success styling (ACC 4)", async () => {
    const user = userEvent.setup();
    mockGet.mockResolvedValue(nineAgents());
    mockPost.mockRejectedValueOnce(
      new ApiError(
        503,
        "the broker refused the enqueue",
        "E_REDIS",
        "check REDIS_URL and that the broker is running",
      ),
    );

    render(<AgentsPage />);
    await screen.findByText("Orchestrator");
    const row = screen
      .getAllByRole("article")
      .find((candidate) => candidate.getAttribute("data-agent") === "orchestrator") as HTMLElement;

    await user.click(within(row).getByRole("button", { name: /run now/i }));
    await user.click(within(row).getByRole("button", { name: /create task/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_REDIS");
    expect(alert).toHaveTextContent("check REDIS_URL and that the broker is running");
    expect(alert).toHaveAttribute("data-tone", "error");
    expect(screen.queryByRole("status")).toBeNull();
    expect(within(row).getByRole("button", { name: /run now/i })).toBeEnabled();
  });

  it("renders loading and failed-poll states without ever claiming an empty roster (ACC 6)", async () => {
    mockGet.mockImplementation(() => new Promise(() => {}));
    const { container, unmount } = render(<AgentsPage />);
    expect(container.querySelector('[aria-busy="true"]')).not.toBeNull();
    expect(screen.queryAllByRole("article")).toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull();
    unmount();

    mockGet.mockReset();
    mockGet.mockRejectedValue(new ApiError(0, "request timed out", "E_TIMEOUT", ""));
    render(<AgentsPage />);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/Could not load agent status/);
    expect(screen.queryAllByRole("article")).toHaveLength(0);
    expect(screen.queryByText(/no agent/i)).toBeNull();
  });

  it("says a rows-less success out loud instead of implying a configured roster", async () => {
    mockGet.mockResolvedValue([]);
    render(<AgentsPage />);
    // V26/L1 re-pin (deliberate): the empty-state copy changed with the brief's honest
    // "no agents reported" wording. Old -> new:
    //   "The status endpoint returned no agent rows."
    //   -> "No agents reported — the status endpoint answered successfully with an empty list."
    // The pin's intent is unchanged and is now checked harder: still no card, and the empty
    // answer is still not allowed to look like a failure.
    expect(
      await screen.findByText(/no agents reported — the status endpoint answered successfully with an empty list\./i),
    ).toBeInTheDocument();
    expect(screen.queryAllByRole("article")).toHaveLength(0);
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
