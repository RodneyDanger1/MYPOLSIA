import { test, expect } from "@playwright/test";

test.describe("Dashboard", () => {
  // Locators are ROLE-scoped on purpose: `getByText("Dashboard")` resolves to BOTH the sidebar link and
  // the page heading, and Playwright's strict mode fails rather than guessing — which is how this suite
  // failed on its first-ever run, alongside the blank-API-key 401 hiding behind it.
  test("loads and shows metrics cards", async ({ page }) => {
    await page.goto("/dashboard");
    await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();
    await expect(page.getByText("Tasks Today")).toBeVisible();
    await expect(page.getByText("MRR")).toBeVisible();
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
