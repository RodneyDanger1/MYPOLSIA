import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

/**
 * Wave V7 / seat S6 — task-card quick actions (V7-T16) + dashboard preview-card deep links
 * (V7-T17). Suite `u7-task-actions`.
 *
 * Mocking strategy: the other task suites stub `@/lib/api` and pin the module-level calls; this
 * suite mocks `fetch` instead, so the flow under test is the REAL client path — URL building,
 * method, JSON body, refusal parsing. The card's run chip must issue the same request shape the
 * sheet's `runNow` speaks: `POST /api/v1/agents/{agent_type}/trigger` with
 * task_title/description/priority. jsdom has no app-router context, so `next/navigation` is
 * mocked and the `next/link` mock hands the href to that router exactly like the real Link does.
 */
const mockRouter = { push: jest.fn() };

jest.mock("next/navigation", () => ({
  useRouter: () => mockRouter,
}));

jest.mock("next/link", () => ({
  __esModule: true,
  default: function MockLink(
    props: { href: string; children: React.ReactNode } & Record<string, unknown>,
  ) {
    const { href, children, ...rest } = props;
    return (
      <a
        href={href}
        {...(rest as Record<string, string>)}
        onClick={(event) => {
          event.preventDefault();
          mockRouter.push(href);
        }}
      >
        {children}
      </a>
    );
  },
}));

import { TasksPreview } from "@/components/dashboard/TasksPreview";
import { TaskCard } from "@/components/tasks/TaskCard";
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
  // jest's jsdom environment ships no fetch; the api client calls the global one (the same
  // assignment idiom `lib/api.test.ts` uses).
  (global as unknown as { fetch: jest.Mock }).fetch = fetchMock;
});

const HOUR_MS = 60 * 60 * 1000;
const DAY_MS = 24 * HOUR_MS;
const ago = (ms: number) => new Date(Date.now() - ms).toISOString();

const FINGERPRINT = "9f2c1e4a5b6d7c8e";
const PARKED_SUMMARY = `awaiting approval: 1 gated action(s) need a token; request one for fingerprint(s) ${FINGERPRINT}`;

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

/** The action row (V7-T16) — selected by its pinned test hook, one card per test. */
const actionRow = () => screen.getByTestId("task-actions");

// ---------------------------------------------------------------------------------------------
// V7-T16 — the quick-action row: reveal recipe, contents from recoveryFor, working run chip.
// ---------------------------------------------------------------------------------------------

