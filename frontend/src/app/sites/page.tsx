"use client";

import { useCallback, useEffect, useState } from "react";

import { SiteCreateForm } from "@/components/sites/SiteCreateForm";
import { SiteList } from "@/components/sites/SiteList";
import { SitePreview } from "@/components/sites/SitePreview";
import type { DeployBoard } from "@/components/sites/SiteDeployControl";
import {
  absoluteSiteUrl,
  describeError,
  type DeployRecord,
  type DeployResponse,
  type DeployStateResponse,
  type SiteCreated,
  type SiteDraft,
  type SiteSummary,
} from "@/components/sites/types";
import { Card } from "@/components/ui";
import { api } from "@/lib/api";

/** A `last_deploy` record from the wire, narrowed field by field (the file is human-editable). */
function asDeployRecord(value: unknown): DeployRecord | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const error = record.error;
  return {
    at: typeof record.at === "string" ? record.at : null,
    state: typeof record.state === "string" && record.state ? record.state : "unknown",
    status: typeof record.status === "string" ? record.status : null,
    deploy_id: typeof record.deploy_id === "string" ? record.deploy_id : null,
    url: typeof record.url === "string" ? record.url : null,
    error:
      error !== null && typeof error === "object" && !Array.isArray(error)
        ? (error as DeployRecord["error"])
        : null,
  };
}

/** `GET /sites/{slug}/deploy` as a usable state, or null when the body is not one.
 *  A non-object body is a BROKEN READ, never "never deployed" — the caller names it. */
function asDeployState(value: unknown): DeployStateResponse | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  if (typeof record.slug !== "string" || typeof record.configured !== "boolean") return null;
  return {
    slug: record.slug,
    configured: record.configured,
    missing: Array.isArray(record.missing)
      ? record.missing.filter((item): item is string => typeof item === "string")
      : [],
    service_id: typeof record.service_id === "string" ? record.service_id : null,
    last_deploy: asDeployRecord(record.last_deploy),
  };
}

