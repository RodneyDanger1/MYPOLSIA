import { test, expect } from "@playwright/test";
import { randomUUID } from "node:crypto";
import { BACKEND_URL, LAB_DB_SKIP_REASON, PREFIX, backendRequest, labDbAvailable } from "./_helpers";

// Unique per RUN: the fixture titles/content carry these tokens so reruns cannot mistake an older
// run's rows for their own.  (Memory fixtures are deliberately NOT auto-deleted here: the row also
// lives as a ChromaDB document, and deleting only one half would leave ghost results — the suite-end
// sweep does it for both stores: playwright.config.ts `globalTeardown` → _helpers.ts → the tag-scoped,
// idempotent e2e/_sweep-fixtures.py.)
const runId = randomUUID().slice(0, 8);
const tokenA = `e2efixture${runId}alpha`;
const tokenB = `e2efixture${runId}beta`;
const titleA = `${PREFIX} memory alpha ${tokenA}`;
const titleB = `${PREFIX} memory beta ${tokenB}`;

test.describe("Memory page", () => {
  // V10 parent guard: this spec seeds fixtures through the LAB backend (:8000, the sqlite+chroma
  // docker-free stack) — without the lab it errors on ECONNREFUSED instead of skipping. Same
  // named-skip contract the sibling fixture specs use (skips BY NAME, never a raw connection error).
  test.skip(!labDbAvailable(), LAB_DB_SKIP_REASON);

  test.beforeAll(async () => {
    if (!labDbAvailable()) return; // the tests above are skipped by name in this shape
    // Real write flow: POST /api/v1/memory stores + indexes. `request` is test-scoped (illegal in
    // beforeAll), so use an equivalent context with the same X-API-Key.
    const ctx = await backendRequest();
    try {
      for (const [label, token, title] of [
        ["alpha", tokenA, titleA],
        ["beta", tokenB, titleB],
      ] as const) {
        const res = await ctx.post(`${BACKEND_URL}/api/v1/memory`, {
          data: {
            category: "e2e-fixture-cat",
            title,
            content: `fixture memory content ${label} - search token ${token}`,
            source: "e2e-spec",
          },
          timeout: 120_000, // first-ever Chroma write loads the embedding model (measured >30s cold)
        });
        expect(res.status(), `seeding memory fixture ${label}`).toBe(201);
      }
    } finally {
      await ctx.dispose();
    }
  });

  test("search form is the honest empty state: no results, and no fetch until the user searches", async ({ page }) => {
    const memoryGets: string[] = [];
    page.on("request", (r) => {
      if (r.method() === "GET" && r.url().includes("/api/v1/memory")) memoryGets.push(r.url());
    });

    await page.goto("/memory");
    await expect(page.getByRole("heading", { name: "Memory" })).toBeVisible();
    await expect(page.getByPlaceholder("Search company memory…")).toBeVisible();
    await expect(page.getByRole("button", { name: "Search" })).toBeVisible();
    await expect(page.locator("main li.card-surface")).toHaveCount(0);

    // Give a hypothetical auto-fetch a beat to fire, then prove none happened — the page must fetch
    // only on an explicit search.
    await page.waitForTimeout(400);
    expect(memoryGets, "the page must not fetch memory rows before a search").toHaveLength(0);
  });

  test("searching by Enter renders every hit the API returned, with content and category chip", async ({ page }) => {
    await page.goto("/memory");
    await page.getByPlaceholder("Search company memory…").fill(tokenA);
    const [resp] = await Promise.all([
      page.waitForResponse(
        (r) => r.request().method() === "GET" && r.url().includes(`/api/v1/memory?q=${tokenA}`),
      ),
      page.keyboard.press("Enter"),
    ]);
    expect(resp.status()).toBe(200);
    const hits = await resp.json();
    expect(hits.length).toBeGreaterThanOrEqual(1);
    const mine = hits.find((h: { title: string | null }) => h.title === titleA);
    expect(mine, "the seeded fixture must be in the search response").toBeTruthy();

    const cards = page.locator("main li.card-surface");
    // Every hit the API returned must be rendered — no more, no less.
    await expect(cards).toHaveCount(hits.length);
    const card = cards.filter({ hasText: titleA });
    await expect(card).toHaveCount(1);
    await expect(card.getByText("e2e-fixture-cat", { exact: true })).toBeVisible(); // category chip
    await expect(card).toContainText(tokenA); // content body carries the token
  });

  test("a second search renders THAT response's order, and an empty query neither fetches nor clears", async ({ page }) => {
    await page.goto("/memory");
    const input = page.getByPlaceholder("Search company memory…");
    const cards = page.locator("main li.card-surface");

    // first search (A)
    await input.fill(tokenA);
    await Promise.all([
      page.waitForResponse(
        (r) => r.request().method() === "GET" && r.url().includes(`/api/v1/memory?q=${tokenA}`),
      ),
      page.keyboard.press("Enter"),
    ]);
    await expect(cards.filter({ hasText: titleA })).toHaveCount(1);

    // second search (B) — the DOM must follow B's own response, first card == B's first hit
    await input.fill(tokenB);
    const [respB] = await Promise.all([
      page.waitForResponse(
        (r) => r.request().method() === "GET" && r.url().includes(`/api/v1/memory?q=${tokenB}`),
      ),
      page.getByRole("button", { name: "Search" }).click(),
    ]);
    expect(respB.status()).toBe(200);
    const hitsB = await respB.json();
    expect(hitsB[0]?.title, "the API's first hit must carry a title").toBeTruthy();
    await expect(cards).toHaveCount(hitsB.length);
    await expect(cards.first()).toContainText(hitsB[0].title);
    await expect(cards.filter({ hasText: titleB })).toHaveCount(1);

    // empty query: the guard clause must neither fire a request nor drop the rendered results
    let extraSearches = 0;
    const onRequest = (r: import("@playwright/test").Request) => {
      if (r.method() === "GET" && r.url().includes("/api/v1/memory?q=")) extraSearches += 1;
    };
    page.on("request", onRequest);
    const before = await cards.count();
    await input.fill("");
    await page.getByRole("button", { name: "Search" }).click();
    await page.waitForTimeout(400);
    page.off("request", onRequest);
    expect(extraSearches, "an empty query must not fire a search request").toBe(0);
    await expect(cards).toHaveCount(before);
  });
});
