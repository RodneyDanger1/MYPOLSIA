/**
 * WAVE V21 / seat L4 — a11y OBSERVATION of the V20 agents toolbox.
 *
 * `components/agents/**` is read-only for this lane: this suite proves the behaviour that shipped
 * in V20 and adds the checks V20 did not run. It changes no product code. Where V20 pinned the
 * happy path on ONE row, this suite pins the contract across the roster:
 *
 *   - `aria-expanded` mirrors the DOM in both directions (true iff the panel exists);
 *   - `aria-controls` is present ONLY while open, so it never points at a missing id, and each
 *     row's id is its own (two open panels do not collide);
 *   - the id `aria-controls` names is the element that actually holds the tools list, and it is
 *     labelled with the agent it belongs to;
 *   - keyboard focus order is DOM order (Run now → Tools per row), and an opened toolbox adds NO
 *     tab stop — Tab moves on to the next row rather than dead-ending in a static list;
 *   - Escape from a control INSIDE the panel (the Retry button of a failed read) collapses it and
 *     returns focus to the trigger; Escape while collapsed is inert and steals no focus.
 *
 * Render/mocking conventions are copied from `v20-agents-toolbox.test.tsx`: the real page is
 * rendered and `global.fetch` is mocked, so `@/lib/api`, the parser and the hook all run for real.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import AgentsPage from "@/app/agents/page";

const API_STATUS = "/api/v1/agents/status";
const API_CAPABILITIES = "/api/v1/ops/capabilities";

/** The minimal wire shape of one `FETCH` response the api client consumes. */
function wire(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    headers: { get: () => null },
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

const STATUS_ROWS = [
  {
    agent_type: "orchestrator",
    last_run_at: null,
    last_run_status: null,
    tasks_today: 0,
    tasks_total: 0,
  },
  {
    agent_type: "finance",
    last_run_at: null,
    last_run_status: null,
    tasks_today: 3,
    tasks_total: 9,
  },
];

const TOOLS = [
  { name: "macro_snapshot", description: "Read the cached macro snapshot." },
  { name: "web_fetch", description: "Fetch one http(s) URL and return its page text." },
];

function mapBody() {
  return {
    ok: true,
    data: {
      agents: [
        { agent_type: "orchestrator", tools: TOOLS, action_classes: ["read"], channels: [] },
        { agent_type: "finance", tools: [], action_classes: ["read"], channels: [] },
      ],
      registry_tools: [],
    },
  };
}

let fetchMock: jest.Mock;

function routeFetch({ capabilities }: { capabilities?: () => Response } = {}) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith(API_STATUS)) return Promise.resolve(wire(STATUS_ROWS));
    if (url.endsWith(API_CAPABILITIES)) {
      return Promise.resolve(capabilities ? capabilities() : wire(mapBody()));
    }
    return Promise.reject(new Error(`unexpected fetch: ${url}`));
  });
}

beforeEach(() => {
  fetchMock = jest.fn();
  global.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  fetchMock.mockReset();
});

async function load() {
  render(<AgentsPage />);
  const orchestrator = await screen.findByTestId("agent-tools-orchestrator");
  const finance = screen.getByTestId("agent-tools-finance");
  const orchestratorToggle = within(orchestrator).getByRole("button", { name: /tools/i });
  const financeToggle = within(finance).getByRole("button", { name: /tools/i });
  // Wait for the one capabilities read to land, so `Tools · 2` (not `Tools`) is the settled label.
  await waitFor(() => expect(orchestratorToggle).toHaveTextContent("Tools · 2"));
  return { orchestrator, finance, orchestratorToggle, financeToggle };
}

