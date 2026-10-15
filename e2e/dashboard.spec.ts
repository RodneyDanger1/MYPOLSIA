import { test, expect } from "@playwright/test";

test.describe("Dashboard", () => {
  // Locators stay ROLE-scoped on purpose: Playwright's strict mode fails rather than guessing, and
  // the pre-overhaul suite tripped on `getByText("Dashboard")` matching both a nav link and the page
  // heading. Since the U1 overhaul the nav lives in the topbar's MENU dropdown (no persistent
  // sidebar), so the heading locator below stays unambiguous while the dropdown is closed.
  test("loads and shows the business panel", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    // `exact: true` remains load-bearing: Playwright's getByText is CASE-INSENSITIVE and
    // substring-based by default, and other surfaces (agent rows, task chips) also mention today's
    // task counts — the label renders exactly once, so exact matching keeps the locator stable.
    await expect(page.getByText("Tasks today", { exact: true })).toBeVisible();
    await expect(page.getByText("MRR", { exact: true })).toBeVisible();
  });

  test("shows the workspace status rail", async ({ page }) => {
    await page.goto("/dashboard");
    const rail = page.getByLabel("Workspace status");
    await expect(rail).toBeVisible();
    // Honest binary state (live dossier §1.3): "Working"/"Idle" plus a run-count line. Assert the
    // stable structure, not one state — the e2e stack's agents are usually idle.
    await expect(rail.getByText(/^(Working|Idle)$/)).toBeVisible();
    await expect(rail.getByText("Last run", { exact: true })).toBeVisible();
  });

  // V10/H2 re-aim + V16 de-pin: the counts here kept going stale ("nine" -> "eleven routes"
  // while nav-items.ts NAV_ITEMS then carried FOURTEEN links after V11 added Invoices/
  // Schedules/Support — and each new wave (V16: Phone) moves it again).  The exact-count pin
  // lives in the shell jest test where nav-items.ts is importable; this e2e spec asserts
  // PRESENCE of the anchor routes + a lower bound, so it guards the real intent (a working MENU
  // carrying every route) without re-pinning the number every wave.
  test("MENU dropdown carries every route (>=14 — nav-items.ts is the source of truth)", async ({ page }) => {
    await page.goto("/dashboard");
    await page.getByTestId("nav-menu-trigger").click();
    const nav = page.getByRole("navigation", { name: "Main" });
    await expect(nav.getByRole("link", { name: "Tasks" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Finance" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Agents" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Planner" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Usage" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Invoices" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Schedules" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Support" })).toBeVisible();
    expect(await nav.getByRole("link").count()).toBeGreaterThanOrEqual(14);
  });
});
