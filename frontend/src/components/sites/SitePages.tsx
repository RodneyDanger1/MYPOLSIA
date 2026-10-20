"use client";

import type { SiteSummary } from "@/components/sites/types";

/**
 * The pages a site ACTUALLY has, as real links (`GET /api/v1/sites` → the site's `meta.json`
 * `pages`).  Wave V26.
 *
 * Why the row drives the list instead of a hard-coded trio: a site created before V26 has only
 * `index.html` on disk, and linking a page that returns 404 would be a fabricated artifact.  The
 * `sitemap.xml` link is shown only for a V26 site for the same reason (only V26 generation writes
 * one).  `custom_domain` is rendered with the honest wording — recorded, not verified.
 */
export function SitePages({ site }: { site: SiteSummary }) {
  const hasV26Pages = Array.isArray(site.pages) && site.pages.length > 0;
  const pages = hasV26Pages ? (site.pages as string[]) : ["index.html"];
  return (
    <div
      data-testid={`site-pages-${site.slug}`}
      className="flex flex-wrap items-center gap-2 border-t border-line px-3 py-2"
    >
      <span className="ui-label text-ink-muted">Pages</span>
      {pages.map((page) => (
        <a
          key={page}
          className="link ui-label"
          href={page === "index.html" ? site.url : `${site.url}${page}`}
          target="_blank"
          rel="noopener"
        >
          {page}
        </a>
      ))}
      {hasV26Pages ? (
        <a
          className="link ui-label"
          href={`${site.url}sitemap.xml`}
          target="_blank"
          rel="noopener"
        >
          sitemap.xml
        </a>
      ) : null}
      {site.custom_domain ? (
        <span className="ui-label text-ink-muted">
          · {site.custom_domain} — recorded, not verified
        </span>
      ) : null}
    </div>
  );
}
