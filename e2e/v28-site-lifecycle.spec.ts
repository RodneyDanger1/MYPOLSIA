import { test, expect } from "@playwright/test";

/**
 * V28 / L4 — the V27 site lifecycle through the served stack.
 *
 *   POST   /api/v1/sites                    -> 201 {slug, url, bytes}
 *   GET    /api/v1/sites?slug=<slug>        -> the one row (pages/template/custom_domain)
 *   POST   /api/v1/sites/<slug>/regenerate  -> 200 the rebuilt record, identity preserved
 *   GET    /sites/<slug>/                   -> index.html      (StaticFiles mount)
 *   GET    /sites/<slug>/about.html
 *   GET    /sites/<slug>/contact.html
 *   GET    /sites/<slug>/sitemap.xml        -> one absolute <loc> per page
 *   DELETE /api/v1/sites/<slug>             -> 200 {slug, deleted:true}; directory gone
 *   GET    /sites?slug=<slug>  /  /sites/<slug>/  -> 404 E_NOT_FOUND after the delete
 *
 * FRESH-DB SAFETY: the site is created with a unique `v28-e2e-<stamp>` slug, so reruns and other
 * lanes can never collide (`POST /sites` refuses an existing slug with E_VALIDATION).  It is
 * deleted in the test body, and `finally` repeats the delete if an assertion failed before that —
 * the DELETE is idempotent from the caller's point of view (second answer is an honest 404).
 */

const PAGES = ["index.html", "about.html", "contact.html"] as const;

type SiteRow = {
  slug: string;
  title: string;
  url: string;
  created_at: string;
  bytes: number;
  pages?: string[] | null;
  template?: string | null;
  custom_domain?: string | null;
};

type Regenerated = {
  slug: string;
  url: string;
  bytes: number;
  title: string;
  created_at: string;
  pages: string[];
  template: string;
  custom_domain: string | null;
};

test.describe("V28/L4 — site lifecycle", () => {
  test("create → regenerate → pages + sitemap → delete → 404", async ({ request }) => {
    const stamp = `${Date.now()}-${Math.floor(Math.random() * 1_000_000)}`;
    const slug = `v28-e2e-${stamp}`;
    const title = `V28 E2E lifecycle ${stamp}`;
    let exists = false;

    try {
      // ── create ──────────────────────────────────────────────────────────────────────────────
      const created = await request.post("/api/v1/sites", {
        data: {
          title,
          tagline: "create, regenerate, serve, delete",
          slug,
          template: "modern",
          custom_domain: "v28-e2e.example",
        },
      });
      expect(created.status(), await created.text()).toBe(201);
      const createdBody = (await created.json()) as { slug: string; url: string; bytes: number };
      expect(createdBody.slug).toBe(slug);
      expect(createdBody.url).toBe(`/sites/${slug}/`);
      expect(createdBody.bytes).toBeGreaterThan(0);
      exists = true;

      const listed = await request.get(`/api/v1/sites?slug=${slug}`);
      expect(listed.status(), await listed.text()).toBe(200);
      const rows = (await listed.json()) as SiteRow[];
      expect(rows, "the exact-slug read must return exactly the created site").toHaveLength(1);
      const before = rows[0]!;
      expect(before.slug).toBe(slug);
      expect(before.title).toBe(title);
      expect(before.pages).toEqual([...PAGES]);
      expect(before.template).toBe("modern");
      expect(before.custom_domain).toBe("v28-e2e.example");

      // ── regenerate: identity preserved, a real rebuild ──────────────────────────────────────
      const regeneratedResponse = await request.post(`/api/v1/sites/${slug}/regenerate`);
      expect(regeneratedResponse.status(), await regeneratedResponse.text()).toBe(200);
      const rebuilt = (await regeneratedResponse.json()) as Regenerated;
      expect(rebuilt.slug, "a rebuild is not a rename").toBe(slug);
      expect(rebuilt.title, "a rebuild is not a re-creation").toBe(before.title);
      expect(rebuilt.created_at, "regenerate keeps the site's original timestamp").toBe(
        before.created_at,
      );
      expect(rebuilt.template).toBe("modern");
      expect(rebuilt.custom_domain).toBe("v28-e2e.example");
      expect(rebuilt.pages).toEqual([...PAGES]);
      expect(rebuilt.bytes).toBeGreaterThan(0);

      // ── the three pages, served after the regenerate ────────────────────────────────────────
      for (const page of ["", ...PAGES]) {
        const response = await request.get(`/sites/${slug}/${page}`);
        expect(
          response.status(),
          `GET /sites/${slug}/${page} after regenerate`,
        ).toBe(200);
        const body = await response.text();
        expect(body.length, `the generated page /${page} cannot be an empty body`).toBeGreaterThan(
          0,
        );
        expect(body, `the generated page /${page} must carry no <script>`).not.toMatch(
          /<script\b/i,
        );
      }

      // The site's own title is really on its index — a 200 that is some other site is not a pass.
      const index = await request.get(`/sites/${slug}/`);
      expect(await index.text()).toContain(title);

      // ── sitemap: absolute locs for exactly the pages this site serves ───────────────────────
      const sitemap = await request.get(`/sites/${slug}/sitemap.xml`);
      expect(sitemap.status()).toBe(200);
      const xml = await sitemap.text();
      expect(xml).toMatch(/<urlset\b/);
      // The index page's canonical URL is the DIRECTORY (`/sites/<slug>/`), not `index.html` —
      // the generator's own V26 contract (pinned by `v27-sites.spec.ts:65-83`).
      for (const page of ["", "about.html", "contact.html"]) {
        expect(xml, `sitemap must list /sites/${slug}/${page}`).toContain(
          `/sites/${slug}/${page}`,
        );
      }
      expect(xml, "sitemap locs must be absolute, never path-only").toMatch(/<loc>https?:\/\//);
      expect(xml.match(/<loc>/g) ?? [], "one loc per page, no duplicates").toHaveLength(
        PAGES.length,
      );

      // ── delete ──────────────────────────────────────────────────────────────────────────────
      const deleted = await request.delete(`/api/v1/sites/${slug}`);
      expect(deleted.status(), await deleted.text()).toBe(200);
      expect(await deleted.json()).toEqual({ slug, deleted: true });
      exists = false;

      // The list's exact-slug read is an honest 404, not an empty list or a zeroed 200.
      const gone = await request.get(`/api/v1/sites?slug=${slug}`);
      expect(gone.status(), await gone.text()).toBe(404);
      expect(gone.headers()["x-polsia-error-code"]).toBe("E_NOT_FOUND");
      expect(((await gone.json()) as { error: { code: string } }).error.code).toBe("E_NOT_FOUND");

      // Every generated file is gone with the directory — the disk is the site's only record.
      for (const page of ["", ...PAGES, "sitemap.xml"]) {
        const response = await request.get(`/sites/${slug}/${page}`);
        expect(
          response.status(),
          `deleted site still serves /sites/${slug}/${page}`,
        ).toBe(404);
      }

      // A second delete is honest too: 404 E_NOT_FOUND, never a fabricated 200.
      const secondDelete = await request.delete(`/api/v1/sites/${slug}`);
      expect(secondDelete.status(), await secondDelete.text()).toBe(404);
      expect(secondDelete.headers()["x-polsia-error-code"]).toBe("E_NOT_FOUND");
    } finally {
      if (exists) {
        await request.delete(`/api/v1/sites/${slug}`).catch(() => undefined);
      }
    }
  });
});
