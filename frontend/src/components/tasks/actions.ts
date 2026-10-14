import { api } from "@/lib/api";

import type { AgentTriggerResponse, TaskRow } from "./types";

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
