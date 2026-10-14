import { test, expect } from "@playwright/test";
import { API_BASE } from "./_helpers";

/**
 * V8/C3 — the chat rail on the dashboard (new region under the feed column): the rail performs
 * its OWN GET /api/v1/chat read, renders the persisted transcript, and a real send round-trips
 * through POST /api/v1/chat into the transcript.
 *
 * SOFT-MARKED until the V10 e2e lab round: F-04 — the e2e lab run predates this lane's commit
 * (the rail did not exist when the lab last ran), so this spec is WRITTEN now and FIRST EXECUTED
 * at V10. The V10 seat removes the `test.fixme` mark below and runs it against the live stack.
 *
 * The send probe is a status question — the deterministic intent router answers those from
 * EXISTING reads (no free-roaming tools, no money movement). This spec never clicks CONFIRM:
 * the confirm/409 matrix is pinned in unit tests (u8-chat.test.tsx), and a lab run must not
 * create tasks as a side effect (the probe message itself persists, by design).
 */

type HistoryBody = {
  items: { role: string; content: string; created_at: string | null; task_id: number | null }[];
  total: number;
};

test.describe("Chat rail", () => {
  test.fixme(
    "reads GET /chat, renders the transcript, and a real send lands with its reply (V10 lab)",
    async ({ page, request }) => {
      const api = await request.get(`${API_BASE}/api/v1/chat?limit=50`);
      expect(api.status()).toBe(200);
      const body = (await api.json()) as HistoryBody;

      // The rail must do its OWN read: a render-without-fetch is not a chat — it is a stale
      // snapshot wearing a transcript.
      const apiCalls: string[] = [];
      page.on("request", (r) => {
        const url = r.url();
        if (url.includes("/api/v1/chat")) apiCalls.push(`${r.method()} ${url}`);
      });

      const response = await page.goto("/dashboard");
      expect(response?.status()).toBe(200);
      await expect(page.getByRole("heading", { name: "Dashboard" })).toBeVisible();

      const rail = page.getByTestId("chat-rail");
      await expect(rail).toBeVisible();

      await expect
        .poll(() => apiCalls.filter((c) => c.startsWith("GET")).length)
        .toBeGreaterThan(0);

      // The persisted transcript is on screen; an empty store states its emptiness honestly.
      for (const item of body.items) {
        expect(await rail.getByText(item.content, { exact: true }).count()).toBeGreaterThan(0);
      }
      if (body.items.length === 0) {
        await expect(rail.getByText(/No messages yet/)).toBeVisible();
      }

      // Composer: empty input cannot send; typing enables the one primary; the send round-trips.
      const composer = rail.getByLabel("Message");
      const send = rail.getByRole("button", { name: "SEND" });
      await expect(send).toBeDisabled();

      const probe = "Lab status check: how many tasks ran today?";
      await composer.fill(probe);
      await expect(send).toBeEnabled();

      const postsBefore = apiCalls.filter(
        (c) => c.startsWith("POST") && c.includes("/api/v1/chat"),
      ).length;
      await send.click();
      await expect
        .poll(
          () =>
            apiCalls.filter((c) => c.startsWith("POST") && c.includes("/api/v1/chat")).length,
        )
        .toBeGreaterThan(postsBefore);

      // Our message AND a Polsia reply are in the transcript, and no failure alert rendered.
      expect(await rail.getByText(probe, { exact: true }).count()).toBeGreaterThan(0);
      expect(await rail.locator('[data-role="polsia"]').count()).toBeGreaterThan(0);
      await expect(rail.getByRole("alert")).toHaveCount(0);
    },
  );
});
