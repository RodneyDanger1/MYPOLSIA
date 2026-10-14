"use client";

import { useRef, useState } from "react";

import { absoluteStamp, relativeTime } from "@/components/dashboard/format";
import { Button, Chip, cn } from "@/components/ui";

import { triggerTaskRun } from "./actions";
import { recoveryFor } from "./recovery";
import { errorText, statusChipClass, statusLabel } from "./status";
import type { TaskRow } from "./types";

/**
 * The board's task card — the live product's most repeated unit (DOSSIER §3.4): one clickable
 * region, title on one ellipsised line, description clamped to two lines, a chip footer and a
 * `View →` affordance. The card's semantics are the OVERLAY wording (`Open task: <title>` —
 * DOSSIER §3.4: board cards open the detail; the dashboard's `Run task:` cards are a different
 * affordance and are not part of this page).
 *
 * V7-T16 adds the hover/focus QUICK-ACTION ROW. The recipe (recorded here — no hover-actions
 * idiom existed in this tree before it; M2 §1.4's precedent for recording a house recipe when
 * none exists):
 *
 *   * wrapper — `group relative` around the UNTOUCHED card button (aria-label, click-to-open
 *     and the `isNext` outline all stay exactly as pinned);
 *   * row — a SIBLING of the card button, absolutely positioned over the footer's right side
 *     (where `View →` sits). Nested inside the button it would be invalid DOM and every chip
 *     click would bubble into the sheet open; absolute + right-anchored means the reveal can
 *     never shift layout;
 *   * reveal — opacity only: `opacity-0` + `group-hover:opacity-100` +
 *     `group-focus-within:opacity-100` + `transition-opacity`. jsdom cannot hover, so
 *     `u7-task-actions` pins the class recipe; keyboard users reach the chips by Tab and the
 *     row stays visible while focus is anywhere inside the card;
 *   * swap — `View →` fades under the same triggers so the two affordances never double-render;
 *   * contents — from `recoveryFor(task)` ONLY (B3's single grammar): always an `Open` chip
 *     (opens the sheet via the existing `onOpen`), plus at most one action chip — the run chip
 *     (`Recovery.label` verbatim, executing the shared `triggerTaskRun` request) or `Review
 *     approval` (which routes to the sheet — the fingerprint-scoped mint genuinely lives
 *     there). `kind: "none"` renders NO action chip: absence is the honest render of a state
 *     with no endpoint behind it.
 *
 * The run chip's outcome contract: in flight it is disabled with an ellipsis suffix; on
 * success the server's message renders inline in the row and the chip STAYS disabled (each
 * trigger CREATES a task — a double-click must never create two); a refusal renders
 * `ApiError.display` (code + how_to_fix) and re-arms the chip, because a refusal created
 * nothing. Once an outcome exists the row pins itself visible (`opacity-100`) — a disabled
 * chip cannot hold focus, and hiding a server response behind hover would be a silent failure.
 */
