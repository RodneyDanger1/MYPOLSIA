"use client";

import { useEffect, useRef, useState } from "react";

import { readErrorText } from "@/components/dashboard/states";
import { Button, cn } from "@/components/ui";
import { AttachmentDescribe } from "@/components/vision/AttachmentDescribe";

import {
  ATTACHMENT_ACCEPT,
  ATTACHMENT_MAX_PER_TASK,
  attachmentProblem,
  deleteAttachment,
  fetchAttachmentBlob,
  formatBytes,
  listAttachments,
  uploadAttachment,
} from "./attachments";
import type { TaskAttachment } from "./types";

/**
 * V25/L1 — a task's images: thumbnails, upload, remove, and honest refusals.
 *
 * The server owns every rule (magic-byte type sniffing, 5 MB per image, 4 per task, 8 MB per
 * task, storage failures).  This component mirrors only the two limits a client can check
 * without reading the bytes — declared type and size — so an obviously unsupported file is
 * never uploaded at all; everything else is rendered as the server's named refusal
 * (`readErrorText` -> `ApiError.display`), never rewritten into a friendlier sentence.
 *
 * Thumbnails are BLOBS fetched with the owner key: a bare `<img src="/api/v1/attachments/1">`
 * could not send `X-API-Key`, and putting the key in a query string would leak it into logs.
 * A preview that cannot be fetched is stated ("preview unavailable") while the attachment's
 * metadata and its Remove control stay usable — a failed thumbnail is not a failed attachment.
 */
