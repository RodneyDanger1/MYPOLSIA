"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { absoluteStamp } from "@/components/dashboard/format";
import { ListSkeleton, ReadError } from "@/components/finance/states";
import { errorText } from "@/components/tasks/status";
import { Button, Card, Chip } from "@/components/ui";
import { API_KEY, API_URL, ApiError, api } from "@/lib/api";

/**
 * R-W2-B3 — the screen half of F-08/F-15, on `/ops` where the runs directory is already visible.
 *
 * Wave 2 shipped the artifact API (`backend/app/api/v1/artifacts.py`) and the measured defects it
 * exposes: run 301 claims a file it never wrote (`files: 0`), run 314 really wrote 2 files /
 * 11,373 B that the owner could not open anywhere. This panel is a thin consumer of the four
 * frozen routes — it invents nothing:
 *
 *   GET /artifacts                    -> run workspaces, INCLUDING runs that wrote nothing
 *   GET /artifacts/{run_id}           -> that run's files (an existing-but-empty dir is total 0)
 *   GET /artifacts/{run_id}/content   -> the file as text + its sha256/bytes/modified headers
 *   GET /artifacts/{run_id}/download  -> the exact bytes as an attachment
 *
 * The states that must never collapse into each other:
 *   * index loading / read failed (named, with Retry) / empty ("no run has written a file");
 *   * a run with `files: 0`  -> "this run wrote nothing" — a MEASURED empty, not a spinner and
 *     not an error (that measured zero is exactly how F-15 becomes visible);
 *   * a run with no workspace -> the server's `E_NOT_FOUND`, named as "no workspace for this run";
 *   * a content read refusal (E_VALIDATION for traversal/oversize/binary) -> the named refusal, never
 *     an empty preview;
 *   * every successful read shows its provenance (sha256 + byte count) from the response headers,
 *     not from a client-side computation.
 *
 * The content/download routes answer RAW BYTES, not JSON, so they are fetched with `fetch` here
 * (the owner key is sent the same way the shared client sends it). No write/upload/delete exists:
 * `code_write` stays the only writer and `/ops` retention the only deleter.
 */

type RunSummary = {
  run_id: string;
  files: number;
  bytes: number;
  latest_modified_at: string | null;
  truncated: boolean;
};

type ArtifactIndex = {
  items: RunSummary[];
  total: number;
  truncated: boolean;
  skipped: { name: string; reason: string }[];
};

type ArtifactFile = { path: string; bytes: number; modified_at: string };

type RunListing = {
  run_id: string;
  items: ArtifactFile[];
  total: number;
  total_bytes: number;
  truncated: boolean;
  skipped_links: number;
};

type FileContent = {
  text: string;
  sha256: string;
  bytes: number | null;
  modified: string | null;
};

type ListingState =
  | { kind: "loading" }
  | { kind: "ready"; data: RunListing }
  | { kind: "error"; message: string; notFound: boolean };

