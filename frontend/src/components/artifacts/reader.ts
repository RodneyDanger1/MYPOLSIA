"use client";

import { type CSSProperties, useCallback, useEffect, useRef, useState } from "react";

import { API_KEY, API_URL, ApiError, api } from "@/lib/api";

/**
 * F-27 (wave 4) — THE ONE READER of the four frozen artifact routes.
 *
 * Wave 2 shipped the API (`backend/app/api/v1/artifacts.py`); wave 3 shipped the `/ops` card;
 * wave 4 wires the pages the owner actually opens (`/runs/{id}`, `/tasks/{id}`).  Three surfaces
 * reading the same four routes must not grow three dialects, so every type, parser, fetch and
 * state hook lives HERE and the surfaces are presentation only:
 *
 *   GET /artifacts                    -> run workspaces, INCLUDING runs that wrote nothing
 *   GET /artifacts/{run_id}           -> that run's files (an existing-but-empty dir is total 0)
 *   GET /artifacts/{run_id}/content   -> the file as text + its sha256/bytes/modified headers
 *   GET /artifacts/{run_id}/download  -> the exact bytes as an attachment
 *
 * The states that must never collapse (the reason this reader exists):
 *   * loading / named read failure / empty index ("no run has written a file");
 *   * a run with `files: 0` -> "this run wrote nothing" — a MEASURED empty, not a spinner and not
 *     an error (that measured zero is how F-15 becomes visible);
 *   * a run with no workspace -> the server's `E_NOT_FOUND`, named as "no workspace for this run";
 *   * a content read refusal (E_VALIDATION for traversal/oversize/binary) -> the named refusal,
 *     never an empty preview;
 *   * every successful read shows its provenance (sha256 + byte count) from the response headers,
 *     not from a client-side computation.
 *
 * Reads happen on mount and on the surface's own Refresh only — never on a page poll tick.  The
 * index is a disk walk; the run list is one `stat`-bounded walk of one directory.  No write,
 * upload or delete exists here: `code_write` stays the only writer and `/ops` retention the only
 * deleter.
 *
 * F-35 (wave 5) — A PATH IS NOT A LABEL.  Every surface prints an artifact path inside the
 * product's micro-label voice (`.ui-label`), whose CSS is `text-transform: uppercase`.  A path is
 * case-sensitive on this server (`brand/brand-one-pager.md` is 200; the uppercased rendering
 * `BRAND/BRAND-ONE-PAGER.MD` is 404 `E_NOT_FOUND`), so a reader copying the screen was copying a
 * name that cannot be fetched.  The surfaces now print every path (and the saved file name in the
 * download notice) with `ARTIFACT_PATH_STYLE`, applied INLINE: `text-transform: none` on the node
 * itself beats the class no matter how stylesheets are layered, and jsdom can assert the computed
 * value, so the rule is testable rather than a comment.  There is ONE definition here because all
 * three surfaces read this module — a second copy could drift back to uppercase.
 */

/**
 * The inline typography fix for any text that must print a server path VERBATIM (F-35).
 *
 * `text-transform` is inherited and inline declarations outrank every stylesheet rule without
 * `!important`, so this is the one guarantee a CSS-class re-order cannot break.  Surfaces keep
 * `.ui-label` for the mono/size/letter-spacing voice; only the case transform is neutralised.
 */
export const ARTIFACT_PATH_STYLE: CSSProperties = { textTransform: "none" };

export type RunSummary = {
  run_id: string;
  files: number;
  bytes: number;
  latest_modified_at: string | null;
  truncated: boolean;
};

export type ArtifactIndex = {
  items: RunSummary[];
  total: number;
  truncated: boolean;
  skipped: { name: string; reason: string }[];
};

export type ArtifactFile = { path: string; bytes: number; modified_at: string };

export type RunListing = {
  run_id: string;
  items: ArtifactFile[];
  total: number;
  total_bytes: number;
  truncated: boolean;
  skipped_links: number;
};

export type FileContent = {
  text: string;
  sha256: string;
  bytes: number | null;
  modified: string | null;
};

/** What a surface has selected to read: one run's file. */
export type ArtifactSelection = { runId: string; path: string };

export type ListingState =
  | { kind: "loading" }
  | { kind: "ready"; data: RunListing }
  | { kind: "error"; message: string; notFound: boolean };

export type ContentState =
  | { kind: "loading" }
  | { kind: "ready"; data: FileContent }
  | { kind: "error"; message: string };

export function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

