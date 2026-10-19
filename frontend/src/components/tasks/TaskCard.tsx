"use client";

import { useEffect, useRef, useState } from "react";

import { absoluteStamp, relativeTime } from "@/components/dashboard/format";
import { readErrorText } from "@/components/dashboard/states";
import { Button, Chip, cn } from "@/components/ui";

import { runThisTask, triggerTaskRun } from "./actions";
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
  onTaskQueued,
}: {
  task: TaskRow;
  /** The newest TO DO row (see `TaskBoard` — the "next task" outline is an INFERENCE). */
  isNext: boolean;
  onOpen: (task: TaskRow) => void;
  /** Called after a write that changes the board (V24: a dispatched run) — the page re-reads. */
  onTaskQueued?: () => void;
}) {
  const done = task.status === "completed";
  const pending = task.status === "pending";
  const recovery = recoveryFor(task);
  const [runBusy, setRunBusy] = useState(false);
  const [runNotice, setRunNotice] = useState<{ tone: "status" | "alert"; text: string } | null>(null);
  /**
   * Latch the V24 "Run this task" action after a 202. The board re-reads immediately, but the
   * server's answer can still be `pending` until a worker claims the row — re-dispatching in
   * that window would enqueue the same task twice. The latch clears only when the row LEAVES
   * pending (a real state change) or when a refusal proves nothing was dispatched.
   */
  const [runThisLatched, setRunThisLatched] = useState(false);
  const [runThisBusy, setRunThisBusy] = useState(false);

  useEffect(() => {
    if (!pending) setRunThisLatched(false);
  }, [pending]);
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

  /**
   * V24/L1 — run THIS row. `POST /tasks/{id}/run` returns 202 `{task_id, status:"dispatched"}`
   * or a named refusal (404 / 409 E_TASK_NOT_RUNNABLE / 503). The success text quotes only
   * what the server said: the broker accepted the dispatch, and the status will move when a
   * worker claims the row — the card never prints `in_progress` on its own. `onTaskQueued`
   * triggers the board's ONE refetch path so the next read is the server's, not an invention.
   */
  const runThis = async () => {
    if (runThisBusy || runThisLatched) return;
    setRunThisBusy(true);
    setRunNotice(null);
    try {
      const result = await runThisTask(task);
      setRunThisLatched(true);
      setRunNotice({
        tone: "status",
        text: `Task #${result.task_id} ${result.status} — the worker will claim it; the status here is still the server’s.`,
      });
      onTaskQueued?.();
    } catch (e) {
      // Nothing was dispatched: the latch stays off so a retry is honest. `readErrorText` is
      // the shared refusal renderer (`E_TASK_NOT_RUNNABLE: … — how_to_fix`), not a rewrite.
      setRunNotice({ tone: "alert", text: readErrorText(e) });
    } finally {
      setRunThisBusy(false);
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
        <span className={cn("flex w-full items-start gap-2", pending && "pr-28")}>
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

      {/* V24/L1 — "Run this task": the action that executes THIS queued row. It lives OUTSIDE
          the card button (invalid DOM to nest) and outside the V7 quick-action row (whose
          labels the shipped suites pin), anchored top-right with the title yielding space.
          Pending only: no other state has anything to dispatch. Default (non-glossy) variant
          on purpose — the board itself carries no primary action. */}
      {pending ? (
        <div data-testid="task-run-this" className="absolute right-2 top-2">
          <Button
            size="sm"
            aria-busy={runThisBusy || undefined}
            disabled={runThisBusy || runThisLatched}
            onClick={runThis}
          >
            {runThisBusy ? "Dispatching…" : "Run this task"}
          </Button>
        </div>
      ) : null}

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
