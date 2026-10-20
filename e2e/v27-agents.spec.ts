import { test, expect } from "@playwright/test";

/**
 * V27 / L6 — the V26/L1 agents-page truth, end to end.
 *
 * The V26 unit pins cover the hook's states; this spec is the browser proof the wave never had:
 *
 *   1. `/agents` renders REAL roster cards — `article[data-agent]` > 0 — fed by
 *      `GET /api/v1/agents/status` (the route AgentsRoster consumes).
 *   2. The page never stays on "Loading agent status…" once the mount read has settled: V26/F1's
 *      bug was a hidden-tab mount that skipped the request entirely and left `loading` true, so
 *      the skeletons and that sentence were permanent.
 *   3. A mount in a HIDDEN tab still renders after the tab becomes visible.  The hidden tab is a
 *      LABELLED browser emulation — a test double for visibility state, not product code: before
 *      any app script runs, `document.visibilityState`/`document.hidden` are overridden and the
 *      spec flips them back with a real `visibilitychange` event.  The request assertion below is
 *      what makes the mount meaningful: the status read must leave the wire while hidden (the V26
 *      fix exempts the mount read from the hidden-tab guard), and only then is the tab shown.
 */

test.describe("V27/L6 — /agents truth", () => {
  test("renders real cards from a hidden mount and never keeps the loading state", async ({
    page,
  }) => {
    const statusReads: string[] = [];
    page.on("request", (request) => {
      if (request.method() === "GET" && request.url().includes("/api/v1/agents/status")) {
        statusReads.push(request.url());
      }
    });

    // TEST DOUBLE (labelled): emulate a tab that was opened in the background.  Nothing here
    // touches product code; the hook reads the same two DOM properties a real hidden tab exposes.
    await page.addInitScript(() => {
      let hidden = true;
      Object.defineProperty(document, "visibilityState", {
        configurable: true,
        get: () => (hidden ? "hidden" : "visible"),
      });
      Object.defineProperty(document, "hidden", {
        configurable: true,
        get: () => hidden,
      });
      (window as unknown as { __v27ShowTab?: () => void }).__v27ShowTab = () => {
        hidden = false;
        document.dispatchEvent(new Event("visibilitychange"));
      };
    });

    await page.goto("/agents");
    await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
    await expect(page.locator("body")).toContainText("Agents");

    // The mount read is exempt from the hidden-tab guard (V26/F1): it must be on the wire while
    // the emulated tab is still hidden.  A build that skipped it cannot satisfy this.
    await expect
      .poll(() => statusReads.length, {
        message:
          "the /agents mount read never called GET /api/v1/agents/status while the tab was hidden — " +
          "this is the V26/F1 skeleton-forever path",
        timeout: 20_000,
      })
      .toBeGreaterThan(0);

    // Now the tab becomes visible, through a real visibilitychange event.
    await page.evaluate(() =>
      (window as unknown as { __v27ShowTab?: () => void }).__v27ShowTab?.(),
    );

    const cards = page.locator("article[data-agent]");
    await expect
      .poll(() => cards.count(), {
        message: "/agents must render at least one real article[data-agent] card",
        timeout: 20_000,
      })
      .toBeGreaterThan(0);

    // Every card names the agent it renders — the roster is data, not a decorative skeleton.
    const agentTypes = await cards.evaluateAll((nodes) =>
      nodes.map((node) => node.getAttribute("data-agent") ?? ""),
    );
    expect(agentTypes.every((name) => name.length > 0)).toBe(true);
    expect(new Set(agentTypes).size).toBe(agentTypes.length);

    // The loading sentence and its skeleton block are GONE — the state reached a terminal answer.
    await expect(page.getByText("Loading agent status…", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Loading agent status...", { exact: true })).toHaveCount(0);
  });
});
