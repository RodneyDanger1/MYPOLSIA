import { test, expect } from "@playwright/test";

/**
 * V28 / L4 — `/ops` → "Run workspace retention" (V27/L2) through the served stack.
 *
 * The panel is a thin consumer of two routes (`backend/app/api/v1/ops.py:399,428`):
 *
 *   POST /api/v1/ops/run-workspace-cleanup   body optional; `apply` defaults FALSE (dry run)
 *   GET  /api/v1/ops/run-workspace-cleanup   `{data: <newest recorded activity row>|null}`
 *
 * What this spec pins, in the order an operator meets it:
 *   1. the panel's FIRST paint is whatever `GET` says — either the recorded row's own numbers/basis
 *      line, or the one honest "nothing recorded yet" sentence (`{data: null}`); it never invents
 *      a zeroed result;
 *   2. **Dry run deletes nothing** — a second preview afterwards reports the SAME `removed` /
 *      `count` / `kept`, and the page's notice says so;
 *   3. **Apply is a two-click verb** — the first press only arms "Confirm apply" and sends NOTHING
 *      (the request log is pinned), Cancel disarms it;
 *   4. **Apply removes only what the preview named** — every removed name is in the preview's
 *      `removed`, every name is run-id-shaped, and `kept` falls by exactly the number removed;
 *   5. the **last result is the server's**, not client memory: a fresh `GET` returns the row this
 *      test's apply wrote, and after a reload the panel paints that row's id, counts and trigger.
 *
 * FRESH-DB SAFETY.  No fixture and no hardcoded count: on an empty/fresh workspace the same code
 * path answers `count: 0, kept: 0`, and every assertion is computed from the responses.  The test
 * is also deliberately DESTRUCTIVE-BY-POLICY: pressing Apply on a stack that really has expired run
 * workspaces removes them — that is the manual surface's purpose, and the policy (newest 20 kept,
 * older than 72 h) is the backend's own.  Activity rows are append-only audit facts; there is no
 * route to delete them, so the only residue is the rows these two POSTs write.
 */

const ROUTE = "/api/v1/ops/run-workspace-cleanup";

type CleanupResult = {
  runs_dir: string;
  count: number;
  kept: number;
  removed: string[];
  skipped: { name: string; reason: string }[];
  policy: { keep_latest: number; max_age_hours: number };
  applied: boolean;
};

type CleanupRecord = {
  activity_id: number;
  created_at: string | null;
  trigger: string;
  result: CleanupResult;
};

type LastResponse = { data: CleanupRecord | null };

/** The registry's own run-id charset (`tool_registry._RUN_ID_RE`). */
const RUN_ID = /^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$/;

