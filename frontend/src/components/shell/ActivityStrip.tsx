"use client";

/**
 * The activity strip + expandable activity log (wave V8b / seat l1-strip-log; ui-gap §7).
 *
 * The strip is the THIRD view of the ONE activity stream (C13) — `useActivityFeed` is the
 * transport for all of them and is untouched here; this file only renders what the hook already
 * reports. It sits directly under the Topbar as one thin chrome band: transport status, the newest
 * event as one line, and a `LOG` affordance that expands the full log into a dialog.
 *
 * One file on purpose: the owner-revert contract is this file plus the two additive lines in
 * `app/layout.tsx`. Presentation vocabulary is mirrored from `components/dashboard/ActivityFeed.tsx`
 * (status words, level colours, the null "date unknown" time wording) — those class values
 * pre-exist in the app, so copying them is reuse, not a new colour. Zero new hex colours: the band
 * uses the `band`/`band-ink` tokens, the overlay uses the `ink`/`line`/`surface` tokens and the
 * `card-surface` recipe, and the interactive pieces reuse `components/ui` (Button, .chip recipes).
 *
 * Overlay grammar mirrors `components/tasks/TaskDetailSheet.tsx`: full-viewport scrim, centred
 * `role="dialog"` panel with `aria-modal`, focus moves in on open and RETURNS to the opener on
 * close, `Esc` closes. This one additionally closes on a scrim click (pinned by the V8b brief;
 * TaskDetailSheet deliberately omits it, and is not edited).
 *
 * ui-gap §7 acceptance carried here: filter chips by stage (ALL · one chip per distinct action
 * observed at open time · OTHER), an event whose action has no chip lands under OTHER and is never
 * silently hidden, COPY writes EXACTLY the rendered visible lines, DOWNLOAD writes the same text as
 * `polsia-activity.log`, and a dropped socket moves the strip's status word without a reload (that
 * reactivity is the hook's; this file renders `status` — including `RECONNECTING (ATTEMPT n)`).
 */
import { useEffect, useId, useMemo, useRef, useState } from "react";

import { Button, cn } from "@/components/ui";
import { useActivityFeed, type FeedStatus } from "@/hooks/useActivityFeed";
import type { ActivityEvent } from "@/lib/api";

/**
 * Transport pill classes — the EXACT values of `STATUS_PILL` in
 * `components/dashboard/ActivityFeed.tsx` (copied so the two views of the one stream cannot
 * drift; these classes already existed in the app).
 */
const STATUS_PILL: Record<FeedStatus, { dot: string; textClass: string }> = {
  connecting: { dot: "bg-[#8b8b8b]", textClass: "text-[#8b8b8b]" },
  live: { dot: "bg-green-600", textClass: "text-green-700" },
  reconnecting: { dot: "bg-amber-500", textClass: "text-amber-700" },
  offline: { dot: "bg-[#B00020]", textClass: "text-[#B00020]" },
};

/** The feed's own status words, verbatim (`pillText` in ActivityFeed.tsx). */
function pillText(status: FeedStatus, attempts: number): string {
  switch (status) {
    case "connecting":
      return "CONNECTING…";
    case "live":
      return "LIVE";
    case "reconnecting":
      return attempts > 0 ? `RECONNECTING (ATTEMPT ${attempts})` : "RECONNECTING";
    case "offline":
      return "OFFLINE";
  }
}

/**
 * Level tag colours — the EXACT `LEVEL_LABEL` / `FALLBACK_LABEL` values from ActivityFeed.tsx.
 * `level` is an open string on the wire (`app/schemas/report.py` types it `str`), so an unknown
 * word keeps its own text and falls through to the gray fallback — never treated as impossible.
 */
const LEVEL_LABEL: Record<string, string> = {
  success: "text-green-700",
  error: "text-red-700",
  warning: "text-amber-700",
  info: "text-blue-700",
};

const FALLBACK_LABEL = "text-gray-500";

/** Measured scrim, one token for every sheet (`components/tasks/TaskDetailSheet.tsx` §1). */
const SCRIM = "rgba(0, 0, 0, 0.55)";

/** How long the transient `COPIED` confirmation stays up. */
export const COPY_CONFIRM_MS = 1500;

/** The download's file name (ui-gap §7 acceptance). */
export const DOWNLOAD_FILENAME = "polsia-activity.log";

/** Chip labels the row set reserves for itself; an action literally named `ALL`/`OTHER` would
 *  collide with them, so such an action gets no chip and its events live under OTHER (visible,
 *  never hidden). */
const RESERVED_CHIPS = new Set(["ALL", "OTHER"]);

/** The feed's own time formatting (`clockText` in ActivityFeed.tsx): `2:36 PM`, and the app-wide
 *  honest placeholder for a null/invalid wire timestamp — never `new Date(null)` (1/1/1970). */
function clockText(iso: string | null): string {
  if (!iso) return "date unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "date unknown";
  return d.toLocaleTimeString("en-US", { hour: "numeric", minute: "2-digit" });
}

