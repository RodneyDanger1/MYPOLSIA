import { test, expect } from "@playwright/test";

import { API_BASE } from "./_helpers";

/**
 * V26/L6 — the deploy surface REFUSES instead of pretending (F7).
 *
 * The V25/L3 contract (`backend/app/api/v1/sites.py:124-155`):
 *   GET  /api/v1/sites/{slug}/deploy  -> {slug, configured, missing, service_id, last_deploy}
 *   POST /api/v1/sites/{slug}/deploy  {dry_run:true} -> 503 E_CONFIG when Render is unconfigured
 *
 * This lab has no `RENDER_API_KEY`/`RENDER_SERVICE_ID` (measured), so the honest path is:
 *   * `configured: false` and `missing` naming the absent variables — never a dead Deploy button;
 *   * a dry run REFUSES with the named `E_CONFIG` before any network call;
 *   * `last_deploy` stays null — a refusal must not be recorded as a deploy;
 *   * `/sites` renders the badge "Not configured — set …" and NO deploy button.
 *
 * If the environment IS configured, the refusal path cannot be exercised: the test SKIPS BY NAME
 * rather than inventing a failure or a fake deploy.
 */

type DeployState = {
  slug: string;
  configured: boolean;
  missing: string[];
  service_id: string | null;
  last_deploy: Record<string, unknown> | null;
};

type SiteRow = { slug: string; title: string; url: string };

test.describe("V26/L6 — site deploy dry run", () => {
  test("configured:false + E_CONFIG naming the missing var; no deploy is recorded", async ({
    page,
    request,
  }) => {
    const sitesResponse = await request.get(`${API_BASE}/api/v1/sites`);
    expect(sitesResponse.status()).toBe(200);
    const sites = (await sitesResponse.json()) as SiteRow[];
    test.skip(
      !Array.isArray(sites) || sites.length === 0,
      "no site exists in the lab; this spec needs one real site row (POST /api/v1/sites)",
    );

    // Pick a site whose deploy state is UNCONFIGURED — the state this lab can prove.
    let site: SiteRow | null = null;
    let state: DeployState | null = null;
    for (const candidate of sites) {
      const response = await request.get(`${API_BASE}/api/v1/sites/${candidate.slug}/deploy`);
      expect(response.status()).toBe(200);
      const candidateState = (await response.json()) as DeployState;
      if (!candidateState.configured) {
        site = candidate;
        state = candidateState;
        break;
      }
    }
    test.skip(
      site === null || state === null,
      "every site IS configured for Render in this environment (RENDER_API_KEY/" +
        "RENDER_SERVICE_ID set) — the unconfigured refusal path cannot be exercised without " +
        "faking a credential",
    );
    const target = site!;
    const before = state!;
    expect(before.slug).toBe(target.slug);

    // The dry run refuses by name — it makes no Render call and records nothing.
    const dryRun = await request.post(`${API_BASE}/api/v1/sites/${target.slug}/deploy`, {
      data: { dry_run: true },
    });
    expect(dryRun.status(), await dryRun.text()).toBe(503);
    expect(dryRun.headers()["x-polsia-error-code"]).toBe("E_CONFIG");
    const refusal = (await dryRun.json()) as {
      error: { code: string; message: string; how_to_fix?: string; context?: { missing?: string[] } };
    };
    expect(refusal.error.code).toBe("E_CONFIG");
    expect(refusal.error.message).toContain("RENDER_API_KEY");
    expect(refusal.error.message).toContain("RENDER_SERVICE_ID");
    expect(refusal.error.context?.missing ?? []).toEqual(
      expect.arrayContaining(["RENDER_API_KEY", "RENDER_SERVICE_ID"]),
    );

    const after = (await (
      await request.get(`${API_BASE}/api/v1/sites/${target.slug}/deploy`)
    ).json()) as DeployState;
    expect(after.last_deploy, "a refused dry run must NOT record a deploy").toEqual(
      before.last_deploy,
    );
    expect(after.configured).toBe(false);

    // The UI: an honest badge and NO Deploy control (a dead button is the defect).
    await page.goto("/sites");
    const row = page.getByTestId(`site-row-${target.slug}`);
    await expect(row).toBeVisible();
    const badge = page.getByTestId(`site-deploy-badge-${target.slug}`);
    await expect(badge).toBeVisible();
    await expect(badge).toContainText("Not configured");
    await expect(badge).toContainText("RENDER_API_KEY");
    await expect(
      page.getByTestId(`site-deploy-button-${target.slug}`),
      "configured:false must NOT render a Deploy button",
    ).toHaveCount(0);
  });
});
