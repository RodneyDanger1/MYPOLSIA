import type { TaskRow } from "./types";

/**
 * The parked-task fingerprint reader.
 *
 * TWO sources, in this order:
 *
 *   1. `TaskResponse.approval_required` — the STRUCTURED list the worker writes onto the task
 *      row when a run parks (`backend/celery_app/tasks/agent_tasks.py`, `APPROVAL_REQUIRED_KEY`).
 *      This is the source of truth: it is data, not a sentence, so a copy edit cannot make the
 *      Approve control disappear (A5-02).
 *   2. `result_summary` prose — the fallback for a server that does not serve the structured
 *      field yet. The producer writes, when a run parks a task `awaiting_approval`:
 *
 *        "awaiting approval: N gated action(s) need a token; request one for fingerprint(s) <fp>[, <fp>…]"
 *
 * Both paths pass the SAME strict token contract (`action_fingerprint`, 8-128 chars —
 * `app/schemas/approval.py`) and both return [] rather than a guess. A present-but-unreadable
 * structured list is deliberately NOT backfilled from prose: the structured answer is the
 * authority, and a control built from a stale sentence could post a token the server rejects.
 * The sheet then states that the fingerprint is missing instead of rendering a control that
 * would post garbage.
 */
const FINGERPRINT_LIST_RE = /fingerprint\(s\)\s+([^\n]+)/i;
const FINGERPRINT_RE = /^[A-Za-z0-9_-]{8,128}$/;

export function fingerprintsFrom(resultSummary: string | null | undefined): string[] {
  if (!resultSummary) return [];
  const match = FINGERPRINT_LIST_RE.exec(resultSummary);
  if (!match) return [];
  return match[1]
    .split(",")
    .map((token) => token.trim())
    .filter((token) => FINGERPRINT_RE.test(token));
}

/** The parked task's fingerprints: the STRUCTURED list first, the prose parser only as a
 *  fallback for a server that does not serve the field yet. */
export function fingerprintsForTask(task: TaskRow): string[] {
  const structured = task.approval_required;
  if (Array.isArray(structured) && structured.length > 0) {
    return structured
      .map((item) => (typeof item?.fingerprint === "string" ? item.fingerprint.trim() : ""))
      .filter((token) => FINGERPRINT_RE.test(token));
  }
  return fingerprintsFrom(task.result_summary);
}