/** The level tag word. A missing/blank level renders as "UNKNOWN" in the gray fallback — the same
 *  honesty convention as the app-wide "date unknown", never a fabricated level. */
function levelWord(level: string): string {
  return typeof level === "string" && level.trim().length > 0 ? level.toUpperCase() : "UNKNOWN";
}

type LineSegment = { key: string; text: string; className: string };

/**
 * A log row IS its line, split into styled segments. `lineOf` is the single source for both the
 * rendered text (concatenated spans — no gaps, no separators in the DOM) and the COPY/DOWNLOAD
 * string, so "the copied text equals the visible lines" holds by construction.
 */
function lineSegments(e: ActivityEvent): LineSegment[] {
  return [
    { key: "time", text: clockText(e.created_at), className: "text-ink-muted" },
    { key: "sep-1", text: " · ", className: "text-ink-muted" },
    { key: "action", text: e.action, className: "uppercase tracking-wider text-ink" },
    { key: "sep-2", text: " — ", className: "text-ink-muted" },
    { key: "summary", text: e.summary, className: "text-ink" },
    { key: "sep-3", text: " · ", className: "text-ink-muted" },
    {
      key: "level",
      text: levelWord(e.level),
      className: cn("uppercase tracking-wider", LEVEL_LABEL[e.level] ?? FALLBACK_LABEL),
    },
  ];
}

function lineOf(e: ActivityEvent): string {
  return lineSegments(e)
    .map((s) => s.text)
    .join("");
}

/** Distinct, non-blank, non-reserved action values — the stage chips, sorted. */
function distinctActions(events: ActivityEvent[]): string[] {
  const out = new Set<string>();
  for (const e of events) {
    const action = e?.action ?? "";
    if (typeof action !== "string" || action.trim().length === 0) continue;
    if (RESERVED_CHIPS.has(action)) continue;
    out.add(action);
  }
  return Array.from(out).sort((a, b) => a.localeCompare(b));
}

