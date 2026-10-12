import { test, expect } from "@playwright/test";
import { API_BASE, BACKEND_URL, PREFIX, sqliteRun } from "./_helpers";

test.describe("Tasks page", () => {
  test("renders the heading and a row set consistent with GET /api/v1/tasks", async ({ page, request }) => {
    const api = await request.get(`${API_BASE}/api/v1/tasks?limit=100`);
    expect(api.status()).toBe(200);
    const tasks = await api.json();

    // The page must READ — the frozen-snapshot defect rendered an empty state with NO api call at all.
    const apiCalls: string[] = [];
    page.on("request", (r) => {
      if (r.method() === "GET" && r.url().includes("/api/v1/tasks")) apiCalls.push(r.url());
    });

    await page.goto("/tasks");
    await expect(page.getByRole("heading", { name: "Tasks" })).toBeVisible();

    if (tasks.length === 0) {
      await expect(
        page.getByText("No tasks yet. Agents will create tasks during their cycles."),
      ).toBeVisible();
    } else {
      // The empty-state paragraph must NOT be there while the API has rows. A page that serves a
      // build-time snapshot (the prerendered-standalone bug this spec caught) renders the empty state
      // forever and fails exactly here.
      await expect(
        page.getByText("No tasks yet.", { exact: false }),
        `/tasks shows its EMPTY state although GET /api/v1/tasks returned ${tasks.length} task(s) — ` +
          "the page is not rendering what the API serves (frozen build-time snapshot?)",
      ).toHaveCount(0);
      const rows = page.locator("main div.bg-gray-800");
      // poll, don't sample: the checks above can resolve while a client-side page is still loading
      await expect
        .poll(async () => rows.count(), {
          message: `/tasks must render at least ${tasks.length} rows (the API state)`,
        })
        .toBeGreaterThanOrEqual(tasks.length);
      // The newest task from the API (created_at DESC, id DESC) must be rendered.
      await expect(page.getByText(tasks[0].title, { exact: true }).first()).toBeVisible();
    }
    expect(
      apiCalls.length,
      "the page must actually CALL GET /api/v1/tasks — an empty/static render without a read is " +
        "the frozen-snapshot defect",
    ).toBeGreaterThan(0);
  });

  test("POST /agents/social_media/trigger creates the task and /tasks shows its row", async ({ page, request }) => {
    const title = `${PREFIX} tasks-spec trigger ${Date.now()}`;
    let taskId: number | null = null;
    try {
      // Real write flow: the same endpoint the Agents page's "Run Now" button calls.
      const trig = await request.post(`${BACKEND_URL}/api/v1/agents/social_media/trigger`, {
        data: { task_title: title },
        timeout: 90_000, // the broker-down enqueue ladder measured ~14s; give contention headroom
      });
      expect(trig.status()).toBe(202);
      const body = await trig.json();
      expect(body.status).toBe("queued");
      expect(body.task_id).toBeGreaterThan(0);
      taskId = body.task_id;

      // Independent read-back: the row is really in the DB, not just echoed in the response.
      const read = await request.get(`${API_BASE}/api/v1/tasks/${taskId}`);
      expect(read.status()).toBe(200);
      const task = await read.json();
      expect(task.title).toBe(title);
      expect(task.agent_type).toBe("social_media");

      // And the page renders it — one row: title, agent type + priority line, status badge.
      await page.goto("/tasks");
      const titleNode = page.getByText(title, { exact: true }).first();
      await expect(
        titleNode,
        `trigger created task #${taskId} ("${title}") but /tasks never renders it — ` +
          "the page is not rendering what the API serves (frozen build-time snapshot?)",
      ).toBeVisible();
      const row = titleNode.locator("xpath=ancestor::div[contains(@class,'bg-gray-800')]");
      await expect(row).toContainText("social media · Priority 3");
      await expect(row.getByText(/^(pending|in_progress|completed|failed)$/)).toBeVisible();
    } finally {
      if (taskId !== null) {
        // No DELETE endpoint exists; remove the obviously-named fixture row directly.
        const del = sqliteRun("DELETE FROM tasks WHERE id = ? AND title = ?", [taskId, title]);
        expect(del.changes, "fixture task row must be removable").toBe(1);
      }
    }
  });
});
