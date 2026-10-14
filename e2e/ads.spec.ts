import { test, expect } from "@playwright/test";
import { API_BASE, LAB_DB_SKIP_REASON, PREFIX, labDbAvailable, sqliteRun } from "./_helpers";

/**
 * Wave U4 re-aim (old -> new) — the page was rebuilt on the U1 design system this wave; every old
 * DOM anchor is gone, and every pin below was re-derived from the API body:
 *
 *   old: campaign cards located by `main div.bg-gray-800`; the meta line pinned as ONE combined
 *        string `"meta · active"`; money pinned at the OLD formatter's raw floats
 *        (`"$3.25 spent"` above a literal `"$12.5/day budget"` — the audit's Q-13 split); the empty
 *        state pinned as the bare `"No campaigns yet."`; no testids, no measured count, no cap
 *        disclosure, no read-URL pin.
 *   new: rows are `[data-testid="campaign-row"]`; platform and status are SEPARATE chips
 *        (`campaign-platform` / `campaign-status` — the status chip renders the `—` marker for a
 *        NULL status, never a fabricated word); money is formatted by the shared finance family
 *        (`components/finance/format.ts` `usdText`, used by `components/ads/AdCampaignCard.tsx`):
 *        thousands separators + two decimals, `—` for null — mirrored below as `usd()`, so this
 *        spec FAILS if the page collapses null into `$0.00` or drops back to a raw float; the
 *        measured count (`ads-count`) and the Q-12 cap marker (`ads-truncation`, present iff the
 *        read filled the page's 50-row cap) are pinned; the empty sentence names the producer and
 *        is asserted through `ads-empty`.
 *
 * The read is still `GET /api/v1/ads/campaigns` (the backend's ONLY ads route,
 * `backend/app/api/v1/ads.py`) with the explicit `limit=50` the page passes; the wire-watch stays
 * — a rendered state WITHOUT the call is the frozen-build defect wearing the honest state. Filters
 * are covered by the unit suite (`src/__tests__/u4-ads.test.tsx`); live-stack filter clicks are not
 * re-driven here because the lab DB's platform/status values are the fixtures themselves.
 */

/** Mirror of the page's money formatter (`usdText`). A non-finite or absent value is the app-wide
 *  "not measured" marker; a real number gets `en-US` grouping and exactly two decimals. */
