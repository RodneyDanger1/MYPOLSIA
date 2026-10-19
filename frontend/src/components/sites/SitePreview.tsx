"use client";

import type { SiteSummary } from "@/components/sites/types";

/**
 * The selected site, rendered by the BROWSER exactly as a visitor would see it.
 *
 * The iframe loads the real served URL — no screenshot, no stored HTML copy that could differ from
 * what `/sites/<slug>/` serves.  `sandbox="allow-same-origin"` is deliberate:
 *   * `allow-same-origin` is REQUIRED for the page's own `styles.css` to load under the deployment's
 *     `default-src 'self'` CSP — a fully opaque origin makes `'self'` match nothing and the preview
 *     would render unstyled (a silent false impression of the page);
 *   * `allow-scripts`, `allow-forms`, `allow-popups` and `allow-top-navigation` are NOT granted, so
 *     even a hostile value that ever reached a generated page cannot execute, submit or navigate
 *     the app.  The generator itself emits no script at all (`services/site_service.py`), so this is
 *     defence in depth rather than the only barrier.
 */
export function SitePreview({ site }: { site: SiteSummary }) {
  const name = site.title || site.slug;
  return (
    <section aria-labelledby="site-preview-heading" className="border border-line">
      <div className="flex flex-wrap items-end gap-2 border-b border-line px-3 py-2">
        <div className="mr-auto">
          <h2 id="site-preview-heading" className="ui-label text-ink-muted">
            Preview
          </h2>
          <p className="text-sm font-semibold text-ink">{name}</p>
        </div>
        <a
          className="link ui-label"
          href={site.url}
          target="_blank"
          rel="noopener"
          aria-label={`Open ${name} full page`}
        >
          Open full page
        </a>
      </div>
      <iframe
        data-testid="site-preview"
        title={`Preview of ${name}`}
        src={site.url}
        sandbox="allow-same-origin"
        className="h-[28rem] w-full bg-surface"
      />
      <p className="border-t border-line px-3 py-2 text-xs text-ink-muted">
        Sandboxed preview: scripts, forms, pop-ups and top-level navigation are not granted.
      </p>
    </section>
  );
}
