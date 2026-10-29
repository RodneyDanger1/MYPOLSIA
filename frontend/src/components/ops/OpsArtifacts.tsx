"use client";

import { useMemo, useState } from "react";

import {
  ARTIFACT_PATH_STYLE,
  type ArtifactSelection,
  bytesText,
  useArtifactContent,
  useArtifactDownload,
  useArtifactIndex,
  useArtifactListing,
} from "@/components/artifacts/reader";
import { absoluteStamp } from "@/components/dashboard/format";
import { ListSkeleton, ReadError } from "@/components/finance/states";
import { Button, Card, Chip } from "@/components/ui";

/**
 * R-W2-B3 — the screen half of F-08/F-15, on `/ops` where the runs directory is already visible.
 * F-27 (wave 4) moved the READING into `components/artifacts/reader.ts` so the same four routes
 * feed this card and the owner's `/runs/{id}` + `/tasks/{id}` surfaces; this file is now the
 * index presentation only.
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
 *   * a content read refusal (E_VALIDATION for traversal/oversize/binary) -> the named refusal,
 *     never an empty preview;
 *   * every successful read shows its provenance (sha256 + byte count) from the response headers,
 *     not from a client-side computation.
 *
 * It reads its index on mount and on its own Refresh only (not on the page's poll tick): the
 * workspace walk is a disk scan and should not fan out every 15 s. No write/upload/delete exists:
 * `code_write` stays the only writer and `/ops` retention the only deleter.
 *
 * F-35 (wave 5): the path button, the preview header and the download notice print the server's
 * exact path/name via `ARTIFACT_PATH_STYLE` from the shared reader — `ui-label`'s CSS uppercased
 * them, and `BRAND/BRAND-ONE-PAGER.MD` 404s where `brand/brand-one-pager.md` is 200.
 */

export function OpsArtifacts() {
  const { index, error, loading, reload } = useArtifactIndex();

  const [openRun, setOpenRun] = useState<string | null>(null);
  const listing = useArtifactListing(openRun);
  const [openFile, setOpenFile] = useState<ArtifactSelection | null>(null);
  const content = useArtifactContent(openFile);
  const {
    notice: downloadNotice,
    error: downloadError,
    download,
    clear: clearDownload,
  } = useArtifactDownload();

  const toggleRun = (runId: string) => {
    clearDownload();
    setOpenFile(null);
    setOpenRun((current) => (current === runId ? null : runId));
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
                                style={ARTIFACT_PATH_STYLE}
                                data-testid={`ops-artifact-file-${run.run_id}-${position}`}
                                title={`open ${file.path}`}
                                onClick={() => {
                                  clearDownload();
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
              {openFile.runId} /{" "}
              <span style={ARTIFACT_PATH_STYLE} data-testid="ops-artifact-selection-path">
                {openFile.path}
              </span>
            </p>
            <Button size="sm" onClick={() => void download(openFile.runId, openFile.path)} data-testid="ops-artifact-download">
              Download
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setOpenFile(null);
                clearDownload();
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
            <p
              className="ui-label mt-1 text-ink-muted"
              style={ARTIFACT_PATH_STYLE}
              data-testid="ops-artifact-download-notice"
            >
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
