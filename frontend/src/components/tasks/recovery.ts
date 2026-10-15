import { fingerprintsFrom } from "./approval";
import type { TaskRow } from "./types";

/**
 * The ONE recovery grammar for a task card's state (B3, wave U3) — exactly one action, or a
 * stated reason why there is none. The sheet renders this value in its single footer slot, so a
 * second, competing CTA cannot exist on one card by construction.
 *
 * What backs each action (verified against the live API this wave — nothing here is invented):
 *   * `run`  — `POST /agents/{agent_type}/trigger` (202). It CREATES a new task row and
 *     dispatches it; it never re-runs the row in front of you, and it spends (budget gate). The
 *     helper text says both out loud. Used for `pending` (the row is queued and running it is
 *     the queue's real verb) and `failed` (B3's run-again path, labelled for what it is).
 *   * `approve` — `POST /approvals` mint + attach (`approval.ts` reads the fingerprints), which
 *     re-queues the parked row. Only drawn when a fingerprint the mint endpoint's own contract
 *     accepts was found in `result_summary`; otherwise `none`.
 *   * `none` — every other state, with the reason stated instead of a control that could not
 *     work: `completed` (nothing to recover — the live product's RETRY-on-a-done-task is the
 *     defect this rule kills), `in_progress` (no stop/advance endpoint), `rejected` /
 *     `cancelled` (no requeue/edit endpoint exists anywhere in the API), null or unrecognised
 *     status words (no truthful action can be named). `PATCH /tasks/{id}/status` exists AND is
 *     drawn — but by the separate `TaskStatusControl` (wave V11 unit 2), NOT by this grammar:
 *     a status write is not a recovery action, so it gets its own section and this table stays
 *     exactly one action per state.
 */
export type Recovery =
  | { kind: "run"; label: string; helper: string }
  | { kind: "approve" }
  | { kind: "none"; reason: string };

export function recoveryFor(task: TaskRow): Recovery {
  switch (task.status) {
    case "pending":
      return {
        kind: "run",
        label: "Run now",
        helper:
          "Run now creates a NEW task for this agent and queues it (it spends from today\u2019s AI budget); this row is never re-run.",
      };
    case "failed":
      return {
        kind: "run",
        label: "Run again",
        helper:
          "Run again creates a NEW task for this agent and queues it (it spends from today\u2019s AI budget); this failed row is never re-run, and its error stays as recorded.",
      };
    case "awaiting_approval":
      return fingerprintsFrom(task.result_summary).length > 0
        ? { kind: "approve" }
        : {
            kind: "none",
            reason:
              "This task is parked awaiting approval, but its result text carries no fingerprint this build can read \u2014 there is nothing to approve from here.",
          };
    case "in_progress":
      return {
        kind: "none",
        reason:
          "In progress \u2014 a run for this task is already underway; this build has no control to stop or advance it.",
      };
    case "rejected":
      return {
        kind: "none",
        reason:
          "Rejected \u2014 the task was refused before execution, and this build has no requeue or edit control for a rejected row.",
      };
    case "cancelled":
      return {
        kind: "none",
        reason:
          "Cancelled \u2014 this row was cancelled; this build has no restart control for a cancelled task.",
      };
    case "completed":
      return {
        kind: "none",
        reason: "Completed \u2014 no recovery action is offered for a finished task.",
      };
    default:
      return task.status
        ? {
            kind: "none",
            reason: `Status "${task.status}" is not a state this build has an action for \u2014 no control is drawn for it.`,
          }
        : {
            kind: "none",
            reason:
              "The wire carries no status for this row \u2014 no truthful action can be named for it.",
          };
  }
}
