# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: agents.spec.ts >> Agents page >> lists all agents with Run Now button
- Location: agents.spec.ts:4:7

# Error details

```
Error: expect(locator).toBeVisible() failed

Locator: getByRole('button', { name: 'Run Now' }).first()
Expected: visible
Timeout: 5000ms
Error: element(s) not found

Call log:
  - Expect "toBeVisible" getByRole('button', { name: 'Run Now' }).first() with timeout 5000ms
  - waiting for getByRole('button', { name: 'Run Now' }).first()

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
- main:
  - heading "Agents" [level=1]
- alert
```

# Test source

```ts
  1  | import { test, expect } from "@playwright/test";
  2  | 
  3  | test.describe("Agents page", () => {
  4  |   test("lists all agents with Run Now button", async ({ page }) => {
  5  |     await page.goto("/agents");
  6  |     await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
> 7  |     await expect(page.getByRole("button", { name: "Run Now" }).first()).toBeVisible();
     |                                                                         ^ Error: expect(locator).toBeVisible() failed
  8  |   });
  9  | 
  10 |   test("trigger button shows queued confirmation", async ({ page }) => {
  11 |     await page.goto("/agents");
  12 |     const firstButton = page.getByRole("button", { name: "Run Now" }).first();
  13 |     await firstButton.click();
  14 |     // Expect either "Triggering…" or a success message
  15 |     await expect(page.locator("text=/queued|Triggering/")).toBeVisible({ timeout: 5000 });
  16 |   });
  17 | });
  18 | 
```