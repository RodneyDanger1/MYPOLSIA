"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";

import { PanelError, PanelSkeleton } from "@/components/dashboard/states";
import { Button, SectionHeader } from "@/components/ui";
import { API_KEY } from "@/lib/api";

import {
  MEDIA_CAP,
  MEDIA_PREVIEW_MAX_BYTES,
  isAudioType,
  isImageType,
  mediaFilename,
  mediaSizeText,
  mediaWhenText,
  parseMediaList,
  parseRefusalBody,
  refusalText,
  type PhoneMediaPart,
} from "./phoneApi";
import { PhoneUnavailable } from "./PhoneStates";
import { usePhoneRead } from "./usePhoneRead";

/**
 * The `/phone` MEDIA surface (wave V18) — the frozen list + stream interface, and nothing else:
 *
 *   GET /api/v1/phone/media?thread_id=N  -> {parts:[…], truncated}   (the read hook validates it)
 *   GET /api/v1/phone/media/{part_id}    -> binary                   (key header, blob fetch)
 *
 * The laws this surface keeps:
 *
 *   - MEDIA IS PER THREAD. The list route takes `thread_id`; without one the section says so
 *     instead of calling a route the contract does not define.
 *   - NO FABRICATED THUMBNAILS. The grid draws a fixed-size frame carrying the real content type
 *     and part id; bytes are fetched only when an image/audio part is OPENED. A 50-part thread
 *     therefore makes zero byte requests until the operator asks for one.
 *   - EVERY OBJECT URL IS REVOKED. The preview owns exactly one URL at a time and revokes it when
 *     the part changes or the viewer unmounts (close/ESC/unmount); the download path revokes after
 *     the anchor click. The bytes are always the server's.
 *   - HONEST PER-PART STATES. 404 = the inline "part not found" chip for THAT part (the gallery and
 *     the arrow keys keep working); 503/E_PHONE_UNAVAILABLE = the page's own PHONE LINK
 *     UNAVAILABLE block, never an empty list. A kind of failure the route names renders by name.
 *   - OVERSIZED PARTS ARE STATED. Above the client's 32 MB inline-preview ceiling the part is
 *     offered as a download (size printed) and its bytes are not fetched until asked.
 *   - NO AUTOPLAY. `<audio controls>` and nothing else.
 *   - KEYBOARD-ONLY PARITY. The grid's buttons are the only entry; the dialog takes focus, traps
 *     Tab, walks parts with ArrowLeft/ArrowRight, closes on Escape, and returns focus to the
 *     button that opened it.
 */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost";

type PartFailure = { kind: "missing" | "unavailable" | "failed"; detail: string };
type FetchedPart = { ok: true; blob: Blob; filename: string } | ({ ok: false } & PartFailure);

/** One streamed part, read exactly like the export download (`PhoneConsole`): a raw fetch with the
 *  client's one credential source, refusals parsed before the error is built so a coded body keeps
 *  its code + fix hint. The response body is the ONLY source of the save name. */
async function fetchPartBytes(partId: number, contentType: string): Promise<FetchedPart> {
  const res = await fetch(`${API_URL}/api/v1/phone/media/${partId}`, {
    headers: { ...(API_KEY ? { "X-API-Key": API_KEY } : {}) },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    let parsed: unknown;
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = undefined;
    }
    const refusal = parseRefusalBody(parsed);
    const detail = refusal ? refusalText(refusal) : `E_HTTP_${res.status}: ${text || res.statusText}`;
    if (res.status === 404) return { ok: false, kind: "missing", detail };
    if (res.status === 503 || refusal?.code === "E_PHONE_UNAVAILABLE") {
      return { ok: false, kind: "unavailable", detail };
    }
    return { ok: false, kind: "failed", detail };
  }
  const blob = await res.blob();
  const disposition = res.headers.get("Content-Disposition");
  return { ok: true, blob, filename: mediaFilename(disposition, partId, contentType) };
}

type BytesState =
  | { kind: "idle" }
  | { kind: "loading" }
  | { kind: "ready"; url: string }
  | { kind: "error"; failure: PartFailure };

/** One part's bytes as an object URL. `enabled=false` (a download-only or oversized part) fetches
 *  nothing. The cleanup revokes whatever URL THIS effect created — a late response never leaks. */
