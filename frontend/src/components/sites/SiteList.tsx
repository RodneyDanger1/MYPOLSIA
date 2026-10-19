"use client";

import { absoluteStamp } from "@/components/dashboard/format";
import { Button } from "@/components/ui";
import { bytesText, type SiteSummary } from "@/components/sites/types";

/**
 * One row per built site — the disk-backed list `GET /api/v1/sites` returned.
 *
 * Open points at the REAL served page (`/sites/<slug>/`, same origin in production: nginx proxies
 * that prefix to the backend's StaticFiles mount), Copy writes the ABSOLUTE address, and Preview
 * selects the row for the sandboxed iframe beside the list.  `created_at`/`bytes` are the server's
 * `meta.json` values; an empty title falls back to the slug, never to an invented name.
 */
export function SiteList({
  rows,
  selectedSlug,
  onSelect,
  onCopy,
}: {
  rows: SiteSummary[];
  selectedSlug: string | null;
  onSelect: (slug: string) => void;
  onCopy: (site: SiteSummary) => void;
}) {
  return (
    <ul className="divide-y divide-line border border-line" data-testid="sites-list">
      {rows.map((site) => (
        <li
          key={site.slug}
          data-testid={`site-row-${site.slug}`}
          className="flex flex-wrap items-center gap-3 px-3 py-2"
        >
          <div className="mr-auto min-w-0">
            <p className="text-sm font-semibold text-ink">{site.title || site.slug}</p>
            <p className="ui-label text-ink-muted">{site.url}</p>
            <p className="ui-label text-ink-muted">
              {`${absoluteStamp(site.created_at)} · ${bytesText(site.bytes)}`}
            </p>
          </div>
          <div className="flex items-center gap-2">
            <a
              className="link ui-label"
              href={site.url}
              target="_blank"
              rel="noopener"
              aria-label={`Open ${site.title || site.slug} in a new tab`}
            >
              Open
            </a>
            <Button
              size="sm"
              aria-label={`Copy URL for ${site.title || site.slug}`}
              onClick={() => onCopy(site)}
            >
              Copy URL
            </Button>
            <Button
              size="sm"
              aria-pressed={selectedSlug === site.slug}
              aria-label={`Preview ${site.title || site.slug}`}
              onClick={() => onSelect(site.slug)}
            >
              Preview
            </Button>
          </div>
        </li>
      ))}
    </ul>
  );
}
