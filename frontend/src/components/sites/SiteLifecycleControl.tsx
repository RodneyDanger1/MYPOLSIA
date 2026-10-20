"use client";

import { Button } from "@/components/ui";
import type { SiteSummary } from "@/components/sites/types";

/**
 * The lifecycle board the `/sites` page keeps for EVERY row, keyed by slug (wave V27, lane L1).
 *
 * One object, like `DeployBoard`: the page owns the calls, this component owns the words.  `armedSlug`
 * is the row whose Delete was clicked ONCE — the row then asks for confirmation and nothing has been
 * sent; only `onDelete` (the Confirm button) calls `DELETE /api/v1/sites/<slug>`.  There is no
 * one-click delete anywhere in this UI.
 *
 * `errors[slug]` is the server's own named refusal, rendered inline on the row it belongs to — a
 * failed delete/regenerate never removes the row or claims success.
 */
export type LifecycleBoard = {
  armedSlug: string | null;
  busySlug: string | null;
  /** WHAT is in flight for `busySlug` — the words follow it, so a delete never reads "Regenerating…". */
  busyKind: "regenerate" | "delete" | null;
  errors: Record<string, string>;
  onArm: (slug: string) => void;
  onCancel: (slug: string) => void;
  onRegenerate: (slug: string) => void;
  onDelete: (slug: string) => void;
};

/**
 * One site's lifecycle controls: Regenerate, the two-step Delete, and — for a site that actually
 * has a `sitemap.xml` (a V26+ site: `pages` is present in its `meta.json`) — a link to it.
 *
 * Regenerate rebuilds the three pages, `styles.css` and the sitemap from the CURRENT company profile
 * (`POST /sites/<slug>/regenerate`); the slug, template and recorded domain are kept by the server.
 * Delete is deliberately armed first: the row spells out exactly what is removed (the pages, the
 * stylesheet, the sitemap and `meta.json`), and Cancel disarms it without a request.
 */
export function SiteLifecycleControl({
  site,
  name,
  lifecycle,
}: {
  site: SiteSummary;
  name: string;
  lifecycle: LifecycleBoard;
}) {
  const armed = lifecycle.armedSlug === site.slug;
  const busy = lifecycle.busySlug === site.slug;
  const error = lifecycle.errors[site.slug] ?? null;
  // The sitemap is written by V26+ generation only: a legacy row has no `pages` and no sitemap, so
  // linking one would be a fabricated artifact (the same rule `SitePages` follows).
  const hasSitemap = Array.isArray(site.pages) && site.pages.length > 0;

  return (
    <div
      data-testid={`site-lifecycle-${site.slug}`}
      className="flex w-full flex-wrap items-center gap-2 border-t border-line px-3 py-2"
    >
      <Button
        size="sm"
        data-testid={`site-regenerate-${site.slug}`}
        aria-label={`Regenerate ${name}`}
        aria-busy={busy && lifecycle.busyKind === "regenerate"}
        disabled={busy}
        onClick={() => lifecycle.onRegenerate(site.slug)}
      >
        {busy && lifecycle.busyKind === "regenerate" ? "Regenerating…" : "Regenerate"}
      </Button>

      {armed ? (
        <>
          <span className="ui-label text-ink-muted">
            Delete {name}? Its pages, stylesheet, sitemap and meta.json are removed.
          </span>
          <Button
            size="sm"
            variant="glossy"
            data-testid={`site-delete-confirm-${site.slug}`}
            aria-label={`Confirm delete ${name}`}
            aria-busy={busy && lifecycle.busyKind === "delete"}
            disabled={busy}
            onClick={() => lifecycle.onDelete(site.slug)}
          >
            {busy && lifecycle.busyKind === "delete" ? "Deleting…" : "Confirm delete"}
          </Button>
          <Button
            size="sm"
            data-testid={`site-delete-cancel-${site.slug}`}
            aria-label={`Cancel delete ${name}`}
            disabled={busy}
            onClick={() => lifecycle.onCancel(site.slug)}
          >
            Cancel
          </Button>
        </>
      ) : (
        <Button
          size="sm"
          data-testid={`site-delete-${site.slug}`}
          aria-label={`Delete ${name}`}
          disabled={busy}
          onClick={() => lifecycle.onArm(site.slug)}
        >
          Delete
        </Button>
      )}

      {hasSitemap ? (
        <a
          className="link ui-label"
          data-testid={`site-sitemap-${site.slug}`}
          href={`${site.url}sitemap.xml`}
          target="_blank"
          rel="noopener"
          aria-label={`Open sitemap for ${name}`}
        >
          Sitemap
        </a>
      ) : null}

      {error !== null ? (
        <p
          role="alert"
          data-testid={`site-lifecycle-error-${site.slug}`}
          className="w-full border border-line bg-surface px-3 py-2 text-xs text-ink"
        >
          {error}
        </p>
      ) : null}
    </div>
  );
}