function usePartBytes(part: PhoneMediaPart, enabled: boolean): BytesState {
  const partId = part.part_id;
  const contentType = part.content_type;
  const [state, setState] = useState<BytesState>({ kind: "idle" });

  useEffect(() => {
    if (!enabled) {
      setState({ kind: "idle" });
      return undefined;
    }
    let cancelled = false;
    let objectUrl: string | null = null;
    setState({ kind: "loading" });
    void fetchPartBytes(partId, contentType)
      .then((result) => {
        if (cancelled) return;
        if (!result.ok) {
          setState({ kind: "error", failure: { kind: result.kind, detail: result.detail } });
          return;
        }
        objectUrl = URL.createObjectURL(result.blob);
        setState({ kind: "ready", url: objectUrl });
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        setState({
          kind: "error",
          failure: { kind: "failed", detail: e instanceof Error ? e.message : String(e) },
        });
      });
    return () => {
      cancelled = true;
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    };
  }, [partId, contentType, enabled]);

  return state;
}

/** One grid cell's real facts — content type, size, the store's own time. */
function partFacts(part: PhoneMediaPart): string {
  return `${mediaSizeText(part.size_bytes)} · ${mediaWhenText(part.when)}`;
}

/** The content-type word in the frame (not an image — nothing is invented). */
function typeWord(contentType: string): string {
  const base = contentType.split(";")[0].trim().toLowerCase();
  if (isImageType(base)) return "IMAGE";
  if (isAudioType(base)) return "AUDIO";
  return "FILE";
}

export function PhoneMediaSection({ threadId }: { threadId: number | null }) {
  if (threadId === null) {
    return (
      <section aria-label="Thread media" data-testid="phone-media" className="space-y-2">
        <SectionHeader kicker="MEDIA" title="Media" />
        <p data-testid="phone-media-needs-thread" className="ui-label text-ink-muted">
          Media is listed per thread — open a thread to see its media parts.
        </p>
      </section>
    );
  }
  // `key` resets the viewer/status state when the operator switches threads.
  return <ThreadMedia key={threadId} threadId={threadId} />;
}

