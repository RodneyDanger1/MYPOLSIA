import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * Wave V8b — seat l2-palette's suite for the Ctrl+K command palette (ui-gap §11).
 *
 * Run: `node node_modules/jest/bin/jest.js --ci u8b-palette < /dev/null` (from `frontend/`).
 *
 * What is pinned here (the sealed brief's list):
 *  1. closed by default (the component renders nothing); Ctrl+K and Cmd+K toggle the dialog;
 *  2. focus lands on the filter input on open, Esc closes and returns focus to the previously
 *     focused element, a scrim press closes;
 *  3. ROUTES carries all ten nav labels in the shared table's order; a query narrows them;
 *  4. TASKS/RUNS/REPORTS render their mocked rows; a failed read renders ONE honest note line
 *     (the error message) inside its section and the section stays visible; the reads are
 *     lazy (issued on first open only, with the pinned paths) and reused for the session;
 *  5. a zero-match query shows the single "No matches." line;
 *  6. ArrowDown ×2 + Enter pushes the second visible item's href and closes;
 *  7. arrows wrap at both ends;
 *  8. Tab is trapped inside the dialog.
 * Plus the honest cap: a section longer than eight renders "…and N more", never a silent cut.
 */

const mockRouter = { push: jest.fn() };

jest.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
}));

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return { ...actual, api: { get: jest.fn() } };
});

import { ApiError, api } from "@/lib/api";
import { NAV_ITEMS } from "@/components/layout/nav-items";
import type { TaskRow } from "@/components/tasks/types";
import type { RunsRow } from "@/components/usage/useRuns";
import { CommandPalette } from "@/components/shell/CommandPalette";

const mockApiGet = api.get as jest.MockedFunction<(path: string) => Promise<unknown>>;

/** The exact read paths the palette speaks (pinned by the brief). */
const TASKS_READ = "/tasks?limit=50";
const RUNS_READ = "/runs?limit=50";
const REPORTS_READ = "/dashboard/reports?limit=20";

/** A `daily_reports` row as `GET /dashboard/reports` serves it (backend `DailyReportResponse`). */
type ReportWireRow = {
  id: number;
  report_date: string;
  morning_plan: string | null;
  evening_summary: string | null;
  tasks_planned: number;
  tasks_completed: number;
};

function taskRow(id: number, title: string): TaskRow {
  return {
    id,
    title,
    agent_type: "social_media",
    status: "pending",
    priority: 3,
    created_at: "2026-10-13T18:00:00Z",
  };
}

function runRow(id: number, taskTitle: string | null): RunsRow {
  return {
    id,
    task_id: 7,
    task_title: taskTitle,
    agent_type: "analytics",
    run_type: "task",
    status: "success",
    attempt: 1,
    started_at: "2026-10-13T18:21:39Z",
    ended_at: "2026-10-13T18:22:10Z",
    duration_secs: 31.2,
    tokens_input: 1200,
    tokens_output: 800,
    tokens_used: 2000,
    cost_usd: 0.4213,
    cost_source: "priced",
    pricing_version: "2026-10-01",
  };
}

function reportRow(id: number, reportDate: string): ReportWireRow {
  return {
    id,
    report_date: reportDate,
    morning_plan: null,
    evening_summary: null,
    tasks_planned: 4,
    tasks_completed: 2,
  };
}

/** One answer per exact request path — an unexpected path is a test bug, not a pass. */
function wire(overrides: Record<string, unknown> = {}): void {
  const answers: Record<string, unknown> = {
    [TASKS_READ]: [taskRow(1, "Draft October newsletter"), taskRow(2, "Audit ad spend")],
    [RUNS_READ]: { items: [runRow(11, "Nightly analytics sync")], total: 1 },
    [REPORTS_READ]: { items: [reportRow(1, "2026-10-13")], total: 1 },
    ...overrides,
  };
  mockApiGet.mockImplementation((path: string) => {
    if (!(path in answers)) return Promise.reject(new Error(`unexpected path in test: ${path}`));
    const answer = answers[path];
    return answer instanceof Error ? Promise.reject(answer) : Promise.resolve(answer);
  });
}

async function openPalette(user: ReturnType<typeof userEvent.setup>) {
  await user.keyboard("{Control>}k{/Control}");
  return screen.getByRole("dialog", { name: "Command palette" });
}

/** All three lazy reads have landed (their rows are visible as options). */
async function waitForReads(): Promise<void> {
  await screen.findByRole("option", { name: /Draft October newsletter/ });
  await screen.findByRole("option", { name: /Nightly analytics sync/ });
  await screen.findByRole("option", { name: /2026-10-13/ });
}

beforeEach(() => {
  mockApiGet.mockReset();
  mockRouter.push.mockReset();
});

