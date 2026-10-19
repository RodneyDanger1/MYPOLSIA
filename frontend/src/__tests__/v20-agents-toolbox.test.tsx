/**
 * WAVE V20 / seat L4 — the Agents page TOOLBOX (owner visibility of each agent's real tools).
 *
 * Written BEFORE the product code exists: pre-fix the Tools section / hook do not resolve, so the
 * RED leg is a module-resolution + missing-testid failure (raw/01-red.txt).
 *
 * Wire truth consumed: `GET /api/v1/ops/capabilities`, the SAME H-C1 payload `/capabilities`
 * renders, parsed by the SAME `capabilitiesFrom` (no second parser): `{agents:[{agent_type,
 * tools:[{name, description}], action_classes, channels}], registry_tools:[…]}` in either the
 * `{ok, data}` envelope or the bare map.
 *
 * Honesty laws pinned here:
 *   - the tools shown for an agent are THAT agent's tools, verbatim from the payload, with the
 *     registry's own one-line description; a description-less tool is MARKED, never embellished;
 *   - a tool-less agent says "no tools" (not blank) only after a successful read that genuinely
 *     lists the agent; a payload missing `tools[]` / omitting the agent is a DIFFERENT, named state;
 *   - a failed capabilities read is the shared `readErrorText` / `ApiError.display` error with a
 *     working Retry — never an empty toolbox;
 *   - one read serves the whole roster (no per-row request storm);
 *   - the expander is a real button with `aria-expanded` + `aria-controls`; Escape collapses and
 *     returns focus; expanding persists nothing and logs nothing.
 *
 * Fetch is mocked at `global.fetch` so the REAL `@/lib/api` client (path, method, refusal parsing)
 * is exercised end to end.
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

function agentRow(agent_type: string, tools: unknown[], extra: Record<string, unknown> = {}) {
  return { agent_type, tools, action_classes: ["read"], channels: [], ...extra };
}

function mapBody(agents: unknown[]) {
  return { ok: true, data: { agents, registry_tools: [] } };
}

const ORCHESTRATOR_TOOLS = [
  {
    name: "macro_snapshot",
    description: "Read the cached macro snapshot (World Bank / FRED / FX).",
  },
  { name: "web_fetch", description: "Fetch one http(s) URL and return its page text." },
];

let fetchMock: jest.Mock;

/** Route the two reads the page makes; `capabilities` is a callable so a test can fail once. */
function routeFetch({
  status = STATUS_ROWS,
  capabilities = () => wire(mapBody([agentRow("orchestrator", ORCHESTRATOR_TOOLS), agentRow("finance", [])])),
}: {
  status?: unknown;
  capabilities?: () => Response | Promise<Response>;
} = {}) {
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.endsWith(API_STATUS)) return Promise.resolve(wire(status));
    if (url.endsWith(API_CAPABILITIES)) return Promise.resolve(capabilities());
    return Promise.reject(new Error(`unexpected fetch: ${url}`));
  });
}

const capabilitiesCalls = () =>
  fetchMock.mock.calls.filter((call) => String(call[0]).endsWith(API_CAPABILITIES));

beforeEach(() => {
  fetchMock = jest.fn();
  global.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
  fetchMock.mockReset();
});

async function expand(user: ReturnType<typeof userEvent.setup>, section: HTMLElement) {
  const toggle = within(section).getByRole("button", { name: /tools/i });
  await user.click(toggle);
  return toggle;
}

// ---------------------------------------------------------------------------------------------
// The read + the expander
// ---------------------------------------------------------------------------------------------

