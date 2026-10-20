import { test, expect } from "@playwright/test";

import { API_BASE } from "./_helpers";

/**
 * V27 / L6 — vision on demand (V26/L4, F4) through the real model.
 *
 * The V26 live drill called `POST /api/v1/vision/describe` with curl.  This spec proves the
 * OPERATOR path the drill could not: a real image uploaded to a real task, the attachment row's
 * **Describe** button clicked in the browser, and the model's sentence rendered inline under the
 * label "Model description — not saved to the task".
 *
 * Two honesty rules are pinned:
 *   1. The description is model output rendered for the operator — it is NOT persisted.  The task's
 *      `result_summary`/`status`/`error_message` are read from the API before and after the click
 *      and must be byte-identical; the page is not allowed to turn this into a task result.
 *   2. A stack with no vision model configured refuses `E_CONFIG` by name.  That is a missing lab
 *      prerequisite, not a product failure, so the spec SKIPS BY NAME on exactly that code (never
 *      on an upstream error, which stays a failure).
 *
 * The fixture is ONE attachment on an EXISTING task (no task row is created), deleted in `finally`.
 */

/** A 1×1 red PNG — valid magic bytes, tiny enough for any upload cap. */
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
const PNG = Buffer.from(PNG_BASE64, "base64");
const MAX_PER_TASK = 4;

type TaskRow = {
  id: number;
  title: string;
  status: string | null;
  result_summary: string | null;
  error_message: string | null;
};
type AttachmentRow = { id: number; filename: string };

test.describe("V27/L6 — vision on demand", () => {
  test("Describe renders a labelled model answer and never overwrites the task result", async ({
    page,
    request,
  }) => {
    test.setTimeout(150_000);

    // Pick an existing task that still has room for one image (the cap is 4 per task).
    const tasksResponse = await request.get(`${API_BASE}/api/v1/tasks?limit=10`);
    expect(tasksResponse.status(), await tasksResponse.text()).toBe(200);
    const tasks = (await tasksResponse.json()) as TaskRow[];
    test.skip(
      !Array.isArray(tasks) || tasks.length === 0,
      "the lab database has no task to attach an image to; this spec needs one real task row",
    );

    let task: TaskRow | null = null;
    for (const candidate of tasks) {
      const attachments = await request.get(
        `${API_BASE}/api/v1/tasks/${candidate.id}/attachments`,
      );
      if (attachments.status() !== 200) continue;
      const rows = (await attachments.json()) as AttachmentRow[];
      if (rows.length < MAX_PER_TASK) {
        task = candidate;
        break;
      }
    }
    test.skip(
      task === null,
      `every task in the first page already carries ${MAX_PER_TASK} images; this spec needs one free slot`,
    );
    const chosen = task as TaskRow;

    const before = (await (
      await request.get(`${API_BASE}/api/v1/tasks/${chosen.id}`)
    ).json()) as TaskRow;

    const filename = `v27-vision-${Date.now()}.png`;
    let attachmentId: number | null = null;
    try {
      const upload = await request.post(`${API_BASE}/api/v1/tasks/${chosen.id}/attachments`, {
        multipart: {
          file: { name: filename, mimeType: "image/png", buffer: PNG },
        },
      });
      expect(upload.status(), await upload.text()).toBe(201);
      const row = (await upload.json()) as AttachmentRow;
      attachmentId = row.id;
      expect(row.filename).toBe(filename);

      // Honest prerequisite probe: the UI call and this probe hit the same route.  A missing key is
      // a named E_CONFIG — skip BY NAME; any other non-200 is the product failing and stays RED.
      const probe = await request.post(`${API_BASE}/api/v1/vision/describe`, {
        data: { attachment_id: attachmentId },
      });
      const probeCode = probe.headers()["x-polsia-error-code"] ?? "";
      test.skip(
        probe.status() !== 200 && probeCode === "E_CONFIG",
        `no vision model is configured on this stack (${probeCode}); this spec needs a live model`,
      );
      expect(probe.status(), await probe.text()).toBe(200);
      const probed = (await probe.json()) as { description: string; model: string };
      expect(typeof probed.description).toBe("string");
      expect(probed.description.trim().length).toBeGreaterThan(0);

      await page.goto(`/tasks/${chosen.id}`);
      const panel = page.getByTestId("task-attachments");
      await expect(panel).toBeVisible();
      await expect(panel.getByText(filename, { exact: true })).toBeVisible();

      // The Describe control is per-attachment and names the file it will describe.
      await panel.getByRole("button", { name: `Describe ${filename}`, exact: true }).click();

      const block = page.getByTestId(`vision-description-${attachmentId}`);
      await expect(block).toBeVisible({ timeout: 120_000 });
      await expect(block).toContainText("Model description — not saved to the task");
      await expect(block).toContainText("Model:");

      // A real sentence, not the label alone and not a placeholder.
      const descriptionText = (await block.locator("p").nth(1).innerText()).trim();
      expect(
        descriptionText.length,
        "the block must render the model's own sentence under its label",
      ).toBeGreaterThan(0);
      expect(descriptionText).not.toBe("Model description — not saved to the task");

      // No task-result overwrite: the API row is identical across the click.
      const after = (await (
        await request.get(`${API_BASE}/api/v1/tasks/${chosen.id}`)
      ).json()) as TaskRow;
      expect(after.result_summary, "Describe must never write the task result").toBe(
        before.result_summary,
      );
      expect(after.status).toBe(before.status);
      expect(after.error_message).toBe(before.error_message);
    } finally {
      if (attachmentId !== null) {
        const deleted = await request.delete(`${API_BASE}/api/v1/attachments/${attachmentId}`);
        expect(deleted.status(), "the fixture attachment must be removable").toBe(204);
      }
    }
  });
});