function usd(value: unknown): string {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return "$" + value.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** One row of `GET /api/v1/ads/campaigns` (`AdCampaignResponse`) — nullable exactly as served. */
type Campaign = {
  id: number;
  platform: string;
  external_id: string | null;
  name: string;
  goal: string | null;
  status: string | null;
  daily_budget_usd: number | null;
  total_spent_usd: number | null;
  created_at: string | null;
};

/** The page's explicit bounded read size (`components/ads/query.ts` CAMPAIGNS_LIMIT). */
const LIST_LIMIT = 50;

test.describe("Ads page", () => {
  test("heading + campaign state consistent with GET /api/v1/ads/campaigns", async ({ page, request }) => {
    const api = await request.get(`${API_BASE}/api/v1/ads/campaigns?limit=${LIST_LIMIT}`);
    expect(api.status()).toBe(200);
    const campaigns = (await api.json()) as Campaign[];

    // The page must READ — the frozen-snapshot defect rendered an empty state with NO api call at all.
    const apiCalls: string[] = [];
    page.on("request", (r) => {
      if (r.method() === "GET" && r.url().includes("/api/v1/ads/campaigns")) apiCalls.push(r.url());
    });

    await page.goto("/ads");
    await expect(page.getByRole("heading", { name: "Ad Campaigns" })).toBeVisible();

    const cards = page.getByTestId("campaign-row");
    if (campaigns.length === 0) {
      await expect(page.getByTestId("ads-empty")).toBeVisible();
      await expect(page.getByTestId("ads-empty")).toContainText("No campaigns yet.");
      await expect(cards).toHaveCount(0);
      await expect(page.getByTestId("ads-count")).toHaveText("0 campaigns");
      await expect(page.getByTestId("ads-truncation")).toHaveCount(0);
    } else {
      // poll, don't sample: the page may still be fetching/rendering when the branch is chosen
      await expect
        .poll(async () => cards.count(), {
          message: `/ads must render the ${campaigns.length} campaign(s) the API serves at limit=${LIST_LIMIT}`,
        })
        .toBe(campaigns.length);
      await expect(page.getByTestId("ads-count")).toHaveText(
        `${campaigns.length} ${campaigns.length === 1 ? "campaign" : "campaigns"}`,
      );

      // Newest first (id DESC): the first card is the API's first campaign — assert its REAL fields.
      const first = campaigns[0]!;
      const firstCard = cards.first();
      await expect(firstCard).toContainText(first.name);
      await expect(firstCard.locator('[data-testid="campaign-platform"]')).toHaveText(first.platform);
      // A NULL status renders the app-wide marker — never a fabricated word.
      await expect(firstCard.locator('[data-testid="campaign-status"]')).toHaveText(first.status ?? "—");
      await expect(firstCard).toContainText(`${usd(first.total_spent_usd)} spent`);
      await expect(firstCard).toContainText(`${usd(first.daily_budget_usd)}/day budget`);
    }

    // Q-12: the cap marker appears iff the read filled or overshot the page's own limit.
    if (campaigns.length >= LIST_LIMIT) {
      await expect(page.getByTestId("ads-truncation")).toContainText(`Showing the first ${LIST_LIMIT}`);
    } else {
      await expect(page.getByTestId("ads-truncation")).toHaveCount(0);
    }

    expect(
      apiCalls.length,
      "the page must actually CALL GET /api/v1/ads/campaigns — an empty/static render without a read " +
        "is the frozen-snapshot defect",
    ).toBeGreaterThan(0);
    // …and the read is the page's own bounded one, which is what makes the count/cap pins above fair.
    expect(apiCalls.some((url) => url.includes(`limit=${LIST_LIMIT}`))).toBe(true);
  });

  test("renders a campaign's platform/status chips and formatted dollar amounts (seeded fixture)", async ({ page, request }) => {
    // Fixture writes go straight into the docker-free lab's SQLite file; the CI stack is Postgres
    // (E2E_SQLITE unset, no repo-local lab DB) — skip BY NAME there, never a raw open error.
    test.skip(!labDbAvailable(), LAB_DB_SKIP_REASON);
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
      // The read succeeded and returned rows — the empty sentence and the failure banner must not be up.
      await expect(page.getByTestId("ads-empty")).toHaveCount(0);
      // The page's own failure banner must be absent — scoped INSIDE main. A PAGE-WIDE count-0 on
      // role=alert can never pass: Next.js's built-in route-announcer renders an empty root-level
      // role=alert on every route (found by the first live lab run of this spec).
      await expect(page.locator("main").getByRole("alert")).toHaveCount(0);

      const card = page.getByTestId("campaign-row").filter({ hasText: name });
      await expect(
        card,
        `GET /api/v1/ads/campaigns serves "${name}" but /ads never renders its card — ` +
          "the page is not rendering what the API serves (frozen build-time snapshot?)",
      ).toHaveCount(1);
      await expect(card).toContainText(name);
      await expect(card.locator('[data-testid="campaign-platform"]')).toHaveText("meta");
      await expect(card.locator('[data-testid="campaign-status"]')).toHaveText("active");
      await expect(card).toContainText("signups"); // goal renders when the row carries one
      // U4 money law: the shared formatter's output — $12.50, NOT the old raw-float "$12.5"
      // (the Q-13 regression this spec now guards by construction).
      await expect(card).toContainText(`${usd(3.25)} spent`);
      await expect(card).toContainText(`${usd(12.5)}/day budget`);
      await expect(card).not.toContainText("$12.5/day budget");
      // id DESC: the freshly inserted campaign has the highest id and is the first card.
      await expect(page.getByTestId("campaign-row").first()).toContainText(name);

      const api = await request.get(`${API_BASE}/api/v1/ads/campaigns?limit=${LIST_LIMIT}`);
      const campaigns = (await api.json()) as Campaign[];
      expect(campaigns.some((c) => c.name === name)).toBe(true);
    } finally {
      const del = sqliteRun("DELETE FROM ad_campaigns WHERE name LIKE ?", [`${PREFIX}%`]);
      expect(del.changes, "fixture campaign row must be removable").toBeGreaterThanOrEqual(1);
    }
  });
});