/** A copy of `record` without `key` — React state updates must never mutate the old object. */
function withoutKey<T>(record: Record<string, T>, key: string): Record<string, T> {
  const next = { ...record };
  delete next[key];
  return next;
}

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
 *
 * Wave V25 adds the deploy board: every row reads `GET /sites/{slug}/deploy` (a local read — the
 * server never calls Render for it), shows its status badge, and the Deploy button asks for a DRY
 * RUN first.  The preview is the exact call — credential masked — and only Confirm posts the live
 * deploy (`{dry_run: false}`).  The badge then reports the Render deploy id; it never says
 * "deployed" for a deploy Render has not acknowledged.  When the server answers
 * `configured: false`, the row renders NO deploy button and names the missing variables.
 *
 * Wave V26 (Sites 2.0) adds the template picker (three named presets), each row's real page list
 * (`pages` from `meta.json`, so a V24 site shows only its index and no invented links), and the
 * custom-domain field.  The domain is a RECORD: it is stored with the site and shown with the
 * wording "recorded, not verified" — nothing in this page resolves DNS or implies the host is live.
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

  // Wave V25 — the deploy board, one entry per slug (see `SiteDeployControl.DeployBoard`).
  const [deployStates, setDeployStates] = useState<Record<string, DeployStateResponse>>({});
  const [deployStateErrors, setDeployStateErrors] = useState<Record<string, string>>({});
  const [deployBusy, setDeployBusy] = useState<string | null>(null);
  const [deployBusyKind, setDeployBusyKind] = useState<"preview" | "deploy" | null>(null);
  const [deployPreviews, setDeployPreviews] = useState<Record<string, DeployResponse>>({});
  const [deployErrors, setDeployErrors] = useState<Record<string, string>>({});

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

  /**
   * One row's deploy state (`GET /sites/{slug}/deploy`).  A body that is not a deploy state is a
   * BROKEN READ named in the row, never "Never deployed"; a failed read is named too.  The read is
   * LOCAL on the server (no Render call), so it is safe to run for every row on load.
   */
  const readDeployState = useCallback(async (slug: string) => {
    try {
      const body = await api.get<unknown>(`/sites/${slug}/deploy`);
      const parsed = asDeployState(body);
      if (parsed === null) {
        setDeployStates((prev) => withoutKey(prev, slug));
        setDeployStateErrors((prev) => ({
          ...prev,
          [slug]: "the server returned no deploy state",
        }));
        return;
      }
      setDeployStates((prev) => ({ ...prev, [slug]: parsed }));
      setDeployStateErrors((prev) => withoutKey(prev, slug));
    } catch (error: unknown) {
      setDeployStates((prev) => withoutKey(prev, slug));
      setDeployStateErrors((prev) => ({ ...prev, [slug]: describeError(error) }));
    }
  }, []);

  useEffect(() => {
    if (rows === null) return;
    for (const row of rows) void readDeployState(row.slug);
  }, [rows, readDeployState]);

  /** Step 1: the DRY RUN. Sends nothing, writes nothing, returns the exact planned call. */
  async function dryRunDeploy(slug: string) {
    setDeployBusy(slug);
    setDeployBusyKind("preview");
    setDeployErrors((prev) => withoutKey(prev, slug));
    setDeployPreviews((prev) => withoutKey(prev, slug));
    try {
      const result = await api.post<DeployResponse>(`/sites/${slug}/deploy`, { dry_run: true });
      setDeployPreviews((prev) => ({ ...prev, [slug]: result }));
    } catch (error: unknown) {
      setDeployErrors((prev) => ({ ...prev, [slug]: describeError(error) }));
    } finally {
      setDeployBusy(null);
      setDeployBusyKind(null);
    }
  }

  /** Step 2: the LIVE deploy, only after the operator saw the plan and confirmed it. */
  async function confirmDeploy(slug: string) {
    setDeployBusy(slug);
    setDeployBusyKind("deploy");
    setDeployErrors((prev) => withoutKey(prev, slug));
    // The preview has served its purpose: drop it so the badge cannot keep claiming "Dry-run ok"
    // while the live call is in flight or after it failed.
    setDeployPreviews((prev) => withoutKey(prev, slug));
    try {
      const result = await api.post<DeployResponse>(`/sites/${slug}/deploy`, { dry_run: false });
      setDeployPreviews((prev) => withoutKey(prev, slug));
      if (!result.dry_run) {
        setDeployStates((prev) => {
          const current = prev[slug];
          return {
            ...prev,
            [slug]: {
              slug,
              configured: result.configured,
              missing: result.configured ? [] : (current?.missing ?? []),
              service_id: current?.service_id ?? null,
              last_deploy: asDeployRecord({
                at: result.at,
                state: result.state,
                status: result.status,
                deploy_id: result.deploy_id,
                url: result.url,
                error: null,
              }),
            },
          };
        });
      }
    } catch (error: unknown) {
      setDeployErrors((prev) => ({ ...prev, [slug]: describeError(error) }));
      // The backend persisted a failed record before refusing: re-read it so the badge tells the
      // truth instead of keeping a stale "never deployed".
      await readDeployState(slug);
    } finally {
      setDeployBusy(null);
      setDeployBusyKind(null);
    }
  }

  function cancelDeployPreview(slug: string) {
    setDeployPreviews((prev) => withoutKey(prev, slug));
    setDeployErrors((prev) => withoutKey(prev, slug));
  }

  async function create(draft: SiteDraft): Promise<boolean> {
    setBusy(true);
    setCreateError(null);
    setCopied(null);
    // V26: `template` is sent only when the operator picked a non-default preset, and
    // `custom_domain` only when one was typed — blank boxes stay omitted so the server's own
    // defaults (classic, no domain) apply instead of a client-side copy of them.
    const body: {
      title?: string;
      tagline?: string;
      template?: string;
      custom_domain?: string;
    } = {};
    if (draft.title) body.title = draft.title;
    if (draft.tagline) body.tagline = draft.tagline;
    if (draft.template) body.template = draft.template;
    if (draft.custom_domain) body.custom_domain = draft.custom_domain;
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

  const deployBoard: DeployBoard = {
    states: deployStates,
    stateErrors: deployStateErrors,
    busySlug: deployBusy,
    busyKind: deployBusyKind,
    previews: deployPreviews,
    errors: deployErrors,
    onDryRun: (slug) => {
      void dryRunDeploy(slug);
    },
    onDeploy: (slug) => {
      void confirmDeploy(slug);
    },
    onCancel: cancelDeployPreview,
  };

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
            deploy={deployBoard}
          />
          {selected !== null ? <SitePreview site={selected} /> : null}
        </div>
      ) : null}
    </div>
  );
}
