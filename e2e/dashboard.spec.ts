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

  // V10/H2 re-aim: this test said "nine routes" with a count(9) assertion, but the menu's real
  // list (nav-items.ts NAV_ITEMS) carries ELEVEN links today — Planner + Usage landed since this
  // spec was written. Title, count and spot-checks were re-aimed to the actual list; the stale
  // claim is quoted here so the correction is auditable.
  test("MENU dropdown carries the eleven routes", async ({ page }) => {
    await page.goto("/dashboard");
    await page.getByTestId("nav-menu-trigger").click();
    const nav = page.getByRole("navigation", { name: "Main" });
    await expect(nav.getByRole("link", { name: "Tasks" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Finance" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Agents" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Planner" })).toBeVisible();
    await expect(nav.getByRole("link", { name: "Usage" })).toBeVisible();
    await expect(nav.getByRole("link")).toHaveCount(11);
  });
});