/* ---------------------------------------------------------------------------
 * 1. open/close grammar
 * ------------------------------------------------------------------------- */

describe("CommandPalette — open/close grammar", () => {
  it("renders nothing until opened, and Ctrl+K / Cmd+K toggle the dialog", async () => {
    const user = userEvent.setup();
    wire();
    const { container } = render(<CommandPalette />);

    // Mounted globally it renders NOTHING until opened.
    expect(container).toBeEmptyDOMElement();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.keyboard("{Control>}k{/Control}");
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();

    // Pressing the combo again while open closes it (toggle).
    await user.keyboard("{Control>}k{/Control}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();

    await user.keyboard("{Meta>}k{/Meta}");
    expect(screen.getByRole("dialog", { name: "Command palette" })).toBeInTheDocument();
  });

  it("focuses the filter input on open, closes on Esc returning focus to the opener, closes on a scrim press", async () => {
    const user = userEvent.setup();
    wire();
    render(
      <>
        <button type="button">Opener</button>
        <CommandPalette />
      </>,
    );
    const opener = screen.getByRole("button", { name: "Opener" });
    opener.focus();
    expect(opener).toHaveFocus();

    await openPalette(user);
    expect(screen.getByLabelText("Command palette search")).toHaveFocus();

    await user.keyboard("{Escape}");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();

    await openPalette(user);
    await user.click(screen.getByTestId("palette-scrim"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(opener).toHaveFocus();
  });
});

/* ---------------------------------------------------------------------------
 * 2. ROUTES + filtering
 * ------------------------------------------------------------------------- */

describe("CommandPalette — ROUTES", () => {
  it("lists all ten routes first, in the shared nav table's order, and a query narrows them", async () => {
    const user = userEvent.setup();
    wire();
    render(<CommandPalette />);
    await openPalette(user);
    await waitForReads();

    expect(screen.getByRole("group", { name: "ROUTES" })).toBeInTheDocument();
    const options = screen.getAllByRole("option");
    // Ten routes + two tasks + one run + one report.
    expect(options).toHaveLength(NAV_ITEMS.length + 2 + 1 + 1);
    for (const [index, item] of NAV_ITEMS.entries()) {
      expect(options[index].textContent).toContain(item.label);
      // The route path is the row's right-hand hint (where Enter goes).
      expect(options[index].textContent).toContain(item.href);
    }

    await user.type(screen.getByLabelText("Command palette search"), "sett");
    const narrowed = screen.getAllByRole("option");
    expect(narrowed).toHaveLength(1);
    expect(narrowed[0].textContent).toContain("Settings");
  });
});

/* ---------------------------------------------------------------------------
 * 3. the lazy reads: rows, failures, once-per-session
 * ------------------------------------------------------------------------- */

describe("CommandPalette — the TASKS/RUNS/REPORTS reads", () => {
  it("renders the mocked rows lazily, once, and reuses them for the session", async () => {
    const user = userEvent.setup();
    wire();
    render(<CommandPalette />);
    await openPalette(user);
    await waitForReads();

    expect(screen.getByRole("option", { name: /Draft October newsletter/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Audit ad spend/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /Nightly analytics sync/ })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: /2026-10-13/ })).toBeInTheDocument();
    for (const header of ["ROUTES", "TASKS", "RUNS", "REPORTS"]) {
      expect(screen.getByRole("group", { name: header })).toBeInTheDocument();
    }

    expect(mockApiGet).toHaveBeenCalledWith(TASKS_READ);
    expect(mockApiGet).toHaveBeenCalledWith(RUNS_READ);
    expect(mockApiGet).toHaveBeenCalledWith(REPORTS_READ);

    // Reused for the session: close and reopen → no second read.
    await user.keyboard("{Escape}");
    await openPalette(user);
    expect(mockApiGet).toHaveBeenCalledTimes(3);
    // Task rows keep their deep link: Enter on a task row goes to /tasks?task=<id>.
    await user.type(screen.getByLabelText("Command palette search"), "Draft October");
    const taskOption = screen.getAllByRole("option")[0];
    expect(taskOption).toHaveAttribute("aria-selected", "false");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(mockRouter.push).toHaveBeenCalledWith("/tasks?task=1");
  });

  it("a failed read renders ONE honest note line in its section, and the section stays visible", async () => {
    const user = userEvent.setup();
    wire({
      [RUNS_READ]: new ApiError(503, "runs read failed", "E_DB", "retry when the backend recovers"),
    });
    render(<CommandPalette />);
    await openPalette(user);

    const runsSection = screen.getByRole("group", { name: "RUNS" });
    expect(
      await within(runsSection).findByText(
        /E_DB: runs read failed — retry when the backend recovers/,
      ),
    ).toBeInTheDocument();
    // Exactly ONE note line — never a dead, silently-empty section.
    expect(screen.getAllByText(/E_DB:/)).toHaveLength(1);
    expect(within(runsSection).getByRole("alert")).toBeInTheDocument();

    // The section header stays visible, and the healthy sections still render their rows.
    expect(screen.getByRole("group", { name: "RUNS" })).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: /Draft October newsletter/ })).toBeInTheDocument();
    expect(await screen.findByRole("option", { name: /2026-10-13/ })).toBeInTheDocument();
  });

  it("names a body that is not a list at all — never folds it into an empty section", async () => {
    const user = userEvent.setup();
    wire({ [TASKS_READ]: { unexpected: true } });
    render(<CommandPalette />);
    await openPalette(user);

    const tasksSection = screen.getByRole("group", { name: "TASKS" });
    expect(await within(tasksSection).findByText(/the server returned no tasks list/)).toBeInTheDocument();
    expect(within(tasksSection).getByRole("alert")).toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
 * 4. honest empty + the cap
 * ------------------------------------------------------------------------- */

describe("CommandPalette — honest states", () => {
  it("a zero-match query shows the single 'No matches.' line", async () => {
    const user = userEvent.setup();
    wire();
    render(<CommandPalette />);
    await openPalette(user);
    await waitForReads();

    await user.type(screen.getByLabelText("Command palette search"), "zzz-nothing");
    expect(screen.getByText("No matches.")).toBeInTheDocument();
    expect(screen.queryAllByRole("option")).toHaveLength(0);
  });

  it("caps a section at eight rows with an honest '…and N more' line", async () => {
    const user = userEvent.setup();
    wire({
      [TASKS_READ]: Array.from({ length: 12 }, (_, n) => taskRow(n + 1, `Widget job ${n + 1}`)),
    });
    render(<CommandPalette />);
    await openPalette(user);
    await screen.findByRole("option", { name: /Widget job 1/ });

    await user.type(screen.getByLabelText("Command palette search"), "widget");
    expect(screen.getAllByRole("option", { name: /Widget job/ })).toHaveLength(8);
    expect(screen.getByText("…and 4 more")).toBeInTheDocument();
  });

  it("renders NO '…and N more' line when a section shows all of its rows (ROUTES exemption — parent regression pin)", async () => {
    // The parent gate found hiddenCount computed against the global cap for EVERY section, so
    // ROUTES — exempt and rendering all ten — still printed "…and 2 more": a claim of two hidden
    // rows that did not exist. This pins the fix: the more-line counts only what the section's
    // OWN cap actually hid.
    const user = userEvent.setup();
    wire();
    render(<CommandPalette />);
    await openPalette(user);
    await waitForReads();

    for (const item of NAV_ITEMS) {
      expect(screen.getByRole("option", { name: new RegExp(item.label) })).toBeInTheDocument();
    }
    expect(screen.queryByText(/…and \d+ more/)).not.toBeInTheDocument();
  });
});

/* ---------------------------------------------------------------------------
 * 5. keyboard grammar inside the dialog
 * ------------------------------------------------------------------------- */

describe("CommandPalette — keyboard grammar", () => {
  it("ArrowDown twice + Enter pushes the second visible item's href and closes", async () => {
    const user = userEvent.setup();
    wire();
    render(<CommandPalette />);
    await openPalette(user);
    await waitForReads();

    await user.keyboard("{ArrowDown}{ArrowDown}");
    const second = screen.getAllByRole("option")[1];
    expect(second.textContent).toContain("Agents");
    expect(second).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{Enter}");
    expect(mockRouter.push).toHaveBeenCalledWith("/agents");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("wraps at both ends: ArrowUp from the first lands on the last, ArrowDown from the last on the first", async () => {
    const user = userEvent.setup();
    wire();
    render(<CommandPalette />);
    await openPalette(user);
    await waitForReads();

    await user.keyboard("{ArrowDown}");
    let options = screen.getAllByRole("option");
    expect(options[0]).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{ArrowUp}");
    options = screen.getAllByRole("option");
    expect(options[options.length - 1]).toHaveAttribute("aria-selected", "true");

    await user.keyboard("{ArrowDown}");
    options = screen.getAllByRole("option");
    expect(options[0]).toHaveAttribute("aria-selected", "true");
  });

  it("keeps Tab trapped inside the dialog", async () => {
    const user = userEvent.setup();
    wire();
    render(<CommandPalette />);
    const dialog = await openPalette(user);

    await user.tab();
    expect(screen.getByLabelText("Command palette search")).toHaveFocus();

    await user.tab({ shift: true });
    expect(screen.getByLabelText("Command palette search")).toHaveFocus();
    expect(dialog.contains(document.activeElement)).toBe(true);
  });
});
