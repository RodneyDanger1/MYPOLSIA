"use client";

import { useState } from "react";

import {
  type ArtifactSelection,
  bytesText,
  useArtifactContent,
  useArtifactDownload,
  useArtifactListing,
} from "@/components/artifacts/reader";
import { absoluteStamp } from "@/components/dashboard/format";
import { Button, Card } from "@/components/ui";

/**
 * F-27 (wave 4) — ONE run's artifacts, where the owner opens the run.
 *
 * Wave 3 put the artifact index on `/ops`; the owner reads a run from `/runs/{id}` (linked from
 * `/usage`) and a task from `/tasks/{id}`, and neither surface could show what the run wrote. This
 * panel reads exactly one route — `GET /artifacts/{run_id}` — through the shared reader
 * (`components/artifacts/reader.ts`), so the four-route contract keeps one dialect.
 *
 * The states are the reader's, rendered honestly:
 *   * loading -> a busy line, never an empty list;
 *   * read failure -> named (E_NOT_FOUND becomes "no workspace for this run"); a POLITE live
 *     region, not `role="alert"`, because these detail pages already own an alert for the task/run
 *     read itself (the `TaskAttachments` precedent);
 *   * ready + 0 files -> "This run wrote nothing" — the measured zero that makes a claimed-but-
 *     missing deliverable visible (F-15), never a spinner and never an error;
 *   * ready + files -> each file opens its exact text with the server's sha256/bytes/modified
 *     provenance, and can be DOWNLOADed byte-for-byte.
 *
 * It reads on mount and on its own Refresh only; no page poll drives it, and nothing here writes.
 */
export function RunArtifacts({
  runId,
  bare = false,
}: {
  runId: number | string;
  /** Render without the card chrome (the task page nests it under its own row). */
  bare?: boolean;
}) {
  const runKey = String(runId);
  const [reloadKey, setReloadKey] = useState(0);
  const listing = useArtifactListing(runKey, reloadKey);
  const [selection, setSelection] = useState<ArtifactSelection | null>(null);
  const content = useArtifactContent(selection);
  const {
    notice: downloadNotice,
    error: downloadError,
    download,
    clear: clearDownload,
  } = useArtifactDownload();

  const body = (
    <>
      <div className="flex flex-wrap items-center gap-2">
        <p className="text-sm text-ink-muted">
          The files this run actually wrote under <code className="font-mono">runs/{runKey}/</code>.
          A run that wrote nothing says so — a deliverable it only claims in prose was not written
          here.
        </p>
        <button
          type="button"
          className="link ui-label ml-auto"
          data-testid="run-artifacts-refresh"
          onClick={() => {
            setSelection(null);
            clearDownload();
            setReloadKey((count) => count + 1);
          }}
        >
          Refresh
        </button>
      </div>

      {listing === null || listing.kind === "loading" ? (
        <p className="ui-label mt-2 text-ink-muted" aria-busy="true" data-testid="run-artifacts-loading">
          Reading run #{runKey}&rsquo;s files…
        </p>
      ) : null}

      {listing !== null && listing.kind === "error" ? (
        <div aria-live="polite" className="mt-2" data-testid="run-artifacts-error">
          <p className="text-sm text-alert">
            {listing.notFound
              ? `no workspace for this run — ${listing.message}`
              : `Could not read this run's files: ${listing.message}`}
          </p>
        </div>
      ) : null}

      {listing !== null && listing.kind === "ready" && listing.data.items.length === 0 ? (
        <p className="mt-2 text-sm text-ink-muted" data-testid="run-artifacts-empty">
          This run wrote nothing — its workspace has no files (measured total 0). A file it claims
          in prose was not written here.
        </p>
      ) : null}

      {listing !== null && listing.kind === "ready" && listing.data.items.length > 0 ? (
        <div data-testid="run-artifacts-list">
          <p className="ui-label mt-2 text-ink-muted" data-testid="run-artifacts-count">
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
                  data-testid={`run-artifact-file-${position}`}
                  title={`open ${file.path}`}
                  onClick={() => {
                    clearDownload();
                    setSelection({ runId: runKey, path: file.path });
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
        </div>
      ) : null}

      {selection !== null ? (
        <div className="mt-2 border border-line bg-surface p-2" data-testid="run-artifact-preview">
          <div className="flex flex-wrap items-center gap-2">
            <p className="ui-label text-ink">
              run {selection.runId} / {selection.path}
            </p>
            <Button
              size="sm"
              onClick={() => void download(selection.runId, selection.path)}
              data-testid="run-artifact-download"
            >
              Download
            </Button>
            <Button
              size="sm"
              onClick={() => {
                setSelection(null);
                clearDownload();
              }}
              data-testid="run-artifact-close"
            >
              Close
            </Button>
          </div>

          {content !== null && content.kind === "ready" ? (
            <p className="ui-label mt-1 text-ink-muted" data-testid="run-artifact-provenance">
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
            <p aria-live="polite" className="mt-1 text-sm text-alert" data-testid="run-artifact-content-error">
              {content.message}
            </p>
          ) : null}

          {content !== null && content.kind === "ready" ? (
            <pre
              className="mt-1 max-h-80 overflow-auto whitespace-pre-wrap border border-line bg-surface-sunk p-2 text-xs text-ink"
              data-testid="run-artifact-content"
            >
              {content.data.text}
            </pre>
          ) : null}

          {downloadNotice !== null ? (
            <p className="ui-label mt-1 text-ink-muted" data-testid="run-artifact-download-notice">
              {downloadNotice}
            </p>
          ) : null}
          {downloadError !== null ? (
            <p aria-live="polite" className="mt-1 text-sm text-alert" data-testid="run-artifact-download-error">
              {downloadError}
            </p>
          ) : null}
        </div>
      ) : null}
    </>
  );

  if (bare) {
    return (
      <section className="mt-2 border border-line bg-surface p-3" data-testid="run-artifacts">
        {body}
      </section>
    );
  }

  return (
    <Card
      data-testid="run-artifacts"
      title="Run artifacts"
      kicker={`RUN #${runKey} · WHAT THIS RUN WROTE`}
    >
      {body}
    </Card>
  );
}
