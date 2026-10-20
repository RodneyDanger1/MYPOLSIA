"use client";

import { Suspense, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";

import { TaskBoard, TASKS_BOARD_LIMIT } from "@/components/tasks/TaskBoard";
import { TaskDetailSheet } from "@/components/tasks/TaskDetailSheet";
import { NewTaskDialog } from "@/components/tasks/NewTaskDialog";
import { errorText } from "@/components/tasks/status";
import type { TaskRow } from "@/components/tasks/types";
import { Button } from "@/components/ui";
import { ApiError, api } from "@/lib/api";

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
 *
 * Wave U4: the page consumes the bell's `?task=N` deep link it has always been pointed at. The
 * param names a task id; once the rows arrive, that task's detail sheet opens — the same sheet
 * the board's own cards open. Wave V15 (S2-F4) makes it work BEYOND the loaded window: an id
 * outside the newest `TASKS_BOARD_LIMIT` rows is read by id (`GET /tasks/{id}` — the route the
 * backend has always served) and opens from that answer with a labelled context note; a 404 or
 * any failed read renders a NAMED miss — never a silent nothing. A junk param is still the
 * documented no-op: nothing is requested, nothing is claimed. A `consumed` ref marks the exact
 * param value once it has been acted on, so an unrelated refresh never re-opens a sheet the
 * user closed, while a NEW param value (a bell click landing on this route) opens the newly
 * requested task. The default export is a thin `Suspense` boundary: Next 14 requires one above
 * `useSearchParams` in a statically-rendered route, and its fallback IS the loading state this
 * page already renders while the rows are not read yet.
 *
 * Wave V11 unit 2: the `New task` control (wiring audit F7) opens `NewTaskDialog`, which POSTs
 * the real create schema; a 201 triggers the same one-shot refetch a queued run does, so the
 * new row appears in the board without inventing one optimistically. The status write lives on
 * the detail surfaces (sheet + `/tasks/[id]`) and reaches this board through the same
 * `onTaskQueued` refetch.
 */

/** `/tasks?task=N` — digits only, a positive safe integer; everything else is "no request". */
function parseTaskParam(raw: string | null): number | null {
  if (raw === null || !/^\d+$/.test(raw)) return null;
  const value = Number(raw);
  return Number.isSafeInteger(value) && value > 0 ? value : null;
}

/** The deep link's OFF-BOARD state: the requested id is being read by id, its fetched row, or
 *  the named miss. `null` = the loaded rows carried (or last carried) the request. */
type OffBoardOpen =
  | { kind: "fetching"; id: number }
  | { kind: "found"; row: TaskRow }
  | { kind: "miss"; id: number; notFound: boolean; message: string };

export default function TasksPage() {
  return (
    <Suspense fallback={<TasksLoading />}>
      <TasksPageContent />
    </Suspense>
  );
}

/** The not-read-yet state — shared by the Suspense fallback and the in-page rows===null state. */
function TasksLoading() {
  return (
    <div aria-busy="true" className="space-y-2">
      <p className="ui-label text-ink-muted">Loading tasks…</p>
      <div className="grid grid-cols-1 gap-[14px] lg:grid-cols-2">
        {Array.from({ length: 4 }).map((_, index) => (
          <div key={index} className="h-[93px] animate-pulse border border-line bg-surface" />
        ))}
      </div>
    </div>
  );
}

function TasksPageContent() {
  const [rows, setRows] = useState<TaskRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  /** The server's declared full count from the board read's `X-Polsia-Total` (same response as
   *  the rows). null = the response carried no readable header; the cap line then says only what
   *  the rows prove, never an invented total. */
  const [boardTotal, setBoardTotal] = useState<number | null>(null);
  const [openId, setOpenId] = useState<number | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [reloadSeq, setReloadSeq] = useState(0);
  const [offBoard, setOffBoard] = useState<OffBoardOpen | null>(null);
  /** V29/L2: a failed by-id read is retryable. Bumping this re-runs the by-id effect even when the
   *  current state is the miss (the effect is keyed to `fetchingId`, which is null for a miss), so
   *  Retry re-issues the read instead of being a no-op. */
  const [retrySeq, setRetrySeq] = useState(0);

  // `useSearchParams` answers null where no provider is mounted (the hook's own source says so;
  // several suites render this page unmocked) — that is "no param in the URL", never a crash.
  const searchParams = useSearchParams();
  const rawTaskParam = searchParams ? searchParams.get("task") : null;
  const requestedTaskId = parseTaskParam(rawTaskParam);
  const consumedParam = useRef<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    // A new board read resets the total: a stale count must never label fresh rows.
    setBoardTotal(null);
    api
      .get<TaskRow[]>(`/tasks?limit=${TASKS_BOARD_LIMIT}`, {
        onHeaders: (headers) => {
          if (cancelled) return;
          const raw = headers.get("X-Polsia-Total");
          if (raw === null) return;
          const parsed = Number(raw.trim());
          if (Number.isSafeInteger(parsed) && parsed >= 0) setBoardTotal(parsed);
        },
      })
      .then((data) => {
        if (cancelled) return;
        // The wire is typed, not trusted — the sites page's own rule since V24 ("a 200 whose body is
        // not a list is named as a broken read"). Without this guard a `null` body leaves
        // `rows === null` with no error and the skeleton renders FOREVER ("not read yet" standing in
        // for a broken read), and an object body reaches `TaskBoard`'s iteration and throws the
        // committed tree away — a header and two buttons, nothing named. Previous rows stay on
        // screen (they are real reads), exactly as a failed refresh already behaves.
        if (!Array.isArray(data)) {
          setError("the server returned no list of tasks");
          return;
        }
        setRows(data);
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

  // The deep link, consumed once per distinct param VALUE. An id the loaded board window carries
  // opens straight from the rows; an id OUTSIDE it gets the real by-id read (`GET /tasks/{id}`,
  // tasks.py:113-114) — never the silent nothing this used to be (S2-F4). A junk param stays the
  // documented no-op: nothing is requested and nothing is claimed.
  useEffect(() => {
    if (rawTaskParam === null) {
      // No request in the URL: re-arm, so a later arrival of the same value counts as new.
      consumedParam.current = null;
      return;
    }
    if (rows === null || consumedParam.current === rawTaskParam) return;
    consumedParam.current = rawTaskParam; // acted on (or honestly un-openable) — never again
    if (requestedTaskId === null) return; // junk param: no-op
    if (rows.some((row) => row.id === requestedTaskId)) {
      setOffBoard(null);
      setOpenId(requestedTaskId);
      return;
    }
    // Outside the loaded set: the by-id read is the only honest answer. Any previously open
    // sheet closes first — the URL's new request supersedes it.
    setOpenId(null);
    setOffBoard({ kind: "fetching", id: requestedTaskId });
  }, [rawTaskParam, requestedTaskId, rows]);

  // The by-id read itself, keyed to the FETCHING id and the Retry counter (not to `rows`): a later
  // board refresh must not cancel an in-flight read, and a Retry on the miss must run it again.
  const fetchingId = offBoard?.kind === "fetching" ? offBoard.id : null;
  useEffect(() => {
    if (fetchingId === null) return;
    let cancelled = false;
    api
      .get<TaskRow>(`/tasks/${fetchingId}`)
      .then((row) => {
        if (cancelled) return;
        // The network boundary is typed, not trusted: a body that is not a task row is a named
        // miss, never a detail sheet rendering `undefined`.
        if (!row || typeof row.id !== "number") {
          setOffBoard({
            kind: "miss",
            id: fetchingId,
            notFound: false,
            message: "the API answered no task row for this id",
          });
          return;
        }
        setOffBoard({ kind: "found", row });
        setOpenId(row.id);
      })
      .catch((e: unknown) => {
        if (cancelled) return;
        // The typeof guard mirrors WebsiteCard's: suites that factory-mock @/lib/api leave
        // ApiError undefined, and instanceof against undefined throws.
        const notFound =
          typeof ApiError !== "undefined" && e instanceof ApiError && e.status === 404;
        setOffBoard({ kind: "miss", id: fetchingId, notFound, message: errorText(e) });
      });
    return () => {
      cancelled = true;
    };
  }, [fetchingId, retrySeq]);

  // The open task: from the loaded rows when the board carries it, else the by-id fetch's row —
  // never a fabricated stand-in for either.
  const openTask =
    openId === null
      ? null
      : (rows?.find((row) => row.id === openId) ??
        (offBoard?.kind === "found" && offBoard.row.id === openId ? offBoard.row : null));

  // The one-row context note for a sheet whose row came from the by-id read: it disappears the
  // moment a refresh puts the row in the board — the note states where the row came from NOW.
  const openedById =
    openTask !== null &&
    offBoard?.kind === "found" &&
    offBoard.row.id === openTask.id &&
    !(rows ?? []).some((row) => row.id === openTask.id);

  return (
    <>
      <div aria-hidden={openTask || dialogOpen ? true : undefined} className="space-y-4">
        <div className="flex flex-wrap items-end justify-between gap-2 border-b border-line pb-2">
          <h1 className="text-3xl font-bold text-ink">Tasks</h1>
          <div className="flex items-center gap-2">
            {/* Bevel on purpose: the board carries no primary action (u2 ACC 3 pin); the dialog's
                one glossy CTA lives inside the dialog. */}
            <Button size="sm" onClick={() => setDialogOpen(true)}>
              New task
            </Button>
            <Button size="sm" onClick={() => setReloadSeq((n) => n + 1)}>
              Refresh
            </Button>
          </div>
        </div>

        {error ? (
          <div role="alert" className="border border-alert bg-surface p-3">
            <p className="ui-label text-alert">Could not load tasks</p>
            <p className="mt-1 text-sm text-ink">{error}</p>
          </div>
        ) : null}

        {/* The deep link's off-board leg: a named progress line while the by-id read is in
            flight, and a named miss — never a silent nothing — when it comes back empty. */}
        {offBoard?.kind === "fetching" ? (
          <p role="status" data-testid="tasks-deeplink-loading" className="ui-label text-ink-muted">
            Opening task #{offBoard.id} — outside the loaded board window, reading it by id…
          </p>
        ) : null}

        {offBoard?.kind === "miss" ? (
          <div
            role="alert"
            data-testid="tasks-deeplink-miss"
            className="border border-alert bg-surface p-3"
          >
            <p className="ui-label text-alert">
              {offBoard.notFound
                ? `Task #${offBoard.id} not found`
                : `Could not open task #${offBoard.id}`}
            </p>
            <p className="mt-1 text-sm text-ink">{offBoard.message}</p>
            {/* V29/L2 T2.1: the miss is retryable. Re-entering `fetching` gives the by-id effect a
                non-null id again; `retrySeq` is the dependency that makes a repeat click a re-read. */}
            <Button
              size="sm"
              className="mt-2"
              data-testid="task-deep-link-retry"
              onClick={() => {
                setOffBoard({ kind: "fetching", id: offBoard.id });
                setRetrySeq((n) => n + 1);
              }}
            >
              Retry
            </Button>
          </div>
        ) : null}

        {error === null && rows === null ? <TasksLoading /> : null}

        {error === null && rows !== null && rows.length === 0 ? (
          // Q-26: the sentence this repo already had — the tone the audit calls correct.
          <p className="ui-label text-ink-muted">
            No tasks yet. Agents will create tasks during their cycles.
          </p>
        ) : null}

        {rows !== null && rows.length > 0 ? (
          <>
            {rows.length === TASKS_BOARD_LIMIT ? (
              // Three honest cases, never two conflated: the header declared MORE than this page
              // (name both numbers), declared exactly this page (nothing is missing — the old copy
              // claimed "the API reports no total, so more may exist" about a total it had just
              // read), or declared nothing at all (say only what the rows prove).
              <p className="ui-label text-ink-muted">
                {boardTotal === null
                  ? `Showing the first ${TASKS_BOARD_LIMIT} tasks — the API reports no total, so more may exist`
                  : boardTotal > rows.length
                    ? `Showing the first ${TASKS_BOARD_LIMIT} of ${boardTotal} tasks`
                    : `Showing all ${rows.length} tasks`}
              </p>
            ) : null}
            <TaskBoard
              rows={rows}
              onOpen={(task) => setOpenId(task.id)}
              onTaskQueued={() => setReloadSeq((n) => n + 1)}
            />
          </>
        ) : null}
      </div>

      {openTask ? (
        <TaskDetailSheet
          task={openTask}
          contextNote={
            openedById
              ? `Task #${openTask.id} was read by id — it is outside the loaded board window (the newest ${TASKS_BOARD_LIMIT} tasks).`
              : null
          }
          onClose={() => setOpenId(null)}
          onTaskQueued={() => setReloadSeq((n) => n + 1)}
        />
      ) : null}

      {dialogOpen ? (
        <NewTaskDialog
          onClose={() => setDialogOpen(false)}
          onCreated={() => setReloadSeq((n) => n + 1)}
        />
      ) : null}
    </>
  );
}