export function TaskAttachments({
  taskId,
  onChanged,
  className,
}: {
  taskId: number;
  /** Called after a successful upload/remove so the owning surface can refetch. */
  onChanged?: () => void;
  className?: string;
}) {
  const [rows, setRows] = useState<TaskAttachment[] | null>(null);
  const [loadError, setLoadError] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);
  const [removing, setRemoving] = useState<number | null>(null);
  const [notice, setNotice] = useState<{ tone: "status" | "alert"; text: string } | null>(null);
  const [previews, setPreviews] = useState<Record<number, string>>({});
  const [previewFailed, setPreviewFailed] = useState<Record<number, boolean>>({});
  /** Every object URL this component created, revoked ONCE on unmount (not on every refetch:
   *  revoking a URL that is still in `previews` would break thumbnails after an upload/remove). */
  const ownedUrls = useRef<string[]>([]);

  const load = async () => {
    setLoadError(null);
    try {
      const next = await listAttachments(taskId);
      setRows(next);
      return next;
    } catch (e) {
      setLoadError(e);
      setRows(null);
      return null;
    }
  };

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setPreviews({});
    setPreviewFailed({});
    listAttachments(taskId)
      .then((next) => {
        if (!cancelled) setRows(next);
      })
      .catch((e) => {
        if (!cancelled) setLoadError(e);
      });
    return () => {
      cancelled = true;
    };
    // `taskId` is the only input: a different task is a different list.
  }, [taskId]);

  // One blob fetch per attachment.  URLs are owned by the component and revoked ON UNMOUNT; a
  // refetch (upload/remove/onChanged) must NOT revoke a URL still rendered in `previews`.
  useEffect(() => {
    if (!rows || rows.length === 0) return;
    let cancelled = false;
    (async () => {
      for (const row of rows) {
        if (previews[row.id] || previewFailed[row.id]) continue;
        try {
          const blob = await fetchAttachmentBlob(row.id);
          if (cancelled) return;
          if (typeof URL.createObjectURL !== "function") {
            // jsdom (and some hardened browsers) have no Blob URL support: state it, keep the
            // attachment and its controls usable — a missing thumbnail is not a missing image.
            setPreviewFailed((prev) => ({ ...prev, [row.id]: true }));
            continue;
          }
          const url = URL.createObjectURL(blob);
          ownedUrls.current.push(url);
          setPreviews((prev) => ({ ...prev, [row.id]: url }));
        } catch {
          if (!cancelled) setPreviewFailed((prev) => ({ ...prev, [row.id]: true }));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
    // Re-run only when the row SET changes; the guards above stop repeat fetches per row.
  }, [rows]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const owned = ownedUrls.current;
    return () => {
      for (const url of owned) URL.revokeObjectURL(url);
      owned.splice(0, owned.length);
    };
  }, []);

  const full = (rows?.length ?? 0) >= ATTACHMENT_MAX_PER_TASK;

  const onPick = async (file: File | undefined) => {
    if (!file || busy) return;
    setNotice(null);
    const problem = attachmentProblem(file);
    if (problem) {
      // Client-side guard: NOTHING is uploaded, and the sentence says exactly that.
      setNotice({ tone: "alert", text: problem });
      return;
    }
    setBusy(true);
    try {
      await uploadAttachment(taskId, file);
      await load();
      setNotice({ tone: "status", text: `"${file.name}" attached.` });
      onChanged?.();
    } catch (e) {
      setNotice({ tone: "alert", text: readErrorText(e) });
    } finally {
      setBusy(false);
    }
  };

  const remove = async (row: TaskAttachment) => {
    if (removing !== null) return;
    setRemoving(row.id);
    setNotice(null);
    try {
      await deleteAttachment(row.id);
      await load();
      setNotice({ tone: "status", text: `"${row.filename}" removed.` });
      onChanged?.();
    } catch (e) {
      setNotice({ tone: "alert", text: readErrorText(e) });
    } finally {
      setRemoving(null);
    }
  };

  return (
    <section data-testid="task-attachments" className={cn("mt-3 border border-line bg-surface-sunk p-3", className)}>
      <p className="ui-label text-ink">Images</p>
      <p className="mt-1 text-xs text-ink-muted">
        PNG, JPEG or WebP — at most 5 MB each and 4 images per task. The model receives the
        images as part of the task it runs.
      </p>

      {loadError ? (
        // A panel-level read failure is announced politely, not as a page ALERT: the images
        // section is additive, and a page that already owns an alert (a status write, a run
        // refusal) must not suddenly have two competing alert regions.  Action refusals below
        // DO use role="alert" — they are the result of something the operator just did.
        <div aria-live="polite" data-testid="attachments-error" className="mt-2 border border-alert p-2">
          <p className="text-sm text-ink">{readErrorText(loadError)}</p>
          <Button size="sm" className="mt-2" onClick={() => void load()}>
            Retry
          </Button>
        </div>
      ) : rows === null ? (
        <p className="mt-2 text-sm text-ink-muted" aria-busy="true">
          Loading images…
        </p>
      ) : rows.length === 0 ? (
        <p className="mt-2 text-sm text-ink-muted">No images attached to this task.</p>
      ) : (
        <ul aria-label="Task images" className="mt-2 grid grid-cols-2 gap-2">
          {rows.map((row) => (
            <li key={row.id} className="flex items-center gap-2 border border-line bg-surface p-2">
              {previews[row.id] ? (
                // eslint-disable-next-line @next/next/no-img-element
                <img
                  src={previews[row.id]}
                  alt={row.filename}
                  loading="lazy"
                  decoding="async"
                  className="h-12 w-12 border border-line object-cover"
                />
              ) : (
                <span
                  aria-hidden="true"
                  className="flex h-12 w-12 items-center justify-center border border-line text-xs text-ink-muted"
                >
                  {previewFailed[row.id] ? "no preview" : "…"}
                </span>
              )}
              <span className="min-w-0 flex-1">
                <span className="block truncate text-sm text-ink" title={row.filename}>
                  {row.filename}
                </span>
                <span className="block text-xs text-ink-muted">{formatBytes(row.size_bytes)}</span>
                {previewFailed[row.id] ? (
                  <span className="block text-xs text-alert">preview unavailable — the image is still attached</span>
                ) : null}
                {/* V26/L4: vision on demand — model output, rendered inline and never saved
                    to the task.  Additive: upload/remove above and below are unchanged. */}
                <AttachmentDescribe
                  attachmentId={row.id}
                  filename={row.filename}
                  className="mt-2"
                />
              </span>
              <Button
                size="sm"
                aria-label={`Remove ${row.filename}`}
                disabled={removing !== null || busy}
                aria-busy={removing === row.id || undefined}
                onClick={() => void remove(row)}
              >
                {removing === row.id ? "Removing…" : "Remove"}
              </Button>
            </li>
          ))}
        </ul>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <input
          type="file"
          accept={ATTACHMENT_ACCEPT}
          aria-label="Attach an image"
          disabled={busy || full}
          onChange={(e) => {
            const file = e.target.files?.[0];
            // Clear the input so choosing the SAME file again re-fires change.
            e.target.value = "";
            void onPick(file);
          }}
          className="text-sm text-ink"
        />
        {busy ? (
          <span className="text-xs text-ink-muted" aria-busy="true">
            Uploading…
          </span>
        ) : null}
        {full ? (
          <span className="text-xs text-ink-muted">
            {ATTACHMENT_MAX_PER_TASK} of {ATTACHMENT_MAX_PER_TASK} images attached — remove one to add another.
          </span>
        ) : null}
      </div>

      {notice ? (
        <p role={notice.tone} data-tone={notice.tone} className="mt-2 text-sm text-ink">
          {notice.text}
        </p>
      ) : null}
    </section>
  );
}
