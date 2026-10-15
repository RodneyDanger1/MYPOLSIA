import { test, expect } from "@playwright/test";
import { API_BASE } from "./_helpers";

/**
 * V9/B5 — the planner page, the menu pin move and the first-dollar checklist, against the live
 * stack.
 *
 * V10/H2: BOTH fixme marks are REMOVED and this spec runs against the live stack (the endpoints it
 * reads landed with V9 and answered live in the V10 lab round; every locator below was verified
 * against today's markup before the marks came off).
 *
 * What this spec deliberately does NOT do: click PROPOSE. A propose is a real model call plus web
 * fetches — money spent inside a lab run — and the wave's e2e pass is about the surface, not about
 * paying for a proposal. The propose→confirm path (including "nothing spends") is pinned in
 * `frontend/src/__tests__/u9-planner.test.tsx` with a mocked api module, and the ONE live propose of
 * this wave is the parent's close action (V9 acceptance), not a spec's side effect.
 *
 * `GET /planner/status` is read out-of-band first so the assertions are derived from the real
 * answer: an app with no confirmed idea must show the three-line flow, and one WITH a confirmed
 * idea must show that idea — both states are honest, and this spec accepts whichever the stack
 * answers, without pretending to know which it is.
 */

type StatusBody = {
  confirmed: { candidate_id?: string; name?: string } | null;
  last_proposed_at: string | null;
};

test.describe("Planner", () => {
  test("reaches /planner from the MENU and renders the flow, the checklist and the propose control (V10 lab)", async ({
    page,
    request,
  }) => {
    const statusResponse = await request.get(`${API_BASE}/api/v1/planner/status`);
    expect(statusResponse.status()).toBe(200);
    const status = (await statusResponse.json()) as StatusBody;

    const response = await page.goto("/dashboard");
    expect(response?.status()).toBe(200);

    // Watch the wire BEFORE navigating into /planner: the page's own status read fires on mount.
    const apiCalls: string[] = [];
    page.on("request", (r) => {
      const url = r.url();
      if (url.includes("/api/v1/planner")) apiCalls.push(`${r.method()} ${url}`);
    });

    // The nav pin this wave moved: the Planner route is reachable from the one MENU dropdown.
    await page.getByTestId("nav-menu-trigger").click();
    const plannerLink = page.getByRole("navigation", { name: "Main" }).getByRole("link", { name: /planner/i });
    await expect(plannerLink).toHaveAttribute("href", "/planner");
    await plannerLink.click();

    await expect(page.getByRole("heading", { level: 1, name: "Planner" })).toBeVisible();

    // The page does its OWN status read (a static page would be a snapshot, not a planner).
    await expect.poll(() => apiCalls.filter((c) => c.startsWith("GET")).length).toBeGreaterThan(0);

    // One of the two honest states, matching what the status endpoint actually answers.
    if (status.confirmed === null) {
      const flow = page.getByTestId("planner-flow");
      await expect(flow).toBeVisible();
      const steps = flow.getByTestId("planner-flow-step");
      await expect(steps).toHaveCount(3);
    } else {
      const confirmed = page.getByTestId("planner-confirmed");
      await expect(confirmed).toBeVisible();
      if (status.confirmed.name) {
        await expect(confirmed).toContainText(status.confirmed.name);
      }
    }

    // The propose control exists and is operable; clicking it is the parent's call (it spends).
    await expect(page.getByTestId("planner-propose")).toBeEnabled();

    // The checklist renders with its renewal-clock rows and their source links.
    await expect(page.getByTestId("first-dollar-checklist")).toBeVisible();
    await expect(page.getByTestId("checklist-step-mn-llc-renewal")).toContainText("$0");

    // The MENU carries every route (nav-items.ts is the source of truth; >=14 since V11 added
    // Invoices/Schedules/Support, and each wave grows it — lower bound on purpose; the exact
    // count pin lives in the shell jest test where nav-items.ts is importable).
    await page.keyboard.press("Escape");
    await page.getByTestId("nav-menu-trigger").click();
    expect(
      await page.getByRole("navigation", { name: "Main" }).getByRole("link").count(),
    ).toBeGreaterThanOrEqual(14);
  });

  test("the finance Planning panel renders its own read or names the refusal (V10 lab)", async ({ page }) => {
    await page.goto("/finance");
    await expect(page.getByRole("heading", { level: 1, name: "Finance" })).toBeVisible();

    const planning = page.getByTestId("finance-planning");
    await expect(planning).toBeVisible();

    // Either the endpoint answered (segments on screen) or the read failed and the failure is
    // NAMED — never an empty panel standing in for a broken read.
    const named = planning.getByText(/Could not load the finance planning view/);
    const burn = planning.getByTestId("planning-burn");
    const burnEmpty = planning.getByTestId("planning-burn-empty");
    await expect(named.or(burn).or(burnEmpty)).toBeVisible();

    // Parent scope amendment: the market context strip lives inside the same panel, as ONE line —
    // facts with their sources, reasons for the sections the endpoint could not measure, or a named
    // failure. All three are honest outcomes for a lab run.
    const market = planning.getByTestId("market-context");
    const marketFailed = planning.getByTestId("market-context-error");
    await expect(market.or(marketFailed)).toBeVisible();
  });
});
