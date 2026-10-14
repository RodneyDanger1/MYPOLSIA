import { test, expect } from "@playwright/test";
import { API_BASE } from "./_helpers";

/**
 * V10/H2 — the dashboard Auto Mode panel (V7-T14) against the live stack.
 *
 * READ-ONLY discipline (V10-D5): a start is NOT clicked — an active window dispatches real work
 * (money runs happen inside it), reversible or not, so the lab asserts the control surface plus
 * the honest status instead of mutating anything. GET /api/v1/auto-mode is read out-of-band
 * first, the panel's own read is watched on the wire, and the rendered state must be the state
 * the API just answered, mirroring the component's own branch (active AND session => the live
 * session surface; anything else => the off surface).
 */

type AutoModeBody = {
  active: boolean;
  session: {
    time_left_seconds: number | null;
    tasks_run: number;
    spend_used: number | null;
  } | null;
};

test.describe("Dashboard Auto Mode panel", () => {
  test("status and control surface match GET /auto-mode — without starting a window (V10 lab)", async ({
    page,
    request,
  }) => {
    const api = await request.get(`${API_BASE}/api/v1/auto-mode`);
    expect(api.status()).toBe(200);
    const body = (await api.json()) as AutoModeBody;

    const apiCalls: string[] = [];
    page.on("request", (r) => {
      if (r.url().includes("/api/v1/auto-mode")) apiCalls.push(`${r.method()} ${r.url()}`);
    });

    const response = await page.goto("/dashboard");
    expect(response?.status()).toBe(200);

    const panel = page
      .locator("section.card-surface")
      .filter({ has: page.getByRole("heading", { name: "Auto Mode" }) });
    await expect(panel).toBeVisible();

    // The panel does its OWN read — a panel that renders without asking is a snapshot.
    await expect.poll(() => apiCalls.filter((c) => c.startsWith("GET")).length).toBeGreaterThan(0);

    // The honest status: what is on screen is what the API just answered.
    if (body.active && body.session !== null) {
      await expect(panel.getByText("Auto Mode on")).toBeVisible();
      await expect(panel.getByText(/tasks done/)).toBeVisible();
      await expect(panel.getByRole("button", { name: "Stop Auto Mode" })).toBeVisible();
      // The stop is NOT clicked either — that window is the owner's, not the lab's.
      await expect(panel.getByRole("button", { name: "Start Auto Mode" })).toHaveCount(0);
    } else {
      await expect(panel.getByText(/Opens a bounded window/)).toBeVisible();
      const durations = panel.getByRole("radiogroup", { name: "Auto mode duration" });
      await expect(durations).toBeVisible();
      await expect(durations.getByRole("radio")).toHaveCount(5);
      for (const label of ["1h", "6h", "24h", "3d", "5d"]) {
        await expect(durations.getByRole("radio", { name: label })).toBeVisible();
      }
      // Nothing is preselected — and the start control refuses to fire until a choice is made.
      await expect(durations.locator('[aria-checked="true"]')).toHaveCount(0);
      await expect(panel.getByRole("button", { name: "Start Auto Mode" })).toBeDisabled();
      // NOT clicked: a start opens a real dispatch window.
      await expect(panel.getByRole("button", { name: "Stop Auto Mode" })).toHaveCount(0);
    }

    // No failure surface rendered: the panel's own read answered (its load failure is role=alert).
    await expect(panel.getByRole("alert")).toHaveCount(0);
  });
});
