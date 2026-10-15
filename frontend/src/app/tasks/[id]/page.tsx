"use client";

import { useEffect, useRef, useState } from "react";

import { TaskDetailContent } from "@/components/tasks/TaskDetailContent";
import { errorText } from "@/components/tasks/status";
import type { TaskRow } from "@/components/tasks/types";
import { api } from "@/lib/api";

import { parseTaskId } from "./parseTaskId";

/**
 * `/tasks/[id]` — the addressable task detail (wave V11 unit 2; the wiring audit's F6: the
 * detail existed only as an overlay reached from the board, so no URL could name one task).
 *
 * The read is the real one: `GET /api/v1/tasks/{id}` (`backend/app/api/v1/tasks.py:113-128`),
 * whose only named refusal is the E_NOT_FOUND envelope (`{code, message, how_to_fix, context}`
 * under `detail`) for a missing row — rendered, never smoothed over. The detail BODY is
 * `TaskDetailContent`, the same component the board's sheet renders, so the two surfaces
 * cannot fork (fields, run truth, recovery grammar, status write).
 *
 * States, per the house rule (no silent catches): loading -> skeleton with `aria-busy`;
 * error -> the named failure under `role="alert"`, with the `‹ All tasks` way back; success ->
 * the detail. A `[id]` that is not a positive integer is NEVER requested — the page says what
 * the route expects instead of issuing a request whose 404 would misstate the problem.
 * Navigation uses plain `<a>` (a full page load): this route works from any render context and
 * next/link's router subtleties buy nothing for a two-link header.
 */

export default function TaskDetailPage({ params }: { params: { id: string } }) {
  const taskId = parseTaskId(params?.id);
  const [row, setRow] = useState<TaskRow | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [reloadSeq, setReloadSeq] = useState(0);
  const lastIdRef = useRef<number | null>(null);

  useEffect(() => {
    if (taskId === null) {
      setRow(null);
      setError(null);
      return;
    }
    let cancelled = false;
    if (lastIdRef.current !== taskId) {
      // A different task: back through "not read yet" (the skeleton is the one honest way to
      // say the view is being replaced). A REFRESH of the same id keeps the previous read on
      // screen while it re-reads.
      setRow(null);
      setError(null);
      lastIdRef.current = taskId;
    }
    api
      .get<TaskRow>(`/tasks/${taskId}`)
      .then((data) => {
        if (!cancelled) setRow(data);
      })
      .catch((e: unknown) => {
        // The previous row (if any) stays on screen under the alert — it is a real read; the
        // alert names why THIS read failed. No silent catch, no empty-detail stand-in.
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [taskId, reloadSeq]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-2 border-b border-line pb-2">
        <a href="/tasks" className="ui-label text-link hover:underline">
          <span aria-hidden>‹</span> All tasks
        </a>
        {taskId !== null ? (
          <span className="ui-label text-ink-muted">Task #{taskId}</span>
        ) : null}
        <button
          type="button"
          className="link ui-label ml-auto"
          onClick={() => setReloadSeq((n) => n + 1)}
          disabled={taskId === null}
        >
          Refresh
        </button>
      </div>

      {taskId === null ? (
        <div role="alert" className="border border-alert bg-surface p-3">
          <p className="ui-label text-alert">Not a task id</p>
          <p className="mt-1 text-sm text-ink">
            This route takes one numeric task id — the address bar carries &ldquo;{params?.id ?? ""}
            &rdquo;. Nothing was requested; open a task from{" "}
            <a href="/tasks" className="link">
              the tasks board
            </a>{" "}
            instead.
          </p>
        </div>
      ) : null}

      {taskId !== null && error ? (
        <div role="alert" className="border border-alert bg-surface p-3">
          <p className="ui-label text-alert">Could not load task</p>
          <p className="mt-1 text-sm text-ink">{error}</p>
        </div>
      ) : null}

      {taskId !== null && row === null && error === null ? (
        <div aria-busy="true" className="space-y-2" data-testid="task-detail-loading">
          <p className="ui-label text-ink-muted">Loading task…</p>
          <div className="card-surface px-4 py-3">
            <div className="h-5 w-2/3 animate-pulse bg-surface-strong" />
            <div className="mt-3 h-3 w-1/3 animate-pulse bg-surface-strong" />
            <div className="mt-2 h-3 w-1/2 animate-pulse bg-surface-strong" />
          </div>
        </div>
      ) : null}

      {taskId !== null && row !== null ? (
        <div className="card-surface px-4 py-3">
          <TaskDetailContent task={row} onTaskQueued={() => setReloadSeq((n) => n + 1)} />
        </div>
      ) : null}
    </div>
  );
}
