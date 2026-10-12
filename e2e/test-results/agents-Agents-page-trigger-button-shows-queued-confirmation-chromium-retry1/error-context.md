# Instructions

- Following Playwright test failed.
- Explain why, be concise, respect Playwright best practices.
- Provide a snippet of code with the fix, if possible.

# Test info

- Name: agents.spec.ts >> Agents page >> trigger button shows queued confirmation
- Location: agents.spec.ts:10:7

# Error details

```
Test timeout of 30000ms exceeded.
```

```
Error: locator.click: Test timeout of 30000ms exceeded.
Call log:
  - waiting for getByRole('button', { name: 'Run Now' }).first()

```

# Page snapshot

```yaml
- generic [active] [ref=e1]:
  - complementary [ref=e2]:
    - generic [ref=e3]:
      - heading "Polsia" [level=1] [ref=e4]
      - paragraph [ref=e5]: AI Business Agent
    - navigation [ref=e6]:
      - link "Dashboard" [ref=e7] [cursor=pointer]:
        - /url: /dashboard
      - link "Agents" [ref=e9] [cursor=pointer]:
        - /url: /agents
      - link "Tasks" [ref=e13] [cursor=pointer]:
        - /url: /tasks
      - link "Social" [ref=e18] [cursor=pointer]:
        - /url: /social
      - link "Outreach" [ref=e21] [cursor=pointer]:
        - /url: /outreach
      - link "Ads" [ref=e25] [cursor=pointer]:
        - /url: /ads
      - link "Finance" [ref=e29] [cursor=pointer]:
        - /url: /finance
      - link "Memory" [ref=e32] [cursor=pointer]:
        - /url: /memory
      - link "Settings" [ref=e37] [cursor=pointer]:
        - /url: /settings
  - main [ref=e41]:
    - heading "Agents" [level=1] [ref=e43]
  - alert [ref=e44]
```

# Test source

```ts
  1  | import { test, expect } from "@playwright/test";
  2  | 
  3  | test.describe("Agents page", () => {
  4  |   test("lists all agents with Run Now button", async ({ page }) => {
  5  |     await page.goto("/agents");
  6  |     await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
  7  |     await expect(page.getByRole("button", { name: "Run Now" }).first()).toBeVisible();
  8  |   });
  9  | 
  10 |   test("trigger button shows queued confirmation", async ({ page }) => {
  11 |     await page.goto("/agents");
  12 |     const firstButton = page.getByRole("button", { name: "Run Now" }).first();
> 13 |     await firstButton.click();
     |                       ^ Error: locator.click: Test timeout of 30000ms exceeded.
  14 |     // Expect either "Triggering…" or a success message
  15 |     await expect(page.locator("text=/queued|Triggering/")).toBeVisible({ timeout: 5000 });
  16 |   });
  17 | });
  18 | 
```