describe("Agents page toolbox (V20 L4)", () => {
  it("gives each agent a collapsed Tools button; expanding shows THAT agent's real tools and descriptions; one capabilities read serves the roster", async () => {
    const user = userEvent.setup();
    routeFetch();

    render(<AgentsPage />);

    const orchestrator = await screen.findByTestId("agent-tools-orchestrator");
    const finance = screen.getByTestId("agent-tools-finance");

    const toggle = within(orchestrator).getByRole("button", { name: /tools/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    // Collapsed means the tools are NOT in the DOM — no hidden duplicate.
    expect(within(orchestrator).queryByText("macro_snapshot")).toBeNull();

    // One read for the whole roster, on the real path, as a GET.
    const calls = capabilitiesCalls();
    expect(calls).toHaveLength(1);
    expect(String(calls[0][0])).toContain(API_CAPABILITIES);
    expect(calls[0][1]).toMatchObject({ method: "GET" });

    await user.click(toggle);

    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(orchestrator).getByText("macro_snapshot")).toBeInTheDocument();
    expect(
      within(orchestrator).getByText("Read the cached macro snapshot (World Bank / FRED / FX)."),
    ).toBeInTheDocument();
    expect(within(orchestrator).getByText("web_fetch")).toBeInTheDocument();
    // aria-controls names a real panel.
    const panelId = toggle.getAttribute("aria-controls");
    expect(panelId).toBeTruthy();
    expect(document.getElementById(panelId as string)).not.toBeNull();

    // The OTHER agent does not inherit these tools, and its empty list is STATED.
    expect(within(finance).queryByText("macro_snapshot")).toBeNull();
    await user.click(within(finance).getByRole("button", { name: /tools/i }));
    expect(within(finance).getByText("No tools are granted to this agent.")).toBeInTheDocument();
    expect(within(finance).queryByText("macro_snapshot")).toBeNull();
  });

  it("Escape collapses the section and returns focus to the button; the button re-opens by keyboard", async () => {
    const user = userEvent.setup();
    routeFetch();

    render(<AgentsPage />);

    const section = await screen.findByTestId("agent-tools-orchestrator");
    const toggle = await expand(user, section);
    const panelId = toggle.getAttribute("aria-controls") as string;
    expect(document.getElementById(panelId)).not.toBeNull();

    await user.keyboard("{Escape}");

    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById(panelId)).toBeNull();
    expect(toggle).toHaveFocus();

    await user.keyboard("{Enter}");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(section).getByText("macro_snapshot")).toBeInTheDocument();
  });

  it("a failed capabilities read shows the SHARED named error with a working Retry — never an empty toolbox", async () => {
    const user = userEvent.setup();
    let calls = 0;
    routeFetch({
      capabilities: () => {
        calls += 1;
        if (calls === 1) {
          return wire(
            {
              ok: false,
              error: {
                code: "E_NOT_FOUND",
                message: "Not Found",
                how_to_fix: "the capabilities route is not served by this build",
              },
            },
            404,
          );
        }
        return wire(mapBody([agentRow("orchestrator", ORCHESTRATOR_TOOLS), agentRow("finance", [])]));
      },
    });

    render(<AgentsPage />);

    const section = await screen.findByTestId("agent-tools-orchestrator");
    const toggle = within(section).getByRole("button", { name: /tools/i });
    // The failure is signalled BEFORE expanding, so the owner is never told a silent lie.
    await waitFor(() => expect(toggle).toHaveTextContent(/unavailable/i));

    await user.click(toggle);
    const alert = await within(section).findByTestId("agents-tools-error");
    expect(alert).toHaveAttribute("role", "alert");
    expect(alert).toHaveTextContent("E_NOT_FOUND");
    expect(alert).toHaveTextContent("the capabilities route is not served by this build");
    // The forbidden stand-in: an error rendered as "no tools".
    expect(within(section).queryByText(/No tools are granted/)).toBeNull();
    // The roster itself is unaffected by the toolbox failure.
    expect(screen.getAllByRole("article")).toHaveLength(2);

    await user.click(within(alert).getByRole("button", { name: /retry/i }));

    await waitFor(() => expect(within(section).getByText("macro_snapshot")).toBeInTheDocument());
    expect(within(section).queryByTestId("agents-tools-error")).toBeNull();
    expect(calls).toBe(2);
  });

  it("a capabilities row with no tools[] is NAMED as unreadable — it is not the same state as an agent granted no tools", async () => {
    const user = userEvent.setup();
    routeFetch({
      capabilities: () =>
        wire(
          mapBody([
            // Hostile row: the frozen parser drops it whole (tools[] absent).
            { agent_type: "orchestrator", action_classes: ["read"], channels: [] },
            agentRow("finance", []),
          ]),
        ),
    });

    render(<AgentsPage />);

    const orchestrator = await screen.findByTestId("agent-tools-orchestrator");
    const orchestrationToggle = within(orchestrator).getByRole("button", { name: /tools/i });
    await waitFor(() => expect(within(orchestrator).queryByText(/loading/i)).toBeNull());
    expect(orchestrationToggle.textContent?.trim()).toBe("Tools");

    await user.click(orchestrationToggle);
    expect(
      within(orchestrator).getByText("The capability map has no readable entry for this agent."),
    ).toBeInTheDocument();
    expect(within(orchestrator).queryByText(/No tools are granted/)).toBeNull();
    expect(within(orchestrator).getByText(/1 unreadable row dropped/)).toBeInTheDocument();

    const finance = screen.getByTestId("agent-tools-finance");
    await user.click(within(finance).getByRole("button", { name: /tools/i }));
    expect(within(finance).getByText("No tools are granted to this agent.")).toBeInTheDocument();
    expect(within(finance).queryByText(/no readable entry/)).toBeNull();
  });

  it("renders unknown tool names verbatim and MARKS a tool the registry gives no description", async () => {
    const user = userEvent.setup();
    routeFetch({
      capabilities: () =>
        wire(
          mapBody([
            agentRow("orchestrator", [
              { name: "zzz_unregistered_tool", description: "Does exactly one thing." },
              { name: "silent_tool", description: 42 },
            ]),
            agentRow("finance", []),
          ]),
        ),
    });

    render(<AgentsPage />);

    const section = await screen.findByTestId("agent-tools-orchestrator");
    await expand(user, section);

    expect(within(section).getByText("zzz_unregistered_tool")).toBeInTheDocument();
    expect(within(section).getByText("Does exactly one thing.")).toBeInTheDocument();
    expect(within(section).getByText("silent_tool")).toBeInTheDocument();
    expect(
      within(section).getByText("The registry states no description for this tool."),
    ).toBeInTheDocument();
    expect(document.body.textContent ?? "").not.toMatch(/undefined|NaN|Invalid Date/);
  });

  it("merges duplicate agent rows by tool name — every granted tool appears once, never twice", async () => {
    const user = userEvent.setup();
    routeFetch({
      capabilities: () =>
        wire(
          mapBody([
            agentRow("orchestrator", [
              { name: "macro_snapshot", description: "Read the cached macro snapshot." },
              { name: "web_fetch", description: "Fetch one http(s) URL." },
            ]),
            agentRow("orchestrator", [
              { name: "macro_snapshot", description: "Read the cached macro snapshot." },
              { name: "pricing_catalog", description: "Read the dated pricing catalog." },
            ]),
            agentRow("finance", []),
          ]),
        ),
    });

    render(<AgentsPage />);

    const section = await screen.findByTestId("agent-tools-orchestrator");
    const toggle = await expand(user, section);

    expect(toggle.textContent?.trim()).toBe("Tools · 3");
    expect(within(section).getAllByRole("listitem")).toHaveLength(3);
    expect(within(section).getAllByText("macro_snapshot")).toHaveLength(1);
    expect(within(section).getByText("web_fetch")).toBeInTheDocument();
    expect(within(section).getByText("pricing_catalog")).toBeInTheDocument();
  });

  it("an empty agents[] map is stated as such — never as every agent having no tools", async () => {
    const user = userEvent.setup();
    routeFetch({ capabilities: () => wire(mapBody([])) });

    render(<AgentsPage />);

    const section = await screen.findByTestId("agent-tools-orchestrator");
    const toggle = within(section).getByRole("button", { name: /tools/i });
    await waitFor(() => expect(toggle.textContent?.trim()).toBe("Tools"));

    await user.click(toggle);
    expect(within(section).getByText(/The capability map lists no agents/)).toBeInTheDocument();
    expect(within(section).queryByText(/No tools are granted/)).toBeNull();
  });

  it("accepts the bare-map wire shape too (today's sibling ops routes) — same parser, same render", async () => {
    const user = userEvent.setup();
    routeFetch({
      capabilities: () =>
        wire({
          agents: [agentRow("orchestrator", ORCHESTRATOR_TOOLS), agentRow("finance", [])],
          registry_tools: [],
        }),
    });

    render(<AgentsPage />);

    const section = await screen.findByTestId("agent-tools-orchestrator");
    await expand(user, section);
    expect(within(section).getByText("macro_snapshot")).toBeInTheDocument();
    expect(within(section).queryByTestId("agents-tools-error")).toBeNull();
  });

  it("a 2xx body that is not the capability map is a NAMED error — never every agent having no tools", async () => {
    const user = userEvent.setup();
    routeFetch({ capabilities: () => wire({ detail: "Not Found" }) });

    render(<AgentsPage />);

    const section = await screen.findByTestId("agent-tools-orchestrator");
    const toggle = within(section).getByRole("button", { name: /tools/i });
    await waitFor(() => expect(toggle).toHaveTextContent(/unavailable/i));

    await user.click(toggle);
    const alert = await within(section).findByTestId("agents-tools-error");
    expect(alert).toHaveTextContent(/capability map/i);
    expect(within(section).queryByText(/No tools are granted/)).toBeNull();
  });

  it("expanding a toolbox persists nothing and logs nothing", async () => {
    const user = userEvent.setup();
    const setItem = jest.spyOn(Storage.prototype, "setItem");
    const getItem = jest.spyOn(Storage.prototype, "getItem");
    const removeItem = jest.spyOn(Storage.prototype, "removeItem");
    const log = jest.spyOn(console, "log").mockImplementation(() => {});
    const info = jest.spyOn(console, "info").mockImplementation(() => {});
    const debug = jest.spyOn(console, "debug").mockImplementation(() => {});

    try {
      routeFetch();
      render(<AgentsPage />);

      const section = await screen.findByTestId("agent-tools-orchestrator");
      await expand(user, section);
      expect(within(section).getByText("macro_snapshot")).toBeInTheDocument();

      expect(setItem).not.toHaveBeenCalled();
      expect(getItem).not.toHaveBeenCalled();
      expect(removeItem).not.toHaveBeenCalled();
      expect(log).not.toHaveBeenCalled();
      expect(info).not.toHaveBeenCalled();
      expect(debug).not.toHaveBeenCalled();
    } finally {
      setItem.mockRestore();
      getItem.mockRestore();
      removeItem.mockRestore();
      log.mockRestore();
      info.mockRestore();
      debug.mockRestore();
    }
  });
});
