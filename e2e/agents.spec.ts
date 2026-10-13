import { test, expect } from "@playwright/test";
import { PREFIX, sqliteAll, sqliteRun } from "./_helpers";

test.describe("Agents page", () => {
  test("lists all agents with Run Now button", async ({ page }) => {
    await page.goto("/agents");
    await expect(page.getByRole("heading", { name: "Agents" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Run Now" }).first()).toBeVisible();
  });

  test("trigger button shows queued confirmation", async ({ page }) => {
    // This click is the suite's only task-row write that goes through the BROWSER: it POSTs
    // /api/v1/agents/ads_management/trigger (the agent list renders ordered by agent_type, so the
    // first "Run Now" is ads_management) and the backend titles the row "Manual ads_management
    // run" — a title a human's own click produces too, so it can never be deleted by pattern.
    // Ride the documented `task_title` field (the same field tasks.spec.ts posts) into that request
    // through a route interception, so the row this spec creates carries the suite's fixture tag.
    // Nothing here or elsewhere asserts on that row's title (the locator below matches the button /
    // message text only), so the tag cannot change an assertion's outcome — what it changes is that
    // the delete-by-tag (here, and in the suite-end sweep) cannot miss a row, even when a later
    // assertion fails after the click.
    const stamp = Date.now();
    const fixtureTitle = `${PREFIX} manual ads_management run ${stamp}`;
    let triggered = false;
    await page.route("**/api/v1/agents/*/trigger", async (route) => {
      const post = (route.request().postDataJSON() ?? {}) as Record<string, unknown>;
      await route.continue({ postData: JSON.stringify({ ...post, task_title: fixtureTitle }) });
    });

    try {
      await page.goto("/agents");
      const firstButton = page.getByRole("button", { name: "Run Now" }).first();
      await firstButton.click();
      triggered = true;
      // Expect either "Triggering…" or a success message
      await expect(page.locator("text=/queued|Triggering/")).toBeVisible({ timeout: 5000 });
    } finally {
      // The same `finally` idiom the other fixture specs use, plus the one thing this spec forces:
      // the click's POST is still in flight when the assertion above settles (the button paints
      // "Triggering…" immediately), and its row is committed only when the handler returns — the
      // campaign measured 12.8–14.4 s on that path. A DELETE issued during the handler's open write
      // transaction would raise SQLITE_BUSY, so first poll for the row (expect.poll — "poll, don't
      // sample", the shape the other specs use), then delete by tag. When the trigger never creates
      // the row (a broker-less stack answers 503 and the row is rolled back — get_db commits only
      // on success), the poll's message says exactly that; the row is what this spec, and the
      // suite-end sweep, remove.
      if (triggered) {
        await expect
          .poll(
            () =>
              sqliteAll<{ n: number }>("SELECT count(*) AS n FROM tasks WHERE title LIKE ?", [
                `${PREFIX}%`,
              ])[0].n,
            {
              message:
                "the Run Now click must create its [e2e-fixture] task row (the trigger commits when " +
                "its handler returns; a broker-less stack answers 503 and rolls the row back)",
              timeout: 20_000,
            },
          )
          .toBeGreaterThan(0);
      }
      // Tag-scoped, like every other fixture cleanup: only rows carrying the suite's marker are
      // deletable — the pre-existing untagged "Manual ads_management run" rows are not ours to touch.
      const del = sqliteRun("DELETE FROM tasks WHERE title LIKE ?", [`${PREFIX}%`]);
      if (triggered) {
        expect(del.changes, "the spec's [e2e-fixture] task row must be removable").toBeGreaterThanOrEqual(1);
      }
    }
  });
});
