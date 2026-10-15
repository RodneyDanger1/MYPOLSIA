"use client";

import { useEffect, useId, useRef } from "react";

import { Button } from "@/components/ui";

import { TaskDetailContent } from "./TaskDetailContent";
import type { TaskRow } from "./types";

/**
 * Measured scrim (sheets-and-composer.md §1): ≈55% black, ONE token for every sheet. The two
 * live measurements read ~50% and ~55%; the cap-12 "not dimmed" exception is deliberately not
 * copied. Exported for the other V11 dialog (`NewTaskDialog`) so the token stays one value.
 */
export const SCRIM = "rgba(0, 0, 0, 0.55)";

/**
 * The task detail overlay (SURFACE 4 → our route-adapted sheet), built to the conventions in
 * sheets-and-composer.md §1: white panel (max 637px — the measured live width), 1px border,
 * shadow, centred over a full-viewport scrim; `role="dialog"` + `aria-modal`; focus moves into
 * the sheet and returns to the control that opened it (B17); `✕` and `Esc` close. Esc and
 * focus-return are OUR additions (the live captures only prove the ✕); scrim-click-to-close is
 * deliberately absent (OPEN DECISION #2 — an accidental dismissal of a data view is worse).
 *
 * Wave V11 unit 2: the sheet's BODY (every field, action and notice) moved to
 * `TaskDetailContent` so the new `/tasks/[id]` route renders the identical detail — this file
 * keeps only the shell (scrim, dialog, focus, header). The header gains an `Open full page`
 * anchor (plain `<a>`, not next/link: the sheet renders in any context and the modal needs a
 * full navigation, not client-side routing subtleties).
 */
export function TaskDetailSheet({
  task,
  onClose,
  onTaskQueued,
}: {
  task: TaskRow;
  onClose: () => void;
  /** Called after a write that changes the board (a queued run / a minted approval / a set status). */
  onTaskQueued?: () => void;
}) {
  const titleId = useId();
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const openerRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    openerRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    closeButtonRef.current?.focus();
    return () => {
      openerRef.current?.focus();
    };
  }, []);

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto p-4"
      style={{ backgroundColor: SCRIM }}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        onKeyDown={(e) => {
          if (e.key === "Escape") onClose();
        }}
        className="card-surface my-4 w-full max-w-[637px] shadow-2xl"
      >
        <header className="flex items-center gap-2 border-b border-line px-4 py-2">
          <button type="button" className="ui-label text-link hover:underline" onClick={onClose}>
            {/* Breadcrumb `‹ ALL TASKS` (SURFACE 4) — on a route this is the sheet's back control. */}
            <span aria-hidden>‹</span> All tasks
          </button>
          {/* The per-task route (V11): the same detail, addressable. */}
          <a href={`/tasks/${task.id}`} className="ui-label text-link hover:underline">
            Open full page
          </a>
          <Button
            ref={closeButtonRef}
            size="sm"
            aria-label="Close task detail"
            className="ml-auto"
            onClick={onClose}
          >
            ✕
          </Button>
        </header>

        <div className="max-h-[70vh] overflow-y-auto px-4 py-3">
          <TaskDetailContent task={task} onTaskQueued={onTaskQueued} titleId={titleId} />
        </div>
      </div>
    </div>
  );
}
