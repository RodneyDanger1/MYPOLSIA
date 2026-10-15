"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { API_KEY, ApiError, api } from "@/lib/api";
import { Button, SectionHeader } from "@/components/ui";

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost";

/**
 * Settings → Site package (V11/W4) — generate AND download the static-site package, per the REAL
 * endpoints (`backend/app/api/v1/site.py` + `site_package_service.py`):
 *
 *   POST /api/v1/site/package          -> {package_dir, files: [...paths], slug}
 *   GET  /api/v1/site/package/download -> the built package as a ZIP, `Content-Disposition:
 *                                         attachment; filename="site-<slug>.zip"` (site.py:29-45)
 *
 * The POST WRITES `site-<slug>/` (index.html + HOSTING-README.md) under `backend/data/exports/`
 * and returns the FILE PATHS. The GET has always served the archive (V12/W-C added the
 * exports-root guard) — wave V15/W2 (item 4-07) wires the download control that was missing and
 * removes the previous copy's false "there is no HTTP download" claim. The download is a raw
 * `fetch` + blob, the exact pattern `components/usage/ExportButton.tsx` established: `lib/api.ts`
 * is JSON-only by construction (`apiFetch` ends in `res.json()`), so the one read it cannot
 * perform happens here — still taking auth from the client's SINGLE credential source (`API_KEY`,
 * imported from `@/lib/api`, sent only when non-empty, the same convention `apiFetch` documents)
 * — and the saved name is the SERVER's own Content-Disposition name, never a client-side guess.
 *
 * Refusals are named, always: `E_CONFIG` on the build (no company row, or its name / contact
 * missing), the `E_NOT_FOUND` / `E_VALIDATION` envelopes on the download (nothing built for this
 * company / an unsafe slug) — each rendered as-is with the server's own fix hint. Building or
 * downloading publishes nothing; that sentence stays true.
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

/** The refusal carried by a response body, in the two wire shapes `lib/api.ts` parses (the
 *  `{ok:false,error:{…}}` envelope and FastAPI's `{detail:{…}}`) — the same module-local mirror
 *  `ExportButton.tsx` carries, for the same reason: the api client is JSON-only and exports
 *  neither this parser nor a blob mode (adding one is a REQUEST for that file's owner). */
function refusalOf(body: unknown): { code: string; message: string; howToFix: string } | null {
  if (!body || typeof body !== "object") return null;
  const b = body as { ok?: unknown; error?: unknown; detail?: unknown };
  const candidate =
    b.ok === false && b.error !== null && typeof b.error === "object"
      ? (b.error as Record<string, unknown>)
      : b.detail !== null && typeof b.detail === "object"
        ? (b.detail as Record<string, unknown>)
        : null;
  if (!candidate || typeof candidate.code !== "string") return null;
  return {
    code: candidate.code,
    message: typeof candidate.message === "string" ? candidate.message : candidate.code,
    howToFix: typeof candidate.how_to_fix === "string" ? candidate.how_to_fix : "",
  };
}

/** The server's own save name out of a `Content-Disposition` header — quoted and unquoted
 *  `filename=` forms both parse. An unrecognisable header falls back to `site-package.zip`: the
 *  CONTENT is still the server's bytes, and a save name is no place to invent a slug. */
function filenameFromDisposition(header: string | null): string {
  if (header) {
    const match = /filename\s*=\s*"?([^";]+)"?/i.exec(header);
    if (match && match[1].trim() !== "") return match[1].trim();
  }
  return "site-package.zip";
}

export function SitePackageSection() {
  const [result, setResult] = useState<SitePackageResult | null>(null);
  const [building, setBuilding] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);
  const [downloadDone, setDownloadDone] = useState<string | null>(null);

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

  /**
   * The real download: GET the ZIP, save it as a blob under the server's own filename. No slug
   * param is sent — the route defaults to the CURRENT company's package, which is the one this
   * section builds and reports. A failure surfaces beside the control, named; never a dead click,
   * never a silent no-op.
   */
  const download = useCallback(async () => {
    setDownloading(true);
    setDownloadError(null);
    setDownloadDone(null);
    try {
      const res = await fetch(`${API_URL}/api/v1/site/package/download`, {
        headers: { ...(API_KEY ? { "X-API-Key": API_KEY } : {}) },
      });
      if (!res.ok) {
        // Read the refusal BEFORE building the error, so a coded body (E_NOT_FOUND /
        // E_VALIDATION) keeps its code + fix hint instead of degrading to a bare HTTP status.
        const text = await res.text().catch(() => res.statusText);
        let parsed: unknown;
        try {
          parsed = JSON.parse(text);
        } catch {
          parsed = undefined;
        }
        const refusal = refusalOf(parsed);
        throw refusal
          ? new ApiError(res.status, refusal.message, refusal.code, refusal.howToFix, parsed)
          : new ApiError(res.status, text || res.statusText, `E_HTTP_${res.status}`);
      }
      const blob = await res.blob();
      const filename = filenameFromDisposition(res.headers.get("Content-Disposition"));
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      if (mounted.current) setDownloadDone(`Downloaded ${filename}.`);
    } catch (e: unknown) {
      if (mounted.current) setDownloadError(errorText(e));
    } finally {
      if (mounted.current) setDownloading(false);
    }
  }, []);

  const files = Array.isArray(result?.files) ? result.files : [];

  return (
    <section className="card-surface p-4 space-y-4" aria-label="Site package">
      <SectionHeader
        kicker="DISTRIBUTION"
        title="Site package"
        action={
          <span className="flex items-center gap-2">
            <Button size="sm" data-testid="settings-site-build" onClick={build} disabled={building}>
              {building ? "Building…" : "Build site package"}
            </Button>
            {/* The download works before a build in THIS session too: the server may already
                hold a package from an earlier one — a refusal then is the named E_NOT_FOUND. */}
            <Button
              size="sm"
              data-testid="settings-site-download"
              onClick={download}
              disabled={downloading}
            >
              {downloading ? "Downloading…" : "Download ZIP"}
            </Button>
          </span>
        }
      />
      <p className="text-sm text-ink">
        Builds a plain static site (index.html + HOSTING-README.md) from the company profile and
        writes it under the backend&apos;s exports folder (POST /api/v1/site/package). No hosting, no
        deployment, no uptime promise — publishing it is yours to do. Download it as a ZIP (GET
        /api/v1/site/package/download): the archive is built server-side from the package files
        and saved under the server&apos;s own name (site-&lt;slug&gt;.zip) — nothing is published by
        building or downloading it.
      </p>

      {error !== null ? (
        <div role="alert" data-testid="settings-site-error" className="border border-alert bg-surface p-3">
          <p className="ui-label text-alert">Site package refused</p>
          <p className="mt-1 text-sm text-ink">{error}</p>
        </div>
      ) : null}

      {downloadError !== null ? (
        <div
          role="alert"
          data-testid="settings-site-download-error"
          className="border border-alert bg-surface p-3"
        >
          <p className="ui-label text-alert">Download refused</p>
          <p className="mt-1 text-sm text-ink">{downloadError}</p>
        </div>
      ) : null}

      {downloadDone !== null ? (
        <p role="status" data-testid="settings-site-download-done" className="ui-label text-ink-muted">
          {downloadDone}
        </p>
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
        </div>
      ) : null}
    </section>
  );
}
