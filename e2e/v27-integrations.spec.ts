import { test, expect } from "@playwright/test";

import { API_BASE } from "./_helpers";

/**
 * V27 / L6 — Settings → Integrations (V26/L2, F2) through the real secret store.
 *
 * The contract this pins, in the order an operator performs it:
 *   POST/GET `/api/v1/integrations`               masked status for six providers
 *   PUT    `/api/v1/integrations/github`          store {GITHUB_TOKEN: value} -> Configured
 *   POST   `/api/v1/integrations/github/test`     REAL read-only probe -> a NAMED refusal
 *   DELETE `/api/v1/integrations/github`          remove the stored value -> Missing
 *
 * Two honesty assertions matter most:
 *   * the fake token must NEVER appear in the DOM (no text node, no attribute, no input value after
 *     the save round-trip) — the store returns only a masked hint, and the panel clears its draft;
 *   * the test action must render the server's refusal by name (`E_AUTH` when GitHub answers 401
 *     for the fake credential, `E_UPSTREAM` when the network leg itself fails) and never "Connected".
 *
 * The provider is mutated for real.  The fake value is written in a `try` and the stored value is
 * removed in `finally`, so a failed assertion still leaves the store clean (the V26 drill's trap
 * pattern).  The spec SKIPS BY NAME when GitHub is configured from the environment: in that case
 * clearing the stored value correctly leaves the provider Configured, so "clear -> Missing" would be
 * a false assertion — the honest answer is that this spec needs an env-free provider.
 */

const PROVIDER = "github";
const SECRET = "GITHUB_TOKEN";
/** Obviously fake: this exact string is asserted absent from the page after the save. */
const FAKE_TOKEN = "v27-e2e-fake-github-token-not-a-credential";

type ProviderStatus = {
  provider: string;
  configured: boolean;
  source: "stored" | "env" | "mixed" | "none";
  read_only: boolean;
};

test.describe("V27/L6 — Settings → Integrations", () => {
  test("Check status -> save fake token -> Configured -> named refusal -> clear -> Missing", async ({
    page,
    request,
  }) => {
    test.setTimeout(120_000);

    const beforeResponse = await request.get(`${API_BASE}/api/v1/integrations/${PROVIDER}`);
    expect(beforeResponse.status(), await beforeResponse.text()).toBe(200);
    const before = (await beforeResponse.json()) as ProviderStatus;

    test.skip(
      before.configured && before.source === "env",
      `${PROVIDER} is configured from the environment (.env); clearing the STORED value would leave ` +
        "it Configured by design, so this spec needs a provider whose status is not env-owned",
    );

    // A stored value left by an interrupted run is this spec's own fixture: remove it before the
    // flow starts, so the first badge really is Missing.
    if (before.source === "stored" || before.source === "mixed") {
      const normalised = await request.delete(`${API_BASE}/api/v1/integrations/${PROVIDER}`);
      expect(normalised.status(), "fixture normalisation: DELETE stored value").toBe(200);
      const afterNormalise = (await normalised.json()) as ProviderStatus;
      expect(afterNormalise.configured).toBe(false);
    }

    try {
      await page.goto("/settings");
      await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();

      // Nothing is read until the owner asks (the panel's own pinned law).
      await page.getByTestId("settings-integrations-load").click();
      await expect(page.getByTestId("settings-integrations-list")).toBeVisible();

      const card = page.getByTestId(`integration-card-${PROVIDER}`);
      await expect(card).toBeVisible();
      const badge = page.getByTestId(`integration-badge-${PROVIDER}`);
      await expect(badge).toHaveText("Missing");

      // Save the clearly-fake token.  The panel's input is the secret's own aria-label.
      await card.getByLabel(SECRET).fill(FAKE_TOKEN);
      await card.getByRole("button", { name: "Save", exact: true }).click();
      await expect(badge).toHaveText("Configured");
      await expect(
        card.getByText("Saved — stored on the backend, never returned by the API.", { exact: true }),
      ).toBeVisible();

      // The value lived in component state for the round-trip only: the box is empty and the
      // token is nowhere in the DOM (nor in any input's value property).
      await expect(card.getByLabel(SECRET)).toHaveValue("");
      expect(
        await page.content(),
        "the fake token must never be rendered by the integrations panel",
      ).not.toContain(FAKE_TOKEN);
      const inputValues = await page
        .locator("input")
        .evaluateAll((nodes) => nodes.map((node) => (node as HTMLInputElement).value));
      expect(inputValues).not.toContain(FAKE_TOKEN);

      // Test connection: a real probe of the fake credential.  A refusal by name is the answer.
      await card.getByRole("button", { name: "Test connection" }).click();
      const testResult = page.getByTestId(`integration-test-result-${PROVIDER}`);
      await expect(testResult).toBeVisible({ timeout: 60_000 });
      await expect(testResult).toContainText("Test refused —");
      await expect(testResult).toContainText(/E_AUTH|E_UPSTREAM/);
      await expect(testResult).not.toContainText("Connected —");
      expect(await page.content()).not.toContain(FAKE_TOKEN);

      // Clear: confirm, then the server's masked status says Missing again.
      await card.getByRole("button", { name: "Clear stored" }).click();
      await expect(page.getByRole("group", { name: "Confirm clearing GitHub" })).toBeVisible();
      await page.getByRole("button", { name: "Confirm clear" }).click();
      await expect(badge).toHaveText("Missing");
      await expect(
        card.getByText("Stored value removed. This provider is not configured now.", {
          exact: true,
        }),
      ).toBeVisible();
      expect(await page.content()).not.toContain(FAKE_TOKEN);
    } finally {
      // Cleanup is an assertion, not a hope: the fake token must be removable even when the flow
      // above failed mid-way (DELETE is idempotent on this route).
      const cleared = await request.delete(`${API_BASE}/api/v1/integrations/${PROVIDER}`);
      expect(cleared.status(), "fixture cleanup: the fake token must be removable").toBe(200);
      const afterClear = (await cleared.json()) as ProviderStatus;
      expect(
        afterClear.source,
        "after cleanup no STORED value may remain — the fake token must be gone",
      ).not.toBe("stored");
      expect(
        afterClear.configured,
        "unless the environment owns it, the provider must read Missing after cleanup",
      ).toBe(afterClear.source === "env");
    }
  });
});
