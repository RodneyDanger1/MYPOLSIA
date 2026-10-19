import { test, expect } from "@playwright/test";

/**
 * V21/L3 — the 404 page, pinned end to end (V20 had none: an unmatched address fell through to
 * Next's default "This page could not be found").
 *
 * `frontend/src/app/not-found.tsx` is the contract of record; this spec asserts only what a browser
 * can observe:
 *
 *   1. THE FACT — `/definitely-not-a-page` answers HTTP **404** and renders the app's own surface
 *      (`data-testid="not-found"`), inside the shell (the menu trigger is present), with the honest
 *      heading "This page does not exist" — not Next's default page.
 *   2. NO FABRICATED ADDRESS — Next hands a not-found segment no path, so the requested address may
 *      not be echoed anywhere on the page. The whole surface's text is checked for it.
 *   3. THE WAY HOME — `not-found-home` is a real link to `/dashboard`, and clicking it actually
 *      navigates there.
 *   4. THE REAL SURFACES — the "Common surfaces" nav links to /tasks, /agents and /social, i.e. the
 *      destinations the menu already serves.
 *
 * Reads only: this file navigates and clicks links; it issues no POST and needs no store/config.
 */

const MISSING_PATH = "/definitely-not-a-page";

test.describe("V21 404 page (live FE)", () => {
  test("an unmatched address renders the app 404 with a working way home", async ({ page }) => {
    const response = await page.goto(MISSING_PATH);
    expect(response, "goto must return a document response").not.toBeNull();
    expect(response?.status(), "an unmatched address must answer HTTP 404").toBe(404);

    const surface = page.getByTestId("not-found");
    await expect(surface).toBeVisible();
    await expect(
      page.getByRole("heading", { level: 1, name: "This page does not exist" }),
    ).toBeVisible();

    // Rendered inside the app shell, not as a bare document.
    await expect(page.getByTestId("nav-menu-trigger")).toBeVisible();

    // Next gives the segment no path; echoing one would be fiction.
    const text = await surface.innerText();
    expect(text).not.toContain("definitely-not-a-page");
    expect(text, "the default Next 404 sentence is the thing this page replaces").not.toContain(
      "This page could not be found",
    );

    // The way home is a real link with the promised target…
    const home = page.getByTestId("not-found-home");
    await expect(home).toBeVisible();
    await expect(home).toHaveAttribute("href", "/dashboard");
    await expect(home).toContainText("Back to the dashboard");

    // …and the menu's most-used surfaces are offered in one click.
    const surfaces = page.getByRole("navigation", { name: "Common surfaces" });
    await expect(surfaces.getByRole("link", { name: /tasks/i })).toHaveAttribute("href", "/tasks");
    await expect(surfaces.getByRole("link", { name: /agents/i })).toHaveAttribute("href", "/agents");
    await expect(surfaces.getByRole("link", { name: /social/i })).toHaveAttribute("href", "/social");

    // Clicking the way home really leaves the 404 surface.
    await home.click();
    await page.waitForURL("**/dashboard");
    await expect(page.getByTestId("not-found")).toHaveCount(0);
  });
});
