import { test, expect } from "@playwright/test";
import { API_BASE } from "./_helpers";

/**
 * V8/C3 — the chat rail on the dashboard (new region under the feed column): the rail performs
 * its OWN GET /api/v1/chat read, renders the persisted transcript, and a real send round-trips
 * through POST /api/v1/chat into the transcript.
 *
 * V10/H2: the fixme mark is REMOVED and this spec runs against the live stack. Locators were
 * re-verified against today's rail markup (Composer aria-label "Message", the SEND button's
 * SEND/SENDING… label, `[data-role]` transcript rows) — no drift since V8.
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
  test(
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

      // The persisted transcript is on screen. NOTE (V10 live-learned): the wire poll above only
      // proves the request FIRED — on a cold stack the response lands a beat later, so each
      // row-presence check is a POLL, never an instant count() (which raced the render and red the
      // first V10 lab run).
      for (const item of body.items) {
        await expect
          .poll(async () => rail.getByText(item.content, { exact: true }).count(), {
            timeout: 10_000,
          })
          .toBeGreaterThan(0);
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

      // Our message AND a Polsia reply are in the transcript — the optimistic rows land when the
      // 201 does, so both presence checks POLL (same race as above), and no failure alert rendered.
      await expect
        .poll(async () => rail.getByText(probe, { exact: true }).count(), { timeout: 10_000 })
        .toBeGreaterThan(0);
      await expect
        .poll(async () => rail.locator('[data-role="polsia"]').count(), { timeout: 10_000 })
        .toBeGreaterThan(0);
      await expect(rail.getByRole("alert")).toHaveCount(0);
    },
  );
});