/** The index body as a usable index, or `null` when the body is not one (a broken read). */
export function asIndex(body: unknown): ArtifactIndex | null {
  if (!isRecord(body) || !Array.isArray(body.items) || typeof body.total !== "number") return null;
  const items: RunSummary[] = [];
  for (const raw of body.items) {
    if (!isRecord(raw) || typeof raw.run_id !== "string") return null;
    items.push({
      run_id: raw.run_id,
      files: typeof raw.files === "number" ? raw.files : 0,
      bytes: typeof raw.bytes === "number" ? raw.bytes : 0,
      latest_modified_at:
        typeof raw.latest_modified_at === "string" ? raw.latest_modified_at : null,
      truncated: raw.truncated === true,
    });
  }
  return {
    items,
    total: body.total,
    truncated: body.truncated === true,
    skipped: Array.isArray(body.skipped)
      ? body.skipped
          .filter(isRecord)
          .map((entry) => ({
            name: typeof entry.name === "string" ? entry.name : "(unnamed)",
            reason: typeof entry.reason === "string" ? entry.reason : "no reason recorded",
          }))
      : [],
  };
}

/** One run's file list as a usable listing, or `null` when the body is not one. */
export function asListing(body: unknown): RunListing | null {
  if (!isRecord(body) || !Array.isArray(body.items)) return null;
  const items: ArtifactFile[] = [];
  for (const raw of body.items) {
    if (!isRecord(raw) || typeof raw.path !== "string") return null;
    items.push({
      path: raw.path,
      bytes: typeof raw.bytes === "number" ? raw.bytes : 0,
      modified_at: typeof raw.modified_at === "string" ? raw.modified_at : "",
    });
  }
  return {
    run_id: typeof body.run_id === "string" ? body.run_id : "",
    items,
    total: typeof body.total === "number" ? body.total : items.length,
    total_bytes: typeof body.total_bytes === "number" ? body.total_bytes : 0,
    truncated: body.truncated === true,
    skipped_links: typeof body.skipped_links === "number" ? body.skipped_links : 0,
  };
}

/** A byte count in the product's display vocabulary; a negative/NaN value is "unknown size". */
export function bytesText(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "unknown size";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

/** The message a refused byte route carries (`{ok:false,error:{code,message,how_to_fix}}`). */
export async function refusalFromResponse(res: Response): Promise<ApiError> {
  const text = await res.text().catch(() => res.statusText);
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    parsed = undefined;
  }
  if (isRecord(parsed)) {
    const envelope = isRecord(parsed.error) ? parsed.error : parsed.detail;
    if (isRecord(envelope) && typeof envelope.code === "string") {
      return new ApiError(
        res.status,
        typeof envelope.message === "string" ? envelope.message : envelope.code,
        envelope.code,
        typeof envelope.how_to_fix === "string" ? envelope.how_to_fix : "",
        parsed,
      );
    }
  }
  return new ApiError(res.status, text || res.statusText, `E_HTTP_${res.status}`, "", undefined);
}

/** Read one file's TEXT through the content route (raw bytes, owner key, provenance headers). */
export async function fetchArtifactContent(runId: string, path: string): Promise<FileContent> {
  const runSegment = encodeURIComponent(runId);
  const pathQuery = encodeURIComponent(path);
  const res = await fetch(
    `${API_URL}/api/v1/artifacts/${runSegment}/content?path=${pathQuery}`,
    { method: "GET", headers: API_KEY ? { "X-API-Key": API_KEY } : {} },
  );
  if (!res.ok) throw await refusalFromResponse(res);
  const headerBytes = res.headers.get("X-Polsia-Artifact-Bytes");
  const parsedBytes = headerBytes === null ? Number.NaN : Number(headerBytes);
  return {
    text: await res.text(),
    sha256: res.headers.get("X-Polsia-Artifact-Sha256") ?? "",
    bytes: Number.isFinite(parsedBytes) ? parsedBytes : null,
    modified: res.headers.get("X-Polsia-Artifact-Modified"),
  };
}

/** Read one file's EXACT BYTES through the download route (the owner key is sent the same way). */
export async function fetchArtifactDownload(runId: string, path: string): Promise<Blob> {
  const runSegment = encodeURIComponent(runId);
  const pathQuery = encodeURIComponent(path);
  const res = await fetch(
    `${API_URL}/api/v1/artifacts/${runSegment}/download?path=${pathQuery}`,
    { method: "GET", headers: API_KEY ? { "X-API-Key": API_KEY } : {} },
  );
  if (!res.ok) throw await refusalFromResponse(res);
  return res.blob();
}

/** The download's display name: the file's own basename (the server also sets a safe one). */
export function artifactFileName(path: string): string {
  const base = path.split("/").pop() ?? path;
  return base === "" ? "artifact.txt" : base;
}

export type ArtifactIndexRead = {
  index: ArtifactIndex | null;
  error: string | null;
  loading: boolean;
  reload: () => void;
};

