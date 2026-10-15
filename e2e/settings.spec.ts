import { test, expect } from "@playwright/test";

test.describe("Settings page", () => {
  test("loads company config", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Company Profile" })).toBeVisible();
    await expect(page.getByRole("tab", { name: "Scheduler" })).toBeVisible();
    // Until the blank-API-key fix this page rendered its FAILURE state instead, because its client-side
    // fetch was 401'd: asserting the absence of that state is the regression guard for it.
    await expect(page.getByText("Failed to load config")).toHaveCount(0);
  });

  test("save button is present", async ({ page }) => {
    await page.goto("/settings");
    await expect(page.getByRole("button", { name: "Save Settings" })).toBeVisible();
  });
});
