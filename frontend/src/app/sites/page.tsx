"use client";

import { useCallback, useEffect, useState } from "react";

import { SiteCreateForm } from "@/components/sites/SiteCreateForm";
import { SiteList } from "@/components/sites/SiteList";
import { SitePreview } from "@/components/sites/SitePreview";
import {
  absoluteSiteUrl,
  describeError,
  type SiteCreated,
  type SiteDraft,
  type SiteSummary,
} from "@/components/sites/types";
import { Card } from "@/components/ui";
import { api } from "@/lib/api";

/**
 * Sites — build and host a real page from the company profile (wave V24, lane L3).
 *
 * Every control calls a REAL route and nothing else (`backend/app/api/v1/sites.py`):
 *   GET  /sites           -> [{slug, title, url, created_at, bytes}], newest first
 *   POST /sites           -> 201 {slug, url, bytes}
 * The generated files are served by the backend's StaticFiles mount at `/sites/<slug>/` (nginx
 * proxies that prefix), so Open/Copy/the iframe all point at the artifact a visitor gets — there is
 * no stored copy in the UI that could drift from the served page.
 *
 * States are disjoint and each is stated in its own words:
 *   loading -> `sites-loading` (aria-busy); "not read yet" is never drawn as "none exist";
 *   error   -> the failure NAMED (`ApiError.display`), with the list REPLACED (never rows under a
 *              banner); a 200 whose body is not a list is named as a broken read;
 *   empty   -> only after a settled read that really returned nothing;
 *   create  -> the server's refusal renders in place (`sites-create-error`) and the typed values
 *              stay in the form; a success re-reads the list, because `created_at`/`bytes` come
 *              from the server's `meta.json`, not from the 201 body.
 *
 * Copy writes the ABSOLUTE url and says "copied" only after the clipboard promise resolved; when
 * the browser exposes no clipboard API the page says so instead of pretending.
 */
export default function SitesPage() {
  const [rows, setRows] = useState<SiteSummary[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [readError, setReadError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);

  const [busy, setBusy] = useState(false);
  const [createError, setCreateError] = useState<string | null>(null);

  const [selectedSlug, setSelectedSlug] = useState<string | null>(null);
  const [copied, setCopied] = useState<{ slug: string; url: string } | null>(null);
  const [copyError, setCopyError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setReadError(null);
    api
      .get<unknown>("/sites")
      .then((body) => {
        if (cancelled) return;
        if (!Array.isArray(body)) {
          setRows(null);
          setReadError("the server returned no list of sites");
          return;
        }
        setRows(body as SiteSummary[]);
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        setRows(null);
        setReadError(describeError(error));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const reload = useCallback(() => setReloadKey((count) => count + 1), []);

  async function create(draft: SiteDraft): Promise<boolean> {
    setBusy(true);
    setCreateError(null);
    setCopied(null);
    const body: { title?: string; tagline?: string } = {};
    if (draft.title) body.title = draft.title;
    if (draft.tagline) body.tagline = draft.tagline;
    try {
      const created = await api.post<SiteCreated>("/sites", body);
      setSelectedSlug(created.slug);
      reload();
      return true;
    } catch (error: unknown) {
      setCreateError(describeError(error));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function copy(site: SiteSummary) {
    setCopyError(null);
    setCopied(null);
    const absolute = absoluteSiteUrl(site.url);
    const clipboard = typeof navigator === "undefined" ? undefined : navigator.clipboard;
    if (!clipboard || typeof clipboard.writeText !== "function") {
      setCopyError("Copy is unavailable in this browser — open the site and copy the address bar.");
      return;
    }
    try {
      await clipboard.writeText(absolute);
      setCopied({ slug: site.slug, url: absolute });
    } catch {
      setCopyError("The browser refused the clipboard write — open the site and copy the address bar.");
    }
  }

  const selected = rows?.find((row) => row.slug === selectedSlug) ?? null;
  const brokenBody = !loading && readError === null && rows === null;

  return (
    <div className="space-y-4">
      <div className="flex items-end gap-2 border-b border-line pb-1">
        <div className="mr-auto">
          <h1 className="text-3xl font-bold text-ink">Sites</h1>
          <p className="ui-label text-ink-muted">
            {rows !== null && readError === null && !brokenBody
              ? `${rows.length} ${rows.length === 1 ? "site" : "sites"} on disk`
              : "Generated from your company profile and served at /sites/<slug>/."}
          </p>
        </div>
      </div>

      <Card>
        <SiteCreateForm busy={busy} onCreate={create} />
        {createError !== null ? (
          <p
            role="alert"
            data-testid="sites-create-error"
            className="mt-3 border border-line bg-surface px-3 py-2 text-sm text-ink"
          >
            {createError}
          </p>
        ) : null}
      </Card>

      {loading ? (
        <p data-testid="sites-loading" aria-busy="true" className="text-sm text-ink-muted">
          Reading sites…
        </p>
      ) : null}

      {!loading && (readError !== null || brokenBody) ? (
        <p
          role="alert"
          data-testid="sites-error"
          className="border border-line bg-surface px-3 py-2 text-sm text-ink"
        >
          {readError ?? "the server returned no list of sites"}
        </p>
      ) : null}

      {!loading && readError === null && !brokenBody && rows !== null && rows.length === 0 ? (
        <p className="text-sm text-ink-muted" data-testid="sites-empty">
          No sites yet. Create one above — it is written to disk and served at{" "}
          <span className="ui-label">/sites/&lt;slug&gt;/</span>.
        </p>
      ) : null}

      {copyError !== null ? (
        <p data-testid="sites-copy-error" className="text-sm text-ink-muted">
          {copyError}
        </p>
      ) : null}

      {copied !== null ? (
        <p data-testid="sites-copied" className="text-sm text-ink-muted">
          Copied {copied.url} to the clipboard.
        </p>
      ) : null}

      {rows !== null && readError === null && !brokenBody && rows.length > 0 ? (
        <div className="space-y-3">
          <SiteList
            rows={rows}
            selectedSlug={selectedSlug}
            onSelect={setSelectedSlug}
            onCopy={copy}
          />
          {selected !== null ? <SitePreview site={selected} /> : null}
        </div>
      ) : null}
    </div>
  );
}
