import { test, expect } from "@playwright/test";

import { API_BASE } from "./_helpers";

/**
 * V27 / L6 — Sites 2.0 (V26/L5, F5) through the served stack.
 *
 * A V26 site is THREE real pages plus a sitemap, not a single generated index:
 *   GET /sites/<slug>/             index.html      (html=True serves the directory index)
 *   GET /sites/<slug>/about.html
 *   GET /sites/<slug>/contact.html
 *   GET /sites/<slug>/sitemap.xml  one absolute <loc> per page
 *
 * The spec reuses an existing V26 site when the stack has one (this lab has the V26 drill rows);
 * on a fresh stack it creates one through the same `POST /api/v1/sites` contract.  Creation is
 * disk-backed; V27 added `DELETE /api/v1/sites/{slug}`, so a spec-created fixture is tagged
 * `v27-e2e-<stamp>` and deleted through that route before the test ends.
 *
 * It also pins the V26 custom-domain contract: a scheme or path is REFUSED BY NAME with
 * `E_VALIDATION` (422).  The value is recorded, never DNS-verified, so a spec that sent
 * `https://…/path` and got 201 would be proving a false claim.
 */

type SiteRow = {
  slug: string;
  pages?: string[] | null;
  custom_domain?: string | null;
};
const V26_PAGES = ["index.html", "about.html", "contact.html"];

test.describe("V27/L6 — sites 2.0", () => {
  test("three pages + sitemap are served, and a non-hostname custom domain is refused", async ({
    request,
  }) => {
    const sitesResponse = await request.get(`${API_BASE}/api/v1/sites`);
    expect(sitesResponse.status(), await sitesResponse.text()).toBe(200);
    const sites = (await sitesResponse.json()) as SiteRow[];

    const reusable = sites.find(
      (site) =>
        Array.isArray(site.pages) &&
        V26_PAGES.every((page) => site.pages?.includes(page)),
    );

    let slug: string;
    if (reusable) {
      slug = reusable.slug;
    } else {
      slug = `v27-e2e-${Date.now()}`;
      const create = await request.post(`${API_BASE}/api/v1/sites`, {
        data: {
          title: "V27 E2E site",
          tagline: "Three pages and a sitemap",
          slug,
          template: "modern",
          custom_domain: "e2e.example",
        },
      });
      expect(create.status(), await create.text()).toBe(201);
      const created = (await create.json()) as { slug: string; url: string };
      expect(created.slug).toBe(slug);
    }

    // The three pages, script-free (the generator's own V26 promise).
    for (const page of ["", "about.html", "contact.html"]) {
      const response = await request.get(`${API_BASE}/sites/${slug}/${page}`);
      expect(response.status(), `site page /sites/${slug}/${page}`).toBe(200);
      const body = await response.text();
      expect(body.length, "a served page cannot be an empty body").toBeGreaterThan(0);
      expect(body, `the generated page /${page} must carry no <script>`).not.toMatch(/<script\b/i);
    }

    // The sitemap: absolute locs for exactly the three pages this site serves.
    const sitemap = await request.get(`${API_BASE}/sites/${slug}/sitemap.xml`);
    expect(sitemap.status()).toBe(200);
    const xml = await sitemap.text();
    expect(xml).toMatch(/<urlset\b/);
    for (const page of ["", "about.html", "contact.html"]) {
      expect(xml, `sitemap must list /sites/${slug}/${page}`).toContain(
        `/sites/${slug}/${page}`,
      );
    }
    expect(xml, "sitemap locs must be absolute, never path-only").toMatch(/<loc>https?:\/\//);

    // Invalid custom domain: a scheme/path is a named E_VALIDATION, not a recorded hostname.
    const badSlug = `v27-e2e-bad-${Date.now()}`;
    const bad = await request.post(`${API_BASE}/api/v1/sites`, {
      data: {
        title: "V27 invalid domain",
        slug: badSlug,
        custom_domain: "https://evil.example/path",
      },
    });
    expect(bad.status(), await bad.text()).toBe(422);
    const headerCode = bad.headers()["x-polsia-error-code"];
    const body = (await bad.json()) as { error?: { code?: string } };
    expect(headerCode ?? body.error?.code).toBe("E_VALIDATION");

    // The fixture was created by this test on a fresh stack; remove it through the V27 route so a
    // CI run leaves no site directory behind (V28/L4 D1 — the old docstring said no route existed).
    if (!reusable) {
      const removed = await request.delete(`${API_BASE}/api/v1/sites/${slug}`);
      expect(removed.status(), await removed.text()).toBe(200);
    }
  });
});