export function TaskCard({
  task,
  isNext,
  onOpen,
}: {
  task: TaskRow;
  /** The newest TO DO row (see `TaskBoard` — the "next task" outline is an INFERENCE). */
  isNext: boolean;
  onOpen: (task: TaskRow) => void;
}) {
  const done = task.status === "completed";
  const recovery = recoveryFor(task);
  const [runBusy, setRunBusy] = useState(false);
  const [runNotice, setRunNotice] = useState<{ tone: "status" | "alert"; text: string } | null>(null);
  /**
   * Latches ONE trigger per card instance after a success. The ref — not just the `disabled`
   * attribute — is the real guard: two clicks inside one tick cannot slip past a re-render.
   * Reset only on a refusal, where nothing was created and a retry is honest.
   */
  const runLatched = useRef(false);

  const runNow = async () => {
    if (runLatched.current || runBusy) return;
    runLatched.current = true;
    setRunBusy(true);
    setRunNotice(null);
    try {
      const result = await triggerTaskRun(task);
      setRunNotice({ tone: "status", text: result.message });
    } catch (e) {
      runLatched.current = false;
      setRunNotice({ tone: "alert", text: errorText(e) });
    } finally {
      setRunBusy(false);
    }
  };

  return (
    <div className="group relative">
      <button
        type="button"
        aria-label={`Open task: ${task.title}`}
        onClick={() => onOpen(task)}
        data-next={isNext ? "true" : undefined}
        /* Live's "next/new" highlight (DOSSIER §3.2 #5): an orange outline ≈#ec6f2b, no fill
           change. The accent token `--accent-soft` is that measured outline colour. */
        style={isNext ? { outline: "2px solid hsl(var(--accent-soft))", outlineOffset: "1px" } : undefined}
        className="card-surface block min-h-[93px] w-full p-3 text-left hover:border-line-strong"
      >
        <span className="flex w-full items-start gap-2">
          <span className="min-w-0 truncate text-base font-bold leading-snug text-ink">{task.title}</span>
          {done ? (
            // The DONE ✓-in-a-square at the title row (DOSSIER §3.3 / cap-05).
            <span
              aria-hidden
              className="ml-auto shrink-0 border border-line px-1 text-xs leading-none text-ink-muted"
            >
              ✓
            </span>
          ) : null}
        </span>
        {/* A null description renders nothing at all — no invented copy (tasks.md §2). */}
        {task.description ? (
          <span className="mt-1 line-clamp-2 block text-sm text-ink-muted">{task.description}</span>
        ) : null}
        <span className="mt-2 flex items-center gap-2">
          {/* The REAL `agent_type` as the chip (tasks.md OPEN DECISION #3): live's FEATURE/RESEARCH
              vocabulary has no backend field, so an invented kind label is not allowed. */}
          <Chip variant="dark">{task.agent_type}</Chip>
          <Chip variant="soft" className={statusChipClass(task.status)}>
            {statusLabel(task.status)}
          </Chip>
          <span className="ui-label ml-auto text-ink-muted">{ageChip(task)}</span>
          {/* Yields its spot while the quick-action row is revealed (same triggers). */}
          <span
            className={cn(
              "ui-label text-ink-muted transition-opacity",
              runNotice === null
                ? "group-hover:opacity-0 group-focus-within:opacity-0"
                : "opacity-0",
            )}
          >
            View →
          </span>
        </span>
      </button>

      {/* The quick-action row (V7-T16) — a sibling of the card button, per the recipe above. */}
      <div
        data-testid="task-actions"
        className={cn(
          "absolute inset-x-3 bottom-3 flex flex-col items-end gap-1 transition-opacity",
          runNotice === null
            ? "opacity-0 group-hover:opacity-100 group-focus-within:opacity-100"
            : "opacity-100",
        )}
      >
        {runNotice ? (
          <p
            role={runNotice.tone === "alert" ? "alert" : "status"}
            data-tone={runNotice.tone}
            className={cn(
              "ui-label max-w-full bg-surface px-1 text-right",
              runNotice.tone === "alert" ? "text-alert" : "text-ink",
            )}
          >
            {runNotice.text}
          </p>
        ) : null}
        <span className="flex items-center gap-1">
          <Button size="sm" onClick={() => onOpen(task)}>
            Open
          </Button>
          {recovery.kind === "run" ? (
            <Button size="sm" disabled={runBusy || runLatched.current} onClick={runNow}>
              {runBusy ? `${recovery.label}…` : recovery.label}
            </Button>
          ) : null}
          {recovery.kind === "approve" ? (
            // Fingerprint-scoped approval is the sheet's job — this card only routes there.
            <Button size="sm" onClick={() => onOpen(task)}>
              Review approval
            </Button>
          ) : null}
        </span>
      </div>
    </div>
  );
}

/**
 * Age chip — DONE cards only (cap-05). `completed_at` arrives this wave (B1) and when it is
 * present the chip is the real completion age; when it is absent the chip quotes the CREATED age
 * and says so in its title, so a creation time is never presented as a completion time
 * (tasks.md GAP-NO-FIELD rule). Neither date present → no chip at all.
 */
function ageChip(task: TaskRow) {
  if (task.status !== "completed") return null;
  if (task.completed_at) {
    return <span title={`completed ${absoluteStamp(task.completed_at)}`}>{relativeTime(task.completed_at)}</span>;
  }
  if (task.created_at) {
    return <span title={`created ${absoluteStamp(task.created_at)}`}>created {relativeTime(task.created_at)}</span>;
  }
  return null;
}
