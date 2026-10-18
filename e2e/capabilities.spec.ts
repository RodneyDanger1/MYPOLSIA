import { test, expect } from "@playwright/test";

/**
 * V17/L4 — `/capabilities` is a first-class nav destination.
 *
 * The page shipped in V16 reachable only from Settings, so nothing proved the MENU could get to it.
 * This spec guards the two facts the nav change actually made true:
 *   1. the topbar MENU carries a `Capabilities` link (nav-items.ts is the source; the dropdown is
 *      generated from it, so a missing link means the table lost the entry);
 *   2. the link lands on the real page — the heading, and then one of the page's three real states
 *      (the map, the honest empty, or a named failure). It never asserts a row count: the map is
 *      read from `GET /api/v1/ops/capabilities`, and this suite must not invent registry contents.
 *
 * Deliberately NOT run by the lane that wrote it (frontend-sweep writes e2e, the verifier runs it).
 */
test.describe("Capabilities", () => {
  test("the MENU carries the Capabilities route and it lands on the real page", async ({ page }) => {
    await page.goto("/dashboard");
    await page.getByTestId("nav-menu-trigger").click();
    const nav = page.getByRole("navigation", { name: "Main" });

    // The nav entry itself — the V17 change.
    const link = nav.getByRole("link", { name: "Capabilities" });
    await expect(link).toBeVisible();
    await expect(link).toHaveAttribute("href", "/capabilities");

    await link.click();
    await expect(page).toHaveURL(/\/capabilities$/);
    await expect(page.getByRole("heading", { name: "Capabilities" })).toBeVisible();

    // One of the page's honest states: the registry map, a successful-but-empty map, or the named
    // failure with its Retry. All three are real answers; a blank page is not. The failure state is
    // targeted by its OWN testid — `getByRole("alert")` also matches Next's framework route
    // announcer (`#__next-route-announcer__`, role=alert on every page), which made the first CI
    // run a strict-mode violation with 2 elements (verified 2026-10-18).
    await expect(
      page
        .getByTestId("capabilities-count")
        .or(page.getByTestId("capabilities-empty"))
        .or(page.getByTestId("capabilities-error")),
    ).toBeVisible();
  });
});
