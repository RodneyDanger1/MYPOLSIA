"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, api } from "@/lib/api";
import { Button, SectionHeader } from "@/components/ui";

/**
 * Settings → Site package (V11/W4) — generate the static-site package, per the REAL endpoint
 * (`backend/app/api/v1/site.py` + `site_package_service.py`):
 *
 *   POST /api/v1/site/package -> {package_dir, files: [...paths], slug}
 *
 * The endpoint WRITES `site-<slug>/` (index.html + HOSTING-README.md) under
 * `backend/data/exports/` and returns the FILE PATHS — nothing more. There is no archive in the
 * response and no GET route serving those files (grepped: `site/package` is the only site route),
 * so this section does not fake a browser download or a progress bar: it reports the server's own
 * answer — the folder, the files it wrote, and the slug — and says plainly that hosting/publishing
 * is not provided (the package's own README states the same).
 *
 * The only refusal is `E_CONFIG`, named: no company row, or its `name` / contact
 * (`notification_email`) missing — rendered as-is with the server's own fix hint.
 */

type SitePackageResult = {
  package_dir: string;
  files: string[];
  slug: string;
};

function errorText(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  if (e instanceof Error) return e.message;
  try {
    return JSON.stringify(e) ?? String(e);
  } catch {
    return String(e);
  }
}

/** The file's display name: the path's last segment, either separator (server-side paths). */
function baseName(path: string): string {
  const parts = String(path).split(/[\\/]/);
  return parts[parts.length - 1] || String(path);
}

export function SitePackageSection() {
  const [result, setResult] = useState<SitePackageResult | null>(null);
  const [building, setBuilding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const build = useCallback(async () => {
    setBuilding(true);
    setError(null);
    try {
      const response = await api.post<SitePackageResult>("/site/package");
      if (!mounted.current) return;
      setResult(response);
    } catch (e: unknown) {
      if (mounted.current) setError(errorText(e));
    } finally {
      if (mounted.current) setBuilding(false);
    }
  }, []);

  const files = Array.isArray(result?.files) ? result.files : [];

  return (
    <section className="card-surface p-4 space-y-4" aria-label="Site package">
      <SectionHeader
        kicker="DISTRIBUTION"
        title="Site package"
        action={
          <Button size="sm" data-testid="settings-site-build" onClick={build} disabled={building}>
            {building ? "Building…" : "Build site package"}
          </Button>
        }
      />
      <p className="text-sm text-ink">
        Builds a plain static site (index.html + HOSTING-README.md) from the company profile and
        writes it under the backend&apos;s exports folder (POST /api/v1/site/package). No hosting, no
        deployment, no uptime promise — publishing it is yours to do.
      </p>

      {error !== null ? (
        <div role="alert" data-testid="settings-site-error" className="border border-alert bg-surface p-3">
          <p className="ui-label text-alert">Site package refused</p>
          <p className="mt-1 text-sm text-ink">{error}</p>
        </div>
      ) : null}

      {result !== null ? (
        <div className="space-y-2" data-testid="settings-site-result">
          <p role="status" className="border border-line-strong bg-surface-sunk px-3 py-2 text-sm text-ink">
            Package written — slug “{result.slug}”.
          </p>
          <p className="ui-label text-ink-muted">
            Folder: <span className="text-ink">{result.package_dir}</span>
          </p>
          <ul className="space-y-1">
            {files.map((path) => (
              <li key={path} className="text-sm text-ink">
                <span className="ui-label text-ink-muted">file</span> {baseName(path)}
                <span className="ui-label ml-2 text-ink-muted">{path}</span>
              </li>
            ))}
          </ul>
          {files.length === 0 ? (
            <p className="ui-label text-ink-muted" data-testid="settings-site-no-files">
              The server reported no files for this package.
            </p>
          ) : null}
          <p className="ui-label text-ink-muted">
            The files live on the server at the paths above — there is no HTTP download for this
            package, and nothing is published by building it.
          </p>
        </div>
      ) : null}
    </section>
  );
}