type ContentState =
  | { kind: "loading" }
  | { kind: "ready"; data: FileContent }
  | { kind: "error"; message: string };

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function asIndex(body: unknown): ArtifactIndex | null {
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

function asListing(body: unknown): RunListing | null {
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

function bytesText(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "unknown size";
  if (value < 1024) return `${value} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
}

/** The message a refused byte route carries (`{ok:false,error:{code,message,how_to_fix}}`). */
async function refusalFromResponse(res: Response): Promise<ApiError> {
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

async function fetchContent(runId: string, path: string): Promise<FileContent> {
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

async function fetchDownload(runId: string, path: string): Promise<Blob> {
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
function fileName(path: string): string {
  const base = path.split("/").pop() ?? path;
  return base === "" ? "artifact.txt" : base;
}

export function OpsArtifacts() {
  const [index, setIndex] = useState<ArtifactIndex | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloadKey, setReloadKey] = useState(0);

  const [openRun, setOpenRun] = useState<string | null>(null);
  const [listing, setListing] = useState<ListingState | null>(null);
  const [openFile, setOpenFile] = useState<{ runId: string; path: string } | null>(null);
  const [content, setContent] = useState<ContentState | null>(null);

  const [downloadNotice, setDownloadNotice] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<string | null>(null);

  // Object URLs created for a download are revoked on unmount as well as after the click.
  const ownedUrls = useRef<string[]>([]);
  useEffect(
    () => () => {
      for (const url of ownedUrls.current) URL.revokeObjectURL(url);
      ownedUrls.current = [];
    },
    [],
  );

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    api
      .get<unknown>("/artifacts")
      .then((body) => {
        if (cancelled) return;
        const parsed = asIndex(body);
        if (parsed === null) {
          setIndex(null);
          setError("the server returned no artifact index");
          setLoading(false);
          return;
        }
        setIndex(parsed);
        setLoading(false);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setIndex(null);
        setError(errorText(e));
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [reloadKey]);

  const reload = useCallback(() => setReloadKey((count) => count + 1), []);

  // Read the open run's file list. A closing/switch always cancels the in-flight read.
  useEffect(() => {
    if (openRun === null) {
      setListing(null);
      return;
    }
    let cancelled = false;
    setListing({ kind: "loading" });
    const runSegment = encodeURIComponent(openRun);
    api
      .get<unknown>(`/artifacts/${runSegment}`)
      .then((body) => {
        if (cancelled) return;
        const parsed = asListing(body);
        if (parsed === null) {
          setListing({ kind: "error", message: "the server returned no artifact list", notFound: false });
          return;
        }
        setListing({ kind: "ready", data: parsed });
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setListing({
          kind: "error",
          message: errorText(e),
          notFound: e instanceof ApiError && e.code === "E_NOT_FOUND",
        });
      });
    return () => {
      cancelled = true;
    };
  }, [openRun]);

  // Read the selected file's text. Selecting another file supersedes this read.
  useEffect(() => {
    if (openFile === null) {
      setContent(null);
      return;
    }
    let cancelled = false;
    setContent({ kind: "loading" });
    fetchContent(openFile.runId, openFile.path)
      .then((data) => {
        if (!cancelled) setContent({ kind: "ready", data });
      })
      .catch((e: unknown) => {
        if (!cancelled) setContent({ kind: "error", message: errorText(e) });
      });
    return () => {
      cancelled = true;
    };
  }, [openFile]);

  const toggleRun = (runId: string) => {
    setDownloadNotice(null);
    setDownloadError(null);
    setOpenFile(null);
    setContent(null);
    setOpenRun((current) => (current === runId ? null : runId));
  };

  const download = async (runId: string, path: string) => {
    setDownloadNotice(null);
    setDownloadError(null);
    try {
      const blob = await fetchDownload(runId, path);
      if (typeof URL.createObjectURL !== "function") {
        setDownloadError(
          "this browser cannot create a download URL — open the file's text preview instead",
        );
        return;
      }
      const url = URL.createObjectURL(blob);
      ownedUrls.current.push(url);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = fileName(path);
      anchor.rel = "noopener";
      anchor.click();
      setDownloadNotice(`saved ${fileName(path)} (${bytesText(blob.size)})`);
    } catch (e: unknown) {
      setDownloadError(errorText(e));
    }
  };

  const countLine = useMemo(() => {
    if (index === null) return null;
    if (index.total === 0) return "0 run workspaces";
    const shown = index.items.length;
    return shown < index.total
      ? `Showing ${shown} of ${index.total} run workspaces`
      : `Showing all ${index.total}`;
  }, [index]);

  return (
    <Card
      data-testid="ops-artifacts"
      title="Run artifacts"
      kicker="RUN WORKSPACES · WHAT EACH RUN WROTE"
      action={
        <button
          type="button"
          className="link ui-label"
          data-testid="ops-artifacts-refresh"
          onClick={reload}
        >
          Refresh
        </button>
      }
    >
      <p className="text-sm text-ink-muted">
        The files each agent run actually wrote under the workspace&rsquo;s{" "}
        <code className="font-mono">runs/</code> folder. A run with{" "}
        <span className="ui-label">0 FILES</span> wrote nothing — that measured zero is how a task
        that only <em>claims</em> a deliverable becomes visible.
      </p>

      {countLine !== null ? (
        <p className="ui-label mt-1 text-ink-muted" data-testid="ops-artifacts-count">
          {countLine}
        </p>
      ) : null}

      {loading ? <ListSkeleton testId="ops-artifacts-loading" /> : null}

      {!loading && error !== null ? (
        <ReadError
          label="the run artifact index"
          message={error}
          onRetry={reload}
          retryTestId="ops-artifacts-retry"
        />
      ) : null}

      {!loading && error === null && index !== null && index.total === 0 ? (
        <p className="mt-2 text-sm text-ink-muted" data-testid="ops-artifacts-empty">
          No run workspaces exist on this volume yet — no run has written a file. This is a measured
          zero from the workspace read, not a failed read.
        </p>
      ) : null}

      {!loading && error === null && index !== null && index.items.length > 0 ? (
        <ul className="mt-2 space-y-2" data-testid="ops-artifacts-list">
          {index.items.map((run) => {
            const expanded = openRun === run.run_id;
            return (
              <li key={run.run_id} className="border border-line bg-surface-sunk p-2">
                <button
                  type="button"
                  className="flex w-full flex-wrap items-center gap-2 text-left"
                  aria-expanded={expanded}
                  data-testid={`ops-artifact-run-${run.run_id}`}
                  onClick={() => toggleRun(run.run_id)}
                >
                  <span className="ui-label text-link">#{run.run_id}</span>
                  <Chip variant={run.files === 0 ? "soft" : "dark"}>
                    {run.files === 0 ? "0 FILES" : `${run.files} FILE${run.files === 1 ? "" : "S"}`}
                  </Chip>
                  <span className="ui-label text-ink-muted" title="exact bytes on the volume">
                    {bytesText(run.bytes)}
                  </span>
                  {run.truncated ? (
                    <span className="ui-label text-ink-muted">(walk truncated)</span>
                  ) : null}
                  <span className="ui-label ml-auto text-ink-muted">
                    {run.latest_modified_at
                      ? `updated ${absoluteStamp(run.latest_modified_at)}`
                      : "no file to date"}
                  </span>
                </button>

                {run.files === 0 ? (
                  <p className="mt-1 text-sm text-ink-muted" data-testid={`ops-artifact-none-${run.run_id}`}>
                    This run wrote nothing — its workspace exists and is empty. A deliverable it
                    claims in prose was not written here.
                  </p>
                ) : null}

                {expanded ? (
                  <div className="mt-2" data-testid={`ops-artifact-panel-${run.run_id}`}>
                    {listing === null || listing.kind === "loading" ? (
                      <p className="ui-label text-ink-muted" aria-busy="true">
                        Reading run #{run.run_id}&rsquo;s files…
                      </p>
                    ) : null}
                    {listing !== null && listing.kind === "error" ? (
                      <p role="alert" className="text-sm text-alert" data-testid={`ops-artifact-error-${run.run_id}`}>
                        {listing.notFound
                          ? `no workspace for this run — ${listing.message}`
                          : listing.message}
                      </p>
                    ) : null}
                    {listing !== null &&
                    listing.kind === "ready" &&
                    listing.data.items.length === 0 ? (
                      <p className="ui-label text-ink-muted" data-testid={`ops-artifact-empty-${run.run_id}`}>
                        this run wrote nothing — the workspace has no files (measured total 0)
                      </p>
                    ) : null}
                    {listing !== null && listing.kind === "ready" && listing.data.items.length > 0 ? (
                      <>
                        <p className="ui-label text-ink-muted">
                          {listing.data.total} file(s) · {bytesText(listing.data.total_bytes)}
                          {listing.data.truncated ? " · list truncated by the server" : ""}
                          {listing.data.skipped_links > 0
                            ? ` · ${listing.data.skipped_links} link(s) skipped, never followed`
                            : ""}
                        </p>
                        <ul className="mt-1 space-y-1">
                          {listing.data.items.map((file, position) => (
                            <li key={file.path} className="flex flex-wrap items-center gap-2">
                              <button
                                type="button"
                                className="ui-label text-link text-left hover:underline"
                                data-testid={`ops-artifact-file-${run.run_id}-${position}`}
                                title={`open ${file.path}`}
                                onClick={() => {
                                  setDownloadNotice(null);
                                  setDownloadError(null);
                                  setOpenFile({ runId: run.run_id, path: file.path });
                                }}
                              >
                                {file.path}
                              </button>
                              <span className="ui-label text-ink-muted">{bytesText(file.bytes)}</span>
                              <span className="ui-label text-ink-muted">
                                {file.modified_at ? absoluteStamp(file.modified_at) : ""}
                              </span>
                            </li>
                          ))}
                        </ul>
                      </>
                    ) : null}
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      ) : null}

      {index !== null && index.skipped.length > 0 ? (
        <p className="mt-1 text-xs text-ink-muted" data-testid="ops-artifacts-skipped">
          {index.skipped.length} directory entr(ies) were not entered:{" "}
          {index.skipped.map((entry) => `${entry.name} — ${entry.reason}`).join("; ")}
        </p>
      ) : null}

      {openFile !== null ? (
        <div className="mt-3 border border-line bg-surface p-2" data-testid="ops-artifact-preview">
          <div className="flex flex-wrap items-center gap-2">
            <p className="ui-label text-ink">
              {openFile.runId} / {openFile.path}
            </p>
            <Button size="sm" onClick={() => void download(openFile.runId, openFile.path)} data-testid="ops-artifact-download">
              Download
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setOpenFile(null);
                setDownloadNotice(null);
                setDownloadError(null);
              }}
              data-testid="ops-artifact-close"
            >
              Close
            </Button>
          </div>

          {content !== null && content.kind === "ready" ? (
            <p className="ui-label mt-1 text-ink-muted" data-testid="ops-artifact-provenance">
              sha256 {content.data.sha256 || "not served"} ·{" "}
              {content.data.bytes === null ? "bytes not served" : bytesText(content.data.bytes)}
              {content.data.modified ? ` · modified ${content.data.modified}` : ""}
            </p>
          ) : null}

          {content !== null && content.kind === "loading" ? (
            <p className="ui-label mt-1 text-ink-muted" aria-busy="true">
              Reading the file…
            </p>
          ) : null}

          {content !== null && content.kind === "error" ? (
            <p role="alert" className="mt-1 text-sm text-alert" data-testid="ops-artifact-content-error">
              {content.message}
            </p>
          ) : null}

          {content !== null && content.kind === "ready" ? (
            <pre
              className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap border border-line bg-surface-sunk p-2 text-xs text-ink"
              data-testid="ops-artifact-content"
            >
              {content.data.text}
            </pre>
          ) : null}

          {downloadNotice !== null ? (
            <p className="ui-label mt-1 text-ink-muted" data-testid="ops-artifact-download-notice">
              {downloadNotice}
            </p>
          ) : null}
          {downloadError !== null ? (
            <p role="alert" className="mt-1 text-sm text-alert" data-testid="ops-artifact-download-error">
              {downloadError}
            </p>
          ) : null}
        </div>
      ) : null}
    </Card>
  );
}
