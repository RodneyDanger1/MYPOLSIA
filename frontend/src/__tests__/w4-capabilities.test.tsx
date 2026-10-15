import { fireEvent, render, screen, within } from "@testing-library/react";

/**
 * WAVE V16 / W4 — the capabilities surface (frozen interface H-C1): `GET /api/v1/ops/capabilities`
 * serving `data = {agents:[{agent_type, tools:[{name, description}], action_classes:[str],
 * channels:[str]}], registry_tools:[{name, description}]}`, derived server-side from
 * `policy_firewall.AGENT_CAPABILITIES` + `tool_registry` (never hand-copied).
 *
 * The house mocking pattern (as in `u11-support`): the factory replaces `@/lib/api`'s `api` object
 * with jest.fn()s while keeping the ACTUAL module's classes — `ApiError` must stay the real class
 * so `errorText`'s `instanceof` branch is exercised. The REAL path is asserted per call.
 *
 * The honesty laws pinned here:
 *   - every rendered name / description / action class / channel comes off the read body VERBATIM
 *     (the registry is the truth — nothing is invented, nothing is prettified);
 *   - BOTH wire shapes render (the plan's envelope `{ok, data}` AND the bare-map shape today's
 *     sibling ops routes serve) — a body carrying neither is a NAMED error, never an empty state;
 *   - a missing endpoint (404 / refusal) renders the named error WITH a working Retry;
 *   - an empty registry renders the honest empty state — the ONE forbidden stand-in here is an
 *     empty state standing in for a failed or unreadable read;
 *   - a malformed agent row is dropped and NAMED (count line), while readable rows still render.
 */

jest.mock("@/lib/api", () => {
  const actual = jest.requireActual("@/lib/api");
  return {
    ...actual,
    api: { get: jest.fn(), post: jest.fn(), put: jest.fn(), patch: jest.fn(), delete: jest.fn() },
  };
});

import { ApiError, api } from "@/lib/api";
import CapabilitiesPage from "@/app/capabilities/page";
import SettingsPage, { type SettingsConfig } from "@/app/settings/page";

const mockGet = api.get as jest.Mock;

beforeEach(() => {
  mockGet.mockReset();
});

/** A resolve/reject handle — the only way to observe a TRUE loading state deterministically. */
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** The wire rows this suite serves — the exact H-C1 element shapes. */
type AgentRow = {
  agent_type: string;
  tools: { name: string; description?: string }[];
  action_classes: string[];
  channels: string[];
};

const orchestratorRow: AgentRow = {
  agent_type: "orchestrator",
  tools: [
    { name: "macro_snapshot", description: "Read the cached macro snapshot (World Bank / FRED / FX)." },
    { name: "pricing_catalog", description: "Read the dated market-rate pricing catalog (50 rows)." },
  ],
  action_classes: ["read", "internal_write"],
  channels: [],
};

const outreachRow: AgentRow = {
  agent_type: "outreach",
  tools: [{ name: "web_fetch", description: "Fetch one http(s) URL and return its page text." }],
  action_classes: ["read", "external_reversible"],
  channels: ["email", "linkedin"],
};

const registryTools: { name: string; description?: string }[] = [
  { name: "web_fetch", description: "Fetch one http(s) URL and return its page text." },
  { name: "macro_snapshot", description: "Read the cached macro snapshot (World Bank / FRED / FX)." },
  { name: "pricing_catalog", description: "Read the dated market-rate pricing catalog (50 rows)." },
];

/** The plan's H-C1 envelope: `{ok: true, data: {agents, registry_tools}}`. */
function envelope(agents: unknown[], tools: unknown[] = registryTools) {
  return { ok: true, data: { agents, registry_tools: tools } };
}

// ---------------------------------------------------------------------------------------------
// The read — GET /ops/capabilities
// ---------------------------------------------------------------------------------------------

