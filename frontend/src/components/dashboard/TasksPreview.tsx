"use client";

import { useEffect, useState } from "react";
import Link from "next/link";

import { Card, Chip } from "@/components/ui";
import { api, type Task } from "@/lib/api";

import { agentLabel, relativeTime } from "./format";
import { PanelError, PanelSkeleton, readErrorText } from "./states";

/**
 * The wire row this preview reads. `GET /tasks` answers with `TaskResponse`
 * (`backend/app/schemas/task.py`), which carries a nullable `description` the shared `Task` type
 * stops short of; reading it here is additive (no shared file is changed) and it is what the live
 * task card shows as its second line. Everything else is the shared row shape.
 */
type TaskRow = Task & { description?: string | null };

/**
 * How many rows the preview asks for — the live widget shows four. The API returns a bare list with
 * no total (`audit Q-12`), so the footer LABELS truncation ("showing the 4 newest — more may
 * exist") instead of inventing the live product's "+4 more tasks" count, which we cannot know.
 */
export const TASKS_PREVIEW_LIMIT = 4;

/**
 * Status text colours as a lookup with a fallback — the tasks-page idiom (the seven real values
 * come from the backend's `VALID_TASK_STATUSES`). A null or unrecognised status renders a neutral
 * chip reading "unknown", never an empty chip and never another status's colour. The 800-step
 * palette keeps every hue ≥ 4.5:1 on the chip's light fill.
 */
const STATUS_TEXT: Record<string, string> = {
  completed: "text-green-800",
  in_progress: "text-blue-800",
  pending: "text-amber-800",
  failed: "text-red-800",
  cancelled: "text-neutral-600",
  awaiting_approval: "text-orange-800",
  rejected: "text-rose-800",
};

/**
 * Middle column, top: the live `Tasks` widget's slot, fed by `GET /tasks` (newest first —
 * `(created_at DESC, id DESC)` is the endpoint's documented ordering).
 *
 * Cost display, honestly mapped: the live card's right-hand slot is `1 CREDIT`, and this repo has
 * no credit or per-task cost field anywhere on the wire — so no credit chip is drawn. The real
 * `priority` column takes that slot instead, with the em dash for a null and the row's relative age
 * beside it.
 */
export function TasksPreview() {
  const [rows, setRows] = useState<TaskRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // V24/L5: the failure's Retry re-issues THIS read (the panel had no way back before).
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setRows(null);
    setError(null);
    api
      .get<TaskRow[]>(`/tasks?limit=${TASKS_PREVIEW_LIMIT}`)
      .then((data) => {
        if (!cancelled) setRows(data);
      })
      .catch((e: unknown) => {
        if (!cancelled) setError(readErrorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const loading = rows === null && !error;

  return (
    <Card
      title="Tasks"
      action={
        <Link href="/tasks" className="ui-label text-link hover:underline">
          See all tasks
        </Link>
      }
    >
      {loading ? <PanelSkeleton rows={3} /> : null}
      {!loading && error ? (
        <PanelError
          label="tasks"
          message={error}
          onRetry={() => setAttempt((n) => n + 1)}
          retryTestId="tasks-preview-retry"
        />
      ) : null}
      {!loading && !error && rows && rows.length === 0 ? (
        <p className="text-sm text-ink-muted">
          No tasks yet. Agents add tasks during their cycles — trigger one from the Agents page,
          then check back.
        </p>
      ) : null}
      {!loading && !error && rows && rows.length > 0 ? (
        <div className="space-y-2">
          {rows.map((task) => (
            <TaskCard key={task.id} task={task} />
          ))}
          <p className="ui-label pt-1 text-ink-muted">
            {rows.length < TASKS_PREVIEW_LIMIT
              ? `Showing all ${rows.length} task${rows.length === 1 ? "" : "s"}`
              : `Showing the ${TASKS_PREVIEW_LIMIT} newest tasks — more may exist`}
          </p>
        </div>
      ) : null}
    </Card>
  );
}

/**
 * V7-T17: the preview card is a real LINK to the shipped deep link — `app/tasks/page.tsx`
 * (U4) consumes `?task=N` and opens that task's detail sheet. The live card's look is kept
 * byte-for-byte (the article keeps its exact classes; the anchor adds `block` only so the
 * click target keeps the article's box). An anchor and not an onClick handler, so
 * middle-click / open-in-new-tab get the real navigation semantics.
 */
function TaskCard({ task }: { task: TaskRow }) {
  const statusText = STATUS_TEXT[task.status ?? ""] ?? "text-ink-muted";
  return (
    <Link href={`/tasks?task=${task.id}`} className="block">
      <article className="border border-line bg-surface-sunk p-3">
        <h3 className="truncate text-base font-bold leading-snug text-ink">{task.title}</h3>
        {task.description ? (
          <p className="mt-1 line-clamp-2 text-sm text-ink-muted">- {task.description}</p>
        ) : null}
        <div className="mt-2 flex items-center justify-between gap-2">
          <span className="flex items-center gap-2">
            <Chip variant="dark">{agentLabel(task.agent_type)}</Chip>
            <Chip variant="soft" className={statusText}>
              {task.status ?? "unknown"}
            </Chip>
          </span>
          <span className="ui-label flex items-center gap-3 text-ink-muted">
            <span className="text-ink">Priority {task.priority ?? "—"}</span>
            <span>{relativeTime(task.created_at)}</span>
          </span>
        </div>
      </article>
    </Link>
  );
}
