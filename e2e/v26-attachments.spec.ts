import { test, expect } from "@playwright/test";

import { API_BASE } from "./_helpers";

/**
 * V26/L6 — the attachments round trip (F6), end to end and through the REAL handlers.
 *
 * The contract the V25/L1 routes ship (`backend/app/api/v1/tasks.py:295-425`):
 *   POST   /tasks/{id}/attachments  multipart `file` -> 201 metadata
 *   GET    /tasks/{id}/attachments  -> [metadata]
 *   GET    /attachments/{id}        -> the bytes, with the SNIFFED content type
 *   DELETE /attachments/{id}        -> 204 (row + blob)
 *
 * This spec uploads a real 1×1 PNG while DECLARING `application/octet-stream`: a server that
 * echoed the client's type would answer octet-stream, so the assertion on `image/png` proves the
 * sniff.  The PNG magic bytes are checked on the served body (not on a header alone), the list is
 * re-read from the API, and the task detail page must render the filename.  The upload is removed
 * in `finally` — a real DELETE, so a run leaves no fixture behind.
 */

/** A 1×1 transparent PNG (67 bytes). */
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==";
const PNG = Buffer.from(PNG_BASE64, "base64");
const PNG_MAGIC = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a];

test.describe("V26/L6 — task image attachments", () => {
  test("upload -> blob serves image/png + PNG magic; the API list and the task page show it", async ({
    page,
    request,
  }) => {
    const tasksResponse = await request.get(`${API_BASE}/api/v1/tasks?limit=1`);
    expect(tasksResponse.status()).toBe(200);
    const tasks = (await tasksResponse.json()) as Array<{ id: number; title: string }>;
    test.skip(
      !Array.isArray(tasks) || tasks.length === 0,
      "the lab database has no task to attach an image to; this spec needs one real task row",
    );
    const task = tasks[0]!;

    const filename = `v26-attachment-${Date.now()}.png`;
    let attachmentId: number | null = null;
    try {
      // Declared as octet-stream ON PURPOSE: the served type must come from the bytes.
      const upload = await request.post(`${API_BASE}/api/v1/tasks/${task.id}/attachments`, {
        multipart: {
          file: { name: filename, mimeType: "application/octet-stream", buffer: PNG },
        },
      });
      expect(upload.status(), await upload.text()).toBe(201);
      const row = (await upload.json()) as {
        id: number;
        task_id: number;
        filename: string;
        mime: string;
        size_bytes: number;
        sha256: string;
      };
      attachmentId = row.id;
      expect(row.task_id).toBe(task.id);
      expect(row.filename).toBe(filename);
      expect(row.mime, "the sniffed type must win over the client's declared type").toBe("image/png");
      expect(row.size_bytes).toBe(PNG.length);

      // The blob: real bytes, sniffed type, no-sniff header.
      const blob = await request.get(`${API_BASE}/api/v1/attachments/${attachmentId}`);
      expect(blob.status()).toBe(200);
      expect(blob.headers()["content-type"]).toContain("image/png");
      // The proxy in front of the lab appends headers, so the value may repeat ("nosniff,
      // nosniff") — the product claim is that the directive is PRESENT on the blob.
      expect(blob.headers()["x-content-type-options"] ?? "").toContain("nosniff");
      const body = await blob.body();
      expect(body.length).toBe(PNG.length);
      expect([...body.subarray(0, PNG_MAGIC.length)], "the served body must BE a PNG").toEqual(
        PNG_MAGIC,
      );

      // The list route serves the row it just accepted.
      const listResponse = await request.get(`${API_BASE}/api/v1/tasks/${task.id}/attachments`);
      expect(listResponse.status()).toBe(200);
      const rows = (await listResponse.json()) as Array<{ id: number; filename: string }>;
      expect(
        rows.some((item) => item.id === attachmentId && item.filename === filename),
        `GET /tasks/${task.id}/attachments does not list attachment #${attachmentId}`,
      ).toBe(true);

      // And the product surface shows it: /tasks/{id} renders the attachment's filename.
      await page.goto(`/tasks/${task.id}`);
      const section = page.getByTestId("task-attachments");
      await expect(section).toBeVisible();
      await expect(
        section.getByText(filename, { exact: true }),
        `the task page never renders ${filename} although the API serves it`,
      ).toBeVisible();
    } finally {
      if (attachmentId !== null) {
        const deleted = await request.delete(`${API_BASE}/api/v1/attachments/${attachmentId}`);
        expect(deleted.status(), "the fixture attachment must be removable").toBe(204);
      }
    }
  });
});