/**
 * The index read (`GET /artifacts`): on mount and on `reload()` — nothing else.  The call is made
 * through the shared client so a refusal reaches the caller as the server's own display string.
 */
export function useArtifactIndex(): ArtifactIndexRead {
  const [index, setIndex] = useState<ArtifactIndex | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);
  const reload = useCallback(() => setReloadKey((count) => count + 1), []);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const body = await api.get<unknown>("/artifacts");
        if (cancelled) return;
        const parsed = asIndex(body);
        if (parsed === null) {
          setIndex(null);
          setError("the server returned no artifact index");
          return;
        }
        setIndex(parsed);
      } catch (e: unknown) {
        if (cancelled) return;
        setIndex(null);
        setError(errorMessage(e));
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  return { index, error, loading, reload };
}

/**
 * One run's listing (`GET /artifacts/{run_id}`), re-read when the selected run changes or the
 * surface's own `reloadKey` bumps.  A closing or switching run always cancels the in-flight read;
 * `null` (nothing selected) is `null` state, never "loading".
 */
export function useArtifactListing(runId: string | null, reloadKey = 0): ListingState | null {
  const [listing, setListing] = useState<ListingState | null>(null);

  useEffect(() => {
    if (runId === null) {
      setListing(null);
      return;
    }
    let cancelled = false;
    setListing({ kind: "loading" });
    // Template-simple on purpose: the UI→route census (`backend/tests/unit/test_v28_wiring.py`)
    // reads path literals out of this source, and `${runSegment}` keeps it resolvable to
    // `/artifacts/{run_id}`. An inline call inside the interpolation breaks that extraction.
    const runSegment = encodeURIComponent(runId);
    (async () => {
      try {
        const body = await api.get<unknown>(`/artifacts/${runSegment}`);
        if (cancelled) return;
        const parsed = asListing(body);
        if (parsed === null) {
          setListing({
            kind: "error",
            message: "the server returned no artifact list",
            notFound: false,
          });
          return;
        }
        setListing({ kind: "ready", data: parsed });
      } catch (e: unknown) {
        if (cancelled) return;
        setListing({
          kind: "error",
          message: errorMessage(e),
          notFound: e instanceof ApiError && e.code === "E_NOT_FOUND",
        });
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [runId, reloadKey]);

  return listing;
}

/** The selected file's TEXT (`GET .../content`); selecting another file supersedes this read. */
export function useArtifactContent(selection: ArtifactSelection | null): ContentState | null {
  const [content, setContent] = useState<ContentState | null>(null);

  useEffect(() => {
    if (selection === null) {
      setContent(null);
      return;
    }
    let cancelled = false;
    setContent({ kind: "loading" });
    fetchArtifactContent(selection.runId, selection.path)
      .then((data) => {
        if (!cancelled) setContent({ kind: "ready", data });
      })
      .catch((e: unknown) => {
        if (!cancelled) setContent({ kind: "error", message: errorMessage(e) });
      });
    return () => {
      cancelled = true;
    };
  }, [selection]);

  return content;
}

export type ArtifactDownload = {
  notice: string | null;
  error: string | null;
  download: (runId: string, path: string) => Promise<void>;
  clear: () => void;
};

/**
 * The DOWNLOAD affordance's own state (`GET .../download` -> Blob -> `<a download>`).  Object URLs
 * are owned here and revoked on unmount as well as replaced after each click.  A browser without
 * `URL.createObjectURL` is told the truth instead of a silent no-op.
 */
export function useArtifactDownload(): ArtifactDownload {
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const ownedUrls = useRef<string[]>([]);

  useEffect(
    () => () => {
      for (const url of ownedUrls.current) URL.revokeObjectURL(url);
      ownedUrls.current = [];
    },
    [],
  );

  const clear = useCallback(() => {
    setNotice(null);
    setError(null);
  }, []);

  const download = useCallback(async (runId: string, path: string) => {
    setNotice(null);
    setError(null);
    try {
      const blob = await fetchArtifactDownload(runId, path);
      if (typeof URL.createObjectURL !== "function") {
        setError(
          "this browser cannot create a download URL — open the file's text preview instead",
        );
        return;
      }
      const url = URL.createObjectURL(blob);
      ownedUrls.current.push(url);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = artifactFileName(path);
      anchor.rel = "noopener";
      anchor.click();
      setNotice(`saved ${artifactFileName(path)} (${bytesText(blob.size)})`);
    } catch (e: unknown) {
      setError(errorMessage(e));
    }
  }, []);

  return { notice, error, download, clear };
}

/** The shared error display (ApiError.display, else the error's own message). */
function errorMessage(e: unknown): string {
  if (e instanceof ApiError) return e.display;
  return e instanceof Error ? e.message : String(e);
}
