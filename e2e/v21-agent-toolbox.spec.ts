import { test, expect, type Page, type Route } from "@playwright/test";

/**
 * V21/L3 — the V20 agents toolbox, pinned end to end.
 *
 * `AgentTools` is the only surface that states what each agent can DO; until now it was jsdom-only
 * (v20-agents-toolbox.test.tsx), so no test proved the browser actually wires the button, the
 * panel, the Escape key or the network read. Each test below pins one observable law:
 *
 *   1. ONE READ FOR THE WHOLE ROSTER — mounting `/agents` issues exactly ONE
 *      `GET /api/v1/ops/capabilities`; every row's `TOOLS · N` button starts `aria-expanded="false"`.
 *   2. OPEN = THE AGENT'S REAL TOOLS — clicking `TOOLS · 3` sets `aria-expanded="true"`, points
 *      `aria-controls` at a real DOM node, and renders the three registry tool names VERBATIM with
 *      the registry's own descriptions from the payload. Clicking again collapses.
 *   3. ESCAPE COLLAPSES AND RETURNS FOCUS — with focus inside the panel, Escape closes it and the
 *      trigger is the focused element again. A second Escape (already collapsed) changes nothing.
 *   4. HONEST EMPTIES — an agent the map lists with zero tools says "No tools are granted to this
 *      agent."; an agent the map has no readable row for says exactly that — never the other
 *      agent's answer, never a fabricated row.
 *   5. A REFUSAL IS NAMED, NEVER AN EMPTY TOOLBOX — a 500 envelope `{ok:false,error:{…}}` makes the
 *      trigger say "Tools — unavailable", renders the shared alert (`agents-tools-error`) with the
 *      server's `code: message — how_to_fix` and a Retry; Retry re-reads and the panel recovers.
 *
 * Environment independence: both reads the page needs are stubbed at the route layer with payloads
 * shaped like the live ones (read from `curl http://127.0.0.1/api/v1/ops/capabilities` at the
 * time of writing — the same `agent_type`/`tools[{name,description}]`/`action_classes`/`channels`
 * rows and the four real registry descriptions). No store, config or lab DB is required, and the
 * only requests intercepted are reads; this file issues NO POST.
 */

const STATUS_GLOB = "**/api/v1/agents/status**";
const CAPABILITIES_GLOB = "**/api/v1/ops/capabilities**";

/** `GET /agents/status` is a bare array of these rows (backend `AgentStatusResponse`). */
const STATUS_BODY = [
  {
    agent_type: "orchestrator",
    last_run_at: "2026-10-19T06:00:00.219294Z",
    last_run_status: "completed",
    tasks_today: 0,
    tasks_total: 3,
  },
  {
    agent_type: "finance",
    last_run_at: "2026-10-19T12:00:02.930033Z",
    last_run_status: "completed",
    tasks_today: 4,
    tasks_total: 30,
  },
  {
    agent_type: "social_media",
    last_run_at: null,
    last_run_status: null,
    tasks_today: 0,
    tasks_total: 0,
  },
  // Present on the roster, ABSENT from the capability map on purpose: this is the
  // "no readable entry" branch, which must not be confused with "known, zero tools".
  {
    agent_type: "email_outreach",
    last_run_at: "2026-10-19T06:01:00.740014Z",
    last_run_status: "completed",
    tasks_today: 1,
    tasks_total: 5,
  },
];

/**
 * The live registry's own words (four tools). Reproduced verbatim so a description mismatch is a
 * real assertion, not a placeholder. `pricing_catalog` is intentionally omitted from the two
 * agents that do not hold it in the live map.
 */
const MACRO = {
  name: "macro_snapshot",
  description:
    "Read the cached macro snapshot the finance page renders (inflation, CPI, FX) with its provenance and per-section reasons. Cache-only: it never fetches, and with no fresh snapshot it answers 'unavailable' instead of guessing.",
};
const PRICING = {
  name: "pricing_catalog",
  description:
    "Read the dated 50-row market-rate pricing catalog (a research snapshot, NOT live pricing) plus its staleness provenance (catalog_as_of, staleness_days, stale). Use it before pricing a service or sanity-checking a quoted price.",
};
const PHONE = {
  name: "phone_read",
  description:
    "Read the owner's phone (Phone Link store) as a JSON object: the newest threads, and one thread's newest messages when thread_id is given. Read-only. Verification codes are NOT available to tools: the result has no codes field, and a message body that looks like a verification code is withheld.",
};

/** The H-C1 envelope the plan freezes: `{ok:true, data:{agents:[…], registry_tools:[…]}}`. */
const CAPABILITIES_BODY = {
  ok: true,
  data: {
    agents: [
      {
        agent_type: "orchestrator",
        tools: [MACRO, PRICING, PHONE],
        action_classes: ["internal_write", "read"],
        channels: [],
      },
      {
        agent_type: "finance",
        tools: [MACRO, PRICING],
        action_classes: ["internal_write", "read"],
        channels: [],
      },
      {
        // The live map's honest zero: listed, granted nothing.
        agent_type: "social_media",
        tools: [],
        action_classes: ["external_reversible", "read"],
        channels: ["x", "linkedin"],
      },
    ],
    registry_tools: [MACRO, PRICING, PHONE],
  },
};

