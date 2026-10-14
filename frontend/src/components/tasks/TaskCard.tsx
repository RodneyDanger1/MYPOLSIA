import { Chip } from "@/components/ui";
import { absoluteStamp, relativeTime } from "@/components/dashboard/format";

import { statusChipClass, statusLabel } from "./status";
import type { TaskRow } from "./types";

/**
 * The board's task card — the live product's most repeated unit (DOSSIER §3.4): one clickable
 * region, title on one ellipsised line, description clamped to two lines, a chip footer and a
 * `View →` affordance. The card's semantics are the OVERLAY wording (`Open task: <title>` —
 * DOSSIER §3.4: board cards open the detail; the dashboard's `Run task:` cards are a different
 * affordance and are not part of this page).
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
  return (
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
        <span className="ui-label text-ink-muted">View →</span>
      </span>
    </button>
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
