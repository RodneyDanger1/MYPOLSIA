"use client";

import { absoluteStamp } from "@/components/dashboard/format";
import { Button } from "@/components/ui";
import { SiteDeployControl, type DeployBoard } from "@/components/sites/SiteDeployControl";
import { SitePages } from "@/components/sites/SitePages";
import { bytesText, type SiteSummary } from "@/components/sites/types";

/**
 * One row per built site — the disk-backed list `GET /api/v1/sites` returned.
 *
 * Open points at the REAL served page (`/sites/<slug>/`, same origin in production: nginx proxies
 * that prefix to the backend's StaticFiles mount), Copy writes the ABSOLUTE address, and Preview
 * selects the row for the sandboxed iframe beside the list.  `created_at`/`bytes` are the server's
 * `meta.json` values; an empty title falls back to the slug, never to an invented name.
 *
 * Wave V25 adds the row's deploy surface (`SiteDeployControl`): a status badge, a Deploy button that
 * previews the exact Render call first, and — when Render is not configured — no button at all.
 * `deploy` is optional so the list itself stays usable without a deploy board (and testable in
 * isolation), but the `/sites` page always passes one.
 */
export function SiteList({
  rows,
  selectedSlug,
  onSelect,
  onCopy,
  deploy,
}: {
  rows: SiteSummary[];
  selectedSlug: string | null;
  onSelect: (slug: string) => void;
  onCopy: (site: SiteSummary) => void;
  deploy?: DeployBoard;
}) {
  return (
    <ul className="divide-y divide-line border border-line" data-testid="sites-list">
      {rows.map((site) => {
        const name = site.title || site.slug;
        return (
          <li key={site.slug} data-testid={`site-row-${site.slug}`}>
            <div className="flex flex-wrap items-center gap-3 px-3 py-2">
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
                  aria-label={`Open ${name} in a new tab`}
                >
                  Open
                </a>
                <Button size="sm" aria-label={`Copy URL for ${name}`} onClick={() => onCopy(site)}>
                  Copy URL
                </Button>
                <Button
                  size="sm"
                  aria-pressed={selectedSlug === site.slug}
                  aria-label={`Preview ${name}`}
                  onClick={() => onSelect(site.slug)}
                >
                  Preview
                </Button>
              </div>
            </div>
            <SitePages site={site} />
            {deploy ? <SiteDeployControl slug={site.slug} name={name} deploy={deploy} /> : null}
          </li>
        );
      })}
    </ul>
  );
}