function ThreadMedia({ threadId }: { threadId: number }) {
  const read = usePhoneRead(`/phone/media?thread_id=${threadId}`, "media", parseMediaList);
  const parts = read.kind === "ok" ? read.data.parts : [];

  const [viewerIndex, setViewerIndex] = useState<number | null>(null);
  const [saved, setSaved] = useState<string | null>(null);
  const [downloadFailure, setDownloadFailure] = useState<PartFailure | null>(null);
  const [downloadingId, setDownloadingId] = useState<number | null>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  const openPart = useCallback((index: number, opener: HTMLElement) => {
    openerRef.current = opener;
    setSaved(null);
    setDownloadFailure(null);
    setViewerIndex(index);
  }, []);

  const closeViewer = useCallback(() => {
    setViewerIndex(null);
    const opener = openerRef.current;
    if (opener && typeof opener.focus === "function") opener.focus();
  }, []);

  const downloadPart = useCallback(async (part: PhoneMediaPart) => {
    setDownloadingId(part.part_id);
    setSaved(null);
    setDownloadFailure(null);
    try {
      const result = await fetchPartBytes(part.part_id, part.content_type);
      if (!result.ok) {
        setDownloadFailure({ kind: result.kind, detail: result.detail });
        return;
      }
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.filename;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setSaved(`Downloaded ${result.filename}.`);
    } catch (e: unknown) {
      setDownloadFailure({ kind: "failed", detail: e instanceof Error ? e.message : String(e) });
    } finally {
      setDownloadingId(null);
    }
  }, []);

  return (
    <section aria-label="Thread media" data-testid="phone-media" className="space-y-2">
      <SectionHeader kicker="MEDIA" title={`Media — thread #${threadId}`} />
      <p data-testid="phone-media-note" className="ui-label text-ink-muted">
        {`Up to the newest ${MEDIA_CAP} media parts for this thread — the route's own order, sizes and times verbatim.`}
      </p>

      {read.kind === "loading" ? <PanelSkeleton rows={2} /> : null}

      {read.kind === "unavailable" ? (
        <PhoneUnavailable testId="phone-media-unavailable" detail={read.detail} />
      ) : null}

      {read.kind === "failed" ? <PanelError label="phone media" message={read.detail} /> : null}

      {read.kind === "ok" && read.data.truncated ? (
        <p data-testid="phone-media-truncated" className="ui-label text-ink-muted">
          {`Showing the newest ${MEDIA_CAP} media parts — the store holds more.`}
        </p>
      ) : null}

      {read.kind === "ok" && parts.length === 0 ? (
        <p data-testid="phone-media-empty" className="ui-label text-ink-muted">
          No media in this thread.
        </p>
      ) : null}

      {read.kind === "ok" && parts.length > 0 ? (
        <ul
          data-testid="phone-media-list"
          className="grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3"
        >
          {parts.map((part, index) => {
            const image = isImageType(part.content_type);
            const audio = isAudioType(part.content_type);
            return (
              <li
                key={part.part_id}
                data-testid="phone-media-item"
                className="flex flex-col gap-1 border border-line bg-surface p-2"
              >
                {/* Fixed-size frame: the grid never reflows when bytes arrive (they don't here —
                    a part renders only in the viewer). The word is the real content type. */}
                <div
                  aria-hidden="true"
                  className="flex h-20 items-center justify-center border border-line bg-surface-sunk"
                >
                  <span className="ui-label text-ink-muted">{typeWord(part.content_type)}</span>
                </div>
                <p className="ui-label break-all text-ink-muted">
                  {`#${part.part_id} · ${part.content_type}`}
                </p>
                <p className="text-sm text-ink">{partFacts(part)}</p>
                {image ? (
                  <Button
                    size="sm"
                    variant="bevel"
                    aria-label={`Open image part #${part.part_id}`}
                    onClick={(event) => openPart(index, event.currentTarget)}
                  >
                    Open image
                  </Button>
                ) : audio ? (
                  <Button
                    size="sm"
                    variant="bevel"
                    aria-label={`Play audio part #${part.part_id}`}
                    onClick={(event) => openPart(index, event.currentTarget)}
                  >
                    Play audio
                  </Button>
                ) : (
                  <Button
                    size="sm"
                    variant="bevel"
                    aria-label={`Download part #${part.part_id}`}
                    disabled={downloadingId === part.part_id}
                    onClick={() => void downloadPart(part)}
                  >
                    {downloadingId === part.part_id ? "Downloading…" : "Download"}
                  </Button>
                )}
              </li>
            );
          })}
        </ul>
      ) : null}

      {saved !== null ? (
        <p role="status" data-testid="phone-media-saved" className="ui-label text-ink-muted">
          {saved}
        </p>
      ) : null}

      {downloadFailure !== null && downloadFailure.kind === "unavailable" ? (
        <PhoneUnavailable testId="phone-media-download-unavailable" detail={downloadFailure.detail} />
      ) : null}

      {downloadFailure !== null && downloadFailure.kind !== "unavailable" ? (
        <p role="alert" data-testid="phone-media-download-error" className="text-sm text-alert">
          {downloadFailure.kind === "missing"
            ? `part not found — ${downloadFailure.detail}`
            : `Could not download this part: ${downloadFailure.detail}`}
        </p>
      ) : null}

      {viewerIndex !== null && parts[viewerIndex] ? (
        <MediaViewer
          parts={parts}
          index={viewerIndex}
          onNavigate={setViewerIndex}
          onClose={closeViewer}
          onDownload={(part) => void downloadPart(part)}
          downloading={downloadingId !== null}
        />
      ) : null}
    </section>
  );
}