test.describe("V28/L4 — run-workspace retention panel", () => {
  test("dry run previews without deleting, apply takes two clicks, and the last result is served", async ({
    page,
    request,
  }) => {
    // What the server had recorded before this test — the panel's first paint must match it.
    const initialResponse = await request.get(ROUTE);
    expect(initialResponse.status(), await initialResponse.text()).toBe(200);
    const initial = (await initialResponse.json()) as LastResponse;

    // Count the panel's POSTs so "the first Apply click sends nothing" is a measured fact.
    const posts: string[] = [];
    page.on("request", (candidate) => {
      if (candidate.method() === "POST" && candidate.url().endsWith(ROUTE)) posts.push(candidate.url());
    });

    await page.goto("/ops");
    await expect(page.getByRole("heading", { name: "Ops" })).toBeVisible();
    const card = page.getByTestId("ops-retention");
    await expect(card).toBeVisible();
    await expect(card.getByText("Run workspace retention")).toBeVisible();

    if (initial.data === null) {
      await expect(page.getByTestId("ops-retention-empty")).toContainText(
        "No cleanup has been recorded yet",
      );
      await expect(page.getByTestId("ops-retention-result")).toHaveCount(0);
    } else {
      await expect(page.getByTestId("ops-retention-result")).toBeVisible();
      await expect(page.getByTestId("ops-retention-count")).toHaveText(
        String(initial.data.result.count),
      );
      await expect(page.getByTestId("ops-retention-kept")).toHaveText(
        String(initial.data.result.kept),
      );
      await expect(page.getByTestId("ops-retention-basis")).toHaveText(
        `last recorded row #${initial.data.activity_id}`,
      );
      await expect(page.getByTestId("ops-retention-applied")).toHaveText(
        initial.data.result.applied ? "APPLIED" : "DRY RUN",
      );
    }

    // ── Dry run ────────────────────────────────────────────────────────────────────────────────
    const [dryResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === "POST" && response.url().endsWith(ROUTE),
      ),
      page.getByTestId("ops-retention-dry-run").click(),
    ]);
    expect(dryResponse.status(), await dryResponse.text()).toBe(200);
    const preview = (await dryResponse.json()) as CleanupResult;
    expect(preview.applied, "the Dry run button must send apply:false").toBe(false);
    expect(preview.count).toBe(preview.removed.length);
    expect(preview.policy.keep_latest).toBe(20);
    expect(preview.policy.max_age_hours).toBe(72);

    await expect(page.getByTestId("ops-retention-notice")).toHaveText(
      `Dry run: ${preview.count} run workspace(s) would be removed; ${preview.kept} kept. Nothing was deleted.`,
    );
    await expect(page.getByTestId("ops-retention-count")).toHaveText(String(preview.count));
    await expect(page.getByTestId("ops-retention-kept")).toHaveText(String(preview.kept));
    await expect(page.getByTestId("ops-retention-applied")).toHaveText("DRY RUN");
    await expect(page.getByTestId("ops-retention-basis")).toHaveText("result of this run");
    await expect(page.getByTestId("ops-retention-policy")).toContainText("keep newest 20");
    await expect(page.getByTestId("ops-retention-policy")).toContainText("max age 72 h");

    // "Dry run keeps all": a SECOND preview sees exactly the same expired set and kept count.
    const repeatResponse = await request.post(ROUTE, { data: { apply: false } });
    expect(repeatResponse.status()).toBe(200);
    const repeat = (await repeatResponse.json()) as CleanupResult;
    expect(repeat.applied).toBe(false);
    expect(repeat.removed, "a dry run must not have deleted anything").toEqual(preview.removed);
    expect(repeat.count).toBe(preview.count);
    expect(repeat.kept).toBe(preview.kept);

    // ── Apply: first click ARMS, it does not send ──────────────────────────────────────────────
    expect(posts.length, "exactly one POST so far — the dry run").toBe(1);
    await page.getByTestId("ops-retention-apply").click();
    await expect(page.getByTestId("ops-retention-confirm-hint")).toBeVisible();
    await expect(page.getByTestId("ops-retention-apply")).toHaveText("Confirm apply");
    // A settle, not an assertion style: give a one-click implementation time to fire, then prove
    // no request left the page.
    await page.waitForTimeout(400);
    expect(
      posts.length,
      "the first Apply press must only arm the confirmation — a one-click delete is forbidden",
    ).toBe(1);

    await page.getByTestId("ops-retention-cancel").click();
    await expect(page.getByTestId("ops-retention-confirm-hint")).toHaveCount(0);
    await page.waitForTimeout(200);
    expect(posts.length, "Cancel must not send anything either").toBe(1);

    // ── Apply: arm, then confirm ───────────────────────────────────────────────────────────────
    await page.getByTestId("ops-retention-apply").click();
    await expect(page.getByTestId("ops-retention-confirm-hint")).toBeVisible();
    const [applyResponse] = await Promise.all([
      page.waitForResponse(
        (response) =>
          response.request().method() === "POST" && response.url().endsWith(ROUTE),
      ),
      page.getByTestId("ops-retention-apply").click(),
    ]);
    expect(applyResponse.status(), await applyResponse.text()).toBe(200);
    const applied = (await applyResponse.json()) as CleanupResult;
    expect(applied.applied, "the confirmed Apply must send apply:true").toBe(true);
    expect(applied.count, "count is the number actually removed").toBe(applied.removed.length);

    // Apply deletes ONLY what the preview named — the same policy, the same candidate set.
    for (const name of applied.removed) {
      expect(preview.removed, `apply removed ${name}, which the preview never named`).toContain(
        name,
      );
      expect(name, "a removed entry must be a run directory name, never a foreign file").toMatch(
        RUN_ID,
      );
    }
    expect(applied.kept, "kept must fall by exactly the number removed").toBe(
      preview.kept - applied.removed.length,
    );

    await expect(page.getByTestId("ops-retention-notice")).toHaveText(
      `Applied: removed ${applied.count} expired run workspace(s); ${applied.kept} kept.`,
    );
    await expect(page.getByTestId("ops-retention-count")).toHaveText(String(applied.count));
    await expect(page.getByTestId("ops-retention-kept")).toHaveText(String(applied.kept));
    await expect(page.getByTestId("ops-retention-applied")).toHaveText("APPLIED");

    // ── Last result is the SERVER's newest recorded row ────────────────────────────────────────
    const lastResponse = await request.get(ROUTE);
    expect(lastResponse.status()).toBe(200);
    const last = (await lastResponse.json()) as LastResponse;
    expect(last.data, "the apply must have written one recorded row").not.toBeNull();
    const stored = last.data!.result;
    expect(last.data!.trigger).toBe("owner");
    expect(stored.applied).toBe(true);
    expect(stored.count).toBe(applied.count);
    expect(stored.kept).toBe(applied.kept);
    expect(stored.runs_dir).toBe(applied.runs_dir);
    expect(stored.policy).toEqual(applied.policy);
    // The activity row caps the name list; the stored prefix must still be the apply's own prefix.
    expect(applied.removed.slice(0, stored.removed.length)).toEqual(stored.removed);
    if (initial.data !== null) {
      expect(last.data!.activity_id).toBeGreaterThan(initial.data.activity_id);
    }

    // A reload reads the same row from the server — the panel never paints client memory.
    await page.reload();
    await expect(page.getByTestId("ops-retention")).toBeVisible();
    await expect(page.getByTestId("ops-retention-result")).toBeVisible();
    await expect(page.getByTestId("ops-retention-basis")).toHaveText(
      `last recorded row #${last.data!.activity_id}`,
    );
    await expect(page.getByTestId("ops-retention-count")).toHaveText(String(applied.count));
    await expect(page.getByTestId("ops-retention-kept")).toHaveText(String(applied.kept));
    await expect(page.getByTestId("ops-retention-applied")).toHaveText("APPLIED");
    await expect(page.getByTestId("ops-retention-trigger")).toContainText("the owner");
  });

  test("a non-boolean apply is refused by name and records nothing", async ({ request }) => {
    const beforeResponse = await request.get(ROUTE);
    expect(beforeResponse.status()).toBe(200);
    const before = (await beforeResponse.json()) as LastResponse;

    const bad = await request.post(ROUTE, { data: { apply: "yes" } });
    expect(bad.status(), await bad.text()).toBe(422);
    expect(bad.headers()["x-polsia-error-code"]).toBe("E_VALIDATION");
    const badBody = (await bad.json()) as {
      error: { code: string; context?: { apply?: string } };
    };
    expect(badBody.error.code).toBe("E_VALIDATION");
    expect(badBody.error.context?.apply).toBe("'yes'");

    const unknown = await request.post(ROUTE, { data: { apply: false, force: true } });
    expect(unknown.status(), await unknown.text()).toBe(422);
    expect(((await unknown.json()) as { error: { code: string } }).error.code).toBe(
      "E_VALIDATION",
    );

    const afterResponse = await request.get(ROUTE);
    const after = (await afterResponse.json()) as LastResponse;
    expect(
      after.data?.activity_id ?? null,
      "a refused body must be validated before anything is recorded",
    ).toBe(before.data?.activity_id ?? null);
  });
});