describe("Agents toolbox a11y (V21 L4 — observation of V20)", () => {
  it("aria-expanded follows the DOM, and aria-controls exists only while its panel does — per row", async () => {
    const user = userEvent.setup();
    routeFetch();
    const { orchestratorToggle, financeToggle } = await load();

    // Collapsed: the attribute is honest and the reference is absent (no id to a missing node).
    expect(orchestratorToggle).toHaveAttribute("aria-expanded", "false");
    expect(orchestratorToggle).not.toHaveAttribute("aria-controls");
    expect(financeToggle).toHaveAttribute("aria-expanded", "false");
    expect(financeToggle).not.toHaveAttribute("aria-controls");

    await user.click(orchestratorToggle);

    expect(orchestratorToggle).toHaveAttribute("aria-expanded", "true");
    const orchestratorPanelId = orchestratorToggle.getAttribute("aria-controls");
    expect(orchestratorPanelId).toBeTruthy();
    const orchestratorPanel = document.getElementById(orchestratorPanelId as string);
    expect(orchestratorPanel).not.toBeNull();

    // The element aria-controls names is the one that HOLDS the list, and it says whose it is.
    expect(orchestratorPanel).toHaveAttribute("role", "group");
    expect(orchestratorPanel).toHaveAccessibleName("Tools for orchestrator");
    expect(within(orchestratorPanel as HTMLElement).getByRole("list")).toBeInTheDocument();
    expect(within(orchestratorPanel as HTMLElement).getAllByRole("listitem")).toHaveLength(2);
    expect(
      within(orchestratorPanel as HTMLElement).getByText("macro_snapshot"),
    ).toBeInTheDocument();

    // Opening the second row mints its OWN id — the two references never collide.
    await user.click(financeToggle);
    expect(financeToggle).toHaveAttribute("aria-expanded", "true");
    const financePanelId = financeToggle.getAttribute("aria-controls");
    expect(financePanelId).toBeTruthy();
    expect(financePanelId).not.toBe(orchestratorPanelId);
    expect(document.getElementById(financePanelId as string)).not.toBeNull();
    expect(document.getElementById(orchestratorPanelId as string)).not.toBeNull();

    // Closing removes the reference AND the target together, in the same render.
    await user.click(orchestratorToggle);
    expect(orchestratorToggle).toHaveAttribute("aria-expanded", "false");
    expect(orchestratorToggle).not.toHaveAttribute("aria-controls");
    expect(document.getElementById(orchestratorPanelId as string)).toBeNull();
    // The other row is untouched by its neighbour closing.
    expect(financeToggle).toHaveAttribute("aria-expanded", "true");
    expect(document.getElementById(financePanelId as string)).not.toBeNull();
  });

  it("keyboard focus order is DOM order (Run now → Tools per row) and an open toolbox adds no tab stop", async () => {
    const user = userEvent.setup();
    routeFetch();

    render(<AgentsPage />);
    const orchestrator = await screen.findByTestId("agent-tools-orchestrator");
    const finance = screen.getByTestId("agent-tools-finance");
    // The toolbox section is the tail of its row; `Run now` belongs to the enclosing article.
    const orchestratorRow = orchestrator.closest("article") as HTMLElement;
    const financeRow = finance.closest("article") as HTMLElement;

    const orchestratorRun = within(orchestratorRow).getByRole("button", { name: /run now/i });
    const orchestratorTools = within(orchestrator).getByRole("button", { name: /tools/i });
    const financeRun = within(financeRow).getByRole("button", { name: /run now/i });
    const financeTools = within(finance).getByRole("button", { name: /tools/i });

    await waitFor(() => expect(orchestratorTools).toHaveTextContent("Tools · 2"));

    await user.tab();
    expect(orchestratorRun).toHaveFocus();
    await user.tab();
    expect(orchestratorTools).toHaveFocus();

    // Open with the keyboard; focus stays on the trigger (no focus theft on expand).
    await user.keyboard("{Enter}");
    expect(orchestratorTools).toHaveAttribute("aria-expanded", "true");
    expect(orchestratorTools).toHaveFocus();

    // The panel's tools are static text — Tab must leave the section, not dead-end inside it.
    await user.tab();
    expect(financeRun).toHaveFocus();
    await user.tab();
    expect(financeTools).toHaveFocus();
  });

  it("Escape from a focused control inside the panel collapses it and returns focus to the trigger", async () => {
    const user = userEvent.setup();
    routeFetch({
      capabilities: () =>
        wire(
          {
            ok: false,
            error: {
              code: "E_INTERNAL",
              message: "Internal error",
              how_to_fix: "retry once the registry route is healthy",
            },
          },
          500,
        ),
    });

    render(<AgentsPage />);
    const section = await screen.findByTestId("agent-tools-orchestrator");
    const toggle = within(section).getByRole("button", { name: /tools/i });
    await waitFor(() => expect(toggle).toHaveTextContent(/unavailable/i));

    await user.click(toggle);
    const alert = await within(section).findByTestId("agents-tools-error");
    const retry = within(alert).getByRole("button", { name: /retry/i });

    // A real focusable control INSIDE the panel: Escape is handled from here, not just the button.
    retry.focus();
    expect(retry).toHaveFocus();

    await user.keyboard("{Escape}");

    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(within(section).queryByTestId("agents-tools-error")).toBeNull();
    expect(toggle).toHaveFocus();
  });

  it("Escape while collapsed is inert — it neither moves focus nor fabricates an open panel", async () => {
    const user = userEvent.setup();
    routeFetch();
    await load();

    const orchestratorRun = screen.getAllByRole("button", { name: /run now/i })[0];
    orchestratorRun.focus();
    expect(orchestratorRun).toHaveFocus();

    await user.keyboard("{Escape}");

    expect(orchestratorRun).toHaveFocus();
    expect(screen.getByTestId("agent-tools-orchestrator")).toBeInTheDocument();
    for (const toggle of screen.getAllByRole("button", { name: /tools/i })) {
      expect(toggle).toHaveAttribute("aria-expanded", "false");
      expect(toggle).not.toHaveAttribute("aria-controls");
    }
  });
});
