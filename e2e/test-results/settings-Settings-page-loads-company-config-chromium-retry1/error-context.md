# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: settings.spec.ts >> Settings page >> loads company config
- Location: settings.spec.ts:4:7

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: getByRole('heading', { name: 'Settings' })
Expected: visible
Timeout: 5000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" getByRole('heading', { name: 'Settings' }) with timeout 5000ms
  - waiting for getByRole('heading', { name: 'Settings' })

```

```yaml
- complementary:
  - heading "Polsia" [level=1]
  - paragraph: AI Business Agent
  - navigation:
    - link "Dashboard":
      - /url: /dashboard
      - img
      - text: Dashboard
    - link "Agents":
      - /url: /agents
      - img
      - text: Agents
    - link "Tasks":
      - /url: /tasks
      - img
      - text: Tasks
    - link "Social":
      - /url: /social
      - img
      - text: Social
    - link "Outreach":
      - /url: /outreach
      - img
      - text: Outreach
    - link "Ads":
      - /url: /ads
      - img
      - text: Ads
    - link "Finance":
      - /url: /finance
      - img
      - text: Finance
    - link "Memory":
      - /url: /memory
      - img
      - text: Memory
    - link "Settings":
      - /url: /settings
      - img
      - text: Settings
- main: Failed to load config
- alert
```

# Test source

```ts
  1  | import { test, expect } from "@playwright/test";
  2  | 
  3  | test.describe("Settings page", () => {
  4  |   test("loads company config", async ({ page }) => {
  5  |     await page.goto("/settings");
> 6  |     await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
     |                                                                   ^ Error: expect(locator).toBeVisible() failed
  7  |     await expect(page.getByText("Company Profile")).toBeVisible();
  8  |     await expect(page.getByText("Scheduler")).toBeVisible();
  9  |     // Until the blank-API-key fix this page rendered its FAILURE state instead, because its client-side
  10 |     // fetch was 401'd: asserting the absence of that state is the regression guard for it.
  11 |     await expect(page.getByText("Failed to load config")).toHaveCount(0);
  12 |   });
  13 | 
  14 |   test("save button is present", async ({ page }) => {
  15 |     await page.goto("/settings");
  16 |     await expect(page.getByRole("button", { name: "Save Settings" })).toBeVisible();
  17 |   });
  18 | });
  19 | 
```