function MediaViewer({
  parts,
  index,
  onNavigate,
  onClose,
  onDownload,
  downloading,
}: {
  parts: PhoneMediaPart[];
  index: number;
  onNavigate: (index: number) => void;
  onClose: () => void;
  onDownload: (part: PhoneMediaPart) => void;
  downloading: boolean;
}) {
  const titleId = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const part = parts[index];
  const image = isImageType(part.content_type);
  const audio = isAudioType(part.content_type);
  const oversized = part.size_bytes > MEDIA_PREVIEW_MAX_BYTES;
  const preview = (image || audio) && !oversized;
  const bytes = usePartBytes(part, preview);

  // Focus enters the dialog on open (the repo's sheet grammar). Close/ESC restore it to the opener.
  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key === "Tab") {
      const panel = dialogRef.current;
      if (!panel) return;
      const focusables = Array.from(
        panel.querySelectorAll<HTMLElement>(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])',
        ),
      ).filter((element) => !element.hasAttribute("disabled") && element.tabIndex >= 0);
      if (focusables.length === 0) return;
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (event.shiftKey) {
        if (active === first || !panel.contains(active)) {
          event.preventDefault();
          last.focus();
        }
      } else if (active === last || !panel.contains(active)) {
        event.preventDefault();
        first.focus();
      }
      return;
    }
    if (event.key === "ArrowRight" || event.key === "ArrowLeft") {
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName ?? "";
      // The audio element's own arrow keys seek the clip; form controls keep their behaviour.
      if (tag === "AUDIO" || tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
      event.preventDefault();
      if (event.key === "ArrowRight" && index < parts.length - 1) onNavigate(index + 1);
      if (event.key === "ArrowLeft" && index > 0) onNavigate(index - 1);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4"
      style={{ backgroundColor: "rgba(20, 20, 20, 0.55)" }}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        data-testid="phone-media-viewer"
        onKeyDown={onKeyDown}
        className="card-surface my-4 flex w-full max-w-[720px] flex-col shadow-2xl"
      >
        <header className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2">
          <h2 id={titleId} className="ui-label text-ink">
            {`Media part #${part.part_id} — ${part.content_type}`}
          </h2>
          <p className="ui-label ml-2 text-ink-muted">{partFacts(part)}</p>
          <Button
            ref={closeRef}
            size="sm"
            aria-label="Close media viewer"
            data-testid="phone-media-close"
            className="ml-auto"
            onClick={onClose}
          >
            ✕
          </Button>
        </header>

        {/* Fixed-height frame: every part renders inside the same box, so opening audio or an
            image cannot shift the page (the grid frame never moves either). */}
        <div className="flex h-[60vh] w-full items-center justify-center overflow-hidden bg-surface-sunk px-3 py-3">
          {bytes.kind === "loading" ? <PanelSkeleton rows={2} /> : null}

          {bytes.kind === "error" && bytes.failure.kind === "missing" ? (
            <p role="alert" data-testid="phone-media-part-missing" className="text-sm text-ink">
              {`part not found — ${bytes.failure.detail}`}
            </p>
          ) : null}

          {bytes.kind === "error" && bytes.failure.kind === "unavailable" ? (
            <PhoneUnavailable testId="phone-media-part-unavailable" detail={bytes.failure.detail} />
          ) : null}

          {bytes.kind === "error" && bytes.failure.kind === "failed" ? (
            <p role="alert" data-testid="phone-media-part-error" className="text-sm text-alert">
              {`Could not load this part: ${bytes.failure.detail}`}
            </p>
          ) : null}

          {bytes.kind === "ready" && image && bytes.url ? (
            <img
              data-testid="phone-media-image"
              src={bytes.url}
              alt={`Media part #${part.part_id} (${part.content_type}), ${mediaWhenText(part.when)}`}
              className="max-h-full max-w-full object-contain"
            />
          ) : null}

          {bytes.kind === "ready" && audio && bytes.url ? (
            <audio
              data-testid="phone-media-audio"
              controls
              src={bytes.url}
              aria-label={`Audio part #${part.part_id}`}
              className="w-full"
            />
          ) : null}

          {!preview ? (
            <div className="space-y-1 text-center">
              {oversized ? (
                <p data-testid="phone-media-oversized" className="text-sm text-ink">
                  {`Part is ${mediaSizeText(part.size_bytes)} — above the ${MEDIA_PREVIEW_MAX_BYTES / (1024 * 1024)} MB inline preview limit; download it instead.`}
                </p>
              ) : (
                <p data-testid="phone-media-no-preview" className="text-sm text-ink">
                  This part has no inline preview — download it.
                </p>
              )}
              <Button
                size="sm"
                variant="bevel"
                aria-label={`Download part #${part.part_id}`}
                data-testid="phone-media-download"
                disabled={downloading}
                onClick={() => onDownload(part)}
              >
                {downloading ? "Downloading…" : "Download"}
              </Button>
            </div>
          ) : null}
        </div>

        <footer className="flex flex-wrap items-center gap-2 border-t border-line px-4 py-2">
          <Button
            size="sm"
            data-testid="phone-media-prev"
            aria-label="Previous media part"
            disabled={index === 0}
            onClick={() => onNavigate(index - 1)}
          >
            Previous
          </Button>
          <p role="status" data-testid="phone-media-counter" className="ui-label text-ink-muted">
            {`Part ${index + 1} of ${parts.length}`}
          </p>
          <Button
            size="sm"
            data-testid="phone-media-next"
            aria-label="Next media part"
            disabled={index === parts.length - 1}
            onClick={() => onNavigate(index + 1)}
          >
            Next
          </Button>
        </footer>
      </div>
    </div>
  );
}