describe("task-card quick actions (V7-T16)", () => {
  it("reveals on hover AND keyboard focus, opacity-only, absolutely positioned (no layout shift)", () => {
    render(<TaskCard task={task()} isNext={false} onOpen={jest.fn()} />);

    const row = actionRow();
    expect(row).toHaveClass("opacity-0");
    expect(row).toHaveClass("group-hover:opacity-100");
    expect(row).toHaveClass("group-focus-within:opacity-100");
    expect(row).toHaveClass("transition-opacity");
    expect(row).toHaveClass("absolute");

    // The group wrapper the reveal hooks into — the card button itself is untouched inside it.
    const group = row.parentElement as HTMLElement;
    expect(group).toHaveClass("group");
    expect(group).toHaveClass("relative");
    expect(within(group).getByRole("button", { name: "Open task: Guide owners to their first connection" })).toBeInTheDocument();

    // "View →" yields its spot while the row is shown — the two affordances never double-render.
    expect(screen.getByText("View →")).toHaveClass("group-hover:opacity-0");
  });

  it("pending: OPEN + the recoveryFor run chip 'Run now'; OPEN opens the sheet and sends nothing", async () => {
    const user = userEvent.setup();
    const onOpen = jest.fn();
    const row = task({ id: 11, title: "Queued work" });
    render(<TaskCard task={row} isNext={false} onOpen={onOpen} />);

    const chips = within(actionRow()).getAllByRole("button");
    expect(chips.map((chip) => chip.textContent)).toEqual(["Open", "Run now"]);

    await user.click(within(actionRow()).getByRole("button", { name: /^open$/i }));
    expect(onOpen).toHaveBeenCalledWith(row);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("completed: NO action chip at all — only OPEN (recoveryFor 'none' is the honest absence)", () => {
    render(
      <TaskCard
        task={task({ id: 12, title: "Finished work", status: "completed", completed_at: ago(HOUR_MS) })}
        isNext={false}
        onOpen={jest.fn()}
      />,
    );

    const chips = within(actionRow()).getAllByRole("button");
    expect(chips.map((chip) => chip.textContent)).toEqual(["Open"]);
    expect(within(actionRow()).queryByRole("button", { name: /run|approve/i })).toBeNull();
  });

  it("failed: the chip is 'Run again' — the recoveryFor label, never a second vocabulary", () => {
    render(<TaskCard task={task({ id: 13, title: "Broken work", status: "failed" })} isNext={false} onOpen={jest.fn()} />);

    const chips = within(actionRow()).getAllByRole("button");
    expect(chips.map((chip) => chip.textContent)).toEqual(["Open", "Run again"]);
    expect(within(actionRow()).queryByRole("button", { name: /^run now$/i })).toBeNull();
  });

  it("awaiting_approval: 'Review approval' routes to the sheet when a fingerprint exists — and nothing extra when none does", async () => {
    const user = userEvent.setup();
    const onOpen = jest.fn();
    const parked = task({
      id: 42,
      title: "Parked on a gated action",
      status: "awaiting_approval",
      result_summary: PARKED_SUMMARY,
    });
    const { unmount } = render(<TaskCard task={parked} isNext={false} onOpen={onOpen} />);

    const review = within(actionRow()).getByRole("button", { name: /review approval/i });
    await user.click(review);
    // The mint is fingerprint-scoped and lives in the sheet: the card only routes there.
    expect(onOpen).toHaveBeenCalledWith(parked);
    expect(fetchMock).not.toHaveBeenCalled();
    unmount();

    render(
      <TaskCard
        task={task({ id: 43, title: "Parked, unreadable", status: "awaiting_approval", result_summary: null })}
        isNext={false}
        onOpen={jest.fn()}
      />,
    );
    expect(within(actionRow()).getAllByRole("button").map((chip) => chip.textContent)).toEqual(["Open"]);
  });

  it("the run chip POSTs the agent-trigger route with the sheet's exact request shape (mocked fetch)", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValueOnce(
      jsonResponse(202, { status: "queued", task_id: 77, message: "Task #77 queued for business_planning." }),
    );

    const row = task({
      id: 4,
      title: "Draft the launch checklist",
      agent_type: "business_planning",
      description: "One checklist",
      priority: 3,
    });
    render(<TaskCard task={row} isNext={false} onOpen={jest.fn()} />);

    await user.click(within(actionRow()).getByRole("button", { name: /^run now$/i }));

    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toMatch(/\/api\/v1\/agents\/business_planning\/trigger$/);
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({
      task_title: "Draft the launch checklist",
      description: "One checklist",
      priority: 3,
    });
  });

  it("keeps a null description/priority off the wire (the sheet's runNow discipline)", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValueOnce(
      jsonResponse(202, { status: "queued", task_id: 9, message: "Task #9 queued for orchestrator." }),
    );

    render(
      <TaskCard
        task={task({ id: 9, title: "No extras", description: null, priority: null })}
        isNext={false}
        onOpen={jest.fn()}
      />,
    );

    await user.click(within(actionRow()).getByRole("button", { name: /^run now$/i }));

    const [, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ task_title: "No extras" });
  });

  it("a double-click while in flight fires ONE request; success shows the server message and keeps the chip disabled", async () => {
    const user = userEvent.setup();
    let release: ((value: unknown) => void) | null = null;
    fetchMock.mockImplementationOnce(
      () =>
        new Promise((resolve) => {
          release = resolve;
        }),
    );

    render(<TaskCard task={task({ id: 4, title: "Queued once" })} isNext={false} onOpen={jest.fn()} />);
    const chip = within(actionRow()).getByRole("button", { name: /^run now$/i });

    await user.dblClick(chip);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // In flight: disabled + the label carries the ellipsis suffix.
    const inFlight = within(actionRow()).getByRole("button", { name: /^run now…$/i });
    expect(inFlight).toBeDisabled();

    await act(async () => {
      release?.(jsonResponse(202, { status: "queued", task_id: 77, message: "Task #77 queued for orchestrator." }));
    });

    // The server's message, rendered inline in the row — and the row stays visible with it.
    expect(await screen.findByRole("status")).toHaveTextContent("Task #77 queued for orchestrator.");
    expect(actionRow()).toHaveClass("opacity-100");
    expect(actionRow()).not.toHaveClass("opacity-0");

    // The chip stays disabled — each trigger CREATES a task; a second click can never create two.
    const after = within(actionRow()).getByRole("button", { name: /^run now$/i });
    expect(after).toBeDisabled();
    await user.click(after);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("a refused POST renders ApiError.display inline (code + how_to_fix) and re-arms the chip", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValueOnce(
      jsonResponse(503, {
        ok: false,
        error: {
          code: "E_REDIS",
          message: "the broker refused the enqueue",
          how_to_fix: "check REDIS_URL and that the broker is running",
        },
      }),
    );

    render(<TaskCard task={task({ id: 5, title: "Refused run" })} isNext={false} onOpen={jest.fn()} />);
    await user.click(within(actionRow()).getByRole("button", { name: /^run now$/i }));

    const refusal = await screen.findByRole("alert");
    expect(refusal).toHaveTextContent("E_REDIS");
    expect(refusal).toHaveTextContent("check REDIS_URL and that the broker is running");
    expect(refusal).toHaveAttribute("data-tone", "alert");

    // The refusal created nothing — the chip is armed again for a retry.
    expect(within(actionRow()).getByRole("button", { name: /^run now$/i })).toBeEnabled();
  });
});

// ---------------------------------------------------------------------------------------------
// V7-T17 — dashboard preview cards land on the shipped deep link /tasks?task=N.
// ---------------------------------------------------------------------------------------------

describe("dashboard TasksPreview deep links (V7-T17)", () => {
  it("a preview card is a link to /tasks?task=N — styling kept, click routes via the router", async () => {
    const user = userEvent.setup();
    fetchMock.mockResolvedValueOnce(
      jsonResponse(200, [
        {
          id: 7,
          title: "fixture: preview task",
          agent_type: "business_planning",
          status: "pending",
          priority: 3,
          created_at: ago(HOUR_MS),
        },
      ]),
    );

    render(<TasksPreview />);
    await screen.findByText("fixture: preview task");

    const link = screen.getByRole("link", { name: /fixture: preview task/ });
    expect(link).toHaveAttribute("href", "/tasks?task=7");
    // The card's own look is kept byte-for-byte — the article's classes are untouched.
    const article = link.querySelector("article");
    expect(article).not.toBeNull();
    expect(article?.className).toBe("border border-line bg-surface-sunk p-3");

    await user.click(link);
    expect(mockRouter.push).toHaveBeenCalledWith("/tasks?task=7");
  });
});
