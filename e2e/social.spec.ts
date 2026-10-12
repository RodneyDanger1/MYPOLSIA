import { test, expect } from "@playwright/test";
import { API_BASE, PREFIX, sqliteRun } from "./_helpers";

test.describe("Social page", () => {
  test("heading + feed state consistent with GET /api/v1/social/posts", async ({ page, request }) => {
    const api = await request.get(`${API_BASE}/api/v1/social/posts?limit=50`);
    expect(api.status()).toBe(200);
    const posts = await api.json();

    // The page must READ — the frozen-snapshot defect rendered an empty state with NO api call at all.
    const apiCalls: string[] = [];
    page.on("request", (r) => {
      if (r.method() === "GET" && r.url().includes("/api/v1/social/posts")) apiCalls.push(r.url());
    });

    await page.goto("/social");
    await expect(page.getByRole("heading", { name: "Social Media" })).toBeVisible();

    if (posts.length === 0) {
      // The honest empty state, and nothing else: no cards may render for an empty feed.
      await expect(page.getByText("No posts yet.")).toBeVisible();
      await expect(page.locator("main div.bg-gray-800")).toHaveCount(0);
    } else {
      const cards = page.locator("main div.bg-gray-800");
      // poll, don't sample: the page may still be fetching/rendering when the branch is chosen
      await expect
        .poll(async () => cards.count(), {
          message: `/social must render at least ${posts.length} card(s) (the API state)`,
        })
        .toBeGreaterThanOrEqual(posts.length);
      await expect(page.getByText(posts[0].content, { exact: true }).first()).toBeVisible();
    }
    expect(
      apiCalls.length,
      "the page must actually CALL GET /api/v1/social/posts — an empty/static render without a read " +
        "is the frozen-snapshot defect",
    ).toBeGreaterThan(0);
  });

  test("renders a post's status, publish date and engagement counters (seeded fixtures)", async ({ page, request }) => {
    // No write API exists for social posts and the broker is down, so the two fixture posts are
    // inserted directly into the same SQLite file the backend serves from — then asserted through the
    // page and cross-checked through GET /api/v1/social/posts.
    const stamp = Date.now();
    const publishedContent = `${PREFIX} fixture-post-alpha ${stamp}`;
    const draftContent = `${PREFIX} fixture-post-beta ${stamp}`;
    try {
      sqliteRun(
        `INSERT INTO social_posts (platform, content, status, published_at, engagement, created_at)
         VALUES ('twitter', ?, 'draft', NULL, NULL, strftime('%Y-%m-%d %H:%M:%S','now','-10 seconds'))`,
        [draftContent],
      );
      sqliteRun(
        `INSERT INTO social_posts (platform, content, status, published_at, engagement, created_at)
         VALUES ('twitter', ?, 'published', '2026-10-05 12:30:00', ?, strftime('%Y-%m-%d %H:%M:%S','now'))`,
        [publishedContent, JSON.stringify({ likes: 11, reposts: 2 })],
      );

      await page.goto("/social");
      await expect(page.getByRole("heading", { name: "Social Media" })).toBeVisible();
      await expect(
        page.getByText("No posts yet."),
        "/social shows its EMPTY state although the seeded fixture post is served by the API — " +
          "the page is not rendering what the API serves (frozen build-time snapshot?)",
      ).toHaveCount(0);

      // Cross-check through the API: the fixture is served, and the date string the page renders is
      // computed from the API's own serialization (locale-coupled on purpose — same host, same Node).
      const api = await request.get(`${API_BASE}/api/v1/social/posts?limit=50`);
      const posts = await api.json();
      const mine = posts.find((p: { content: string }) => p.content === publishedContent);
      expect(mine, "seeded post must be served by the API").toBeTruthy();
      const expectedDate = new Date(mine.published_at).toLocaleDateString();

      const publishedCard = page.locator("main div.bg-gray-800").filter({ hasText: publishedContent });
      await expect(publishedCard).toHaveCount(1);
      await expect(publishedCard).toContainText("published");
      await expect(publishedCard).toContainText(expectedDate);
      await expect(publishedCard).toContainText("likes: 11");
      await expect(publishedCard).toContainText("reposts: 2");

      const draftCard = page.locator("main div.bg-gray-800").filter({ hasText: draftContent });
      await expect(draftCard).toHaveCount(1);
      await expect(draftCard).toContainText("draft");
      await expect(draftCard).not.toContainText("likes");

      // Newest first (created_at DESC, id DESC): the fresh published post is the first card.
      await expect(page.locator("main div.bg-gray-800").first()).toContainText(publishedContent);
    } finally {
      const del = sqliteRun("DELETE FROM social_posts WHERE content LIKE ?", [`${PREFIX}%`]);
      expect(del.changes, "both fixture posts must be removable").toBeGreaterThanOrEqual(2);
    }
  });
});
