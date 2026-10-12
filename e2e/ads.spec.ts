import { test, expect } from "@playwright/test";
import { API_BASE, PREFIX, sqliteRun } from "./_helpers";

test.describe("Ads page", () => {
  test("heading + campaign state consistent with GET /api/v1/ads/campaigns", async ({ page, request }) => {
    const api = await request.get(`${API_BASE}/api/v1/ads/campaigns`);
    expect(api.status()).toBe(200);
    const campaigns = await api.json();

    // The page must READ — the frozen-snapshot defect rendered an empty state with NO api call at all.
    const apiCalls: string[] = [];
    page.on("request", (r) => {
      if (r.method() === "GET" && r.url().includes("/api/v1/ads/campaigns")) apiCalls.push(r.url());
    });

    await page.goto("/ads");
    await expect(page.getByRole("heading", { name: "Ad Campaigns" })).toBeVisible();

    const cards = page.locator("main div.bg-gray-800");
    if (campaigns.length === 0) {
      await expect(page.getByText("No campaigns yet.")).toBeVisible();
      await expect(cards).toHaveCount(0);
    } else {
      // poll, don't sample: the page may still be fetching/rendering when the branch is chosen
      await expect
        .poll(async () => cards.count(), {
          message: `/ads must render at least ${campaigns.length} card(s) (the API state)`,
        })
        .toBeGreaterThanOrEqual(campaigns.length);
      // Newest first (id DESC): the first card is the API's first campaign.
      await expect(cards.first()).toContainText(campaigns[0].name);
    }
    expect(
      apiCalls.length,
      "the page must actually CALL GET /api/v1/ads/campaigns — an empty/static render without a read " +
        "is the frozen-snapshot defect",
    ).toBeGreaterThan(0);
  });

  test("renders a campaign's platform/status line and formatted dollar amounts (seeded fixture)", async ({ page, request }) => {
    // No write API exists for campaigns and the broker is down; seed one obviously-named row into the
    // same SQLite file the backend serves from, then assert its rendering end-to-end.
    const stamp = Date.now();
    const name = `${PREFIX} campaign ${stamp}`;
    try {
      sqliteRun(
        `INSERT INTO ad_campaigns (platform, name, goal, status, daily_budget_usd, total_spent_usd, created_at)
         VALUES ('meta', ?, 'signups', 'active', 12.5, 3.25, strftime('%Y-%m-%d %H:%M:%S','now'))`,
        [name],
      );

      await page.goto("/ads");
      await expect(page.getByRole("heading", { name: "Ad Campaigns" })).toBeVisible();
      await expect(page.getByText("No campaigns yet.")).toHaveCount(0);

      const card = page.locator("main div.bg-gray-800").filter({ hasText: name });
      await expect(
        card,
        `GET /api/v1/ads/campaigns serves "${name}" but /ads never renders its card — ` +
          "the page is not rendering what the API serves (frozen build-time snapshot?)",
      ).toHaveCount(1);
      await expect(card).toContainText(name);
      await expect(card).toContainText("meta · active");
      await expect(card).toContainText("$3.25 spent"); // total_spent_usd.toFixed(2)
      await expect(card).toContainText("$12.5/day budget"); // raw number, not toFixed — 12.5 renders as "12.5"
      // id DESC: the freshly inserted campaign has the highest id and is the first card.
      await expect(page.locator("main div.bg-gray-800").first()).toContainText(name);

      const api = await request.get(`${API_BASE}/api/v1/ads/campaigns`);
      const campaigns = await api.json();
      expect(campaigns.some((c: { name: string }) => c.name === name)).toBe(true);
    } finally {
      const del = sqliteRun("DELETE FROM ad_campaigns WHERE name LIKE ?", [`${PREFIX}%`]);
      expect(del.changes, "fixture campaign row must be removable").toBeGreaterThanOrEqual(1);
    }
  });
});