function message(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function ActivityStrip() {
  // The ONE stream (C13): the hook owns the socket; this component only renders its report.
  const { events, status, error, attempts } = useActivityFeed();

  const [open, setOpen] = useState(false);
  const [filter, setFilter] = useState<string>("ALL");
  /** The chips are frozen from the events observed AT OPEN TIME; events that arrive later with a
   *  new action simply have no chip and show under OTHER. */
  const [chipActions, setChipActions] = useState<string[]>([]);
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState<string | null>(null);

  const titleId = useId();
  const logButtonRef = useRef<HTMLButtonElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);
  const copiedTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const pill = STATUS_PILL[status];
  const latest = events.length > 0 ? events[0] : null;

  // Focus grammar of TaskDetailSheet.tsx: capture where focus was, move it into the dialog, and
  // hand it back on close. The LOG button takes focus on a real click; falling back to it keeps
  // the return honest even when the opener was activated by an assistive path that never focused.
  useEffect(() => {
    if (!open) return;
    const active = document.activeElement;
    openerRef.current =
      active instanceof HTMLElement && active !== document.body ? active : logButtonRef.current;
    closeButtonRef.current?.focus();
    return () => {
      openerRef.current?.focus();
    };
  }, [open]);

  useEffect(
    () => () => {
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
    },
    [],
  );

  const chipSet = useMemo(() => new Set(chipActions), [chipActions]);

  const shown = useMemo(() => {
    if (filter === "ALL") return events;
    if (filter === "OTHER") return events.filter((e) => !chipSet.has(e.action));
    return events.filter((e) => e.action === filter);
  }, [events, filter, chipSet]);

  const visibleText = shown.map(lineOf).join("\n");
  const chips = ["ALL", ...chipActions, "OTHER"];

  const openOverlay = () => {
    setChipActions(distinctActions(events));
    setFilter("ALL");
    setCopyError(null);
    setCopied(false);
    setOpen(true);
  };

  const closeOverlay = () => setOpen(false);

  const toggleLog = () => {
    if (open) closeOverlay();
    else openOverlay();
  };

  /**
   * COPY — `navigator.clipboard.writeText` of EXACTLY the visible filtered lines (ui-gap §7).
   * Where the clipboard is refused (insecure context / no user activation), the failure is named
   * with a how_to_fix rather than silently doing nothing.
   */
  const copy = async () => {
    setCopyError(null);
    try {
      if (
        typeof navigator === "undefined" ||
        !navigator.clipboard ||
        typeof navigator.clipboard.writeText !== "function"
      ) {
        throw new Error("navigator.clipboard.writeText is unavailable in this context");
      }
      await navigator.clipboard.writeText(visibleText);
      setCopied(true);
      if (copiedTimer.current) clearTimeout(copiedTimer.current);
      copiedTimer.current = setTimeout(() => setCopied(false), COPY_CONFIRM_MS);
    } catch (e) {
      setCopied(false);
      setCopyError(
        `${message(e)} — how_to_fix: use DOWNLOAD; the clipboard needs a user gesture and a secure (https or localhost) context.`,
      );
    }
  };

  /** DOWNLOAD — the same text as a text blob named `polsia-activity.log`, objectURL revoked after. */
  const download = () => {
    const blob = new Blob([visibleText], { type: "text/plain" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = DOWNLOAD_FILENAME;
    document.body.appendChild(anchor);
    anchor.click();
    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
  };

  return (
    <section
      role="region"
      aria-label="Activity strip"
      data-testid="activity-strip"
      data-status={status}
      className="chrome-band border-b border-line-strong font-mono text-[10px] uppercase tracking-wider"
    >
      <div className="mx-auto flex w-full max-w-[1600px] items-center gap-3 px-4 py-1.5 text-band-ink">
        <span role="status" className="flex shrink-0 items-center gap-1.5">
          <span aria-hidden="true" className={cn("h-2 w-2 rounded-full", pill.dot)} />
          <span className={pill.textClass}>{pillText(status, attempts)}</span>
        </span>

        {latest ? (
          <span data-testid="strip-latest" className="min-w-0 flex-1 truncate">
            {`> ${latest.action} — ${latest.summary}`}
          </span>
        ) : error ? null : status === "live" ? (
          <span data-testid="strip-empty" className="min-w-0 flex-1 truncate">
            Waiting for activity…
          </span>
        ) : null}

        {error ? (
          <span
            role="alert"
            data-testid="strip-error"
            className="min-w-0 flex-1 truncate text-band-ink"
          >
            {`! ${error}`}
          </span>
        ) : null}

        <Button
          ref={logButtonRef}
          size="sm"
          data-testid="strip-log"
          aria-haspopup="dialog"
          aria-expanded={open}
          className="ml-auto shrink-0"
          onClick={toggleLog}
        >
          Log
        </Button>
      </div>

      {open ? (
        <div
          data-testid="log-scrim"
          className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4"
          style={{ backgroundColor: SCRIM }}
          onClick={(e) => {
            if (e.target === e.currentTarget) closeOverlay();
          }}
        >
          <div
            ref={dialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby={titleId}
            data-testid="activity-log"
            className="card-surface my-4 flex w-full max-w-[637px] flex-col shadow-2xl"
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                closeOverlay();
                return;
              }
              // Tab is trapped inside the modal: focus must not walk into the page behind it.
              if (e.key !== "Tab") return;
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
              if (e.shiftKey) {
                if (active === first || !panel.contains(active)) {
                  e.preventDefault();
                  last.focus();
                }
              } else if (active === last || !panel.contains(active)) {
                e.preventDefault();
                first.focus();
              }
            }}
          >
            <header className="flex items-center gap-2 border-b border-line px-4 py-2">
              <h2 id={titleId} className="ui-label text-ink">
                Activity log
              </h2>
              <Button
                ref={closeButtonRef}
                size="sm"
                data-testid="log-close"
                aria-label="Close activity log"
                className="ml-auto"
                onClick={closeOverlay}
              >
                ✕
              </Button>
            </header>

            <div
              data-testid="log-chips"
              className="flex flex-wrap items-center gap-1.5 border-b border-line px-4 py-2"
            >
              {chips.map((chip) => (
                <button
                  key={chip}
                  type="button"
                  data-testid={`chip-${chip}`}
                  aria-pressed={filter === chip}
                  className={cn("chip", filter === chip ? "chip-dark" : "chip-soft")}
                  onClick={() => setFilter(chip)}
                >
                  {chip}
                </button>
              ))}
            </div>

            <div className="flex flex-wrap items-center gap-2 border-b border-line px-4 py-2">
              <span data-testid="log-counter" className="ui-label text-ink-muted">
                {`${shown.length} OF ${events.length}`}
              </span>
              {copied ? (
                <span role="status" data-testid="log-copied" className="ui-label text-green-700">
                  COPIED
                </span>
              ) : null}
              <Button
                size="sm"
                data-testid="log-copy"
                className="ml-auto"
                onClick={() => void copy()}
              >
                Copy
              </Button>
              <Button size="sm" data-testid="log-download" onClick={download}>
                Download .log
              </Button>
            </div>

            {copyError ? (
              <p
                role="alert"
                data-testid="log-copy-error"
                className="border-b border-alert px-4 py-2 font-mono text-[10px] text-alert"
              >
                {copyError}
              </p>
            ) : null}

            <div className="max-h-[70vh] overflow-y-auto">
              {shown.length === 0 ? (
                <p data-testid="log-empty" className="px-4 py-3 font-serif text-sm text-ink-muted">
                  No events match this filter.
                </p>
              ) : (
                <ul data-testid="log-rows">
                  {shown.map((e, i) => (
                    <li
                      key={String(e?.id ?? `${e?.created_at}-${i}`)}
                      data-testid="log-row"
                      data-action={e.action}
                      data-level={e.level}
                      className="border-b border-line px-4 py-1.5 font-mono text-[10px] leading-relaxed last:border-b-0"
                    >
                      {lineSegments(e).map((segment) => (
                        <span key={segment.key} className={segment.className}>
                          {segment.text}
                        </span>
                      ))}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          </div>
        </div>
      ) : null}
    </section>
  );
}
