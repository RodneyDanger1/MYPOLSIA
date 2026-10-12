import { test, expect } from "@playwright/test";

test.describe("Dashboard", () => {
  // Locators are ROLE-scoped on purpose: `getByText("Dashboard")` resolves to BOTH the sidebar link and
  // the page heading, and Playwright's strict mode fails rather than guessing — which is how this suite
  // failed on its first-ever run, alongside the blank-API-key 401 hiding behind it.
  test("loads and shows metrics cards", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    // `exact: true` is load-bearing: Playwright's getByText is CASE-INSENSITIVE and substring-based by
    // default, so "Tasks Today" also matches the nine agent cards that read "· 0 tasks today" — 10
    // elements once the grid had loaded and 1 before it, i.e. a failure that depended on load order.
    await expect(page.getByText("Tasks Today", { exact: true })).toBeVisible();
    await expect(page.getByText("MRR", { exact: true })).toBeVisible();
  });

  test("shows agent status grid", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByText("Agent Status")).toBeVisible();
  });

  test("sidebar navigation links are present", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByRole("link", { name: "Finance" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Agents" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Tasks" })).toBeVisible();
  });
});