describe("capabilities read (H-C1)", () => {
  it("shows the loading state, reads GET /ops/capabilities (envelope shape), then renders the REAL map: agents → tools (with registry descriptions) → action classes → channels", async () => {
    const d = deferred<unknown>();
    mockGet.mockReturnValueOnce(d.promise);

    render(<CapabilitiesPage />);

    expect(screen.getByTestId("capabilities-loading")).toHaveAttribute("aria-busy", "true");
    expect(mockGet).toHaveBeenCalledWith("/ops/capabilities");

    d.resolve(envelope([orchestratorRow, outreachRow]));

    const card = await screen.findByTestId("capability-agent-orchestrator");
    // The agent's OWN tools — names and the registry's descriptions, verbatim.
    expect(within(card).getByText("macro_snapshot")).toBeInTheDocument();
    expect(within(card).getByText(/Read the cached macro snapshot/)).toBeInTheDocument();
    // Action classes render exactly as the registry words them.
    expect(within(card).getByText("read")).toBeInTheDocument();
    expect(within(card).getByText("internal_write")).toBeInTheDocument();
    // channels=[] is stated, not fabricated.
    expect(within(card).getByText("No external channels.")).toBeInTheDocument();
    // The OTHER agent's tools do not leak into this card (the map is per-agent).
    expect(within(card).queryByText("web_fetch")).toBeNull();

    const outreach = screen.getByTestId("capability-agent-outreach");
    expect(within(outreach).getByText("web_fetch")).toBeInTheDocument();
    expect(within(outreach).getByText("email")).toBeInTheDocument();
    expect(within(outreach).getByText("linkedin")).toBeInTheDocument();

    // The catalog section lists every registry tool, measured off THIS read.
    const registry = screen.getByTestId("capabilities-registry");
    expect(within(registry).getAllByText("web_fetch").length).toBeGreaterThan(0);
    expect(screen.getByTestId("capabilities-count")).toHaveTextContent("2 agents · 3 registry tools");
    expect(screen.queryByTestId("capabilities-loading")).toBeNull();
  });

  it("also renders the bare-map shape today's sibling ops routes serve (no envelope)", async () => {
    mockGet.mockResolvedValueOnce({ agents: [orchestratorRow], registry_tools: registryTools });

    render(<CapabilitiesPage />);

    const card = await screen.findByTestId("capability-agent-orchestrator");
    expect(within(card).getByText("pricing_catalog")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("unwraps a body whose `data` carries the frozen arrays even without the ok flag", async () => {
    mockGet.mockResolvedValueOnce({ data: { agents: [outreachRow], registry_tools: [] } });

    render(<CapabilitiesPage />);

    const card = await screen.findByTestId("capability-agent-outreach");
    expect(within(card).getByText("web_fetch")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("an empty registry after a successful read prints the honest empty state only — never an error, never rows", async () => {
    mockGet.mockResolvedValueOnce(envelope([], []));

    render(<CapabilitiesPage />);

    expect(await screen.findByTestId("capabilities-empty")).toBeInTheDocument();
    expect(screen.getByTestId("capabilities-count")).toHaveTextContent("0 agents · 0 registry tools");
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByTestId(/^capability-agent-/)).toBeNull();
    expect(screen.queryByTestId("capabilities-dropped")).toBeNull();
  });

  it("a missing endpoint (404) renders the NAMED error — the empty copy never stands in for a failed read — and Retry re-reads and renders the real rows", async () => {
    mockGet.mockRejectedValueOnce(
      new ApiError(404, "Not Found", "E_HTTP_404", "the endpoint is not served by this build"),
    );

    render(<CapabilitiesPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_HTTP_404");
    expect(alert).toHaveTextContent("the endpoint is not served by this build");
    // The one forbidden stand-in: a failed read rendered as "nothing to show".
    expect(screen.queryByTestId("capabilities-empty")).toBeNull();

    mockGet.mockResolvedValueOnce(envelope([orchestratorRow]));
    fireEvent.click(within(alert).getByRole("button", { name: /^retry$/i }));

    expect(await screen.findByTestId("capability-agent-orchestrator")).toBeInTheDocument();
    expect(mockGet).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("a 2xx body that is not the capability map is a NAMED error — never an empty state", async () => {
    // The shape a proxy / a wrong route answers with: `{"detail": "Not Found"}` at 200.
    mockGet.mockResolvedValueOnce({ detail: "Not Found" });

    render(<CapabilitiesPage />);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent(/capability map/i);
    expect(screen.queryByTestId("capabilities-empty")).toBeNull();
  });

  it("drops a malformed agent row BY NAME (count line) while every readable row still renders — never a fabricated row", async () => {
    mockGet.mockResolvedValueOnce(
      envelope([
        orchestratorRow,
        // Hostile row: no tools / action_classes / channels arrays at all.
        { agent_type: "hostile_row" } as unknown as AgentRow,
      ]),
    );

    render(<CapabilitiesPage />);

    expect(await screen.findByTestId("capability-agent-orchestrator")).toBeInTheDocument();
    const dropped = screen.getByTestId("capabilities-dropped");
    expect(dropped).toHaveTextContent("1 unreadable row dropped");
    // The unreadable row contributes NOTHING to the DOM — not even its name.
    expect(screen.queryByText("hostile_row")).toBeNull();
    // And no placeholder vocabulary leaks anywhere on the page.
    expect(document.body.textContent ?? "").not.toMatch(/undefined|NaN|Invalid Date/);
  });

  it("agent-empty tool lists and description-less tool rows are STATED, not fabricated", async () => {
    mockGet.mockResolvedValueOnce(
      envelope(
        [
          {
            agent_type: "business_planning",
            tools: [],
            action_classes: ["read"],
            channels: ["ops"],
          },
        ],
        [{ name: "mystery_tool" }],
      ),
    );

    render(<CapabilitiesPage />);

    const card = await screen.findByTestId("capability-agent-business_planning");
    expect(within(card).getByText("No registry tools are granted to this agent.")).toBeInTheDocument();

    const registry = screen.getByTestId("capabilities-registry");
    expect(within(registry).getByText("mystery_tool")).toBeInTheDocument();
    expect(within(registry).getByText("The registry states no description for this tool.")).toBeInTheDocument();
  });
});

// ---------------------------------------------------------------------------------------------
// The wiring — the Settings entry (the documented minimal-noise decision)
// ---------------------------------------------------------------------------------------------

describe("capabilities wiring (Settings entry)", () => {
  const wireConfig: SettingsConfig = {
    name: "Polsia",
    mission: null,
    vision: null,
    description: null,
    target_market: null,
    value_prop: null,
    website_url: null,
    industry: null,
    product_type: null,
    timezone: null,
    daily_cycle_hour: null,
  };

  it("the Settings page links to /capabilities and fires no capabilities read of its own", async () => {
    mockGet.mockImplementation((path: string) => {
      if (path === "/config") return Promise.resolve(wireConfig);
      if (path === "/config/deepseek-key") {
        return Promise.resolve({ configured: false, source: "none" as const, masked: "" });
      }
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    render(<SettingsPage />);

    const link = await screen.findByTestId("settings-capabilities-link");
    expect(link).toHaveAttribute("href", "/capabilities");
    // The entry point is a plain link: it must not fire the capabilities read on mount.
    expect(mockGet).not.toHaveBeenCalledWith("/ops/capabilities");
  });
});
