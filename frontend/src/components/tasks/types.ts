import type { Task } from "@/lib/api";

/**
 * The wire row these pages render. `GET /tasks` answers with the backend's `TaskResponse`
 * (`backend/app/schemas/task.py`), which carries fields the shared `Task` type stops short of;
 * declaring them here as OPTIONAL additions is additive (no shared file is changed) — the same
 * pattern the dashboard's `TasksPreview` uses for `description`.
 *
 * `completed_at` arrives in this same wave (B1 adds it to `TaskResponse`, nullable). The
 * consumer side is optional and coded defensively: null/absent means "not measured" and renders
 * as the honest absence (the row or chip is omitted), never as a zero or a fabricated date.
 *
 * Deliberately NOT consumed: `cost_usd` / `tokens_in` / `tokens_out` (B1's other additive
 * fields). The owner ruled out credit/price display, and tasks.md acceptance #3 forbids a price
 * string on this page — so no per-task money figure is rendered anywhere in this wave.
 */
export type TaskRow = Task & {
  description?: string | null;
  source?: string | null;
  result_summary?: string | null;
  error_message?: string | null;
  completed_at?: string | null;
};

/** `POST /agents/{agent_type}/trigger` (agents.py:54-58, 202). */
export type AgentTriggerResponse = {
  status: string;
  task_id: number;
  message: string;
};

/**
 * `POST /approvals` (approvals.py:103-140, 201). `token` is present on the wire but is
 * deliberately never rendered — the sheet reports the mint and its expiry, not the secret.
 * `expires_at` is integer epoch SECONDS (`int(minted.expires_at)` server-side).
 */
export type ApprovalIssueResponse = {
  token: string;
  action_fingerprint: string;
  expires_at: number;
  ttl_s: number;
};
