import { api } from "@/lib/api";

import type { AgentTriggerResponse, TaskRow, TaskRunDispatchResponse } from "./types";

/**
 * The ONE shared request shape for a task's `run` recovery action — `POST
 * /agents/{agent_type}/trigger` (202). Per the backend's own contract (tasks.md §2) this
 * CREATES a new task row (`source="manual"`) and dispatches it; it never re-runs the row in
 * front of you, and it spends from the day's AI budget.
 *
 * Extracted in wave V7 (seat S6 — the parent-recorded correction C8): the board card's
 * quick-action chip and the detail sheet's `runNow` must issue the identical request, so the
 * two surfaces cannot drift apart. The sheet keeps its inline handler this wave (its file is
 * outside this seat's territory); it is pinned to this same shape by `u3-task-truth`, and a
 * future edit must migrate it onto this helper.
 *
 * The real `agent_type` is substituted into the route; `description` / `priority` stay OFF the
 * wire entirely when the row carries null (never a fabricated 3 — the GAP-NO-FIELD rule).
 */
export function triggerTaskRun(task: TaskRow): Promise<AgentTriggerResponse> {
  return api.post<AgentTriggerResponse>(`/agents/${task.agent_type}/trigger`, {
    task_title: task.title,
    description: task.description ?? undefined,
    priority: task.priority ?? undefined,
  });
}

/**
 * V24/L1 — the OTHER run verb: execute THIS row (`POST /tasks/{task_id}/run`, 202). Manually
 * created tasks used to sit `pending` forever because the only run control (`triggerTaskRun`
 * above) creates a NEW task; this one dispatches the row in front of you.
 *
 * No body is needed — the path names the task. The response's `status` is `"dispatched"`
 * (the broker accepted the message), never the task's own status: the UI must re-read the task
 * to learn whether a worker has claimed it. A refusal (404, 409 E_TASK_NOT_RUNNABLE, 503) is
 * the shared `ApiError.display` text, rendered by `readErrorText` at the call site.
 */
export function runThisTask(task: TaskRow): Promise<TaskRunDispatchResponse> {
  return api.post<TaskRunDispatchResponse>(`/tasks/${task.id}/run`);
}
