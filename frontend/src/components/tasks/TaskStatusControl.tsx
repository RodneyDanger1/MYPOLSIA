"use client";

import { useId, useState } from "react";

import { Button } from "@/components/ui";
import { api } from "@/lib/api";

import { errorText } from "./status";
import type { TaskRow } from "./types";

/**
 * The status-write control (wave V11 unit 2; corrected by wave B2 / item B2-C5-R1) — the UI half
 * of `PATCH /tasks/{task_id}/status`.
 *
 * The guard, read off the tree (`backend/app/api/v1/tasks.py` →
 * `task_service.transition_task_status`): the typed body is `TaskStatusUpdate`
 * (`schemas/task.py`) and there IS ONE transition matrix — `task_service.TASK_TRANSITIONS`
 * (F-17, wave B1). `PATCH` routes every value through it:
 *  - an UNKNOWN value is refused by name (`E_VALIDATION`, 422) BEFORE the row is touched;
 *  - a KNOWN value that is not a legal target of the CURRENT status — including a no-op write —
 *    is `E_TASK_NOT_RUNNABLE` (409) with `from`, `to` and the `accepted` set in the context;
 *  - a missing task answers the same `E_NOT_FOUND` (404) envelope the read routes use.
 *
 * So the select is NOT the seven-value wire enum. It offers exactly
 * `task_service.legal_task_status_targets(current)` — the accepted set the matrix itself declares,
 * alphabetically, because that is how the service sorts the tuple it puts on the wire in
 * `context.accepted`. A terminal row (`completed`/`cancelled`/`rejected`) offers nothing and says
 * so; a row with NO recorded status has no matrix row either, so it offers nothing too.
 *
 * Where that set comes from. The API serves it in the 409's `context.accepted`; there is no
 * GET-side per-row field yet (REQUESTS.md R4 asks for one). Until then the browser mirrors the
 * matrix in `LEGAL_STATUS_TARGETS` below, and `__tests__/tasks-status-matrix.test.tsx` parses
 * `task_service.py` and fails on any drift — the mirror cannot quietly become a second, wrong
 * authority. A 409 whose `accepted` disagrees with the mirror replaces the offered options with
 * the server's answer on the spot.
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
 *  rejected — migration 0006 appended `rejected`). This is the ENUM, not the writable set: which
 *  of these a given row may move to is `legalTaskStatusTargets` below. */
export const TASK_STATUS_VALUES = [
  "pending",
  "in_progress",
  "completed",
  "failed",
  "cancelled",
  "awaiting_approval",
  "rejected",
] as const;

/**
 * The transition matrix, mirrored from `task_service.TASK_TRANSITIONS`. Each list is the target
 * set for that CURRENT status, sorted exactly as `legal_task_status_targets()` sorts it before it
 * goes onto the wire. Terminal states carry `[]`. Keep this table alphabetical per status; the
 * parity test compares it to the parsed backend matrix, so an added or removed edge fails there.
 */
export const LEGAL_STATUS_TARGETS: Record<string, readonly string[]> = {
  pending: ["cancelled", "completed", "failed", "in_progress", "rejected"],
  in_progress: ["awaiting_approval", "cancelled", "completed", "failed", "pending", "rejected"],
  awaiting_approval: ["cancelled", "pending"],
  failed: ["pending"],
  completed: [],
  cancelled: [],
  rejected: [],
};

/**
 * `task_service.legal_task_status_targets` in the browser: the statuses `current` may move to.
 * Empty means terminal — or that `current` is unrecorded/unknown, in which case the matrix has no
 * row and the server has no accepted target either. Never a guessed edge.
 */
export function legalTaskStatusTargets(current: string | null | undefined): readonly string[] {
  if (!current) return [];
  return LEGAL_STATUS_TARGETS[current] ?? [];
}

/**
 * The `accepted` list a refusal carried, read structurally: the API client keeps the parsed body
 * on `ApiError.body`, and the frozen envelope puts the set under `detail.context.accepted`
 * (HTTPException shape) or `error.context.accepted` (MeridianError shape). `null` = the refusal did
 * not declare one, so the mirror stands.
 */
function acceptedFrom(e: unknown): string[] | null {
  const body = (e as { body?: unknown } | null)?.body;
  if (!body || typeof body !== "object") return null;
  const envelope = ((body as { detail?: unknown }).detail ??
    (body as { error?: unknown }).error) as { context?: { accepted?: unknown } } | undefined;
  const accepted = envelope?.context?.accepted;
  if (!Array.isArray(accepted)) return null;
  return accepted.filter((value): value is string => typeof value === "string");
}

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
  /** The server's own `accepted` set, tagged with the status it was declared for. Beats the mirror
   *  for as long as that status holds; a later row/state change simply stops matching. */
  const [declared, setDeclared] = useState<{
    status: string | null;
    targets: readonly string[];
  } | null>(null);

  const current = task.status ?? null;
  const targets =
    declared && declared.status === current ? declared.targets : legalTaskStatusTargets(current);

  // A no-op write is not offered: the button arms only when a DIFFERENT, legal value is chosen.
  const canApply = !busy && choice !== "" && choice !== current && targets.includes(choice);

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
      // Refused by name: E_VALIDATION (unknown value) / E_TASK_NOT_RUNNABLE (illegal cell) /
      // E_NOT_FOUND (missing task) render as the server wrote them. A 409 that declares the
      // accepted set replaces the options — the server's matrix, not this mirror, then decides.
      const accepted = acceptedFrom(e);
      if (accepted) setDeclared({ status: current, targets: accepted });
      setNotice({ tone: "alert", text: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  const terminal = current !== null && targets.length === 0;

  return (
    <section data-testid="status-write" className="mt-4 border-t border-line pt-3">
      <p className="ui-label text-ink-muted">Change status</p>
      <p className="mt-1 text-xs text-ink-muted">
        Current: <span data-testid="status-current">{current ?? "not recorded"}</span>
      </p>
      {targets.length > 0 ? (
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
            {targets.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <Button size="sm" disabled={!canApply} onClick={apply}>
            {busy ? "Setting…" : "Set status"}
          </Button>
        </div>
      ) : (
        <p data-testid="status-no-target" className="mt-1 text-sm text-ink">
          {terminal
            ? `"${current}" is a terminal state: the transition matrix declares no target from it, so the server refuses every status write to this row.`
            : "This row has no recorded status, so the matrix has no target for it — the server refuses every status write until a writer that owns the row records one."}
        </p>
      )}
      <p className="mt-1 text-xs text-ink-muted">
        One write: PATCH /tasks/{task.id}/status. The options are the legal targets of the current
        status in the server&apos;s one transition matrix (task_service.TASK_TRANSITIONS) — any
        other cell is refused by name (E_TASK_NOT_RUNNABLE, 409, with the accepted set in the
        refusal), and an unknown value is E_VALIDATION. Setting a status does not run the task.
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