/** The refusal shape `lib/api.ts` parses from the app's `{ok:false, error:{…}}` envelope. */
const REFUSAL_BODY = {
  ok: false,
  error: {
    code: "E_TOOL_REGISTRY_UNAVAILABLE",
    message: "the tool registry read failed",
    how_to_fix: "retry when the registry is reachable",
  },
};

type State = {
  /** Serve the refusal instead of the map. Flipped mid-test to prove Retry re-reads. */
  refuse: boolean;
  /** Every capabilities GET the page issues, in order. */
  reads: string[];
};

async function stubAgentReads(page: Page, state: State): Promise<void> {
  await page.route(STATUS_GLOB, async (route: Route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(STATUS_BODY),
    });
  });

  await page.route(CAPABILITIES_GLOB, async (route: Route) => {
    if (route.request().method() !== "GET") {
      await route.continue();
      return;
    }
    state.reads.push(route.request().url());
    await route.fulfill({
      status: state.refuse ? 500 : 200,
      contentType: "application/json",
      headers: { "access-control-allow-origin": "*" },
      body: JSON.stringify(state.refuse ? REFUSAL_BODY : CAPABILITIES_BODY),
    });
  });
}

async function openAgents(page: Page): Promise<State> {
  const state: State = { refuse: false, reads: [] };
  await stubAgentReads(page, state);
  await page.goto("/agents");
  await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
  await expect(page.locator("[data-agent]")).toHaveCount(STATUS_BODY.length);
  return state;
}

/** That agent's toolbox container (testid is `agent-tools-${agentType}`). */
function toolsSection(page: Page, agentType: string) {
  return page.getByTestId(`agent-tools-${agentType}`);
}

/** The trigger button inside one agent's toolbox. */
function toolsButton(page: Page, agentType: string) {
  return toolsSection(page, agentType).getByRole("button", { name: /^tools/i });
}

