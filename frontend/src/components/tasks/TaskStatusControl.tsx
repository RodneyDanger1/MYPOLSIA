"use client";

import { useId, useState } from "react";

import { Button } from "@/components/ui";
import { api } from "@/lib/api";

import { errorText } from "./status";
import type { TaskRow } from "./types";

/**
 * The status-write control (wave V11 unit 2) — the UI half of T12/D20's
 * `PATCH /tasks/{task_id}/status`, now that the api client carries `patch`
 * (`api.patch<T>(path, body?, init?)` — the frozen interface W3 adds to `lib/api.ts`; this
 * component consumes it and NEVER edits api.ts).
 *
 * The guard, read off the tree (`backend/app/api/v1/tasks.py:131-162` →
 * `task_service.update_task_status`): the typed body is `TaskStatusUpdate`
 * (`schemas/task.py`) and the ONE refusal is the value set — `task_service.VALID_TASK_STATUSES`,
 * derived from `models/task.py::TaskStatus`, is the single authority. There is NO transition
 * matrix in the service: every value below is writable from every current status, an unknown
 * word is refused by NAME (E_VALIDATION, 422, the accepted set in the refusal context) BEFORE
 * the row is touched, and a missing task answers the same E_NOT_FOUND (404) envelope the read
 * routes use. The control therefore offers exactly the seven wire values, marks the current
 * one, and renders every refusal as the server spelled it — it never hides an allowed write
 * and never invents one the guard would refuse.
 *
 * The payload is EXACTLY `{ status }` — no result_summary/error_message is collected. The
 * service writes those fields only when supplied (its own docstring rule), so omitting them
 * can never wipe a previously recorded message; sending nulls would be noise, not data.
 *
 * `role`/`data-tone` notice rendering mirrors the sheet's run/approve notices so the two
 * write paths read the same.
 */

/** The wire value set — `task_service.VALID_TASK_STATUSES`, in `TaskStatus` definition order
 *  (`models/task.py`: pending, in_progress, completed, failed, cancelled, awaiting_approval,
 *  rejected — migration 0006 appended `rejected`). Duplicated here as data because the
 *  frontend has no generated client; the pinned list keeps the two from drifting silently. */
export const TASK_STATUS_VALUES = [
  "pending",
  "in_progress",
  "completed",
  "failed",
  "cancelled",
  "awaiting_approval",
  "rejected",
] as const;

type Notice = { tone: "status" | "alert"; text: string };

export function TaskStatusControl({
  task,
  onChanged,
}: {
  task: TaskRow;
  /** Called with the SERVER's returned row after a successful write (nothing on refusal). */
  onChanged?: (updated: TaskRow) => void;
}) {
  const selectId = useId();
  const [choice, setChoice] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice | null>(null);

  const current = task.status ?? "";
  // A no-op write is not offered: the button arms only when a DIFFERENT, real value is chosen.
  const canApply = !busy && choice !== "" && choice !== current;

  const apply = async () => {
    if (!canApply) return;
    setBusy(true);
    setNotice(null);
    try {
      // api.patch is the frozen interface (`api.patch<T>(path, body?, init?)`) — and the body
      // is exactly `{ status }` (TaskStatusUpdate's other two fields stay unsent, see above).
      const updated = await api.patch<TaskRow>(`/tasks/${task.id}/status`, { status: choice });
      const applied = updated?.status ?? choice;
      setNotice({
        tone: "status",
        text: `Task #${task.id} status set to "${applied}" — this is the server's response, not a local guess.`,
      });
      setChoice("");
      onChanged?.(updated);
    } catch (e) {
      // Refused by name: E_VALIDATION (unknown value) / E_NOT_FOUND (missing task) render as
      // the server wrote them. Nothing was written, so the visible state does not move.
      setNotice({ tone: "alert", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <section data-testid="status-write" className="mt-4 border-t border-line pt-3">
      <p className="ui-label text-ink-muted">Change status</p>
      <div className="mt-1 flex flex-wrap items-center gap-2">
        <label htmlFor={selectId} className="ui-label text-ink-muted">
          New status
        </label>
        <select
          id={selectId}
          value={choice}
          onChange={(e) => {
            setChoice(e.target.value);
            setNotice(null);
          }}
          className="border border-line-strong bg-surface px-2 py-1 text-sm text-ink"
        >
          <option value="">Choose a status…</option>
          {TASK_STATUS_VALUES.map((value) => (
            <option key={value} value={value}>
              {value}
              {value === current ? " (current)" : ""}
            </option>
          ))}
        </select>
        <Button size="sm" disabled={!canApply} onClick={apply}>
          {busy ? "Setting…" : "Set status"}
        </Button>
      </div>
      <p className="mt-1 text-xs text-ink-muted">
        One write: PATCH /tasks/{task.id}/status. The server accepts exactly these seven values
        and writes nothing when it refuses — an unknown value comes back named (E_VALIDATION),
        a missing task answers E_NOT_FOUND. Setting a status does not run the task
        {task.status === null ? "; this row's status is not recorded, so any value you pick is a real write" : ""}.
      </p>
      {notice ? (
        <div
          role={notice.tone}
          data-tone={notice.tone}
          className={
            notice.tone === "alert"
              ? "mt-2 border border-alert bg-surface p-2"
              : "mt-2 border border-line-strong bg-surface-sunk p-2"
          }
        >
          <p className="text-sm text-ink">{notice.text}</p>
        </div>
      ) : null}
    </section>
  );
}
