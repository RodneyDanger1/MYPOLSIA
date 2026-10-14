import { test, expect } from "@playwright/test";
import { API_BASE } from "./_helpers";

/**
 * V10/H2 — Settings → Scheduler: the Night Task section (V7-T12) against the live stack.
 *
 * READ-ONLY discipline (V10-D5): the enable toggle is NOT flipped and nothing is saved — a flip
 * plus save arms real night runs. The section must render the REAL values from GET /config (read
 * out-of-band first), and the save surface is asserted as its honest idle self: nothing is dirty,
 * so "Save Settings" is disabled until a change — the lab makes none.
 */

type ConfigBody = {
  timezone: string | null;
  daily_cycle_hour: number | null;
  night_task_enabled: boolean;
  night_task_hour: number | null;
  night_task_limit: number | null;
};

test.describe("Settings — Night task", () => {
  test("Scheduler tab renders the stored night-task config without enabling anything (V10 lab)", async ({
    page,
    request,
  }) => {
    const api = await request.get(`${API_BASE}/api/v1/config`);
    expect(api.status()).toBe(200);
    const config = (await api.json()) as ConfigBody;

    const response = await page.goto("/settings");
    expect(response?.status()).toBe(200);
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

    await page.getByRole("tab", { name: "Scheduler" }).click();
    const panel = page.getByRole("tabpanel", { name: "Scheduler" });
    await expect(panel).toBeVisible();

    // The scheduler's own stored values render (timezone from GET /config, as-is).
    await expect(panel.getByLabel("Timezone")).toHaveValue(config.timezone ?? "");

    // Night task section: labels + the real config values.
    await expect(panel.getByText("Night task", { exact: true })).toBeVisible();
    await expect(
      panel.getByText(/Hands the top of the queue to the dispatcher once per night/),
    ).toBeVisible();
    await expect(panel.getByText("Night task hour (company-local)", { exact: true })).toBeVisible();
    await expect(panel.getByText("Night task limit", { exact: true })).toBeVisible();

    const enabled = panel.getByRole("switch", { name: "Night task enabled" });
    await expect(enabled).toBeVisible();
    await expect(enabled).toHaveAttribute("aria-checked", String(config.night_task_enabled));

    const hours = panel.getByRole("radiogroup", { name: "Night task hour" });
    await expect(hours.getByRole("radio")).toHaveCount(24);
    if (config.night_task_hour === null) {
      // An unset hour is stored as NO value (the tick resolves its documented 23:00 at run time) —
      // nothing may render as preselected.
      await expect(hours.locator('[aria-checked="true"]')).toHaveCount(0);
    } else {
      const label = `${String(config.night_task_hour).padStart(2, "0")}:00`;
      await expect(hours.getByRole("radio", { name: label })).toHaveAttribute(
        "aria-checked",
        "true",
      );
    }

    const limits = panel.getByRole("radiogroup", { name: "Night task limit" });
    await expect(limits.getByRole("radio")).toHaveCount(5);
    if (config.night_task_limit !== null) {
      const label = config.night_task_limit === 1 ? "1 task" : `${config.night_task_limit} tasks`;
      await expect(limits.getByRole("radio", { name: label })).toHaveAttribute(
        "aria-checked",
        "true",
      );
    }

    // The honest save surface: nothing changed in this run, so the one PUT control is idle-disabled.
    const save = page.getByRole("button", { name: "Save Settings" });
    await expect(save).toBeVisible();
    await expect(save).toBeDisabled();
    await expect(
      page.getByText(/Saves the config fields you changed — one PUT for the whole form\./),
    ).toBeVisible();

    // Still untouched: the toggle reads the same value it did before the assertions.
    await expect(enabled).toHaveAttribute("aria-checked", String(config.night_task_enabled));
  });
});