test.describe("V21 agents toolbox (live FE)", () => {
  test("one capabilities read for the roster; every TOOLS button starts collapsed", async ({ page }) => {
    const state = await openAgents(page);

    // The button states the count it can prove: "TOOLS · 3" (CSS uppercases the label token).
    for (const [agentType, count] of [
      ["orchestrator", "3"],
      ["finance", "2"],
      ["social_media", "0"],
    ] as const) {
      const button = toolsButton(page, agentType);
      await expect(button).toHaveText(new RegExp(`tools · ${count}`, "i"));
      await expect(button).toHaveAttribute("aria-expanded", "false");
      // Collapsed means no panel is referenced or rendered.
      expect(await button.getAttribute("aria-controls"), `${agentType} is collapsed`).toBeNull();
    }
    // An agent with no readable map row cannot honestly claim a count.
    await expect(toolsButton(page, "email_outreach")).toHaveText(/^tools$/i);
    await expect(toolsButton(page, "email_outreach")).toHaveAttribute("aria-expanded", "false");

    // ONE read for every row — never one request per agent.
    expect(state.reads, "the roster must issue exactly one capabilities read").toHaveLength(1);
    const url = new URL(state.reads[0]);
    expect(url.pathname).toBe("/api/v1/ops/capabilities");
  });

  test("click opens the panel with the agent's tools and their descriptions; click again collapses", async ({
    page,
  }) => {
    await openAgents(page);
    const button = toolsButton(page, "orchestrator");
    await expect(button).toHaveAttribute("aria-expanded", "false");

    await button.click();
    await expect(button).toHaveAttribute("aria-expanded", "true");

    // aria-controls must point at a real node in the DOM (not a dangling id).
    const controls = await button.getAttribute("aria-controls");
    expect(controls, "an open toolbox must name its panel").toBeTruthy();
    const panel = page.locator(`[id="${controls}"]`);
    await expect(panel).toBeVisible();
    await expect(panel).toHaveAttribute("role", "group");
    await expect(panel).toHaveAttribute("aria-label", "Tools for orchestrator");

    // Exactly the three tools that agent is granted, in payload order, deduped by the view.
    const rows = panel.locator("li");
    await expect(rows).toHaveCount(3);
    await expect(rows.nth(0)).toContainText("macro_snapshot");
    await expect(rows.nth(1)).toContainText("pricing_catalog");
    await expect(rows.nth(2)).toContainText("phone_read");

    // Descriptions are the registry's own words — verbatim, not embellished.
    await expect(rows.nth(0)).toContainText("Cache-only: it never fetches");
    await expect(rows.nth(1)).toContainText("a research snapshot, NOT live pricing");
    await expect(rows.nth(2)).toContainText("the result has no codes field");

    // The other agents' toolboxes stay shut — opening one is not opening all.
    await expect(toolsButton(page, "finance")).toHaveAttribute("aria-expanded", "false");
    await expect(page.getByTestId("agents-tools-error")).toHaveCount(0);

    // A second click on the same trigger collapses it and removes the panel.
    await button.click();
    await expect(button).toHaveAttribute("aria-expanded", "false");
    await expect(panel).toHaveCount(0);
  });

  test("Escape collapses and returns focus to the trigger; a second Escape is a no-op", async ({
    page,
  }) => {
    await openAgents(page);
    const button = toolsButton(page, "finance");
    await button.click();
    await expect(button).toHaveAttribute("aria-expanded", "true");
    const controls = await button.getAttribute("aria-controls");
    const panel = page.locator(`[id="${controls}"]`);
    await expect(panel).toBeVisible();

    // Put focus genuinely INSIDE the section (the panel itself is not tabbable in product code;
    // making it programmatically focusable is the smallest way to originate the key event there).
    await panel.evaluate((element) => {
      const node = element as HTMLElement;
      node.tabIndex = -1;
      node.focus();
    });
    await expect(panel).toBeFocused();

    await page.keyboard.press("Escape");
    await expect(button).toHaveAttribute("aria-expanded", "false");
    await expect(panel).toHaveCount(0);
    await expect(button, "Escape must return focus to the trigger").toBeFocused();

    // Collapsed already: Escape must not re-open it.
    await page.keyboard.press("Escape");
    await expect(button).toHaveAttribute("aria-expanded", "false");
    await expect(panel).toHaveCount(0);
  });

  test("a no-tools agent and an unmapped agent each say the honest, DIFFERENT thing", async ({
    page,
  }) => {
    await openAgents(page);

    // Listed with zero grants: a real answer, said as itself.
    const zero = toolsButton(page, "social_media");
    await expect(zero).toHaveText(/tools · 0/i);
    await zero.click();
    const zeroPanel = page.locator(`[id="${await zero.getAttribute("aria-controls")}"]`);
    await expect(zeroPanel).toContainText("No tools are granted to this agent.");
    await expect(zeroPanel, "zero tools must not be rendered as a dropped row").not.toContainText(
      "no readable entry",
    );
    await expect(zeroPanel.locator("li")).toHaveCount(0);

    // No readable row for this agent: a different fact, with its own sentence.
    const unmapped = toolsButton(page, "email_outreach");
    await unmapped.click();
    const unmappedPanel = page.locator(`[id="${await unmapped.getAttribute("aria-controls")}"]`);
    await expect(unmappedPanel).toContainText("The capability map has no readable entry for this agent.");
    await expect(unmappedPanel).not.toContainText("No tools are granted to this agent.");
    await expect(unmappedPanel.locator("li")).toHaveCount(0);
  });

  test("a 500 refusal renders the shared UNAVAILABLE alert; Retry re-reads and recovers", async ({
    page,
  }) => {
    const state: State = { refuse: true, reads: [] };
    await stubAgentReads(page, state);
    await page.goto("/agents");
    await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();

    // The failure is visible BEFORE expanding: the trigger itself says unavailable…
    const button = toolsButton(page, "orchestrator");
    await expect(button).toContainText(/tools — unavailable/i);
    await expect(button).toHaveAttribute("aria-expanded", "false");

    await button.click();
    await expect(button).toHaveAttribute("aria-expanded", "true");
    const alert = page.getByTestId("agents-tools-error");
    await expect(alert).toBeVisible();
    await expect(alert).toContainText("Could not load this agent");
    // The shared error vocabulary: `code: message — how_to_fix`, never a generic sentence.
    await expect(alert).toContainText(
      "E_TOOL_REGISTRY_UNAVAILABLE: the tool registry read failed — retry when the registry is reachable",
    );
    const retry = alert.getByRole("button", { name: "Retry" });
    await expect(retry).toBeVisible();

    // A refusal is NEVER an empty toolbox: no agent claims "no tools" off a failed read.
    const text = await page.locator("body").innerText();
    expect(text).not.toContain("No tools are granted to this agent.");
    expect(state.reads.length, "the failed read uses the client's bounded retries").toBeGreaterThan(0);
    const beforeRetry = state.reads.length;

    // Retry goes back through the wire and the panel recovers on the real payload.
    state.refuse = false;
    await retry.click();
    await expect(alert).toHaveCount(0);
    await expect(button).toHaveText(/tools · 3/i);
    const controls = await button.getAttribute("aria-controls");
    await expect(page.locator(`[id="${controls}"]`)).toContainText("macro_snapshot");
    expect(state.reads.length, "Retry must issue a fresh read").toBeGreaterThan(beforeRetry);
  });
});
