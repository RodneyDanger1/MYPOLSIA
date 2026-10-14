"use client";

import { useEffect, useState } from "react";

import { TaskBoard, TASKS_BOARD_LIMIT } from "@/components/tasks/TaskBoard";
import { TaskDetailSheet } from "@/components/tasks/TaskDetailSheet";
import { errorText } from "@/components/tasks/status";
import type { TaskRow } from "@/components/tasks/types";
import { Button } from "@/components/ui";
import { api } from "@/lib/api";

/**
 * Client component on purpose — kept from the page this replaces, because the lesson stands:
 *
 * This page used to be an async Server Component doing `await api.get(...)`. Next prerenders this route
 * (`○ (Static)` in the build output), so that fetch ran at BUILD time and the empty result was baked into
 * the HTML: the page showed its empty state forever, even while the API served rows. The repo's e2e run
 * caught it — `GET /api/v1/tasks` returned 11 tasks and the page still rendered "No tasks yet."
 *
 * The old `catch { return [] }` made it worse: a failed read became the words "no data", which is the one
 * thing this project forbids. Three states now, and they are different on purpose:
 *   loading  -> a skeleton with `aria-busy`, because "not read yet" is not "empty"
 *   error    -> the named failure (`ApiError.display`: code + how_to_fix), never empty copy
 *   empty    -> printed ONLY when the read succeeded and really returned nothing
 *
 * No polling here (tasks.md §3.9 / C7: live refreshes on explicit demand). The board fetches on
 * mount, offers an explicit refresh, and re-reads once after a write that changes it (a queued
 * run or a minted approval) — a one-shot refetch after a user action, not an auto-refresh loop.
 */
export default function TasksPage() {
  const [rows, setRows] = useState<TaskRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [reloadSeq, setReloadSeq] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    api
      .get<TaskRow[]>(`/tasks?limit=${TASKS_BOARD_LIMIT}`)
      .then((data) => {
        if (!cancelled) setRows(data);
      })
      .catch((e: unknown) => {
        // The previous rows (if any) stay on screen under the alert — they are real reads; the
        // alert names why THIS read failed. No silent catch, no empty-list stand-in.
        if (!cancelled) setError(errorText(e));
      });
    return () => {
      cancelled = true;
    };
  }, [reloadSeq]);

  const openTask = rows?.find((row) => row.id === openId) ?? null;

  return (
    <>
      <div aria-hidden={openTask ? true : undefined} className="space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-2 border-b border-line pb-2">
          <h1 className="text-3xl font-bold text-ink">Tasks</h1>
          <Button size="sm" onClick={() => setReloadSeq((n) => n + 1)}>
            Refresh
          </Button>
        </div>

        {error ? (
          <div role="alert" className="border border-alert bg-surface p-3">
            <p className="ui-label text-alert">Could not load tasks</p>
            <p className="mt-1 text-sm text-ink">{error}</p>
          </div>
        ) : null}

        {error === null && rows === null ? (
          <div aria-busy="true" className="space-y-2">
            <p className="ui-label text-ink-muted">Loading tasks…</p>
            <div className="grid grid-cols-1 gap-[14px] lg:grid-cols-2">
              {Array.from({ length: 4 }).map((_, index) => (
                <div key={index} className="h-[93px] animate-pulse border border-line bg-surface" />
              ))}
            </div>
          </div>
        ) : null}

        {error === null && rows !== null && rows.length === 0 ? (
          // Q-26: the sentence this repo already had — the tone the audit calls correct.
          <p className="ui-label text-ink-muted">
            No tasks yet. Agents will create tasks during their cycles.
          </p>
        ) : null}

        {rows !== null && rows.length > 0 ? (
          <>
            {rows.length === TASKS_BOARD_LIMIT ? (
              <p className="ui-label text-ink-muted">
                Showing the first {TASKS_BOARD_LIMIT} tasks — the API reports no total, so more may
                exist
              </p>
            ) : null}
            <TaskBoard rows={rows} onOpen={(task) => setOpenId(task.id)} />
          </>
        ) : null}
      </div>

      {openTask ? (
        <TaskDetailSheet
          task={openTask}
          onClose={() => setOpenId(null)}
          onTaskQueued={() => setReloadSeq((n) => n + 1)}
        />
      ) : null}
    </>
  );
}
